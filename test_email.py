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


def test_changelog_hides_email_keeps_user_visible_first():
    entries = A.load_changelog_entries()
    ids = [e['id'] for e in entries]
    assert ids[0] == 'unsubscribe-confirm-click'
    assert 'partial-preview-minutes-wording' in ids
    assert 'partial-trial-preview' in ids
    assert 'email-alerts-coming-soon' not in set(ids)


def test_reenable_feed_alerts_resets_baseline():
    uid = _make_user('reenable@test.com')
    with A.app.app_context():
        feed = A.SavedFeed(
            user_id=uid, name='Demo', rss_url='https://feeds.example.com/re.xml',
            email_new_episodes=False, alerts_initialized=True,
            last_seen_episode_guid='guid-old',
            last_seen_published_ts=123.0,
        )
        A.db.session.add(feed)
        A.db.session.commit()
        feed_id = feed.id
    client = A.app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(uid)
        sess['_fresh'] = True
    resp = client.post(
        f'/feeds/{feed_id}/email-alerts',
        data={'email_new_episodes': '1'},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    with A.app.app_context():
        feed = A.db.session.get(A.SavedFeed, feed_id)
        assert feed.email_new_episodes is True
        assert feed.alerts_initialized is False
        assert feed.last_seen_episode_guid is None
        assert feed.last_seen_published_ts is None


def test_alert_poller_feed_cap_is_40mb():
    assert episode_alerts.FEED_MAX_BYTES == 40 * 1024 * 1024
    assert episode_alerts.FEED_EARLY_STOP_ITEMS >= episode_alerts.MAX_EPISODES_PER_DIGEST


def test_fetch_feed_for_alerts_early_stops(monkeypatch):
    """Streaming reader stops after enough </item> closes (newest-first)."""
    items = []
    for i in range(80):
        items.append(
            f'<item><title>Ep {i}</title><guid>g-{i}</guid>'
            f'<enclosure url="https://cdn.example.com/{i}.mp3" '
            f'type="audio/mpeg"/></item>'
        )
    body = (
        b'<?xml version="1.0"?><rss version="2.0"><channel><title>Big</title>'
        + ''.join(items).encode()
        + b'</channel></rss>'
    )
    # Deliver in small chunks so early-stop can fire mid-stream.
    chunk_size = 512

    class FakeResp:
        status_code = 200
        is_redirect = False
        is_permanent_redirect = False

        def raise_for_status(self):
            return None

        def iter_content(self, size):
            for i in range(0, len(body), chunk_size):
                yield body[i:i + chunk_size]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class FakeSession:
        def get(self, *a, **kw):
            return FakeResp()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(A.requests, 'Session', FakeSession)
    monkeypatch.setattr(A, '_is_fetchable_url', lambda url: True)
    got = A.fetch_feed_for_alerts('https://feeds.example.com/big.xml')
    assert got is not None
    n_items = got.count(b'</item>')
    assert n_items >= episode_alerts.FEED_EARLY_STOP_ITEMS
    # Chunk boundary may include a few extras; must not pull all 80.
    assert n_items < 80
    assert len(got) < len(body)


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


# ---------------------------------------------------------------------------
# Alert poller: baseline ordering + digest timeout claim keep
# ---------------------------------------------------------------------------

SAMPLE_FEED_OLDEST_FIRST = b"""<?xml version="1.0"?>
<rss version="2.0">
  <channel>
    <title>Oldest First Show</title>
    <item>
      <title>Episode 1</title>
      <guid>guid-old-1</guid>
      <pubDate>Sun, 01 Jan 2023 12:00:00 GMT</pubDate>
      <enclosure url="https://cdn.example.com/1.mp3" type="audio/mpeg"/>
    </item>
    <item>
      <title>Episode 2</title>
      <guid>guid-old-2</guid>
      <pubDate>Mon, 01 Jan 2024 12:00:00 GMT</pubDate>
      <enclosure url="https://cdn.example.com/2.mp3" type="audio/mpeg"/>
    </item>
    <item>
      <title>Episode 3</title>
      <guid>guid-old-3</guid>
      <pubDate>Tue, 01 Jan 2025 12:00:00 GMT</pubDate>
      <enclosure url="https://cdn.example.com/3.mp3" type="audio/mpeg"/>
    </item>
  </channel>
</rss>
"""


def test_baseline_from_episodes_uses_newest_publish_date():
    """Oldest-first feeds must not watermark on items[0] (the oldest)."""
    episodes = episode_alerts.parse_feed_episodes(SAMPLE_FEED_OLDEST_FIRST)
    assert episodes[0]['guid'] == 'guid-old-1'
    guid, ts = episode_alerts.baseline_from_episodes(episodes)
    assert guid == 'guid-old-3'
    assert ts == episodes[-1]['published_ts']


def test_oldest_first_feed_baselines_without_flood(monkeypatch):
    _enable_mail(monkeypatch)
    _isolate_alert_feeds()
    uid = _make_user('oldest-first@test.com')
    with A.app.app_context():
        A.db.session.add(A.SavedFeed(
            user_id=uid, name='OF', rss_url='https://feeds.example.com/of.xml',
            email_new_episodes=True,
        ))
        A.db.session.commit()

    with mock.patch.object(mailer.requests, 'post') as post:
        stats = episode_alerts.run_new_episode_poll(
            app=A.app, db=A.db, User=A.User, SavedFeed=A.SavedFeed,
            EmailSentLog=A.EmailSentLog,
            fetch_feed=lambda url: SAMPLE_FEED_OLDEST_FIRST,
            public_base_url='https://podskrift.com',
            secret_key=A.app.secret_key,
        )
    assert stats['baselines_set'] == 1
    assert stats['emails_sent'] == 0
    post.assert_not_called()
    with A.app.app_context():
        feed = A.SavedFeed.query.filter_by(user_id=uid).one()
        assert feed.alerts_initialized is True
        assert feed.last_seen_episode_guid == 'guid-old-3'


def test_digest_send_timeout_keeps_sent_log_claims(monkeypatch):
    """Ambiguous Mailgun timeout must not release claims (no duplicate digest)."""
    _enable_mail(monkeypatch)
    _isolate_alert_feeds()
    uid = _make_user('digest-timeout@test.com')
    with A.app.app_context():
        feed = A.SavedFeed(
            user_id=uid, name='Demo', rss_url='https://feeds.example.com/to.xml',
            email_new_episodes=True, alerts_initialized=True,
            last_seen_episode_guid='guid-2',
            last_seen_published_ts=1704110400.0,
        )
        A.db.session.add(feed)
        A.db.session.commit()

    with mock.patch.object(
        mailer.requests, 'post',
        side_effect=mailer.requests.Timeout('slow'),
    ):
        stats = episode_alerts.run_new_episode_poll(
            app=A.app, db=A.db, User=A.User, SavedFeed=A.SavedFeed,
            EmailSentLog=A.EmailSentLog,
            fetch_feed=lambda url: SAMPLE_FEED_V2,
            public_base_url='https://podskrift.com',
            secret_key=A.app.secret_key,
        )
    assert stats['emails_sent'] == 0
    with A.app.app_context():
        claims = A.EmailSentLog.query.filter_by(user_id=uid).count()
        assert claims >= 1, 'timeout must keep the sent-log claim'
        # Second poll must not re-send / re-claim the same episode.
        stats2 = episode_alerts.run_new_episode_poll(
            app=A.app, db=A.db, User=A.User, SavedFeed=A.SavedFeed,
            EmailSentLog=A.EmailSentLog,
            fetch_feed=lambda url: SAMPLE_FEED_V2,
            public_base_url='https://podskrift.com',
            secret_key=A.app.secret_key,
        )
    assert stats2['emails_sent'] == 0
