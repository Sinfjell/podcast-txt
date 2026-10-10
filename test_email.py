"""Transactional email: Mailgun sender, prefs, transcript-ready, new-episode poller.

Mailgun is always mocked — no network. DATABASE_URL / Sentry / PostHog isolation
matches test_app.py (throwaway DB before importing app).
"""

from __future__ import annotations

import os
import tempfile
import time
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

# Prefer the suite DB already configured by test_app.py (collected first).
# Overwriting DATABASE_URL here used to point cross-process children at an
# empty file while users lived in test_app's DB — every reserve looked refused.
if not os.environ.get('DATABASE_URL'):
    _TEST_DB = os.path.join(tempfile.mkdtemp(prefix='podskrift-email-'), 'test.db')
    os.environ['DATABASE_URL'] = f'sqlite:///{_TEST_DB}'
os.environ.setdefault('SENTRY_DSN', '')
os.environ.setdefault('POSTHOG_KEY', '')
os.environ.setdefault('POSTHOG_HOST', '')
os.environ.setdefault('PODSKRIFT_DISABLE_WATCHDOG', '1')
os.environ.setdefault('PODSKRIFT_DISABLE_SHUTDOWN_HANDLERS', '1')
os.environ['EMAIL_ENABLED'] = '0'
os.environ.pop('MAILGUN_API_KEY', None)
os.environ.pop('MAILGUN_DOMAIN', None)

import app as A  # noqa: E402
import email_notify  # noqa: E402
import mail as mailer  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_mail_env(monkeypatch):
    monkeypatch.setenv('EMAIL_ENABLED', '0')
    monkeypatch.delenv('MAILGUN_API_KEY', raising=False)
    monkeypatch.delenv('MAILGUN_DOMAIN', raising=False)
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
    monkeypatch.setenv('MAILGUN_BASE_URL', 'https://api.eu.mailgun.net')
    monkeypatch.setattr(A, 'PUBLIC_BASE_URL', 'https://podskrift.com')


# ---------------------------------------------------------------------------
# Mail sender
# ---------------------------------------------------------------------------

def test_mail_noop_when_disabled(monkeypatch):
    monkeypatch.setenv('EMAIL_ENABLED', '0')
    with mock.patch.object(mailer.requests, 'post') as post:
        assert mailer.send_email(
            to='a@b.com', subject='Hi', text='body', kind='t') == mailer.SEND_FAILED
        post.assert_not_called()


def test_mail_noop_when_misconfigured(monkeypatch):
    monkeypatch.setenv('EMAIL_ENABLED', '1')
    monkeypatch.delenv('MAILGUN_API_KEY', raising=False)
    with mock.patch.object(mailer.requests, 'post') as post:
        assert mailer.send_email(
            to='a@b.com', subject='Hi', text='body', kind='t') == mailer.SEND_FAILED
        post.assert_not_called()


def test_mail_posts_to_eu_endpoint(monkeypatch):
    _enable_mail(monkeypatch)
    fake = mock.Mock()
    fake.status_code = 200
    fake.text = 'ok'
    with mock.patch.object(mailer.requests, 'post', return_value=fake) as post:
        assert mailer.send_email(
            to='a@b.com', subject='Hi', text='plain', html='<p>x</p>',
            headers={'List-Unsubscribe': '<https://x>'},
            tags=['transcript_ready'],
            kind='transcript_ready',
        ) == mailer.SEND_SENT
    assert post.call_count == 1
    args, kwargs = post.call_args
    assert args[0] == 'https://api.eu.mailgun.net/v3/podskrift.com/messages'
    assert kwargs['auth'] == ('api', 'key-test')
    pairs = kwargs['data']
    assert ('to', 'a@b.com') in pairs
    assert ('subject', 'Hi') in pairs
    assert ('h:List-Unsubscribe', '<https://x>') in pairs
    assert ('o:tag', 'transcript_ready') in pairs
    assert ('o:tracking', 'no') in pairs
    assert ('o:tracking-clicks', 'no') in pairs
    assert ('o:tracking-opens', 'no') in pairs
    assert not any(k == 'h:Reply-To' for k, _ in pairs)


def test_mail_domain_defaults_to_root(monkeypatch):
    monkeypatch.setenv('EMAIL_ENABLED', '1')
    monkeypatch.setenv('MAILGUN_API_KEY', 'key-test')
    monkeypatch.delenv('MAILGUN_DOMAIN', raising=False)
    assert mailer.mailgun_domain() == 'podskrift.com'
    fake = mock.Mock(status_code=200, text='ok')
    with mock.patch.object(mailer.requests, 'post', return_value=fake) as post:
        assert mailer.send_email(
            to='a@b.com', subject='Hi', text='x', kind='t') == mailer.SEND_SENT
    assert post.call_args[0][0].endswith('/v3/podskrift.com/messages')


def test_mail_retries_5xx_then_succeeds(monkeypatch):
    _enable_mail(monkeypatch)
    bad = mock.Mock(status_code=502, text='bad')
    good = mock.Mock(status_code=200, text='ok')
    with mock.patch.object(mailer.requests, 'post', side_effect=[bad, good]) as post:
        with mock.patch.object(mailer.time, 'sleep'):
            assert mailer.send_email(
                to='a@b.com', subject='Hi', text='x', kind='t',
                max_attempts=3,
                idempotency_key='stable-key-1',
            ) == mailer.SEND_SENT
    assert post.call_count == 2
    msg_ids = [
        dict(c.kwargs['data']).get('h:Message-Id') for c in post.call_args_list
    ]
    assert msg_ids[0] and msg_ids[0] == msg_ids[1]


def test_mail_does_not_retry_ambiguous_transport_errors(monkeypatch):
    _enable_mail(monkeypatch)
    with mock.patch.object(
        mailer.requests, 'post',
        side_effect=mailer.requests.Timeout('slow'),
    ) as post:
        with mock.patch.object(mailer.time, 'sleep') as sleep:
            assert mailer.send_email(
                to='a@b.com', subject='Hi', text='x', kind='t',
                max_attempts=3,
            ) == mailer.SEND_AMBIGUOUS
    assert post.call_count == 1
    sleep.assert_not_called()


def test_mail_4xx_warning_no_pii_sentry_once(monkeypatch, sentry_events, caplog):
    _enable_mail(monkeypatch)
    bad = mock.Mock(status_code=400, text='to address "secret@pii.example" is invalid')
    import logging
    with caplog.at_level(logging.WARNING, logger='mail'):
        with mock.patch.object(mailer.requests, 'post', return_value=bad):
            assert mailer.send_email(
                to='secret@pii.example', subject='Hi', text='x', kind='t',
            ) == mailer.SEND_FAILED
            assert mailer.send_email(
                to='other@pii.example', subject='Hi', text='x', kind='t',
            ) == mailer.SEND_FAILED
    joined = ' '.join(r.getMessage() for r in caplog.records)
    assert 'secret@pii.example' not in joined
    assert 'other@pii.example' not in joined
    assert 'HTTP 400' in joined
    # Warning only — logging integration must not invent ERROR events.
    assert not any(r.levelno >= logging.ERROR for r in caplog.records if r.name == 'mail')
    kinds = [e.get('tags', {}).get('mail.kind') for e in sentry_events]
    assert kinds.count('http_400') == 1


def test_mail_sentry_once_per_kind(monkeypatch, sentry_events):
    _enable_mail(monkeypatch)
    bad = mock.Mock(status_code=500, text='nope')
    with mock.patch.object(mailer.requests, 'post', return_value=bad):
        with mock.patch.object(mailer.time, 'sleep'):
            assert mailer.send_email(
                to='a@b.com', subject='Hi', text='x', kind='t',
                max_attempts=2) == mailer.SEND_FAILED
            assert mailer.send_email(
                to='b@b.com', subject='Hi', text='x', kind='t',
                max_attempts=2) == mailer.SEND_FAILED
    kinds = [e.get('tags', {}).get('mail.kind') for e in sentry_events]
    assert kinds.count('http_500') == 1


@pytest.fixture
def sentry_events():
    import sentry_sdk
    from sentry_sdk.transport import Transport
    import observability

    class Capture(Transport):
        def __init__(self):
            super().__init__()
            self.events = []

        def capture_envelope(self, envelope):
            event = envelope.get_event()
            if event is not None:
                self.events.append(event)

    transport = Capture()
    assert observability.init_sentry(
        dsn='https://public@sentry.invalid/1', transport=transport)
    yield transport.events
    sentry_sdk.get_client().close()
    sentry_sdk.init()


# ---------------------------------------------------------------------------
# Prefs / unsubscribe / timing
# ---------------------------------------------------------------------------

def test_unsubscribe_token_roundtrip():
    token = email_notify.make_unsubscribe_token('secret', 42)
    assert email_notify.parse_unsubscribe_token('secret', token) == 42
    assert email_notify.parse_unsubscribe_token('other', token) is None


def test_unsubscribe_get_shows_confirm_without_unsubscribing():
    uid = _make_user('unsub@test.com')
    token = email_notify.make_unsubscribe_token(A.app.secret_key, uid)
    client = A.app.test_client()
    resp = client.get(f'/email/unsubscribe/{token}')
    assert resp.status_code == 200
    body = resp.data.lower()
    assert b'yes, unsubscribe me' in body
    assert b'confirm' in resp.data  # hidden confirm field
    with A.app.app_context():
        user = A.db.session.get(A.User, uid)
        assert user.email_unsubscribed_at is None
        assert user.email_transcript_ready is not False


def test_unsubscribe_confirm_post_works_without_login():
    uid = _make_user('unsub-confirm@test.com')
    token = email_notify.make_unsubscribe_token(A.app.secret_key, uid)
    client = A.app.test_client()
    resp = client.post(
        f'/email/unsubscribe/{token}', data={'confirm': '1'})
    assert resp.status_code == 200
    assert b'unsubscribed' in resp.data.lower()
    with A.app.app_context():
        user = A.db.session.get(A.User, uid)
        assert user.email_unsubscribed_at is not None
        assert user.email_transcript_ready is False


def test_unsubscribe_post_one_click_rfc8058():
    uid = _make_user('unsub2@test.com')
    token = email_notify.make_unsubscribe_token(A.app.secret_key, uid)
    resp = A.app.test_client().post(
        f'/email/unsubscribe/{token}',
        data={'List-Unsubscribe': 'One-Click'},
    )
    assert resp.status_code == 200
    assert resp.data == b''
    with A.app.app_context():
        user = A.db.session.get(A.User, uid)
        assert user.email_unsubscribed_at is not None


def test_should_send_transcript_ready_rules():
    now = datetime.now(timezone.utc)

    class T:
        pass

    long_job = T()
    long_job.started_at = now - timedelta(seconds=90)
    long_job.completed_at = now
    long_job.last_polled_at = now  # still watching, but job was slow
    assert email_notify.should_send_transcript_ready(long_job, now=now)

    short_watching = T()
    short_watching.started_at = now - timedelta(seconds=20)
    short_watching.completed_at = now
    short_watching.last_polled_at = now - timedelta(seconds=5)
    assert not email_notify.should_send_transcript_ready(short_watching, now=now)

    short_away = T()
    short_away.started_at = now - timedelta(seconds=20)
    short_away.completed_at = now
    short_away.last_polled_at = now - timedelta(seconds=180)
    assert email_notify.should_send_transcript_ready(short_away, now=now)


def test_transcript_ready_idempotent(monkeypatch, ph_events):
    _enable_mail(monkeypatch)
    uid = _make_user('ready@test.com')
    with A.app.app_context():
        task = A.TranscriptionTask(
            id='ready-task-1', user_id=uid, episode_title='Ep One',
            podcast_name='Show', status='completed',
            started_at=datetime.now(timezone.utc) - timedelta(minutes=5),
            completed_at=datetime.now(timezone.utc),
        )
        A.db.session.add(task)
        A.db.session.commit()
        user = A.db.session.get(A.User, uid)
        fake = mock.Mock(status_code=200, text='ok')
        with mock.patch.object(mailer.requests, 'post', return_value=fake) as post:
            assert email_notify.notify_transcript_ready(
                db=A.db, user=user, task=task, EmailSentLog=A.EmailSentLog,
                public_base_url='https://podskrift.com',
                secret_key=A.app.secret_key,
            )
            assert email_notify.notify_transcript_ready(
                db=A.db, user=user, task=task, EmailSentLog=A.EmailSentLog,
                public_base_url='https://podskrift.com',
                secret_key=A.app.secret_key,
            ) is False
        assert post.call_count == 1
    sent = [e for e in ph_events.events if e['event'] == 'email_sent']
    assert len(sent) == 1
    assert sent[0]['properties']['type'] == 'transcript_ready'


@pytest.fixture
def ph_events(monkeypatch):
    import analytics

    class Fake:
        def __init__(self):
            self.events = []

        def capture(self, event, **kwargs):
            self.events.append({
                'event': event,
                'distinct_id': kwargs.get('distinct_id'),
                'properties': kwargs.get('properties') or {},
            })

    fake = Fake()
    monkeypatch.setattr(analytics, '_client', fake)
    yield fake
    monkeypatch.setattr(analytics, '_client', False)


def test_settings_email_toggle():
    uid = _make_user('prefs@test.com')
    client = A.app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(uid)
        sess['_fresh'] = True
    page = client.get('/settings').data.decode()
    assert 'email_transcript_ready' in page
    assert 'Email me when a transcript is ready' in page
    client.post('/settings', data={'form': 'email_prefs'})  # unchecked → off
    with A.app.app_context():
        assert A.db.session.get(A.User, uid).email_transcript_ready is False
    client.post('/settings', data={
        'form': 'email_prefs', 'email_transcript_ready': '1'})
    with A.app.app_context():
        assert A.db.session.get(A.User, uid).email_transcript_ready is True


def test_saved_feed_columns_have_migrations():
    from models import SavedFeed, SAVED_FEED_COLUMN_MIGRATIONS
    baseline = {'id', 'user_id', 'name', 'rss_url', 'created_at'}
    cols = {c.name for c in SavedFeed.__table__.columns}
    added = cols - baseline
    assert added == set(SAVED_FEED_COLUMN_MIGRATIONS)


def test_email_migration_on_legacy_production_schema():
    """Simulate CURRENT production schema (pre-email), then run migrations.

    Applies the same SQL the app helpers run, against a throwaway SQLite file,
    without rebinding the live Flask engine (that would poison the suite DB).
    """
    import sqlite3
    import tempfile
    from models import (
        USER_COLUMN_MIGRATIONS, SAVED_FEED_COLUMN_MIGRATIONS,
        TASK_COLUMN_MIGRATIONS,
    )

    path = os.path.join(tempfile.mkdtemp(prefix='podskrift-mig-'), 'legacy.db')
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
                api_key_created_at DATETIME
            );
            CREATE TABLE saved_feeds (
                id INTEGER NOT NULL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                name VARCHAR(255) NOT NULL,
                rss_url VARCHAR(1024) NOT NULL,
                created_at DATETIME,
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
                trial_seconds_charged INTEGER,
                paid_seconds_charged INTEGER,
                trial_settled BOOLEAN NOT NULL DEFAULT 0,
                partial_meta TEXT,
                FOREIGN KEY(user_id) REFERENCES users (id)
            );
        """)
        conn.commit()

        def cols(table):
            return {r[1] for r in conn.execute(f'PRAGMA table_info({table})')}

        # Pre-email production (post-#59): has partial_meta, lacks email columns.
        assert 'partial_meta' in cols('transcription_tasks')
        assert 'email_transcript_ready' not in cols('users')
        assert 'email_new_episodes' not in cols('saved_feeds')
        assert 'last_polled_at' not in cols('transcription_tasks')

        # Mirror ensure_email_sent_log_table: CREATE then index (never reverse).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS email_sent_log (
                id INTEGER NOT NULL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                kind VARCHAR(64) NOT NULL,
                idempotency_key VARCHAR(255) NOT NULL UNIQUE,
                created_at DATETIME,
                FOREIGN KEY(user_id) REFERENCES users (id)
            )
        """)
        conn.commit()
        assert 'idempotency_key' in cols('email_sent_log')
        conn.execute(
            'CREATE INDEX IF NOT EXISTS ix_email_sent_log_user_id '
            'ON email_sent_log (user_id)'
        )
        conn.commit()

        migrations = (
            ('users', USER_COLUMN_MIGRATIONS),
            ('saved_feeds', SAVED_FEED_COLUMN_MIGRATIONS),
            ('transcription_tasks', TASK_COLUMN_MIGRATIONS),
        )
        for table, mapping in migrations:
            existing = cols(table)
            for column, ddl_type in mapping.items():
                if column in existing:
                    continue
                conn.execute(
                    f'ALTER TABLE {table} ADD COLUMN {column} {ddl_type}'
                )
                conn.commit()
            # Idempotent second pass (like two gunicorn workers).
            existing = cols(table)
            for column, ddl_type in mapping.items():
                assert column in existing

        for col in USER_COLUMN_MIGRATIONS:
            assert col in cols('users')
        for col in SAVED_FEED_COLUMN_MIGRATIONS:
            assert col in cols('saved_feeds')
        assert 'last_polled_at' in cols('transcription_tasks')
        assert 'partial_meta' in cols('transcription_tasks')
        idx = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name='email_sent_log'"
            )
        }
        assert 'ix_email_sent_log_user_id' in idx
    finally:
        conn.close()


def test_email_migration_on_fresh_db():
    """Suite DB was create_all()'d at import — new columns must be present."""
    with A.app.app_context():
        A.ensure_email_sent_log_table()
        A.apply_column_migrations()
        assert 'email_transcript_ready' in A._live_columns('users')
        assert 'email_new_episodes' in A._live_columns('saved_feeds')
        assert 'last_polled_at' in A._live_columns('transcription_tasks')
        assert 'idempotency_key' in A._live_columns('email_sent_log')


def test_status_updates_last_polled_at():
    uid = _make_user('poll@test.com')
    with A.app.app_context():
        A.db.session.add(A.TranscriptionTask(
            id='poll-task', user_id=uid, episode_title='Ep',
            status='transcribing', progress=10,
        ))
        A.db.session.commit()
    client = A.app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(uid)
        sess['_fresh'] = True
    assert client.get('/status/poll-task').status_code == 200
    with A.app.app_context():
        task = A.db.session.get(A.TranscriptionTask, 'poll-task')
        assert task.last_polled_at is not None


def test_list_unsubscribe_headers_in_transcript_mail(monkeypatch):
    _enable_mail(monkeypatch)
    uid = _make_user('hdr@test.com')
    with A.app.app_context():
        task = A.TranscriptionTask(
            id='hdr-task', user_id=uid, episode_title='Ep',
            status='completed',
            started_at=datetime.now(timezone.utc) - timedelta(minutes=10),
            completed_at=datetime.now(timezone.utc),
        )
        A.db.session.add(task)
        A.db.session.commit()
        user = A.db.session.get(A.User, uid)
        fake = mock.Mock(status_code=200, text='ok')
        with mock.patch.object(mailer.requests, 'post', return_value=fake) as post:
            email_notify.notify_transcript_ready(
                db=A.db, user=user, task=task, EmailSentLog=A.EmailSentLog,
                public_base_url='https://podskrift.com',
                secret_key=A.app.secret_key,
            )
        pairs = post.call_args.kwargs['data']
        keys = {k for k, _ in pairs}
        assert 'h:List-Unsubscribe' in keys
        assert 'h:List-Unsubscribe-Post' in keys


def test_partial_preview_email_wording(monkeypatch):
    """Free-preview jobs must not claim the full transcript is ready."""
    _enable_mail(monkeypatch)
    uid = _make_user('partial-mail@test.com')
    with A.app.app_context():
        task = A.TranscriptionTask(
            id='partial-mail-task', user_id=uid,
            episode_title='Long Ep', podcast_name='Show',
            status='completed',
            partial_meta=A.encode_partial_task_meta(3600, 9000),
            started_at=datetime.now(timezone.utc) - timedelta(minutes=10),
            completed_at=datetime.now(timezone.utc),
        )
        A.db.session.add(task)
        A.db.session.commit()
        user = A.db.session.get(A.User, uid)
        fake = mock.Mock(status_code=200, text='ok')
        with mock.patch.object(mailer.requests, 'post', return_value=fake) as post:
            assert email_notify.notify_transcript_ready(
                db=A.db, user=user, task=task, EmailSentLog=A.EmailSentLog,
                public_base_url='https://podskrift.com',
                secret_key=A.app.secret_key,
            )
        pairs = dict(post.call_args.kwargs['data'])
        assert pairs['subject'].startswith('Free preview ready:')
        assert 'free preview' in pairs['text'].lower()
        assert 'finish the rest' in pairs['text'].lower()
        assert 'Your transcript of' not in pairs['text']
        assert '300 min for $5' in pairs['text']
        assert 'utm_content=pack_offer' in pairs['text']
        assert 'ref=email_pack' in pairs['text']
        assert 'unsubscribe' in pairs['text'].lower()


def test_remaining_minutes_line_and_pack_offer_helpers():
    assert email_notify.approx_episodes_phrase(63, 45) == 'about 1-2 more episodes'
    assert email_notify.approx_episodes_phrase(90, 45) == 'about 2 more episodes'
    assert email_notify.approx_episodes_phrase(10, 45) == 'less than 1 more episode'
    line = email_notify.format_remaining_minutes_line(63, 0, typical_episode_min=45)
    assert line == 'You have 63 free minutes left (about 1-2 more episodes).'
    mixed = email_notify.format_remaining_minutes_line(20, 40, typical_episode_min=45)
    assert '20 free + 40 paid' in mixed
    assert email_notify.should_include_pack_offer(30, 0) is True
    assert email_notify.should_include_pack_offer(63, 0) is True  # trial-only
    assert email_notify.should_include_pack_offer(0, 200) is False
    assert email_notify.should_include_pack_offer(10, 100) is False
    assert email_notify.should_include_pack_offer(0, 40) is True  # low paid
    assert email_notify.should_include_pack_offer(200, 0, is_partial=True) is True


def test_transcript_ready_email_low_minutes_includes_pack_offer():
    """Low remaining (or trial-only) → remaining line + single 300-for-$5 link."""
    subject, text, html = email_notify.build_transcript_ready_bodies(
        podcast_name='Show',
        episode_title='Ep One',
        transcript_url='https://podskrift.com/transcription/abc',
        unsub_url='https://podskrift.com/email/unsubscribe/tok',
        pack_url='https://podskrift.com/pricing',
        remaining_free_min=30,
        remaining_paid_min=0,
        typical_episode_min=45,
    )
    assert subject.startswith('Transcript ready:')
    assert 'You have 30 free minutes left' in text
    assert 'about 1 more episode' in text
    assert 'Need more minutes? 300 min for $5:' in text
    assert 'utm_source=email' in text
    assert 'utm_content=pack_offer' in text
    assert 'ref=email_pack' in text
    assert '/pricing' in text
    assert 'unsubscribe' in text.lower()
    assert '300 min for $5' in html
    assert 'utm_content=pack_offer' in html
    assert 'You have 30 free minutes left' in html


def test_transcript_ready_email_plenty_minutes_skips_pack_offer():
    """Plenty of paid minutes → remaining line only; no pack CTA."""
    subject, text, html = email_notify.build_transcript_ready_bodies(
        podcast_name='Show',
        episode_title='Ep Two',
        transcript_url='https://podskrift.com/transcription/xyz',
        unsub_url='https://podskrift.com/email/unsubscribe/tok',
        pack_url=None,
        remaining_free_min=0,
        remaining_paid_min=200,
        typical_episode_min=45,
    )
    assert subject.startswith('Transcript ready:')
    assert 'You have 200 paid minutes left' in text
    assert 'about 4-5 more episodes' in text
    assert '300 min for $5' not in text
    assert 'utm_content=pack_offer' not in text
    assert 'Need more minutes?' not in text
    assert 'unsubscribe' in text.lower()
    assert 'You have 200 paid minutes left' in html
    assert '300 min for $5' not in html
    assert 'utm_content=pack_offer' not in html

    # Remaining-line sample from the product ask (trial-only still gets CTA).
    _, sample, _ = email_notify.build_transcript_ready_bodies(
        podcast_name='Show',
        episode_title='Ep',
        transcript_url='https://podskrift.com/transcription/x',
        unsub_url='https://podskrift.com/email/unsubscribe/tok',
        pack_url='https://podskrift.com/pricing',
        remaining_free_min=63,
        remaining_paid_min=0,
        typical_episode_min=45,
    )
    assert 'You have 63 free minutes left (about 1-2 more episodes).' in sample
    assert '300 min for $5' in sample


def _enable_trial(monkeypatch):
    monkeypatch.setattr(A, 'GLOBAL_OPENAI_KEY', 'sk-global-not-a-real-key')
    monkeypatch.setattr(A, 'TRIAL_ENABLED', True)
    monkeypatch.setattr(A, 'TRIAL_DEFAULT_SECONDS', 180 * 60)
    monkeypatch.setattr(A, 'TRIAL_DAILY_SECONDS', 10 ** 7)
    monkeypatch.setattr(A, 'TRIAL_GLOBAL_SECONDS', 0)


def test_notify_transcript_ready_low_vs_plenty_minutes(monkeypatch):
    """Integration: Mailgun body includes balance; pack CTA only when low."""
    _enable_mail(monkeypatch)
    _enable_trial(monkeypatch)

    def _send(email, *, limit, used, paid=0, task_id):
        uid = _make_user(email, limit=limit, used=used)
        with A.app.app_context():
            u = A.db.session.get(A.User, uid)
            u.paid_seconds_balance = paid
            A.db.session.commit()
            task = A.TranscriptionTask(
                id=task_id, user_id=uid,
                episode_title='Balance Ep', podcast_name='Show',
                status='completed',
                audio_duration=45 * 60.0,
                started_at=datetime.now(timezone.utc) - timedelta(minutes=10),
                completed_at=datetime.now(timezone.utc),
            )
            A.db.session.add(task)
            A.db.session.commit()
            user = A.db.session.get(A.User, uid)
            fake = mock.Mock(status_code=200, text='ok')
            with mock.patch.object(mailer.requests, 'post', return_value=fake) as post:
                assert email_notify.notify_transcript_ready(
                    db=A.db, user=user, task=task, EmailSentLog=A.EmailSentLog,
                    public_base_url='https://podskrift.com',
                    secret_key=A.app.secret_key,
                )
            return dict(post.call_args.kwargs['data'])

    low = _send('low-bal@test.com', limit=40 * 60, used=10 * 60, task_id='bal-low')
    assert 'You have 30 free minutes left' in low['text']
    assert '300 min for $5' in low['text']
    assert 'utm_content=pack_offer' in low['text']
    assert 'ref=email_pack' in low['text']
    assert 'unsubscribe' in low['text'].lower()

    # Plenty of paid minutes (not trial-only) → balance line, no pack CTA.
    plenty = _send(
        'plenty-bal@test.com', limit=10 * 60, used=10 * 60,
        paid=200 * 60, task_id='bal-plenty')
    assert 'You have 200 paid minutes left' in plenty['text']
    assert '300 min for $5' not in plenty['text']
    assert 'utm_content=pack_offer' not in plenty['text']
    assert 'unsubscribe' in plenty['text'].lower()


def test_changelog_hides_email_keeps_user_visible_first():
    entries = A.load_changelog_entries()
    ids = [e['id'] for e in entries]
    # Infra/plumbing (deploy drain, restarts) never goes in What's new.
    assert 'resume-after-deploy' not in ids
    assert 'partial-preview-minutes-wording' in ids
    assert 'partial-trial-preview' in ids
    assert 'email-alerts-coming-soon' not in set(ids)


def test_transcript_ready_scheduled_off_worker_path(monkeypatch):
    """Worker must not block on Mailgun; notify runs on a background thread."""
    uid = _make_user('sched@test.com')
    started = []

    class FakeThread:
        def __init__(self, target=None, daemon=None, name=None):
            self.target = target
            self.daemon = daemon
            self.name = name

        def start(self):
            started.append(self.name)
            # Run inline so the suite can assert the send happened.
            if self.target:
                self.target()

    _enable_mail(monkeypatch)
    monkeypatch.setattr(A.threading, 'Thread', FakeThread)
    fake = mock.Mock(status_code=200, text='ok')
    with mock.patch.object(mailer.requests, 'post', return_value=fake) as post:
        A._schedule_transcript_ready_email('missing-task', uid)
        # Missing task → no send.
        assert post.call_count == 0

        with A.app.app_context():
            task = A.TranscriptionTask(
                id='sched-task', user_id=uid, episode_title='Ep',
                status='completed',
                started_at=datetime.now(timezone.utc) - timedelta(minutes=5),
                completed_at=datetime.now(timezone.utc),
            )
            A.db.session.add(task)
            A.db.session.commit()
        A._schedule_transcript_ready_email('sched-task', uid)
        assert post.call_count == 1
    assert any(n and n.startswith('transcript-ready-') for n in started)

