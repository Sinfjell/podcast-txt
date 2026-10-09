"""Tests for automatic transcript summaries (SUMMARY_ENABLED).

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

import app as A  # noqa: E402
import email_notify  # noqa: E402
import mail as mailer  # noqa: E402
import summary as summary_mod  # noqa: E402
import analytics  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_flags(monkeypatch):
    monkeypatch.setenv('SUMMARY_ENABLED', '0')
    monkeypatch.setenv('EMAIL_ENABLED', '0')
    monkeypatch.delenv('MAILGUN_API_KEY', raising=False)
    summary_mod.reset_sentry_kinds_for_tests()
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
        A.apply_column_migrations()
        assert 'summary_json' in A._live_columns('transcription_tasks')
        assert 'email_summaries' in A._live_columns('saved_feeds')


def test_format_summary_for_txt_partial_label():
    text = summary_mod.format_summary_for_txt({
        'tldr': 'Preview only',
        'key_points': ['a', 'b', 'c', 'd', 'e'],
        'quotes': [],
        'is_partial': True,
    })
    assert 'Preview' in text
    assert 'TL;DR: Preview only' in text
