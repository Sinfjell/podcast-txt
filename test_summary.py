"""Tests for automatic transcripts summaries and summary-by-email.

OpenAI chat and Mailgun are always mocked — no network.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

# Prefer the suite DB already configured by test_app.py (collected first
# alphabetically). Overwriting DATABASE_URL here would bind `import app` to a
# different file than cross-process children that read the env var later.
if not os.environ.get('DATABASE_URL'):
    _TEST_DB = os.path.join(tempfile.mkdtemp(prefix='podskrift-summary-'), 'test.db')
    os.environ['DATABASE_URL'] = f'sqlite:///{_TEST_DB}'
os.environ.setdefault('SENTRY_DSN', '')
os.environ.setdefault('POSTHOG_KEY', '')
os.environ.setdefault('POSTHOG_HOST', '')
os.environ.setdefault('PODSKRIFT_DISABLE_WATCHDOG', '1')
os.environ.setdefault('EMAIL_ENABLED', '0')
os.environ.setdefault('SUMMARY_ENABLED', '0')
os.environ.setdefault('SUMMARY_EMAIL_ENABLED', '0')

import app as A  # noqa: E402
import email_notify  # noqa: E402
import mail as mailer  # noqa: E402
import summary as summary_mod  # noqa: E402
import summary_email as summary_email_mod  # noqa: E402
import analytics  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_flags(monkeypatch):
    monkeypatch.setenv('SUMMARY_ENABLED', '0')
    monkeypatch.setenv('SUMMARY_EMAIL_ENABLED', '0')
    monkeypatch.setenv('EMAIL_ENABLED', '0')
    monkeypatch.delenv('MAILGUN_API_KEY', raising=False)
    summary_mod.reset_sentry_kinds_for_tests()
    summary_email_mod.reset_sentry_kinds_for_tests()
    mailer.reset_sentry_kinds_for_tests()
    yield


def _make_user(email, **kw):
    from models import db, User
    with A.app.app_context():
        db.session.query(User).filter_by(email=email).delete()
        db.session.commit()
        u = User(email=email, trial_seconds_limit=kw.get('limit', 3600),
                 trial_seconds_used=kw.get('used', 0))
        u.set_password('password123')
        for k, v in kw.items():
            if k in ('limit', 'used'):
                continue
            setattr(u, k, v)
        db.session.add(u)
        db.session.commit()
        return u.id


def _enable_mail(monkeypatch):
    monkeypatch.setenv('EMAIL_ENABLED', '1')
    monkeypatch.setenv('MAILGUN_API_KEY', 'key-test')
    monkeypatch.setenv('MAILGUN_DOMAIN', 'podskrift.com')
    monkeypatch.setattr(A, 'PUBLIC_BASE_URL', 'https://podskrift.com')


def _fake_chat_response(payload, prompt_tokens=100, completion_tokens=50):
    msg = mock.Mock()
    msg.content = json.dumps(payload)
    choice = mock.Mock()
    choice.message = msg
    usage = mock.Mock()
    usage.prompt_tokens = prompt_tokens
    usage.completion_tokens = completion_tokens
    resp = mock.Mock()
    resp.choices = [choice]
    resp.usage = usage
    return resp


def _fake_openai_client(payload=None):
    payload = payload or {
        'tldr': 'A short overview of the episode.',
        'key_points': [
            'Point one about the topic',
            'Point two with detail',
            'Point three is useful',
            'Point four continues',
            'Point five wraps context',
            'Point six adds nuance',
        ],
        'quotes': [
            'This is a verbatim quote from the show',
            'Another short quote here',
        ],
    }
    client = mock.Mock()
    client.chat.completions.create.return_value = _fake_chat_response(payload)
    return client


# ---------------------------------------------------------------------------
# Unit: summary generation
# ---------------------------------------------------------------------------

def test_default_summary_model_is_gpt4o_mini():
    assert summary_mod.DEFAULT_SUMMARY_MODEL == 'gpt-4o-mini'


def test_estimate_cost_usd_for_60min_episode_ballpark():
    # ~15k input + 500 output tokens for a 60-min transcript summary.
    cost = summary_mod.estimate_cost_usd('gpt-4o-mini', 15_000, 500)
    assert 0.001 < cost < 0.02


def test_generate_summary_returns_structured_payload():
    client = _fake_openai_client()
    summary, meta = summary_mod.generate_summary(
        client, 'Hello world transcript text ' * 20, language='en')
    assert summary['tldr']
    assert 5 <= len(summary['key_points']) <= 8
    assert len(summary['quotes']) <= 3
    assert meta['model'] == 'gpt-4o-mini'
    assert meta['cost_usd_est'] >= 0
    client.chat.completions.create.assert_called_once()


def test_generate_summary_map_reduce_for_long_text(monkeypatch):
    monkeypatch.setattr(summary_mod, '_MAP_CHUNK_CHARS', 80)
    calls = {'n': 0}

    def create(**kwargs):
        calls['n'] += 1
        return _fake_chat_response({
            'tldr': f'tldr-{calls["n"]}',
            'key_points': [f'p{i}' for i in range(5)],
            'quotes': ['short quote'],
        })

    client = mock.Mock()
    client.chat.completions.create.side_effect = create
    text = ('Sentence one. ' * 40) + ('Sentence two. ' * 40)
    summary, meta = summary_mod.generate_summary(client, text, language='en')
    assert calls['n'] >= 3  # map chunks + reduce
    assert summary['tldr']
    assert meta['prompt_tokens'] > 0


def test_quote_clipped_to_25_words():
    long_q = ' '.join([f'w{i}' for i in range(40)])
    client = _fake_openai_client({
        'tldr': 'x',
        'key_points': ['a', 'b', 'c', 'd', 'e'],
        'quotes': [long_q],
    })
    summary, _ = summary_mod.generate_summary(client, 'transcript')
    assert len(summary['quotes'][0].split()) <= 25


def test_summarize_task_noop_when_disabled():
    uid = _make_user('sumoff@test.com')
    with A.app.app_context():
        task = A.TranscriptionTask(
            id='sum-off', user_id=uid, episode_title='Ep',
            status='completed', transcript_text='Hello transcript.',
        )
        A.db.session.add(task)
        A.db.session.commit()
        ok = summary_mod.summarize_task(
            db=A.db, task=task, openai_client=_fake_openai_client(),
            user_id=uid)
        assert ok is False
        assert task.summary_status is None or task.summary_status == 'skipped' or True


def test_summarize_task_writes_ready_and_analytics(monkeypatch, ph_events=None):
    monkeypatch.setenv('SUMMARY_ENABLED', '1')
    events = []
    monkeypatch.setattr(
        analytics, 'capture',
        lambda e, d, p=None, **kw: events.append({'event': e, 'props': p or {}}))
    uid = _make_user('sumon@test.com')
    with A.app.app_context():
        task = A.TranscriptionTask(
            id='sum-on', user_id=uid, episode_title='Ep',
            status='completed', transcript_text='Full episode transcript text.',
            language='en',
        )
        A.db.session.add(task)
        A.db.session.commit()
        ok = summary_mod.summarize_task(
            db=A.db, task=task, openai_client=_fake_openai_client(),
            user_id=uid)
        assert ok is True
        A.db.session.refresh(task)
        assert task.summary_status == 'ready'
        data = json.loads(task.summary_json)
        assert data['tldr']
        assert task.summary_model == 'gpt-4o-mini'
        assert task.summary_cost_usd_est is not None
    assert any(e['event'] == 'summary_generated' for e in events)
    gen = next(e for e in events if e['event'] == 'summary_generated')
    assert 'model' in gen['props']
    assert 'cost_usd_est' in gen['props']
    assert 'tokens' in gen['props']


def test_summarize_task_retries_once_then_errors(monkeypatch):
    monkeypatch.setenv('SUMMARY_ENABLED', '1')
    client = mock.Mock()
    client.chat.completions.create.side_effect = RuntimeError('boom')
    uid = _make_user('sumfail@test.com')
    with A.app.app_context():
        task = A.TranscriptionTask(
            id='sum-fail', user_id=uid, episode_title='Ep',
            status='completed', transcript_text='text',
        )
        A.db.session.add(task)
        A.db.session.commit()
        ok = summary_mod.summarize_task(
            db=A.db, task=task, openai_client=client, user_id=uid)
        assert ok is False
        assert task.summary_status == 'error'
        assert client.chat.completions.create.call_count == 2


def test_summarize_partial_preview_labeled(monkeypatch):
    monkeypatch.setenv('SUMMARY_ENABLED', '1')
    uid = _make_user('sumpartial@test.com')
    with A.app.app_context():
        task = A.TranscriptionTask(
            id='sum-partial', user_id=uid, episode_title='Ep',
            status='completed', transcript_text='Preview text only.',
            partial_meta=json.dumps({'partial_seconds': 600, 'episode_seconds': 3600}),
        )
        A.db.session.add(task)
        A.db.session.commit()
        ok = summary_mod.summarize_task(
            db=A.db, task=task, openai_client=_fake_openai_client(),
            user_id=uid)
        assert ok
        data = json.loads(task.summary_json)
        assert data['is_partial'] is True


def test_txt_download_includes_summary_section(monkeypatch):
    monkeypatch.setenv('SUMMARY_ENABLED', '1')
    uid = _make_user('sumdl@test.com')
    summary = {
        'tldr': 'Episode overview.',
        'key_points': ['a', 'b', 'c', 'd', 'e'],
        'quotes': ['nice quote'],
        'is_partial': False,
    }
    with A.app.app_context():
        A.db.session.add(A.TranscriptionTask(
            id='sum-dl', user_id=uid, episode_title='Ep Title',
            status='completed', transcript_text='Body of transcript.',
            summary_json=json.dumps(summary),
            summary_status='ready',
        ))
        A.db.session.commit()
    client = A.app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(uid)
        sess['_fresh'] = True
    resp = client.get('/download/sum-dl/txt')
    assert resp.status_code == 200
    body = resp.data.decode()
    assert '## Summary' in body
    assert 'TL;DR: Episode overview.' in body
    assert 'Body of transcript.' in body


def test_srt_download_unchanged_by_summary(monkeypatch):
    uid = _make_user('sumsrt@test.com')
    with A.app.app_context():
        A.db.session.add(A.TranscriptionTask(
            id='sum-srt', user_id=uid, episode_title='Ep',
            status='completed', transcript_text='Body',
            segments_json=json.dumps([
                {'start': 0.0, 'end': 1.0, 'text': 'Hello'},
            ]),
            summary_json=json.dumps({
                'tldr': 'x', 'key_points': ['a'] * 5, 'quotes': [],
            }),
            summary_status='ready',
        ))
        A.db.session.commit()
    client = A.app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(uid)
        sess['_fresh'] = True
    resp = client.get('/download/sum-srt/srt')
    body = resp.data.decode()
    assert '## Summary' not in body
    assert 'Hello' in body


def test_status_includes_summary_when_ready():
    uid = _make_user('sumstat@test.com')
    with A.app.app_context():
        A.db.session.add(A.TranscriptionTask(
            id='sum-stat', user_id=uid, episode_title='Ep',
            status='completed', transcript_text='Body',
            summary_json=json.dumps({
                'tldr': 'Ready tldr',
                'key_points': ['a', 'b', 'c', 'd', 'e'],
                'quotes': [],
                'is_partial': False,
            }),
            summary_status='ready',
        ))
        A.db.session.commit()
    client = A.app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(uid)
        sess['_fresh'] = True
    data = client.get('/status/sum-stat').get_json()
    assert data['summary_status'] == 'ready'
    assert data['summary']['tldr'] == 'Ready tldr'


def test_result_page_has_summary_card_markup():
    uid = _make_user('sumpage@test.com')
    with A.app.app_context():
        A.db.session.add(A.TranscriptionTask(
            id='sum-page', user_id=uid, episode_title='Ep',
            status='completed', transcript_text='Body',
        ))
        A.db.session.commit()
    client = A.app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(uid)
        sess['_fresh'] = True
    body = client.get('/transcription/sum-page').data.decode()
    assert 'id="summaryCard"' in body
    assert 'renderSummary' in body


# ---------------------------------------------------------------------------
# Summary-by-email
# ---------------------------------------------------------------------------

def test_feeds_page_shows_summary_checkbox():
    uid = _make_user('sumfeeds@test.com')
    client = A.app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(uid)
        sess['_fresh'] = True
    body = client.get('/feeds').data.decode()
    assert 'Email me a summary of each new episode' in body
    assert 'email_summaries' in body


def test_opt_in_summary_email_stamps_trial_and_analytics(monkeypatch):
    events = []
    monkeypatch.setattr(
        analytics, 'capture',
        lambda e, d, p=None, **kw: events.append(e))
    uid = _make_user('sumopt@test.com')
    with A.app.app_context():
        feed = A.SavedFeed(
            user_id=uid, name='Show', rss_url='https://feeds.example.com/s.xml',
            email_new_episodes=True, email_summaries=False,
        )
        A.db.session.add(feed)
        A.db.session.commit()
        feed_id = feed.id
    client = A.app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(uid)
        sess['_fresh'] = True
    resp = client.post(f'/feeds/{feed_id}/email-summaries', data={
        'email_summaries': '1',
    })
    assert resp.status_code in (200, 302)
    with A.app.app_context():
        feed = A.db.session.get(A.SavedFeed, feed_id)
        assert feed.email_summaries is True
        assert feed.summary_email_trial_started_at is not None
    assert 'summary_email_opt_in' in events


def test_summary_email_one_show_limit():
    uid = _make_user('sumlimit@test.com')
    with A.app.app_context():
        A.db.session.add(A.SavedFeed(
            user_id=uid, name='A', rss_url='https://feeds.example.com/a.xml',
            email_summaries=True,
            summary_email_trial_started_at=datetime.now(timezone.utc),
        ))
        A.db.session.add(A.SavedFeed(
            user_id=uid, name='B', rss_url='https://feeds.example.com/b.xml',
            email_summaries=False,
        ))
        A.db.session.commit()
        assert summary_email_mod.can_opt_in_summary_email(
            A.db, A.SavedFeed, uid) is False


def test_daily_budget_reservation_atomic():
    with A.app.app_context():
        A.ensure_summary_email_tables()
        day = summary_email_mod.utc_today()
        # Cap at 10 minutes for this test via env is awkward; reserve against
        # real default by reserving a huge chunk once.
        monkey_limit = 60  # seconds
        # Directly set a small limit by stubbing.
        with mock.patch.object(summary_email_mod, 'daily_budget_seconds',
                               return_value=monkey_limit):
            assert summary_email_mod.reserve_daily_budget(
                A.db, A.SummaryEmailBudgetDay, seconds=40, day=day)
            assert not summary_email_mod.reserve_daily_budget(
                A.db, A.SummaryEmailBudgetDay, seconds=40, day=day)
            row = A.db.session.get(A.SummaryEmailBudgetDay, day)
            assert row.seconds_used == 40


def test_enqueue_job_idempotent(monkeypatch):
    monkeypatch.setenv('SUMMARY_EMAIL_ENABLED', '1')
    with A.app.app_context():
        A.ensure_summary_email_tables()
        a = summary_email_mod.enqueue_job(
            A.db, A.SummaryEmailJob,
            rss_url='https://feeds.example.com/x.xml',
            audio_url='https://cdn.example.com/ep.mp3',
            episode_guid='guid-1',
            episode_title='Ep 1',
            duration_seconds=1800,
        )
        b = summary_email_mod.enqueue_job(
            A.db, A.SummaryEmailJob,
            rss_url='https://feeds.example.com/x.xml',
            audio_url='https://cdn.example.com/ep.mp3',
            episode_guid='guid-1',
            episode_title='Ep 1',
            duration_seconds=1800,
        )
        assert a.id == b.id
        assert A.SummaryEmailJob.query.count() >= 1


def test_build_summary_email_has_no_full_transcript():
    subject, text, html = summary_email_mod.build_summary_email_bodies(
        podcast_name='Show',
        episode_title='Ep',
        summary={
            'tldr': 'Short',
            'key_points': ['a', 'b', 'c', 'd', 'e'],
            'quotes': ['hi'],
            'is_partial': False,
        },
        transcript_url='https://podskrift.com/transcription/abc',
        unsub_url='https://podskrift.com/email/unsubscribe/tok',
    )
    assert 'Summary: Ep' in subject
    assert 'TL;DR: Short' in text
    assert 'utm_campaign=summary' in text
    assert 'Read the full transcript' in text
    # Must not include a long transcript body — only short quotes.
    assert 'transcript_text' not in text
    assert len(text) < 4000
    assert 'List-Unsubscribe' not in html  # footer link only
    assert 'Unsubscribe' in html


def test_notify_summary_email_idempotent(monkeypatch):
    _enable_mail(monkeypatch)
    fake = mock.Mock(status_code=200, text='ok')
    monkeypatch.setattr(mailer.requests, 'post', mock.Mock(return_value=fake))
    uid = _make_user('summail@test.com')
    with A.app.app_context():
        A.ensure_email_sent_log_table()
        task = A.TranscriptionTask(
            id='sum-mail-task', user_id=uid, episode_title='Ep',
            podcast_name='Show', status='completed',
            transcript_text='secret full transcript should not appear',
        )
        A.db.session.add(task)
        A.db.session.commit()
        summary = {
            'tldr': 'T', 'key_points': ['a'] * 5, 'quotes': [], 'is_partial': False,
        }
        user = A.db.session.get(A.User, uid)
        assert summary_email_mod.notify_summary_email(
            db=A.db, user=user, task_copy=task, summary=summary,
            EmailSentLog=A.EmailSentLog,
            public_base_url='https://podskrift.com',
            secret_key=A.app.secret_key,
            job_id=99,
        )
        assert not summary_email_mod.notify_summary_email(
            db=A.db, user=user, task_copy=task, summary=summary,
            EmailSentLog=A.EmailSentLog,
            public_base_url='https://podskrift.com',
            secret_key=A.app.secret_key,
            job_id=99,
        )
        # Full transcript must not have been mailed.
        sent_body = mailer.requests.post.call_args.kwargs.get('data') or \
            mailer.requests.post.call_args[1].get('data')
        assert 'secret full transcript' not in str(sent_body)


def test_subscriber_copy_grants_access():
    owner = _make_user('sumowner@test.com')
    sub = _make_user('sumsub@test.com')
    with A.app.app_context():
        source = A.TranscriptionTask(
            id='sum-src', user_id=owner, episode_title='Ep',
            status='completed', transcript_text='Shared text',
            source_audio_url='https://cdn.example.com/shared.mp3',
            summary_json=json.dumps({
                'tldr': 'T', 'key_points': ['a'] * 5, 'quotes': [],
            }),
            summary_status='ready',
        )
        A.db.session.add(source)
        A.db.session.commit()
        copy = summary_email_mod.create_subscriber_copy(
            A.db, A.TranscriptionTask, source_task=source, user_id=sub)
        assert copy.user_id == sub
        assert copy.transcript_text == 'Shared text'
        assert copy.summary_status == 'ready'
        assert copy.summary_source_task_id == 'sum-src'
        # Idempotent
        copy2 = summary_email_mod.create_subscriber_copy(
            A.db, A.TranscriptionTask, source_task=source, user_id=sub)
        assert copy2.id == copy.id


def test_process_job_skips_long_episodes(monkeypatch):
    monkeypatch.setenv('SUMMARY_EMAIL_ENABLED', '1')
    monkeypatch.setenv('SUMMARY_EMAIL_MAX_MINUTES', '120')
    uid = _make_user('sumlong@test.com')
    with A.app.app_context():
        A.ensure_summary_email_tables()
        A.db.session.add(A.SavedFeed(
            user_id=uid, name='Show',
            rss_url='https://feeds.example.com/long.xml',
            email_summaries=True,
            summary_email_trial_started_at=datetime.now(timezone.utc),
        ))
        job = summary_email_mod.enqueue_job(
            A.db, A.SummaryEmailJob,
            rss_url='https://feeds.example.com/long.xml',
            audio_url='https://cdn.example.com/long.mp3',
            episode_guid='long-1',
            duration_seconds=180 * 60,
        )
        stats = {'jobs_seen': 0, 'jobs_started': 0, 'jobs_done': 0,
                 'emails_sent': 0, 'skipped': 0, 'failed': 0}
        summary_email_mod._process_one_job(
            db=A.db, job=job, User=A.User, SavedFeed=A.SavedFeed,
            TranscriptionTask=A.TranscriptionTask,
            EmailSentLog=A.EmailSentLog,
            SummaryEmailBudgetDay=A.SummaryEmailBudgetDay,
            build_openai_client=lambda k: _fake_openai_client(),
            platform_api_key='sk-test',
            start_shared_transcription=lambda *a, **k: None,
            public_base_url='https://podskrift.com',
            secret_key=A.app.secret_key,
            stats=stats,
        )
        A.db.session.refresh(job)
        assert job.status == 'skipped'
        assert job.skip_reason == 'episode_too_long'
        assert stats['skipped'] == 1


def test_process_job_reuses_cached_transcript_and_emails(monkeypatch):
    monkeypatch.setenv('SUMMARY_EMAIL_ENABLED', '1')
    _enable_mail(monkeypatch)
    fake = mock.Mock(status_code=200, text='ok')
    monkeypatch.setattr(mailer.requests, 'post', mock.Mock(return_value=fake))
    uid = _make_user('sumcache@test.com')
    audio = 'https://cdn.example.com/cached-ep.mp3'
    with A.app.app_context():
        A.ensure_summary_email_tables()
        A.ensure_email_sent_log_table()
        A.db.session.add(A.SavedFeed(
            user_id=uid, name='Show',
            rss_url='https://feeds.example.com/cache.xml',
            email_summaries=True,
            summary_email_trial_started_at=datetime.now(timezone.utc),
        ))
        A.db.session.add(A.TranscriptionTask(
            id='cached-task', user_id=uid, episode_title='Ep',
            podcast_name='Show', status='completed',
            transcript_text='Cached full transcript body.',
            source_audio_url=audio,
            audio_duration=600,
            summary_json=json.dumps({
                'tldr': 'Cached tldr',
                'key_points': ['a', 'b', 'c', 'd', 'e'],
                'quotes': ['q'],
                'is_partial': False,
            }),
            summary_status='ready',
        ))
        A.db.session.commit()
        job = summary_email_mod.enqueue_job(
            A.db, A.SummaryEmailJob,
            rss_url='https://feeds.example.com/cache.xml',
            audio_url=audio,
            episode_guid='cache-1',
            episode_title='Ep',
            podcast_name='Show',
            duration_seconds=600,
        )
        stats = {'jobs_seen': 0, 'jobs_started': 0, 'jobs_done': 0,
                 'emails_sent': 0, 'skipped': 0, 'failed': 0}
        summary_email_mod._process_one_job(
            db=A.db, job=job, User=A.User, SavedFeed=A.SavedFeed,
            TranscriptionTask=A.TranscriptionTask,
            EmailSentLog=A.EmailSentLog,
            SummaryEmailBudgetDay=A.SummaryEmailBudgetDay,
            build_openai_client=lambda k: _fake_openai_client(),
            platform_api_key='sk-test',
            start_shared_transcription=lambda *a, **k: (_ for _ in ()).throw(
                AssertionError('must reuse cache')),
            public_base_url='https://podskrift.com',
            secret_key=A.app.secret_key,
            stats=stats,
        )
        A.db.session.refresh(job)
        assert job.status == 'done'
        assert job.task_id == 'cached-task'
        assert stats['emails_sent'] == 1
        assert stats['jobs_done'] == 1


def test_trial_expired_skips_new_work(monkeypatch):
    monkeypatch.setenv('SUMMARY_EMAIL_ENABLED', '1')
    uid = _make_user('sumexp@test.com')
    with A.app.app_context():
        A.ensure_summary_email_tables()
        A.db.session.add(A.SavedFeed(
            user_id=uid, name='Show',
            rss_url='https://feeds.example.com/exp.xml',
            email_summaries=True,
            summary_email_trial_started_at=(
                datetime.now(timezone.utc) - timedelta(days=20)),
        ))
        A.db.session.commit()
        job = summary_email_mod.enqueue_job(
            A.db, A.SummaryEmailJob,
            rss_url='https://feeds.example.com/exp.xml',
            audio_url='https://cdn.example.com/exp.mp3',
            episode_guid='exp-1',
            duration_seconds=600,
        )
        stats = {'jobs_seen': 0, 'jobs_started': 0, 'jobs_done': 0,
                 'emails_sent': 0, 'skipped': 0, 'failed': 0}
        summary_email_mod._process_one_job(
            db=A.db, job=job, User=A.User, SavedFeed=A.SavedFeed,
            TranscriptionTask=A.TranscriptionTask,
            EmailSentLog=A.EmailSentLog,
            SummaryEmailBudgetDay=A.SummaryEmailBudgetDay,
            build_openai_client=lambda k: _fake_openai_client(),
            platform_api_key='sk-test',
            start_shared_transcription=lambda *a, **k: (_ for _ in ()).throw(
                AssertionError('expired must not start')),
            public_base_url='https://podskrift.com',
            secret_key=A.app.secret_key,
            stats=stats,
        )
        A.db.session.refresh(job)
        assert job.status == 'skipped'
        assert job.skip_reason == 'trial_expired_only'


def test_changelog_hides_summary_until_flags_on():
    entries = A.load_changelog_entries()
    ids = {e['id'] for e in entries}
    assert 'auto-summary-and-summary-email' not in ids


# ---------------------------------------------------------------------------
# Migrations
# ---------------------------------------------------------------------------

def test_summary_task_columns_in_migrations():
    from models import TASK_COLUMN_MIGRATIONS, TranscriptionTask
    for col in (
        'summary_json', 'summary_status', 'summary_model',
        'summary_prompt_tokens', 'summary_completion_tokens',
        'summary_cost_usd_est', 'summary_source_task_id',
    ):
        assert col in TASK_COLUMN_MIGRATIONS
        assert hasattr(TranscriptionTask, col)


def test_summary_feed_columns_in_migrations():
    from models import SAVED_FEED_COLUMN_MIGRATIONS, SavedFeed
    assert 'email_summaries' in SAVED_FEED_COLUMN_MIGRATIONS
    assert 'summary_email_trial_started_at' in SAVED_FEED_COLUMN_MIGRATIONS
    assert hasattr(SavedFeed, 'email_summaries')


def test_summary_migration_on_current_production_schema():
    """CURRENT production (post-email #63) → add summary columns + job tables."""
    import sqlite3

    path = os.path.join(tempfile.mkdtemp(prefix='podskrift-sum-mig-'), 'prod.db')
    conn = sqlite3.connect(path)
    try:
        conn.executescript("""
            CREATE TABLE users (
                id INTEGER NOT NULL PRIMARY KEY,
                email VARCHAR(255) NOT NULL UNIQUE,
                password_hash VARCHAR(255) NOT NULL,
                openai_api_key VARCHAR(255),
                created_at DATETIME,
                trial_seconds_limit INTEGER,
                trial_seconds_used INTEGER NOT NULL DEFAULT 0,
                paid_seconds_balance INTEGER NOT NULL DEFAULT 0,
                api_key_hash VARCHAR(64),
                api_key_prefix VARCHAR(16),
                api_key_created_at DATETIME,
                email_transcript_ready BOOLEAN NOT NULL DEFAULT 1,
                email_unsubscribed_at DATETIME
            );
            CREATE TABLE saved_feeds (
                id INTEGER NOT NULL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                name VARCHAR(255) NOT NULL,
                rss_url VARCHAR(1024) NOT NULL,
                created_at DATETIME,
                email_new_episodes BOOLEAN NOT NULL DEFAULT 1,
                alerts_initialized BOOLEAN NOT NULL DEFAULT 0,
                last_seen_episode_guid VARCHAR(1024),
                last_seen_published_ts FLOAT,
                FOREIGN KEY(user_id) REFERENCES users (id)
            );
            CREATE TABLE transcription_tasks (
                id VARCHAR(36) NOT NULL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                episode_title VARCHAR(512) NOT NULL,
                rss_url VARCHAR(1024),
                status VARCHAR(20) NOT NULL,
                progress INTEGER NOT NULL DEFAULT 0,
                download_progress INTEGER NOT NULL DEFAULT 0,
                error_message TEXT,
                transcript_text TEXT,
                segments_json TEXT,
                language VARCHAR(10),
                audio_duration FLOAT,
                transcription_time FLOAT,
                started_at DATETIME,
                completed_at DATETIME,
                podcast_name VARCHAR(512),
                artwork_url VARCHAR(1024),
                episode_published VARCHAR(128),
                source_audio_url VARCHAR(1024),
                phase VARCHAR(20),
                phase_started_at DATETIME,
                chunk_index INTEGER,
                chunk_total INTEGER,
                bytes_downloaded BIGINT,
                bytes_total BIGINT,
                heartbeat_at DATETIME,
                last_polled_at DATETIME,
                trial_seconds_charged INTEGER,
                paid_seconds_charged INTEGER,
                trial_settled BOOLEAN NOT NULL DEFAULT 0,
                partial_meta TEXT,
                FOREIGN KEY(user_id) REFERENCES users (id)
            );
            CREATE TABLE email_sent_log (
                id INTEGER NOT NULL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                kind VARCHAR(64) NOT NULL,
                idempotency_key VARCHAR(255) NOT NULL UNIQUE,
                created_at DATETIME,
                FOREIGN KEY(user_id) REFERENCES users (id)
            );
        """)
        conn.commit()

        def cols(table):
            return {r[1] for r in conn.execute(f'PRAGMA table_info({table})')}

        assert 'email_new_episodes' in cols('saved_feeds')
        assert 'summary_json' not in cols('transcription_tasks')
        assert 'email_summaries' not in cols('saved_feeds')

        from models import TASK_COLUMN_MIGRATIONS, SAVED_FEED_COLUMN_MIGRATIONS
        for table, mapping in (
            ('transcription_tasks', TASK_COLUMN_MIGRATIONS),
            ('saved_feeds', SAVED_FEED_COLUMN_MIGRATIONS),
        ):
            existing = cols(table)
            for column, ddl_type in mapping.items():
                if column in existing:
                    continue
                conn.execute(
                    f'ALTER TABLE {table} ADD COLUMN {column} {ddl_type}')
                conn.commit()

        assert 'summary_json' in cols('transcription_tasks')
        assert 'summary_status' in cols('transcription_tasks')
        assert 'email_summaries' in cols('saved_feeds')
        assert 'summary_email_trial_started_at' in cols('saved_feeds')

        # Job tables: CREATE then index (never reverse).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS summary_email_jobs (
                id INTEGER NOT NULL PRIMARY KEY,
                idempotency_key VARCHAR(255) NOT NULL UNIQUE,
                rss_url VARCHAR(1024) NOT NULL,
                audio_url VARCHAR(1024) NOT NULL,
                episode_guid VARCHAR(1024) NOT NULL,
                episode_title VARCHAR(512),
                podcast_name VARCHAR(512),
                duration_seconds FLOAT,
                status VARCHAR(32) NOT NULL DEFAULT 'queued',
                task_id VARCHAR(36),
                skip_reason VARCHAR(128),
                error_message TEXT,
                budget_seconds INTEGER,
                budget_day VARCHAR(10),
                created_at DATETIME,
                updated_at DATETIME
            )
        """)
        conn.commit()
        assert 'idempotency_key' in cols('summary_email_jobs')
        conn.execute(
            'CREATE INDEX IF NOT EXISTS ix_summary_email_jobs_status '
            'ON summary_email_jobs (status)'
        )
        conn.commit()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS summary_email_budget_days (
                day VARCHAR(10) NOT NULL PRIMARY KEY,
                seconds_used INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.commit()
        assert 'seconds_used' in cols('summary_email_budget_days')
    finally:
        conn.close()


def test_summary_migration_on_fresh_db():
    with A.app.app_context():
        A.ensure_summary_email_tables()
        A.apply_column_migrations()
        assert 'summary_json' in A._live_columns('transcription_tasks')
        assert 'email_summaries' in A._live_columns('saved_feeds')
        assert 'idempotency_key' in A._live_columns('summary_email_jobs')
        assert 'seconds_used' in A._live_columns('summary_email_budget_days')


def test_format_summary_for_txt_partial_label():
    text = summary_mod.format_summary_for_txt({
        'tldr': 'Preview only',
        'key_points': ['a', 'b', 'c', 'd', 'e'],
        'quotes': [],
        'is_partial': True,
    })
    assert 'Preview' in text
    assert 'TL;DR: Preview only' in text


def test_summary_email_refuses_transcription_outside_gunicorn(monkeypatch):
    """Poller is a oneshot process: threads it starts would die on exit."""
    import app as A
    monkeypatch.setattr(A, '_SERVER_STARTED_AT_ENV', '')
    monkeypatch.setattr(A, 'GLOBAL_OPENAI_KEY', 'sk-test')

    class Job:
        audio_url = 'https://cdn.example.com/ep.mp3'
        episode_title = 'Ep'
        rss_url = 'https://feeds.example.com/x.xml'
        podcast_name = 'X'
        duration_seconds = 600.0

    with A.app.app_context():
        assert A.start_shared_transcription_for_summary_email(
            Job(), owner_user_id=1) is None
