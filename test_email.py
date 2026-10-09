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

_TEST_DB = os.path.join(tempfile.mkdtemp(prefix='podskrift-email-'), 'test.db')
os.environ['DATABASE_URL'] = f'sqlite:///{_TEST_DB}'
os.environ['SENTRY_DSN'] = ''
os.environ['POSTHOG_KEY'] = ''
os.environ['POSTHOG_HOST'] = ''
os.environ['PODSKRIFT_DISABLE_WATCHDOG'] = '1'
os.environ['EMAIL_ENABLED'] = '0'
os.environ.pop('MAILGUN_API_KEY', None)
os.environ.pop('MAILGUN_DOMAIN', None)

import app as A  # noqa: E402
import email_notify  # noqa: E402
import episode_alerts  # noqa: E402
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
            to='a@b.com', subject='Hi', text='body', kind='t') is False
        post.assert_not_called()


def test_mail_noop_when_misconfigured(monkeypatch):
    monkeypatch.setenv('EMAIL_ENABLED', '1')
    monkeypatch.delenv('MAILGUN_API_KEY', raising=False)
    with mock.patch.object(mailer.requests, 'post') as post:
        assert mailer.send_email(
            to='a@b.com', subject='Hi', text='body', kind='t') is False
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
        ) is True
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
        assert mailer.send_email(to='a@b.com', subject='Hi', text='x', kind='t')
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
            ) is True
    assert post.call_count == 2


def test_mail_sentry_once_per_kind(monkeypatch, sentry_events):
    _enable_mail(monkeypatch)
    bad = mock.Mock(status_code=500, text='nope')
    with mock.patch.object(mailer.requests, 'post', return_value=bad):
        with mock.patch.object(mailer.time, 'sleep'):
            assert mailer.send_email(
                to='a@b.com', subject='Hi', text='x', kind='t',
                max_attempts=2) is False
            assert mailer.send_email(
                to='b@b.com', subject='Hi', text='x', kind='t',
                max_attempts=2) is False
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


def test_unsubscribe_route_works_without_login():
    uid = _make_user('unsub@test.com')
    token = email_notify.make_unsubscribe_token(A.app.secret_key, uid)
    client = A.app.test_client()
    resp = client.get(f'/email/unsubscribe/{token}')
    assert resp.status_code == 200
    assert b'unsubscribed' in resp.data.lower()
    with A.app.app_context():
        user = A.db.session.get(A.User, uid)
        assert user.email_unsubscribed_at is not None
        assert user.email_transcript_ready is False


def test_unsubscribe_post_one_click():
    uid = _make_user('unsub2@test.com')
    token = email_notify.make_unsubscribe_token(A.app.secret_key, uid)
    resp = A.app.test_client().post(f'/email/unsubscribe/{token}')
    assert resp.status_code == 200


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


def test_follow_defaults_email_alerts_on():
    uid = _make_user('follow@test.com')
    with A.app.app_context():
        task = A.TranscriptionTask(
            id='follow-task', user_id=uid, episode_title='Ep',
            rss_url='https://feeds.example.com/show.xml',
            podcast_name='Show', status='completed',
        )
        A.db.session.add(task)
        A.db.session.commit()
    client = A.app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(uid)
        sess['_fresh'] = True
    resp = client.post('/transcription/follow-task/follow')
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['following'] is True
    assert data['email_new_episodes'] is True
    with A.app.app_context():
        feed = A.SavedFeed.query.filter_by(user_id=uid).one()
        assert feed.email_new_episodes is True


def test_feeds_page_shows_alert_checkbox():
    uid = _make_user('feedsui@test.com')
    client = A.app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(uid)
        sess['_fresh'] = True
    body = client.get('/feeds').data.decode()
    assert 'Email me when new episodes come out' in body


# ---------------------------------------------------------------------------
# New-episode poller
# ---------------------------------------------------------------------------

SAMPLE_FEED = b"""<?xml version="1.0"?>
<rss version="2.0">
  <channel>
    <title>Demo Show</title>
    <item>
      <title>Episode 2</title>
      <guid>guid-2</guid>
      <pubDate>Mon, 01 Jan 2024 12:00:00 GMT</pubDate>
      <enclosure url="https://cdn.example.com/2.mp3" type="audio/mpeg"/>
    </item>
    <item>
      <title>Episode 1</title>
      <guid>guid-1</guid>
      <pubDate>Sun, 01 Jan 2023 12:00:00 GMT</pubDate>
      <enclosure url="https://cdn.example.com/1.mp3" type="audio/mpeg"/>
    </item>
  </channel>
</rss>
"""

SAMPLE_FEED_V2 = b"""<?xml version="1.0"?>
<rss version="2.0">
  <channel>
    <title>Demo Show</title>
    <item>
      <title>Episode 3</title>
      <guid>guid-3</guid>
      <pubDate>Tue, 01 Jan 2025 12:00:00 GMT</pubDate>
      <enclosure url="https://cdn.example.com/3.mp3" type="audio/mpeg"/>
    </item>
    <item>
      <title>Episode 2</title>
      <guid>guid-2</guid>
      <pubDate>Mon, 01 Jan 2024 12:00:00 GMT</pubDate>
      <enclosure url="https://cdn.example.com/2.mp3" type="audio/mpeg"/>
    </item>
    <item>
      <title>Episode 1</title>
      <guid>guid-1</guid>
      <pubDate>Sun, 01 Jan 2023 12:00:00 GMT</pubDate>
      <enclosure url="https://cdn.example.com/1.mp3" type="audio/mpeg"/>
    </item>
  </channel>
</rss>
"""


def _isolate_alert_feeds():
    """Turn off other tests' feeds so poller counters stay local."""
    with A.app.app_context():
        A.SavedFeed.query.update({'email_new_episodes': False})
        A.db.session.commit()


def test_first_poll_baselines_without_email(monkeypatch):
    _enable_mail(monkeypatch)
    _isolate_alert_feeds()
    uid = _make_user('base@test.com')
    with A.app.app_context():
        A.db.session.add(A.SavedFeed(
            user_id=uid, name='Demo', rss_url='https://feeds.example.com/a.xml',
            email_new_episodes=True,
        ))
        A.db.session.commit()

    def fetch(url):
        return SAMPLE_FEED

    with mock.patch.object(mailer.requests, 'post') as post:
        stats = episode_alerts.run_new_episode_poll(
            app=A.app, db=A.db, User=A.User, SavedFeed=A.SavedFeed,
            EmailSentLog=A.EmailSentLog, fetch_feed=fetch,
            public_base_url='https://podskrift.com',
            secret_key=A.app.secret_key,
        )
    assert stats['baselines_set'] == 1
    assert stats['emails_sent'] == 0
    post.assert_not_called()
    with A.app.app_context():
        feed = A.SavedFeed.query.filter_by(user_id=uid).one()
        assert feed.alerts_initialized is True
        assert feed.last_seen_episode_guid == 'guid-2'


def test_second_poll_sends_digest_once(monkeypatch, ph_events):
    _enable_mail(monkeypatch)
    _isolate_alert_feeds()
    uid = _make_user('digest@test.com')
    with A.app.app_context():
        # 2024-01-01 12:00:00 UTC
        feed = A.SavedFeed(
            user_id=uid, name='Demo', rss_url='https://feeds.example.com/b.xml',
            email_new_episodes=True, alerts_initialized=True,
            last_seen_episode_guid='guid-2',
            last_seen_published_ts=1704110400.0,
        )
        A.db.session.add(feed)
        A.db.session.commit()

    fake = mock.Mock(status_code=200, text='ok')
    with mock.patch.object(mailer.requests, 'post', return_value=fake) as post:
        stats = episode_alerts.run_new_episode_poll(
            app=A.app, db=A.db, User=A.User, SavedFeed=A.SavedFeed,
            EmailSentLog=A.EmailSentLog,
            fetch_feed=lambda url: SAMPLE_FEED_V2,
            public_base_url='https://podskrift.com',
            secret_key=A.app.secret_key,
        )
        stats2 = episode_alerts.run_new_episode_poll(
            app=A.app, db=A.db, User=A.User, SavedFeed=A.SavedFeed,
            EmailSentLog=A.EmailSentLog,
            fetch_feed=lambda url: SAMPLE_FEED_V2,
            public_base_url='https://podskrift.com',
            secret_key=A.app.secret_key,
        )
    assert stats['emails_sent'] == 1
    assert stats['episodes_announced'] == 1
    assert stats2['emails_sent'] == 0
    assert post.call_count == 1
    pairs = post.call_args.kwargs['data']
    body = dict(pairs).get('text', '')
    assert 'Episode 3' in body
    assert 'utm_source=email' in body
    assert 'List-Unsubscribe' in str(pairs)


def test_global_unsub_skips_digest(monkeypatch):
    _enable_mail(monkeypatch)
    _isolate_alert_feeds()
    uid = _make_user('nosend@test.com')
    with A.app.app_context():
        user = A.db.session.get(A.User, uid)
        user.email_unsubscribed_at = datetime.now(timezone.utc)
        A.db.session.add(A.SavedFeed(
            user_id=uid, name='Demo', rss_url='https://feeds.example.com/c.xml',
            email_new_episodes=True, alerts_initialized=True,
            last_seen_episode_guid='guid-2',
            last_seen_published_ts=1.0,
        ))
        A.db.session.commit()
    with mock.patch.object(mailer.requests, 'post') as post:
        stats = episode_alerts.run_new_episode_poll(
            app=A.app, db=A.db, User=A.User, SavedFeed=A.SavedFeed,
            EmailSentLog=A.EmailSentLog,
            fetch_feed=lambda url: SAMPLE_FEED_V2,
            public_base_url='https://podskrift.com',
            secret_key=A.app.secret_key,
        )
    assert stats['emails_sent'] == 0
    assert stats['skipped_disabled'] >= 1
    post.assert_not_called()


# ---------------------------------------------------------------------------
# Schema migrations
# ---------------------------------------------------------------------------

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


def test_changelog_hides_email_keeps_partial_preview_first():
    entries = A.load_changelog_entries()
    assert entries[0]['id'] == 'partial-trial-preview'
    assert 'email-alerts-coming-soon' not in {e['id'] for e in entries}
