"""Regression tests for the transcription pipeline.

Focused on the defects this suite was written to catch: a progress bar that
reported numbers it did not have, a download bar stuck at 0%, and an
unvalidated server-side fetch of a client-supplied URL.

Run: pytest test_app.py
"""

import os
import tempfile
import time
from datetime import datetime, timedelta, timezone

import pytest

# Importing app.py executes its module-level app_context block: db.create_all,
# the ALTER TABLE migrations, the orphan sweep and a DROP TABLE. Point it at a
# throwaway file BEFORE the import so running the suite can never touch real data.
_TEST_DB = os.path.join(tempfile.mkdtemp(prefix='podskrift-test-'), 'test.db')
os.environ['DATABASE_URL'] = f'sqlite:///{_TEST_DB}'

import app as A  # noqa: E402 - must follow the DATABASE_URL assignment


def test_suite_runs_against_a_throwaway_database():
    """Guards the isolation above: a regression here silently mutates real data."""
    assert A.app.config['SQLALCHEMY_DATABASE_URI'].endswith('test.db')
    assert 'data/podcast.db' not in A.app.config['SQLALCHEMY_DATABASE_URI']


class FakeTask:
    """Stand-in for a TranscriptionTask row."""

    def __init__(self, **kw):
        self.status = 'transcribing'
        self.phase = 'transcribing'
        self.progress = A.PHASE_SPANS['transcribing'][0]
        self.chunk_index = 0
        self.chunk_total = 1
        self.audio_duration = 3600.0
        self.bytes_downloaded = 0
        self.bytes_total = 0
        self.phase_started_at = datetime.now(timezone.utc)
        self.__dict__.update(kw)

    def elapsed(self, seconds):
        self.phase_started_at = datetime.now(timezone.utc) - timedelta(seconds=seconds)
        return self


def progress_at(task, seconds):
    return A.compute_live_progress(task.elapsed(seconds))


# --------------------------------------------------------------------------
# Progress
# --------------------------------------------------------------------------

def test_single_chunk_progress_advances_over_time():
    """The original bug: one chunk meant `10 + int((0/1)*60)` for the whole run."""
    early, _ = progress_at(FakeTask(chunk_total=1), 30)
    later, _ = progress_at(FakeTask(chunk_total=1), 240)
    assert later > early


def test_progress_never_stalls_when_slower_than_estimate():
    """A hard cap would park the bar at 97% -- the same freeze at a nicer number."""
    at_estimate, _ = progress_at(FakeTask(chunk_total=1), 300)
    overrun, _ = progress_at(FakeTask(chunk_total=1), 900)
    way_over, _ = progress_at(FakeTask(chunk_total=1), 3000)
    assert overrun > at_estimate
    assert way_over > overrun


def test_progress_never_reaches_100_while_running():
    percent, _ = progress_at(FakeTask(chunk_total=1), 10 ** 6)
    assert percent < 100


def test_progress_never_goes_backwards():
    """The stored checkpoint is a floor, so a poll can't rewind the bar."""
    task = FakeTask(chunk_total=3, chunk_index=2, progress=83)
    percent, _ = progress_at(task, 0)
    assert percent >= 83


def test_terminal_states():
    assert A.compute_live_progress(FakeTask(status='completed'))[0] == 100
    assert A.compute_live_progress(FakeTask(status='error', progress=42))[0] == 42


@pytest.mark.parametrize('done,total,expected', [(0, 4, 30), (2, 4, 65), (4, 4, 100)])
def test_checkpoints_span_the_transcribe_band(done, total, expected):
    assert A._transcribe_checkpoint(done, total) == expected


def test_checkpoint_handles_zero_chunks():
    assert A._transcribe_checkpoint(0, 0) == A.PHASE_SPANS['transcribing'][0]


def test_eta_shrinks_as_work_progresses():
    _, early = progress_at(FakeTask(chunk_total=2), 10)
    _, later = progress_at(FakeTask(chunk_total=2, chunk_index=1, progress=65), 10)
    assert later < early


def test_stale_task_is_failed_when_its_status_is_polled():
    """A task orphaned by a restart has a fresh heartbeat, so the boot sweep skips
    it. The poll the waiting page makes is what must notice."""
    from models import TranscriptionTask

    with A.app.app_context():
        from models import db
        stale_id = 'stale-poll-test'
        old = db.session.get(TranscriptionTask, stale_id)
        if old:
            db.session.delete(old)
            db.session.commit()
        now = datetime.now(timezone.utc)
        db.session.add(TranscriptionTask(
            id=stale_id, user_id=1, episode_title='x', status='transcribing',
            phase='transcribing', progress=53, started_at=now - timedelta(hours=2),
            heartbeat_at=now - timedelta(hours=2),
        ))
        db.session.commit()
        task = db.session.get(TranscriptionTask, stale_id)
        assert A._fail_if_stale(task) is True
        assert task.status == 'error'
        assert 'restarted' in task.error_message


def test_stale_window_scales_with_chunk_length():
    """A 24 MB chunk of low-bitrate audio can legitimately run past 15 minutes;
    a flat window would kill a job that is still working."""
    class T:
        chunk_total = 1
        audio_duration = 50 * 60      # one ~50-minute chunk (24 MB @ 64 kbps)
        bytes_downloaded = None

    class NoInfo:
        chunk_total = None
        audio_duration = None
        bytes_downloaded = None

    slow_chunk_runtime = (50 * 60) / A.WHISPER_REALTIME_FACTOR
    assert A._stale_after_seconds(T()) > slow_chunk_runtime * 4
    assert A._stale_after_seconds(T()) > A.STALE_TASK_SECONDS
    # With nothing to go on, fall back to the flat floor
    assert A._stale_after_seconds(NoInfo()) == A.STALE_TASK_SECONDS


def test_stale_window_covers_the_splitting_phase():
    """chunk_total is not set yet while ffmpeg splits, so the window has to come
    from the episode length or a large file gets declared dead mid-split."""
    class Splitting:
        chunk_total = None
        audio_duration = 3 * 60 * 60      # a 3-hour episode
        bytes_downloaded = None

    assert A._stale_after_seconds(Splitting()) > A.STALE_TASK_SECONDS


def test_whisper_client_gives_up_before_the_task_is_presumed_dead(monkeypatch):
    """A hanging Whisper call must fail before _fail_if_stale() kills the task.

    Asserts the values on the constructed client, not just the constants -- an
    earlier version of this test passed even with the timeout removed entirely.
    """
    monkeypatch.setattr(A, 'GLOBAL_OPENAI_KEY', 'sk-test-not-a-real-key')
    client = A.get_openai_client()

    assert client is not None
    assert client.timeout == A.WHISPER_TIMEOUT_SECONDS
    assert client.max_retries == A.WHISPER_MAX_RETRIES

    # max_retries=N means N+1 total attempts
    worst_case = client.timeout * (client.max_retries + 1)
    assert worst_case < A.STALE_TASK_SECONDS


def test_stale_window_falls_back_to_downloaded_size_without_a_feed_duration():
    """Not every feed publishes itunes:duration, and audio_duration is only
    measured after splitting -- the phase the window is meant to cover."""
    class NoDuration:
        chunk_total = None
        audio_duration = None
        bytes_downloaded = 300 * 1024 * 1024   # a large episode already on disk

    class Tiny:
        chunk_total = None
        audio_duration = None
        bytes_downloaded = 2 * 1024 * 1024

    assert A._stale_after_seconds(NoDuration()) > A.STALE_TASK_SECONDS
    assert A._stale_after_seconds(Tiny()) == A.STALE_TASK_SECONDS


def test_live_task_is_not_failed():
    from models import TranscriptionTask, db

    with A.app.app_context():
        live_id = 'live-poll-test'
        old = db.session.get(TranscriptionTask, live_id)
        if old:
            db.session.delete(old)
            db.session.commit()
        now = datetime.now(timezone.utc)
        db.session.add(TranscriptionTask(
            id=live_id, user_id=1, episode_title='x', status='transcribing',
            phase='transcribing', progress=53, started_at=now - timedelta(hours=2),
            heartbeat_at=now,
        ))
        db.session.commit()
        task = db.session.get(TranscriptionTask, live_id)
        assert A._fail_if_stale(task) is False
        assert task.status == 'transcribing'


# --------------------------------------------------------------------------
# Download reporting
# --------------------------------------------------------------------------

def test_download_progress_tracks_bytes():
    task = FakeTask(phase='downloading', status='downloading', progress=0,
                    bytes_downloaded=10_000_000, bytes_total=20_000_000)
    percent, eta = progress_at(task, 10)
    lo, hi = A.PHASE_SPANS['downloading']
    assert percent == pytest.approx(lo + (hi - lo) / 2, abs=1)
    assert eta is not None


def test_download_without_content_length_reports_no_percentage():
    """No content-length means no honest number; the UI animates instead."""
    task = FakeTask(phase='downloading', status='downloading', progress=0,
                    bytes_downloaded=12_000_000, bytes_total=0)
    percent, eta = progress_at(task, 20)
    assert percent == 0 and eta is None


def test_frontend_animates_when_download_is_unmeasurable():
    """Guards the template contract the above relies on."""
    tpl = open('templates/transcription.html').read()
    assert "d.phase === 'downloading' && !d.bytes_total)" in tpl
    assert "? '100%' : (d.progress || 0) + '%'" in tpl


# --------------------------------------------------------------------------
# URL safety
# --------------------------------------------------------------------------

@pytest.mark.parametrize('url', [
    'https://dts.podtrac.com/redirect.mp3/example.mp3',
    'http://example.com/episode.mp3',
])
def test_public_audio_urls_are_fetchable(url):
    assert A._is_fetchable_url(url) is True


@pytest.mark.parametrize('url', [
    'http://127.0.0.1:5000/status/x',
    'http://localhost:5000/',
    'http://169.254.169.254/latest/meta-data/',
    'http://192.168.1.1/admin',
    'http://10.0.0.5/internal',
    'file:///etc/passwd',
    'gopher://example.com/',
    'not a url',
    '',
])
def test_private_and_non_http_urls_are_rejected(url):
    assert A._is_fetchable_url(url) is False


class _FakeResponse:
    def __init__(self, status, location=None):
        self.status_code = status
        self.headers = {'location': location} if location else {}
        self.is_redirect = status in (301, 302, 303, 307, 308)
        self.is_permanent_redirect = status in (301, 308)

    def close(self):
        pass

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size=8192):
        return iter([b'x' * 100])


def _download_with(responder, tmp_path):
    """Run download_audio against a fake transport, returning the URLs it hit."""
    from unittest import mock

    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        return responder(url)

    with mock.patch.object(A.requests, 'get', side_effect=fake_get), \
            mock.patch.object(A, '_update_task'):
        try:
            A.download_audio(
                'http://example.com/ep.mp3', str(tmp_path / 'a.mp3'), 'task-id'
            )
            error = None
        except Exception as exc:  # noqa: BLE001 - the message is the assertion
            error = str(exc)
    return calls, error


def test_redirect_into_private_network_is_refused(tmp_path):
    """A public host must not be able to 302 into the private network."""
    def responder(url):
        if 'example.com' in url:
            return _FakeResponse(302, 'http://169.254.169.254/latest/meta-data/')
        return _FakeResponse(200)

    calls, error = _download_with(responder, tmp_path)
    assert error is not None
    assert not any('169.254' in c for c in calls), 'metadata endpoint was contacted'


def test_public_redirect_chain_still_works(tmp_path):
    """Podtrac-style redirects are how most podcast audio is actually served."""
    def responder(url):
        if 'example.com' in url:
            return _FakeResponse(302, 'https://www.iana.org/real.mp3')
        return _FakeResponse(200)

    calls, error = _download_with(responder, tmp_path)
    assert error is None
    assert len(calls) == 2


def test_http_error_without_a_response_is_reported_cleanly(tmp_path):
    """A transport-level HTTPError has no .response; dereferencing it blindly
    surfaced a raw AttributeError as the user's error message."""
    import requests
    from unittest import mock

    def boom(url, **kwargs):
        raise requests.exceptions.HTTPError('connection reset')

    with mock.patch.object(A.requests, 'get', side_effect=boom), \
            mock.patch.object(A, '_update_task'):
        with pytest.raises(Exception) as exc:
            A.download_audio(
                'http://example.com/ep.mp3', str(tmp_path / 'a.mp3'), 'task-id'
            )
    assert not isinstance(exc.value, AttributeError)
    assert 'HTTP error' in str(exc.value)


def test_redirect_loop_is_bounded(tmp_path):
    calls, error = _download_with(
        lambda url: _FakeResponse(302, 'https://www.iana.org/next.mp3'), tmp_path
    )
    assert len(calls) == A.MAX_REDIRECTS
    assert 'Too many redirects' in error


# --------------------------------------------------------------------------
# Language selection
# --------------------------------------------------------------------------

def test_norwegian_is_the_default():
    assert 'no' in A.VALID_LANGUAGE_CODES
    assert A.SUPPORTED_LANGUAGES[0][0] == '', 'auto-detect should lead the list'


def test_auto_detect_is_a_valid_choice():
    assert '' in A.VALID_LANGUAGE_CODES


# --------------------------------------------------------------------------
# Misc
# --------------------------------------------------------------------------

def test_best_artwork_prefers_largest_and_handles_episode_keys():
    # Episode results carry 60/160/600 but never 100
    assert A._best_artwork({'artworkUrl160': 'b', 'artworkUrl60': 'c'}) == 'b'
    assert A._best_artwork({'artworkUrl600': 'a', 'artworkUrl160': 'b'}) == 'a'
    assert A._best_artwork({}) == ''


@pytest.mark.parametrize('elapsed,expected_lo,expected_hi', [
    (0, 0.0, 0.01), (150, 0.44, 0.46), (300, 0.89, 0.91),
])
def test_asymptotic_fraction_is_linear_up_to_the_estimate(elapsed, expected_lo, expected_hi):
    assert expected_lo <= A._asymptotic_fraction(elapsed, 300) <= expected_hi


def test_asymptotic_fraction_keeps_climbing_past_the_estimate_without_reaching_one():
    a = A._asymptotic_fraction(600, 300)
    b = A._asymptotic_fraction(3000, 300)
    assert 0.9 < a < b < 1.0


def test_asymptotic_fraction_handles_zero_estimate():
    assert A._asymptotic_fraction(10, 0) == 0.9


def test_parse_duration_formats():
    assert A._parse_duration('90') == 90
    assert A._parse_duration('1:30') == 90
    assert A._parse_duration('1:01:30') == 3690
    assert A._parse_duration('') is None
    assert A._parse_duration('garbage') is None


def test_every_model_column_added_after_release_has_a_migration():
    """A column added to the model but not the map breaks existing databases."""
    from models import TranscriptionTask, TASK_COLUMN_MIGRATIONS
    original = {
        'id', 'user_id', 'episode_title', 'rss_url', 'status', 'progress',
        'download_progress', 'error_message', 'transcript_text', 'segments_json',
        'language', 'transcription_time', 'started_at', 'completed_at',
    }
    model_columns = {c.name for c in TranscriptionTask.__table__.columns}
    added = model_columns - original
    assert added == set(TASK_COLUMN_MIGRATIONS), (
        f'missing migrations for {added - set(TASK_COLUMN_MIGRATIONS)}'
    )


# --------------------------------------------------------------------------
# API key validation (the top production failure)
# --------------------------------------------------------------------------

@pytest.mark.parametrize('key', [
    'Clashofc*ans1',        # a real password pasted into the key field
    'hhhhhh123456',         # keyboard mash
    'zemfyz-nonsense',      # wrong prefix
    'sk-short',             # right prefix, too short
    '', None,
])
def test_obvious_non_keys_are_rejected_without_calling_openai(key):
    assert A.looks_like_openai_key(key) is False


def test_plausible_key_shape_is_accepted():
    assert A.looks_like_openai_key('sk-' + 'a' * 40) is True


def test_bad_shape_never_reaches_the_network(monkeypatch):
    def explode(*a, **kw):
        raise AssertionError('OpenAI must not be called for an obvious non-key')
    monkeypatch.setattr(A, 'OpenAI', explode)
    ok, msg = A.verify_openai_key('Clashofc*ans1')
    assert ok is False
    assert 'sk-' in msg


class _Status(Exception):
    def __init__(self, status_code):
        super().__init__('raw provider text with a secret in it')
        self.status_code = status_code


@pytest.mark.parametrize('status,expect', [
    (401, 'rejected'),
    (429, 'out of credit'),
    (403, 'not allowed'),
    (500, 'server error'),
])
def test_openai_errors_become_human_messages(status, expect):
    msg = A.describe_openai_error(_Status(status))
    assert expect in msg


def test_provider_text_is_never_echoed_back():
    """OpenAI echoes the submitted key in 401s, and users paste passwords there."""
    secret = 'Clashofc*ans1'

    class Leaky(Exception):
        status_code = 401
        def __str__(self):
            return f"Incorrect API key provided: {secret}"

    assert secret not in A.describe_openai_error(Leaky())


# --------------------------------------------------------------------------
# Registration abuse limits
# --------------------------------------------------------------------------

def test_register_is_rate_limited_per_ip():
    """Simulates the route: check, then record only when an account is created."""
    A._register_attempts.clear()
    ip = '203.0.113.7'
    created = sum(1 for _ in range(10) if A.register_reserve_slot(ip) is not None)
    assert created == A.REGISTER_MAX_PER_IP


def test_rate_limit_is_per_ip_not_global():
    A._register_attempts.clear()
    for _ in range(A.REGISTER_MAX_PER_IP):
        assert A.register_reserve_slot('198.51.100.1') is not None
    assert A.register_reserve_slot('198.51.100.1') is None
    assert A.register_reserve_slot('198.51.100.2') is not None


def test_the_domain_that_created_seven_bot_accounts_is_blocked():
    assert A.is_disposable_email('mizhtxgh@immenseignite.info') is True
    assert A.is_disposable_email('morten.slemdal@gmail.com') is False


# --------------------------------------------------------------------------
# Duration without librosa
# --------------------------------------------------------------------------

def test_duration_falls_back_when_ffprobe_is_unavailable(tmp_path, monkeypatch):
    f = tmp_path / 'a.mp3'
    f.write_bytes(b'\x00' * (2 * 1024 * 1024))

    def boom(*a, **kw):
        raise OSError('ffprobe not installed')

    monkeypatch.setattr(A.subprocess, 'run', boom)
    assert A.get_audio_duration(str(f)) == pytest.approx(120, abs=1)


def test_librosa_is_not_imported():
    """It dragged in 398 MB of a 547 MB venv for one call.

    Checks the import graph and requirements.txt, not the word: the docstring
    in get_audio_duration mentions librosa deliberately, to explain why it is
    gone. Reclaiming the disk needs a venv rebuild on the host -- see the
    deploy notes; this test only guards the code and the manifest.
    """
    import ast as _ast

    tree = _ast.parse(open('app.py').read())
    imported = set()
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Import):
            imported.update(a.name.split('.')[0] for a in node.names)
        elif isinstance(node, _ast.ImportFrom) and node.module:
            imported.add(node.module.split('.')[0])
    assert 'librosa' not in imported
    assert 'librosa' not in open('requirements.txt').read()


def test_duration_uses_ffprobe(monkeypatch, tmp_path):
    f = tmp_path / 'a.mp3'
    f.write_bytes(b'\x00' * 1024)
    called = {}

    class R:
        stdout = '123.45\n'

    def fake_run(cmd, **kw):
        called['cmd'] = cmd
        return R()

    monkeypatch.setattr(A.subprocess, 'run', fake_run)
    assert A.get_audio_duration(str(f)) == pytest.approx(123.45)
    assert called['cmd'][0] == 'ffprobe' 


# --------------------------------------------------------------------------
# Regressions found in review of this change
# --------------------------------------------------------------------------

def test_client_ip_ignores_spoofable_forwarded_for():
    """Plesk nginx appends the real peer to X-Forwarded-For, so element 0 is
    whatever the client sent. Trusting it let one host create unlimited
    accounts by rotating the header."""
    with A.app.test_request_context(
        headers={'X-Forwarded-For': '1.2.3.4, 203.0.113.9', 'X-Real-IP': '203.0.113.9'},
        environ_base={'REMOTE_ADDR': '127.0.0.1'},
    ):
        assert A._client_ip() == '203.0.113.9'


def test_proxy_headers_are_only_trusted_from_the_proxy():
    """Swapping X-Forwarded-For for X-Real-IP fixes nothing if the header is
    trusted from any peer -- it is equally attacker-controlled."""
    # Direct connection: the client's own X-Real-IP must be ignored
    with A.app.test_request_context(
        headers={'X-Real-IP': '1.2.3.4', 'X-Forwarded-For': '5.6.7.8'},
        environ_base={'REMOTE_ADDR': '203.0.113.9'},
    ):
        assert A._client_ip() == '203.0.113.9'

    # Via the local proxy, with no header set: fall back to the peer
    with A.app.test_request_context(environ_base={'REMOTE_ADDR': '127.0.0.1'}):
        assert A._client_ip() == '127.0.0.1'


def test_rate_limit_counts_created_accounts_not_failed_attempts():
    """Three password typos must not burn the hourly quota."""
    A._register_attempts.clear()
    ip = '203.0.113.50'
    # Five failed attempts: each reserves, then releases because no account was made
    for _ in range(5):
        token = A.register_reserve_slot(ip)
        assert token is not None
        A.register_release_slot(ip, token)
    # The full quota is still available
    for _ in range(A.REGISTER_MAX_PER_IP):
        assert A.register_reserve_slot(ip) is not None
    assert A.register_reserve_slot(ip) is None


def test_settings_never_persists_a_rejected_key(monkeypatch):
    """The central claim of this change, previously verified only by reading."""
    from models import db, User

    with A.app.app_context():
        db.session.query(User).filter_by(email='reject@test.com').delete()
        db.session.commit()
        u = User(email='reject@test.com')
        u.set_password('password123')
        db.session.add(u)
        db.session.commit()
        uid = u.id

    A.app.config['TESTING'] = True
    client = A.app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(uid)
        sess['_fresh'] = True

    for bad in ('Clashofc*ans1', 'hhhhhh123456'):
        client.post('/settings', data={'openai_api_key': bad}, follow_redirects=True)
        with A.app.app_context():
            assert db.session.get(User, uid).openai_api_key is None

    with A.app.app_context():
        db.session.query(User).filter_by(id=uid).delete()
        db.session.commit()


def test_unreachable_openai_does_not_reject_the_key(monkeypatch):
    """A 500 means we could not check the key, not that it is wrong -- an
    OpenAI outage must not make the key unsaveable."""
    class Down(Exception):
        status_code = 503

    def boom(*a, **kw):
        raise Down()

    monkeypatch.setattr(A, 'OpenAI', boom)
    ok, msg = A.verify_openai_key('sk-' + 'a' * 40)
    assert ok is True
    assert 'could not be reached' in msg.lower()


def test_real_openai_exception_is_recognised_and_translated():
    """_is_openai_error must catch the SDK's own classes, not just anything
    carrying a status_code."""
    import openai

    exc = openai.AuthenticationError.__new__(openai.AuthenticationError)
    exc.status_code = 401
    assert A._is_openai_error(exc) is True
    assert 'rejected' in A.describe_openai_error(exc)

    assert A._is_openai_error(ValueError('unrelated')) is False


def test_verify_context_does_not_talk_about_episodes():
    """describe_openai_error is now shared with the settings page."""
    msg = A.describe_openai_error(A.APITimeoutError(request=None), context='verify')
    assert 'episode' not in msg.lower()


def test_403_message_has_no_stray_apostrophe():
    class Forbidden(Exception):
        status_code = 403
    assert "key 's" not in A.describe_openai_error(Forbidden())


def test_parallel_burst_from_one_ip_cannot_exceed_the_limit():
    """The regression that split check-from-record introduced: 20 concurrent
    signups all passed the check before any of them recorded."""
    import threading

    A._register_attempts.clear()
    ip = '203.0.113.77'
    granted = []
    barrier = threading.Barrier(20)

    def attempt():
        barrier.wait()          # maximise overlap
        if A.register_reserve_slot(ip) is not None:
            granted.append(1)

    threads = [threading.Thread(target=attempt) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(granted) == A.REGISTER_MAX_PER_IP, (
        f'{len(granted)} of 20 concurrent signups granted, '
        f'limit is {A.REGISTER_MAX_PER_IP}'
    )


def test_non_openai_exception_with_status_code_is_not_treated_as_openai():
    """Guards the narrowing: hasattr(exc, 'status_code') would have given a
    future non-OpenAI error OpenAI-flavoured text."""
    class ThirdPartyError(Exception):
        status_code = 401

    assert A._is_openai_error(ThirdPartyError()) is False


def test_sk_shaped_key_rejected_by_openai_is_not_persisted(monkeypatch):
    """The higher-risk path: shape check passes, OpenAI returns 401."""
    from models import db, User

    class Unauthorized(Exception):
        status_code = 401

    def reject(*a, **kw):
        raise Unauthorized()

    monkeypatch.setattr(A, 'OpenAI', reject)

    with A.app.app_context():
        db.session.query(User).filter_by(email='sk401@test.com').delete()
        db.session.commit()
        u = User(email='sk401@test.com')
        u.set_password('password123')
        db.session.add(u)
        db.session.commit()
        uid = u.id

    A.app.config['TESTING'] = True
    client = A.app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(uid)
        sess['_fresh'] = True

    client.post('/settings', data={'openai_api_key': 'sk-' + 'b' * 40},
                follow_redirects=True)
    with A.app.app_context():
        assert db.session.get(User, uid).openai_api_key is None
        db.session.query(User).filter_by(id=uid).delete()
        db.session.commit()


def test_out_of_credit_key_is_still_saved(monkeypatch):
    """429 means the key authenticated but the account has no credit. Refusing
    the save would leave that user unable to store a working key at all."""
    class RateLimited(Exception):
        status_code = 429

    def limited(*a, **kw):
        raise RateLimited()

    monkeypatch.setattr(A, 'OpenAI', limited)
    ok, msg = A.verify_openai_key('sk-' + 'c' * 40)
    assert ok is True
    assert 'credit' in msg.lower()


# --------------------------------------------------------------------------
# Route-level enforcement
#
# The limiter functions were well covered, but nothing drove POST /register --
# so the whole abuse control could be unwired from the route with a green suite.
# --------------------------------------------------------------------------

def _fresh_client():
    """A client with a cleared rate counter."""
    A.app.config['TESTING'] = True
    A._register_attempts.clear()
    return A.app.test_client()


def _new_session():
    """A new session WITHOUT clearing the counter.

    register() redirects authenticated users, and a successful signup logs you
    in, so the next attempt needs a fresh session to reach the limiter at all.
    """
    A.app.config['TESTING'] = True
    return A.app.test_client()


def _signup(client, email, password='abcdefgh1', confirm=None):
    return client.post('/register', data={
        'email': email,
        'password': password,
        'password2': confirm if confirm is not None else password,
    }, follow_redirects=True)


def _purge(emails):
    from models import db, User
    with A.app.app_context():
        for e in emails:
            db.session.query(User).filter_by(email=e).delete()
        db.session.commit()


def test_register_route_enforces_the_limit():
    """Kills the mutant where the reserve call is removed from register()."""
    emails = [f'route{i}@example.com' for i in range(6)]
    _purge(emails)
    client = _fresh_client()
    created = 0
    for e in emails:
        body = _signup(client, e).data.decode()
        if 'Add your OpenAI API key' in body:
            created += 1
            client = _new_session()
    _purge(emails)
    assert created == A.REGISTER_MAX_PER_IP, f'{created} accounts created, limit is 3'


def test_register_route_releases_the_slot_on_validation_failure():
    """Kills the mutants that delete the finally-release or the created flag."""
    emails = [f'rel{i}@example.com' for i in range(4)]
    _purge(emails)
    client = _fresh_client()

    # Five mismatched-password attempts must cost nothing
    for _ in range(5):
        body = _signup(client, 'rel0@example.com', confirm='WRONG').data.decode()
        assert 'Passwords do not match' in body

    created = 0
    for e in emails:
        body = _signup(client, e).data.decode()
        if 'Add your OpenAI API key' in body:
            created += 1
            client = _new_session()
    _purge(emails)
    assert created == A.REGISTER_MAX_PER_IP


def test_register_route_blocks_the_disposable_domain():
    client = _fresh_client()
    body = _signup(client, 'x@immenseignite.info').data.decode()
    assert 'real email address' in body
    from models import db, User
    with A.app.app_context():
        assert User.query.filter_by(email='x@immenseignite.info').first() is None


# --------------------------------------------------------------------------
# Plan items that previously shipped with no binding test
# --------------------------------------------------------------------------

def test_unverified_key_flashes_as_a_warning_not_success(monkeypatch):
    """Green "success" for a key we could not check is misleading."""
    from models import db, User

    class Down(Exception):
        status_code = 503

    monkeypatch.setattr(A, 'OpenAI', lambda *a, **kw: (_ for _ in ()).throw(Down()))

    with A.app.app_context():
        db.session.query(User).filter_by(email='warn@test.com').delete()
        db.session.commit()
        u = User(email='warn@test.com')
        u.set_password('password123')
        db.session.add(u)
        db.session.commit()
        uid = u.id

    A.app.config['TESTING'] = True
    client = A.app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(uid)
        sess['_fresh'] = True

    body = client.post('/settings', data={'openai_api_key': 'sk-' + 'd' * 40},
                       follow_redirects=True).data.decode()
    # Match the rendered flash div, not the stylesheet -- base.html inlines
    # `.alert-warning { ... }`, so a bare substring check passes on every page.
    import re as _re
    flashes = _re.findall(r'<div class="alert alert-(\w+)">', body)
    assert flashes, 'no flash rendered'
    assert 'warning' in flashes, f'flash categories were {flashes}, expected a warning'
    assert 'success' not in flashes
    with A.app.app_context():
        assert db.session.get(User, uid).openai_api_key is not None
        db.session.query(User).filter_by(id=uid).delete()
        db.session.commit()


def test_out_of_credit_message_says_the_key_was_saved(monkeypatch):
    class RateLimited(Exception):
        status_code = 429

    monkeypatch.setattr(A, 'OpenAI',
                        lambda *a, **kw: (_ for _ in ()).throw(RateLimited()))
    ok, msg = A.verify_openai_key('sk-' + 'e' * 40)
    assert ok is True
    assert 'saved' in msg.lower()


def test_pragma_failure_is_logged_not_swallowed(caplog):
    """Losing WAL must be visible: backup-db.sh's use of .backup assumes it.

    sqlite3.Connection.cursor is a C slot and cannot be monkeypatched, so this
    subclasses it -- which also keeps the isinstance() guard in _sqlite_pragmas
    satisfied, exactly as a real connection would.
    """
    import logging

    class FailingCursor:
        def execute(self, *a):
            raise A.sqlite3.Error('unable to open database file')

        def close(self):
            pass

    class FailingConnection(A.sqlite3.Connection):
        def cursor(self, *a, **kw):
            return FailingCursor()

    conn = A.sqlite3.connect(':memory:', factory=FailingConnection)
    try:
        with caplog.at_level(logging.WARNING, logger=A.app.logger.name):
            A._sqlite_pragmas(conn, None)   # must not raise
        assert any('pragma' in r.message.lower() for r in caplog.records), (
            'a failed PRAGMA was swallowed silently'
        )
    finally:
        conn.close()


def test_ipv6_loopback_is_also_a_trusted_proxy_peer():
    """nginx dialling `localhost` may resolve to ::1 before 127.0.0.1."""
    with A.app.test_request_context(
        headers={'X-Real-IP': '203.0.113.9'},
        environ_base={'REMOTE_ADDR': '::1'},
    ):
        assert A._client_ip() == '203.0.113.9'


# --------------------------------------------------------------------------
# Pin the limiter's semantics, not just its outcomes
# --------------------------------------------------------------------------

def test_release_returns_your_own_reservation_not_the_newest():
    """held.pop() kept the count right but discarded the wrong timestamp, so
    the window expired early and freed several slots at once."""
    A._register_attempts.clear()
    ip = '203.0.113.90'
    first = A.register_reserve_slot(ip)
    second = A.register_reserve_slot(ip)
    assert first is not None and second is not None

    A.register_release_slot(ip, first)
    assert A._register_attempts[ip] == [second], (
        'release gave back the wrong reservation'
    )


def test_releasing_a_pruned_token_does_not_steal_a_live_reservation():
    A._register_attempts.clear()
    ip = '203.0.113.91'
    stale = (time.time() - A.REGISTER_WINDOW_SECONDS - 10, 'gone')
    live = A.register_reserve_slot(ip)
    A.register_release_slot(ip, stale)          # must be a no-op
    assert A._register_attempts[ip] == [live]


@pytest.mark.parametrize('override,expect', [
    ({'password2': 'MISMATCH'}, 'Passwords do not match'),
    ({'password': 'short1', 'password2': 'short1'}, 'at least 8 characters'),
    ({'email': 'not-an-email'}, 'valid email address'),
    ({'email': 'x@immenseignite.info'}, 'real email address'),
])
def test_every_validation_failure_gives_the_slot_back(override, expect):
    """Only the password-mismatch path was covered; the others would have
    permanently burned a signup slot each."""
    A._register_attempts.clear()
    client = _fresh_client()
    data = {'email': 'slot@example.com', 'password': 'abcdefgh1',
            'password2': 'abcdefgh1'}
    data.update(override)

    for _ in range(A.REGISTER_MAX_PER_IP + 2):
        body = client.post('/register', data=data, follow_redirects=True).data.decode()
        assert expect in body

    assert A._register_attempts.get('127.0.0.1', []) == [], (
        'a failed validation kept its reservation'
    )


def test_duplicate_email_gives_the_slot_back():
    A._register_attempts.clear()
    _purge(['dupe@example.com'])
    client = _fresh_client()
    assert 'Add your OpenAI API key' in _signup(client, 'dupe@example.com').data.decode()

    client = _new_session()
    for _ in range(4):
        body = _signup(client, 'dupe@example.com').data.decode()
        assert 'already exists' in body
    _purge(['dupe@example.com'])
    # one slot for the account that was created, none for the duplicates
    assert len(A._register_attempts.get('127.0.0.1', [])) == 1


def test_register_route_is_atomic_under_a_parallel_burst():
    """Drives the ROUTE, not the helper: the non-atomic check-then-record
    design passes every serial test but let 20 concurrent signups through."""
    import threading

    emails = [f'burst{i}@example.com' for i in range(12)]
    _purge(emails)
    A._register_attempts.clear()
    A.app.config['TESTING'] = True

    # Timeouts everywhere: a thread dying before the barrier would otherwise
    # hang the whole run, and pytest-timeout is not installed.
    barrier = threading.Barrier(len(emails), timeout=30)
    results = []
    errors = []

    def attempt(email):
        try:
            client = A.app.test_client()
            barrier.wait()
            body = client.post('/register', data={
                'email': email, 'password': 'abcdefgh1', 'password2': 'abcdefgh1',
            }, follow_redirects=True).data.decode()
            results.append('Add your OpenAI API key' in body)
        except Exception as exc:            # noqa: BLE001 - reported below
            errors.append(f'{email}: {type(exc).__name__}: {exc}')

    threads = [threading.Thread(target=attempt, args=(e,)) for e in emails]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not any(t.is_alive() for t in threads), 'a request thread did not finish'

    from models import User
    with A.app.app_context():
        created = User.query.filter(User.email.like('burst%@example.com')).count()
    _purge(emails)

    # Without these, a burst where most threads CRASHED is indistinguishable
    # from one where most were correctly rate-limited.
    assert not errors, f'threads raised: {errors}'
    assert len(results) == len(emails), (
        f'only {len(results)} of {len(emails)} requests completed'
    )
    assert sum(results) == A.REGISTER_MAX_PER_IP, (
        f'{sum(results)} signups reported success, limit is {A.REGISTER_MAX_PER_IP}'
    )
    assert created == A.REGISTER_MAX_PER_IP, (
        f'{created} of {len(emails)} concurrent signups committed, limit is '
        f'{A.REGISTER_MAX_PER_IP}'
    )


def test_limiter_constants_are_what_production_expects():
    """Every other assert compares against the constant, so changing it here
    would silently move the limit or disable the window."""
    assert A.REGISTER_MAX_PER_IP == 3
    assert A.REGISTER_WINDOW_SECONDS == 3600


def test_window_actually_expires_reservations():
    """Deleting the window filter would lock a user out permanently."""
    A._register_attempts.clear()
    ip = '203.0.113.92'
    old = time.time() - A.REGISTER_WINDOW_SECONDS - 1
    A._register_attempts[ip] = [(old, f'n{i}') for i in range(A.REGISTER_MAX_PER_IP)]
    assert A.register_reserve_slot(ip) is not None, 'expired reservations never freed'


# --------------------------------------------------------------------------
# Trial metering
#
# These guard real money. The global OPENAI_API_KEY used to be handed to every
# keyless user by User.get_openai_key(), uncounted and uncapped -- setting that
# one env var would have been an open wallet.
# --------------------------------------------------------------------------

def _make_user(email, key=None, limit=None, used=0):
    from models import db, User
    with A.app.app_context():
        db.session.query(User).filter_by(email=email).delete()
        db.session.commit()
        u = User(email=email, openai_api_key=key,
                 trial_seconds_limit=limit, trial_seconds_used=used)
        u.set_password('password123')
        db.session.add(u)
        db.session.commit()
        return u.id


def _used(user_id):
    from models import db, User
    with A.app.app_context():
        return db.session.get(User, user_id).trial_seconds_used


@pytest.fixture
def trial_on(monkeypatch):
    """Turn the trial on with a small, easy-to-reason-about allowance."""
    monkeypatch.setattr(A, 'GLOBAL_OPENAI_KEY', 'sk-global-not-a-real-key')
    monkeypatch.setattr(A, 'TRIAL_ENABLED', True)
    monkeypatch.setattr(A, 'TRIAL_DEFAULT_SECONDS', 600)        # 10 min
    monkeypatch.setattr(A, 'TRIAL_GLOBAL_SECONDS', 10 ** 7)     # effectively off
    monkeypatch.setattr(A, 'TRIAL_MAX_EPISODE_SECONDS', 10800)
    return True


def test_no_global_key_means_no_trial(monkeypatch):
    """Safe by default: without a key of ours there is nothing to give away."""
    monkeypatch.setattr(A, 'GLOBAL_OPENAI_KEY', None)
    assert A.trial_available() is False
    uid = _make_user('notrial@test.com')
    from models import db, User
    with A.app.app_context():
        assert A.resolve_openai_key(db.session.get(User, uid)) == (None, None)


def test_kill_switch_stops_the_trial(trial_on, monkeypatch):
    monkeypatch.setattr(A, 'TRIAL_ENABLED', False)
    assert A.trial_available() is False


def test_own_key_beats_the_trial_key(trial_on):
    """A user's own key costs us nothing, so it must always win."""
    from models import db, User
    uid = _make_user('ownkey@test.com', key='sk-' + 'u' * 40)
    with A.app.app_context():
        key, source = A.resolve_openai_key(db.session.get(User, uid))
    assert source == 'user'
    assert key.startswith('sk-uuu')


def test_keyless_user_gets_the_trial_key(trial_on):
    from models import db, User
    uid = _make_user('keyless@test.com')
    with A.app.app_context():
        key, source = A.resolve_openai_key(db.session.get(User, uid))
    assert (key, source) == ('sk-global-not-a-real-key', 'trial')


def test_reserve_stops_at_the_per_user_limit(trial_on):
    uid = _make_user('cap@test.com', limit=600)
    with A.app.app_context():
        assert A.trial_reserve(uid, 400) is True
        assert A.trial_reserve(uid, 300) is False   # would total 700 > 600
        assert A.trial_reserve(uid, 200) is True    # exactly 600 fits
    assert _used(uid) == 600


def test_reserve_stops_at_the_global_ceiling(trial_on, monkeypatch):
    """The per-user cap bounds nothing on its own -- signups are free, so N
    accounts cost N x the grant. This ceiling is what caps the actual bill."""
    from models import db, User
    with A.app.app_context():
        db.session.execute(A.text('UPDATE users SET trial_seconds_used = 0'))
        db.session.commit()
    monkeypatch.setattr(A, 'TRIAL_GLOBAL_SECONDS', 900)
    a = _make_user('g1@test.com', limit=6000)
    b = _make_user('g2@test.com', limit=6000)
    with A.app.app_context():
        assert A.trial_reserve(a, 600) is True
        # b is nowhere near its own limit, but the service as a whole is.
        assert A.trial_reserve(b, 600) is False
        assert A.trial_reserve(b, 300) is True


def test_parallel_reservations_cannot_oversubscribe(trial_on):
    """The rate limiter shipped with exactly this hole: a check and a record
    that were not one atomic step let 20 concurrent callers all pass."""
    import threading as _t
    uid = _make_user('race@test.com', limit=1000)
    granted = []
    lock = _t.Lock()

    def attempt():
        with A.app.app_context():
            ok = A.trial_reserve(uid, 100)
        if ok:
            with lock:
                granted.append(1)

    threads = [_t.Thread(target=attempt) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(granted) == 10, f'{len(granted)} of 20 granted against a 10-slot cap'
    assert _used(uid) == 1000


def test_failed_task_refunds_its_reservation(trial_on):
    from models import db, TranscriptionTask
    uid = _make_user('refund@test.com', limit=600)
    with A.app.app_context():
        assert A.trial_reserve(uid, 300) is True
        t = TranscriptionTask(id='trial-refund-1', user_id=uid, episode_title='x',
                              status='error', trial_seconds_charged=300)
        db.session.add(t)
        db.session.commit()
        assert A.trial_refund_task(t) == 300
    assert _used(uid) == 0


def test_refund_is_idempotent(trial_on):
    """The worker thread and the stale sweeper both settle the same dead task,
    each holding its own copy of the row read before either acted. Both see
    charged=300, so the "already zero" early return does not save us -- only
    the conditional UPDATE decides which one may move the balance.
    """
    from models import db, TranscriptionTask
    import types

    uid = _make_user('refund2@test.com', limit=600)
    with A.app.app_context():
        A.trial_reserve(uid, 300)
        db.session.add(TranscriptionTask(id='trial-refund-2', user_id=uid,
                                         episode_title='x', status='error',
                                         trial_seconds_charged=300))
        db.session.commit()

        # Two stale views of the same row, as two gunicorn workers would have.
        fields = dict(id='trial-refund-2', user_id=uid, trial_seconds_charged=300,
                      chunk_total=None, chunk_index=None)
        worker = types.SimpleNamespace(**fields)
        sweeper = types.SimpleNamespace(**fields)
        assert A.trial_refund_task(worker) == 300
        assert A.trial_refund_task(sweeper) == 0, 'refunded twice'
    assert _used(uid) == 0


def test_reconcile_releases_an_overestimate(trial_on):
    """Feeds without itunes:duration reserve a flat estimate. A 5-minute
    episode must not keep charging for the 30 minutes we guessed."""
    from models import db, TranscriptionTask
    uid = _make_user('recon@test.com', limit=3600)
    with A.app.app_context():
        A.trial_reserve(uid, 1800)
        db.session.add(TranscriptionTask(id='trial-recon-1', user_id=uid,
                                         episode_title='x', status='transcribing',
                                         trial_seconds_charged=1800))
        db.session.commit()
        A.trial_reconcile_task('trial-recon-1', 300)
        assert db.session.get(TranscriptionTask, 'trial-recon-1').trial_seconds_charged == 300
    assert _used(uid) == 300


def test_reconcile_tops_up_an_underestimate(trial_on):
    from models import db, TranscriptionTask
    uid = _make_user('recon2@test.com', limit=3600)
    with A.app.app_context():
        A.trial_reserve(uid, 600)
        db.session.add(TranscriptionTask(id='trial-recon-2', user_id=uid,
                                         episode_title='x', status='transcribing',
                                         trial_seconds_charged=600))
        db.session.commit()
        A.trial_reconcile_task('trial-recon-2', 900)
    assert _used(uid) == 900


def test_reconcile_refuses_an_episode_that_does_not_fit(trial_on):
    """The feed said 10 minutes, the file is 40. Refusing here is free;
    refusing after the first chunk is not."""
    from models import db, TranscriptionTask
    uid = _make_user('recon3@test.com', limit=900)
    with A.app.app_context():
        A.trial_reserve(uid, 600)
        db.session.add(TranscriptionTask(id='trial-recon-3', user_id=uid,
                                         episode_title='x', status='transcribing',
                                         trial_seconds_charged=600))
        db.session.commit()
        with pytest.raises(A.TrialExhausted):
            A.trial_reconcile_task('trial-recon-3', 2400)
        # Still holding only the original reservation, nothing extra taken.
        assert db.session.get(TranscriptionTask, 'trial-recon-3').trial_seconds_charged == 600
    assert _used(uid) == 600


def test_reconcile_refuses_an_over_long_episode(trial_on, monkeypatch):
    from models import db, TranscriptionTask
    monkeypatch.setattr(A, 'TRIAL_MAX_EPISODE_SECONDS', 1800)
    uid = _make_user('recon4@test.com', limit=10 ** 6)
    with A.app.app_context():
        A.trial_reserve(uid, 600)
        db.session.add(TranscriptionTask(id='trial-recon-4', user_id=uid,
                                         episode_title='x', status='transcribing',
                                         trial_seconds_charged=600))
        db.session.commit()
        with pytest.raises(A.TrialExhausted):
            A.trial_reconcile_task('trial-recon-4', 3600)


def test_own_key_task_is_never_metered(trial_on):
    """trial_seconds_charged is NULL for own-key work; reconcile must ignore it."""
    from models import db, TranscriptionTask
    uid = _make_user('unmetered@test.com', key='sk-' + 'z' * 40, limit=600)
    with A.app.app_context():
        db.session.add(TranscriptionTask(id='trial-unmetered', user_id=uid,
                                         episode_title='x', status='transcribing',
                                         trial_seconds_charged=None))
        db.session.commit()
        A.trial_reconcile_task('trial-unmetered', 99999)
    assert _used(uid) == 0


def test_billing_ignores_the_duration_the_client_claimed(trial_on, monkeypatch, tmp_path):
    """The hole the first cut of this feature shipped with.

    Both the reservation and task.audio_duration came from duration_min, a
    form field on the direct path and itunes:duration on the RSS path -- both
    client-supplied. Reconcile therefore compared a number to itself and was a
    guaranteed no-op: claim one minute, transcribe four hours on our key.
    """
    from models import db, TranscriptionTask

    uid = _make_user('liar@test.com', limit=3600)
    audio = tmp_path / 'ep.mp3'
    audio.write_bytes(b'\0' * 1024)
    calls = []
    # The file really is four hours long; the client said one minute.
    monkeypatch.setattr(A, 'probe_audio_duration', lambda f: 14400.0)
    monkeypatch.setattr(A, 'split_audio_if_needed', lambda f, **kw: [str(audio)])
    monkeypatch.setattr(A, '_transcribe_chunks',
                        lambda *a, **kw: calls.append(a) or ('text', []))

    with A.app.app_context():
        A.trial_reserve(uid, 60)
        db.session.add(TranscriptionTask(
            id='trial-liar', user_id=uid, episode_title='x', status='downloading',
            audio_duration=60.0, trial_seconds_charged=60))
        db.session.commit()
        with pytest.raises(A.TrialExhausted):
            A.transcribe_audio(str(audio), 'trial-liar', object(), language='no')

    assert calls == [], 'four hours of audio was billed as one claimed minute'


def test_billing_uses_the_size_estimate_when_ffprobe_fails(trial_on, monkeypatch, tmp_path):
    """An unreadable file must still be charged for -- otherwise "corrupt the
    header" is a way to transcribe for free."""
    from models import db, TranscriptionTask

    uid = _make_user('unreadable@test.com', limit=3600)
    audio = tmp_path / 'ep.mp3'
    audio.write_bytes(b'\0' * (40 * 1024 * 1024))   # ~40 min at 1 MB/min
    monkeypatch.setattr(A, 'probe_audio_duration', lambda f: None)
    monkeypatch.setattr(A, 'split_audio_if_needed', lambda f, **kw: [str(audio)])
    monkeypatch.setattr(A, '_transcribe_chunks', lambda *a, **kw: ('text', []))

    with A.app.app_context():
        A.trial_reserve(uid, 60)
        db.session.add(TranscriptionTask(
            id='trial-unreadable', user_id=uid, episode_title='x',
            status='downloading', audio_duration=60.0, trial_seconds_charged=60))
        db.session.commit()
        A.transcribe_audio(str(audio), 'trial-unreadable', object(), language='no')

    assert _used(uid) == pytest.approx(2400, abs=60), (
        'size-based estimate was not charged'
    )


def test_a_swept_task_stops_billing(trial_on, monkeypatch, tmp_path):
    """The stale sweeper refunds a task whose worker is still running. If the
    worker keeps going, the episode is transcribed free AND the allowance is
    handed back -- so the worker has to notice and stop."""
    from models import db, TranscriptionTask

    uid = _make_user('swept@test.com', limit=3600)
    sent = []

    class FakeChunk:
        text = 'hi'
        segments = []
        language = 'no'

    class FakeClient:
        class audio:
            class transcriptions:
                @staticmethod
                def create(**kw):
                    sent.append(kw)
                    # The sweeper fires between chunk 1 and chunk 2.
                    db.session.execute(A.text(
                        "UPDATE transcription_tasks SET status='error' WHERE id='trial-swept'"))
                    db.session.commit()
                    return FakeChunk()

    chunks = []
    for i in range(3):
        f = tmp_path / f'c{i}.mp3'
        f.write_bytes(b'\0' * 16)
        chunks.append(str(f))

    with A.app.app_context():
        db.session.add(TranscriptionTask(
            id='trial-swept', user_id=uid, episode_title='x', status='transcribing',
            chunk_total=3, chunk_index=0, trial_seconds_charged=900))
        db.session.commit()
        with pytest.raises(A.TaskAbandoned):
            A._transcribe_chunks(chunks, set(chunks), 'trial-swept', FakeClient(),
                                 'no', 900.0)

    assert len(sent) == 1, f'{len(sent)} chunks billed after the task was swept'


def test_refund_keeps_what_was_already_spent(trial_on):
    """Chunks already sent to Whisper are billed to us whatever happens next.
    Refunding them would hand back money that is gone."""
    from models import db, TranscriptionTask

    uid = _make_user('prorata@test.com', limit=3600)
    with A.app.app_context():
        A.trial_reserve(uid, 900)
        t = TranscriptionTask(id='trial-prorata', user_id=uid, episode_title='x',
                              status='error', chunk_total=3, chunk_index=2,
                              trial_seconds_charged=900)
        db.session.add(t)
        db.session.commit()
        assert A.trial_refund_task(t) == 300      # one of three chunks unspent
    assert _used(uid) == 600


def test_refund_returns_everything_before_the_first_chunk(trial_on):
    from models import db, TranscriptionTask

    uid = _make_user('prorata2@test.com', limit=3600)
    with A.app.app_context():
        A.trial_reserve(uid, 900)
        t = TranscriptionTask(id='trial-prorata2', user_id=uid, episode_title='x',
                              status='error', chunk_total=None, chunk_index=None,
                              trial_seconds_charged=900)
        db.session.add(t)
        db.session.commit()
        assert A.trial_refund_task(t) == 900
    assert _used(uid) == 0


def test_negative_duration_is_treated_as_unstated(trial_on):
    """A negative duration_min reserved nothing: trial_reserve() grants any
    non-positive request, so it was a free pass past the meter."""
    assert A._positive_float_or_none('-500') is None
    assert A._positive_float_or_none('0') is None
    assert A._positive_float_or_none('12.5') == 12.5

    uid = _make_user('negative@test.com', limit=3600)
    A.app.config['TESTING'] = True
    client = A.app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(uid)
        sess['_fresh'] = True
    client.post('/start_transcription', data={
        'audio_url': 'https://example.com/ep.mp3',
        'episode_title': 'Ep', 'duration_min': '-500', 'language': 'no',
    })
    # Falls back to the flat unknown-episode estimate rather than reserving 0.
    assert _used(uid) == A.TRIAL_UNKNOWN_ESTIMATE_SECONDS


def test_global_ceiling_message_does_not_contradict_itself(trial_on, monkeypatch):
    """"You have 60 minutes left, and this needs 30" -- while refusing."""
    from models import db
    with A.app.app_context():
        db.session.execute(A.text('UPDATE users SET trial_seconds_used = 0'))
        db.session.commit()
    monkeypatch.setattr(A, 'TRIAL_GLOBAL_SECONDS', 60)
    uid = _make_user('ceiling@test.com', limit=36000)
    A.app.config['TESTING'] = True
    client = A.app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(uid)
        sess['_fresh'] = True
    resp = client.post('/start_transcription', data={
        'audio_url': 'https://example.com/ep.mp3',
        'episode_title': 'Ep', 'duration_min': '30', 'language': 'no',
    })
    assert resp.status_code == 402
    error = resp.get_json()['error']
    assert 'budgeted' in error, error
    assert 'minutes left' not in error, f'contradicts itself: {error}'


def test_start_transcription_refuses_an_exhausted_trial(trial_on):
    uid = _make_user('exhausted@test.com', limit=600, used=600)
    A.app.config['TESTING'] = True
    client = A.app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(uid)
        sess['_fresh'] = True

    resp = client.post('/start_transcription', data={
        'audio_url': 'https://example.com/ep.mp3',
        'episode_title': 'Ep',
        'duration_min': '30',
        'language': 'no',
    })
    assert resp.status_code == 402
    assert 'trial' in resp.get_json()['error'].lower()


def test_exhausted_trial_reads_as_no_key(trial_on):
    """Which is what puts the "add your key" prompt in front of the people
    who have actually hit the wall."""
    uid = _make_user('prompt@test.com', limit=600, used=600)
    A.app.config['TESTING'] = True
    client = A.app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(uid)
        sess['_fresh'] = True
    with A.app.test_request_context():
        pass
    body = client.get('/settings').data.decode()
    assert 'trial is used up' in body.lower()

def test_reservations_are_atomic_across_processes(trial_on):
    """The threaded test above shares one interpreter, and a threading.Lock
    would pass it. Podskrift runs `gunicorn --workers 2`, so the guarantee has
    to hold between separate PROCESSES -- which only the conditional UPDATE
    gives us. This spawns real ones to prove it.
    """
    import subprocess
    import sys

    uid = _make_user('crossproc@test.com', limit=600)
    child = (
        'import os, sys;'
        f'os.environ["DATABASE_URL"] = {os.environ["DATABASE_URL"]!r};'
        'os.environ["OPENAI_API_KEY"] = "sk-global-not-a-real-key";'
        'sys.path.insert(0, %r);' % os.path.dirname(os.path.abspath(__file__)) +
        'import app;'
        'app.TRIAL_DEFAULT_SECONDS = 600;'
        'app.TRIAL_GLOBAL_SECONDS = 10 ** 7;'
        f'ctx = app.app.app_context(); ctx.push();'
        f'print("GRANTED" if app.trial_reserve({uid}, 100) else "REFUSED")'
    )
    procs = [subprocess.Popen([sys.executable, '-c', child],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True) for _ in range(12)]
    outs = []
    for p in procs:
        out, err = p.communicate(timeout=120)
        assert p.returncode == 0, f'child failed: {err[-400:]}'
        outs.append(out.strip())

    granted = outs.count('GRANTED')
    assert granted == 6, f'{granted} of 12 processes granted against a 6-slot cap'
    assert _used(uid) == 600
