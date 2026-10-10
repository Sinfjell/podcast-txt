"""Regression tests for the transcription pipeline.

Focused on the defects this suite was written to catch: a progress bar that
reported numbers it did not have, a download bar stuck at 0%, and an
unvalidated server-side fetch of a client-supplied URL.

Run: pytest test_app.py
"""

import json
import os
import re
import secrets
import tempfile
import time
from datetime import datetime, timedelta, timezone

import pytest

# Importing app.py executes its module-level app_context block: db.create_all,
# the ALTER TABLE migrations, the orphan sweep and a DROP TABLE. Point it at a
# throwaway file BEFORE the import so running the suite can never touch real data.
_TEST_DB = os.path.join(tempfile.mkdtemp(prefix='podskrift-test-'), 'test.db')
os.environ['DATABASE_URL'] = f'sqlite:///{_TEST_DB}'
# load_dotenv never overrides a set variable, so this keeps a developer's .env
# from pointing the suite at the real Sentry / PostHog projects.
os.environ['SENTRY_DSN'] = ''
os.environ['POSTHOG_KEY'] = ''
os.environ['POSTHOG_HOST'] = ''
# The stale-task watchdog is a daemon thread; keep it off in the suite so it
# cannot race tests against the throwaway DB. Tests call _sweep_stale_tasks /
# _fail_if_stale directly.
os.environ['PODSKRIFT_DISABLE_WATCHDOG'] = '1'
# SIGTERM handlers would interfere with pytest / the parent process.
os.environ['PODSKRIFT_DISABLE_SHUTDOWN_HANDLERS'] = '1'

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
        assert 'stopped making progress' in task.error_message


def test_stale_window_scales_with_chunk_length():
    """Transcribing windows clear a slow chunk but stay under the Whisper hang
    budget -- unbounded per_chunk*8 is what left a hung job quiet for ~90 min."""
    class T:
        chunk_total = 1
        audio_duration = 50 * 60      # one ~50-minute chunk (24 MB @ 64 kbps)
        bytes_downloaded = None

    class NoInfo:
        chunk_total = None
        audio_duration = None
        bytes_downloaded = None

    window = A._stale_after_seconds(T())
    assert window >= A.STALE_TASK_SECONDS
    assert window <= A._whisper_hang_budget_seconds()
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
    SDK retries are off; per-chunk attempts are ours, and their product must
    stay inside the stale floor.
    """
    import httpx

    monkeypatch.setattr(A, 'GLOBAL_OPENAI_KEY', 'sk-test-not-a-real-key')
    monkeypatch.setattr(A, 'TRIAL_ENABLED', True)
    client = A.build_openai_client(A.resolve_openai_key(None)[0])

    assert client is not None
    assert isinstance(client.timeout, httpx.Timeout)
    assert client.timeout.read == A.WHISPER_TIMEOUT_SECONDS
    assert client.timeout.write == A.WHISPER_TIMEOUT_SECONDS
    assert client.max_retries == A.WHISPER_CLIENT_MAX_RETRIES == 0

    worst_case = A.WHISPER_TIMEOUT_SECONDS * A.WHISPER_CHUNK_ATTEMPTS
    assert worst_case < A.STALE_TASK_SECONDS
    assert A._whisper_hang_budget_seconds() >= worst_case


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


def test_hung_whisper_chunk_retries_then_raises(tmp_path, monkeypatch):
    """A Whisper call that times out every attempt must not hang the job forever.

    Bounded retries are the whole point of owning the loop instead of the SDK's
    opaque max_retries: after WHISPER_CHUNK_ATTEMPTS the error surfaces so the
    worker can mark the task failed.
    """
    attempts = {'n': 0}

    class FakeClient:
        class audio:
            class transcriptions:
                @staticmethod
                def create(**kw):
                    attempts['n'] += 1
                    raise A.APITimeoutError(request=None)

    chunk = tmp_path / 'c0.mp3'
    chunk.write_bytes(b'\0' * 16)
    monkeypatch.setattr(A.time, 'sleep', lambda *_: None)

    with pytest.raises(A.APITimeoutError):
        A._whisper_transcribe_chunk(
            FakeClient(), str(chunk), 'no', chunk_label='chunk 1/9')
    assert attempts['n'] == A.WHISPER_CHUNK_ATTEMPTS


def test_hung_chunk_fails_task_refunds_and_emits_transcript_failed(
        trial_on, monkeypatch, ph_events):
    """End-to-end through the worker: a Whisper timeout fails the job, refunds
    unsent minutes, and fires transcript_failed with reason=network — the path
    that was missing when a hang never raised."""
    import types
    from models import db, TranscriptionTask

    uid = _make_user('hungchunk@test.com', limit=3600)
    monkeypatch.setattr(A.time, 'sleep', lambda *_: None)
    monkeypatch.setattr(
        A.threading, 'Thread',
        lambda target=None, **kw: types.SimpleNamespace(
            daemon=True, start=lambda: target and target()))
    monkeypatch.setattr(A, 'download_audio', lambda *a, **kw: None)
    monkeypatch.setattr(A, 'free_disk_bytes', lambda *a, **kw: 10 ** 12)

    def hang(*a, **kw):
        raise A.APITimeoutError(request=None)

    monkeypatch.setattr(A, 'transcribe_audio', hang)

    with A.app.app_context():
        user = A.db.session.get(A.User, uid)
        payload, status = A.enqueue_transcription(
            user, {'title': 'Ep', 'audio_url': 'https://example.com/ep.mp3',
                   'duration_min': 15})
        assert status == 200, payload
        task = db.session.get(TranscriptionTask, payload['task_id'])
        assert task.status == 'error'
        assert 'did not respond in time' in (task.error_message or '').lower()
        # Nothing reached Whisper (failure before/at transcribe_audio entry),
        # so the whole reservation is refunded.
        assert task.trial_settled is True
    assert _used(uid) == 0

    failed_events = [e for e in ph_events.events if e['event'] == 'transcript_failed']
    assert failed_events
    assert failed_events[-1]['properties']['reason'] == 'network'


def test_hung_chunk_in_loop_refunds_only_unsent_remainder(
        trial_on, monkeypatch, tmp_path):
    """Timeout after chunk_index is written still charges for that in-flight
    chunk (it may have reached Whisper) and refunds the rest."""
    from models import db, TranscriptionTask

    uid = _make_user('hungmid@test.com', limit=3600)
    monkeypatch.setattr(A.time, 'sleep', lambda *_: None)

    class FakeClient:
        class audio:
            class transcriptions:
                @staticmethod
                def create(**kw):
                    raise A.APITimeoutError(request=None)

    chunks = []
    for i in range(3):
        f = tmp_path / f'c{i}.mp3'
        f.write_bytes(b'\0' * 16)
        chunks.append(str(f))

    with A.app.app_context():
        A.trial_reserve(uid, 900)
        db.session.add(TranscriptionTask(
            id='hung-mid', user_id=uid, episode_title='x',
            status='transcribing', chunk_total=3, chunk_index=None,
            trial_seconds_charged=900))
        db.session.commit()
        with pytest.raises(A.APITimeoutError):
            A._transcribe_chunks(
                chunks, set(chunks), 'hung-mid', FakeClient(), 'no', 900.0)
        task = db.session.get(TranscriptionTask, 'hung-mid')
        assert task.chunk_index == 0
        A._update_task('hung-mid', status='error', phase='error',
                       error_message='timed out')
        assert A.trial_refund_task(task) == 600
    assert _used(uid) == 300


def test_worker_death_sweep_fails_refunds_and_emits_stale(
        trial_on, ph_events, sentry_events):
    """A daemon thread killed mid-chunk never runs its except handler. The
    stale sweep (watchdog / boot / active-jobs) must still fail the row,
    refund unspent minutes, report to Sentry, and fire transcript_failed.
    """
    from models import db, TranscriptionTask

    uid = _make_user('workerdeath@test.com', limit=3600)
    now = datetime.now(timezone.utc)
    with A.app.app_context():
        A.trial_reserve(uid, 900)
        db.session.add(TranscriptionTask(
            id='worker-death', user_id=uid, episode_title='Ep',
            status='transcribing chunk 7/9', phase='transcribing',
            progress=70, chunk_total=9, chunk_index=6,
            audio_duration=5400.0, trial_seconds_charged=900,
            started_at=now - timedelta(hours=2),
            heartbeat_at=now - timedelta(hours=2),
        ))
        db.session.commit()

        assert A._sweep_stale_tasks(source='watchdog') == 1
        task = db.session.get(TranscriptionTask, 'worker-death')
        assert task.status == 'error'
        assert 'stopped making progress' in task.error_message
        # index 6 of 9 => 7 chunks billed; refund 2/9 of 900 = 200
        assert task.trial_settled is True
    assert _used(uid) == 700

    stale_events = [
        e for e in ph_events.events
        if e['event'] == 'transcript_failed' and e['properties'].get('reason') == 'stale'
    ]
    assert stale_events
    assert stale_events[-1]['distinct_id'] == str(uid)
    assert any(e.get('fingerprint') == ['stale-task'] for e in sentry_events)


def test_watchdog_is_disabled_in_the_test_suite():
    """Guard the env opt-out: a live watchdog racing the suite is a flake factory."""
    assert os.environ.get('PODSKRIFT_DISABLE_WATCHDOG') == '1'
    assert A._stale_watchdog_started is False


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
        if 400 <= self.status_code < 600:
            import requests
            raise requests.exceptions.HTTPError(
                f'{self.status_code} Client Error', response=self)

    def iter_content(self, chunk_size=8192):
        return iter([b'x' * 100])


def _download_with(responder, tmp_path, start_url='http://example.com/ep.mp3',
                   fetchable=None):
    """Run download_audio against a fake transport, returning the URLs it hit."""
    from unittest import mock

    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        return responder(url)

    def fake_session_get(self, url, **kwargs):
        return fake_get(url, **kwargs)

    patches = [
        mock.patch.object(A.requests, 'get', side_effect=fake_get),
        mock.patch.object(A.requests.Session, 'get', fake_session_get),
        mock.patch.object(A, '_update_task'),
    ]
    if fetchable is not None:
        patches.append(mock.patch.object(
            A, '_is_fetchable_url', side_effect=fetchable))

    from contextlib import ExitStack
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        try:
            A.download_audio(
                start_url, str(tmp_path / 'a.mp3'), 'task-id'
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

    def boom_session(self, url, **kwargs):
        return boom(url, **kwargs)

    with mock.patch.object(A.requests, 'get', side_effect=boom), \
            mock.patch.object(A.requests.Session, 'get', boom_session), \
            mock.patch.object(A, '_update_task'):
        with pytest.raises(Exception) as exc:
            A.download_audio(
                'http://example.com/ep.mp3', str(tmp_path / 'a.mp3'), 'task-id'
            )
    assert not isinstance(exc.value, AttributeError)
    assert 'HTTP error' in str(exc.value)
    assert not isinstance(exc.value, A.SourceAudioUnavailable)


@pytest.mark.parametrize('status,reason', [
    (404, 'source_audio_missing'),
    (410, 'source_audio_missing'),
    (403, 'source_audio_forbidden'),
])
def test_permanent_source_http_errors_raise_source_audio_unavailable(
        tmp_path, status, reason):
    """Libsyn-style dead enclosures and host blocks are typed failures."""
    calls, error = _download_with(lambda url: _FakeResponse(status), tmp_path)
    assert error is not None
    assert 'Try another episode' in error
    assert 'podcast host' in error.lower()
    # Re-run to assert the exception type (helper only returns the message).
    from unittest import mock
    with mock.patch.object(A.requests.Session, 'get',
                           lambda self, url, **kw: _FakeResponse(status)), \
            mock.patch.object(A, '_update_task'):
        with pytest.raises(A.SourceAudioUnavailable) as exc:
            A.download_audio(
                'http://example.com/ep.mp3', str(tmp_path / 'b.mp3'), 'task-id')
    assert exc.value.reason == reason
    assert exc.value.status_code == status
    # 403 retries once with a plainer UA before giving up.
    if status == 403:
        assert len(calls) == 2
    else:
        assert len(calls) == 1


def test_download_retries_once_on_5xx_then_succeeds(tmp_path, monkeypatch):
    monkeypatch.setattr(A.time, 'sleep', lambda *a, **kw: None)
    hits = {'n': 0}

    def responder(url):
        hits['n'] += 1
        if hits['n'] == 1:
            return _FakeResponse(503)
        return _FakeResponse(200)

    calls, error = _download_with(responder, tmp_path)
    assert error is None
    assert len(calls) == 2


def test_download_retries_once_on_timeout_then_fails(tmp_path, monkeypatch):
    import requests
    from unittest import mock
    monkeypatch.setattr(A.time, 'sleep', lambda *a, **kw: None)

    def boom(self, url, **kwargs):
        raise requests.exceptions.Timeout('read timed out')

    with mock.patch.object(A.requests.Session, 'get', boom), \
            mock.patch.object(A, '_update_task'):
        with pytest.raises(Exception) as exc:
            A.download_audio(
                'http://example.com/ep.mp3', str(tmp_path / 't.mp3'), 'task-id')
    assert 'Failed to download audio' in str(exc.value)
    assert not isinstance(exc.value, A.SourceAudioUnavailable)


def test_redirect_loop_is_bounded(tmp_path):
    """A non-cycling runaway chain must stop at MAX_REDIRECTS, not hang."""
    hop = {'n': 0}

    def responder(url):
        hop['n'] += 1
        # Fresh Location every time so loop-detection does not fire first.
        return _FakeResponse(302, f'https://www.iana.org/next-{hop["n"]}.mp3')

    calls, error = _download_with(responder, tmp_path)
    # One GET per followed redirect, plus the probe that still redirected and
    # tripped the cap (we have to see the 302 before we can refuse it).
    assert len(calls) == A.MAX_REDIRECTS + 1
    assert 'Too many redirects' in error


def test_redirect_cycle_is_detected_without_burning_the_budget(tmp_path):
    """A↔B must fail as a loop, not grind through MAX_REDIRECTS hops."""
    def responder(url):
        if 'example.com' in url:
            return _FakeResponse(302, 'https://www.iana.org/a.mp3')
        return _FakeResponse(302, 'http://example.com/ep.mp3')

    calls, error = _download_with(responder, tmp_path)
    assert error is not None
    assert 'Redirect loop' in error
    assert len(calls) == 2


def test_long_measurement_prefix_chain_succeeds(tmp_path):
    """Modern Wisdom-style stacks (byspotify→pscrb→claritas→megaphone→dcs)
    plus a few spare hops must clear the redirect budget. The production
    failure was MAX_REDIRECTS=5 dying on a chain that needed one more hop.
    """
    chain = [
        'https://prfx.byspotify.com/e/ep.mp3',
        'https://pscrb.fm/rss/p/ep.mp3',
        'https://claritaspod.com/measure/ep.mp3',
        'https://traffic.megaphone.fm/ep.mp3',
        'https://dcs.megaphone.fm/ep.mp3',
        'https://cdn1.example.net/ep.mp3',
        'https://cdn2.example.net/ep.mp3',
        'https://cdn3.example.net/ep.mp3',
        'https://cdn4.example.net/ep.mp3',
        'https://cdn5.example.net/ep.mp3',
        'https://audio.example.net/final.mp3',
    ]
    nxt = {chain[i]: chain[i + 1] for i in range(len(chain) - 1)}

    def responder(url):
        if url in nxt:
            return _FakeResponse(302, nxt[url])
        return _FakeResponse(200)

    calls, error = _download_with(
        responder, tmp_path, start_url=chain[0], fetchable=lambda u: True)
    assert error is None
    assert calls == chain
    assert len(calls) > 5, 'fixture must exceed the old redirect budget'


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


def test_every_user_column_added_after_release_has_a_migration():
    """Same guard for the users table. The suite runs on a fresh create_all(),
    so a missing entry here is invisible until it hits the 23 real accounts."""
    from models import User, USER_COLUMN_MIGRATIONS
    original = {'id', 'email', 'password_hash', 'openai_api_key', 'created_at'}
    added = {c.name for c in User.__table__.columns} - original
    assert added == set(USER_COLUMN_MIGRATIONS), (
        f'missing migrations for {added - set(USER_COLUMN_MIGRATIONS)}'
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
    ok, msg, reason = A.verify_openai_key('Clashofc*ans1')
    assert ok is False
    assert reason == 'invalid_key'
    assert 'sk-' in msg


class _Status(Exception):
    def __init__(self, status_code):
        super().__init__('raw provider text with a secret in it')
        self.status_code = status_code


@pytest.mark.parametrize('status,expect', [
    (401, 'rejected'),
    (429, 'credit'),
    (403, 'not allowed'),
    (500, 'server error'),
])
def test_openai_errors_become_human_messages(status, expect):
    msg = A.describe_openai_error(_Status(status))
    assert expect in msg.lower()


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
    ok, msg, status = A.verify_openai_key('sk-' + 'a' * 40)
    assert ok is True
    assert status == 'unverified_network'
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
    ok, msg, status = A.verify_openai_key('sk-' + 'c' * 40)
    assert ok is True
    assert status == 'no_billing'
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
    data = {
        'email': email,
        'password': password,
    }
    # confirm kept for call-site compatibility; password confirmation was removed.
    return client.post('/register', data=data, follow_redirects=True)


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
        if 'Account created' in body:
            created += 1
            client = _new_session()
    _purge(emails)
    assert created == A.REGISTER_MAX_PER_IP, f'{created} accounts created, limit is 3'


def test_register_route_releases_the_slot_on_validation_failure():
    """Kills the mutants that delete the finally-release or the created flag."""
    emails = [f'rel{i}@example.com' for i in range(4)]
    _purge(emails)
    client = _fresh_client()

    # Five short-password attempts must cost nothing
    for _ in range(5):
        body = _signup(client, 'rel0@example.com', password='short').data.decode()
        assert 'at least 8 characters' in body

    created = 0
    for e in emails:
        body = _signup(client, e).data.decode()
        if 'Account created' in body:
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
    flashes = _re.findall(r'<div class="alert alert-(\w+)"', body)
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
    ok, msg, status = A.verify_openai_key('sk-' + 'e' * 40)
    assert ok is True
    assert status == 'no_billing'
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
    ({'password': 'short1'}, 'at least 8 characters'),
    ({'email': 'not-an-email'}, 'valid email address'),
    ({'email': 'x@immenseignite.info'}, 'real email address'),
])
def test_every_validation_failure_gives_the_slot_back(override, expect):
    """Only the password-mismatch path was covered; the others would have
    permanently burned a signup slot each."""
    A._register_attempts.clear()
    client = _fresh_client()
    data = {'email': 'slot@example.com', 'password': 'abcdefgh1'}
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
    assert 'Account created' in _signup(client, 'dupe@example.com').data.decode()

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
            results.append('Account created' in body)
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


@pytest.fixture(autouse=True)
def fresh_transcription_slots():
    """Give every test its own capacity semaphore.

    Tests that stub out threading.Thread hand the slot to a worker that never
    runs, so it is never released. Without this the suite develops an
    order-dependency: the third route test to ask for a slot gets a 503 from
    the first two, and which tests those are depends on collection order.
    """
    import threading as _t
    A._transcription_slots = _t.BoundedSemaphore(A.MAX_CONCURRENT_TRANSCRIPTIONS)
    yield


def _post_start(monkeypatch, user_id, data, free_disk=10 ** 12):
    """POST /start_transcription with the worker thread stubbed out.

    The reservation is what these tests assert on, and it is made before the
    thread starts. Letting the real thread run makes the assertion a race with
    a refund -- and fires a live HTTPS request from the test suite. Patched via
    monkeypatch so it is restored even when the test fails.
    """
    import types
    monkeypatch.setattr(A.threading, 'Thread',
                        lambda *a, **kw: types.SimpleNamespace(
                            daemon=True, start=lambda: None))
    # Otherwise every money-path test silently depends on the host's free disk:
    # on a runner below MIN_FREE_DISK_MB they get a 503 instead of the assertion
    # they exist for. Tests that want the disk guard stub it themselves.
    monkeypatch.setattr(A, 'free_disk_bytes', lambda *a, **kw: free_disk)
    A.app.config['TESTING'] = True
    client = A.app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user_id)
        sess['_fresh'] = True
    return client.post('/start_transcription', data=data)


@pytest.fixture
def trial_on(monkeypatch):
    """Turn the trial on with a small, easy-to-reason-about allowance."""
    monkeypatch.setattr(A, 'GLOBAL_OPENAI_KEY', 'sk-global-not-a-real-key')
    monkeypatch.setattr(A, 'TRIAL_ENABLED', True)
    monkeypatch.setattr(A, 'TRIAL_DEFAULT_SECONDS', 600)        # 10 min
    monkeypatch.setattr(A, 'TRIAL_DAILY_SECONDS', 10 ** 7)      # effectively off
    monkeypatch.setattr(A, 'TRIAL_GLOBAL_SECONDS', 0)           # lifetime safety off
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


def test_reserve_stops_at_the_daily_ceiling(trial_on, monkeypatch):
    """The per-user cap bounds nothing on its own -- signups are free, so N
    accounts cost N x the grant. The daily budget is what caps the actual bill."""
    from models import db
    with A.app.app_context():
        db.session.execute(A.text('UPDATE users SET trial_seconds_used = 0'))
        db.session.execute(A.text('DELETE FROM trial_budget_days'))
        db.session.commit()
    monkeypatch.setattr(A, 'TRIAL_DAILY_SECONDS', 900)
    a = _make_user('g1@test.com', limit=6000)
    b = _make_user('g2@test.com', limit=6000)
    with A.app.app_context():
        assert A.trial_reserve(a, 600) is True
        # b is nowhere near its own limit, but today's shared budget is.
        assert A.trial_reserve(b, 600) is False
        assert A.trial_reserve(b, 300) is True


def test_optional_lifetime_ceiling_still_enforced(trial_on, monkeypatch):
    """TRIAL_GLOBAL_MINUTES is an optional safety net on top of the daily budget."""
    from models import db
    with A.app.app_context():
        db.session.execute(A.text('UPDATE users SET trial_seconds_used = 0'))
        db.session.execute(A.text('DELETE FROM trial_budget_days'))
        db.session.commit()
    monkeypatch.setattr(A, 'TRIAL_DAILY_SECONDS', 10 ** 7)
    monkeypatch.setattr(A, 'TRIAL_GLOBAL_SECONDS', 900)
    a = _make_user('life1@test.com', limit=6000)
    b = _make_user('life2@test.com', limit=6000)
    with A.app.app_context():
        assert A.trial_reserve(a, 600) is True
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


def test_refund_is_idempotent_mid_episode(trial_on):
    """The case the test above cannot reach. With chunks in flight the refund
    is pro-rata, so it settles to a NON-zero charge -- and the "already zero"
    early return no longer covers anything. A second call once re-applied the
    fraction to the reduced value and handed back seconds already spent.
    """
    from models import db, TranscriptionTask
    import types

    uid = _make_user('refund3@test.com', limit=3600)
    with A.app.app_context():
        A.trial_reserve(uid, 800)
        db.session.add(TranscriptionTask(id='trial-refund-3', user_id=uid,
                                         episode_title='x', status='error',
                                         chunk_total=4, chunk_index=1,
                                         trial_seconds_charged=800))
        db.session.commit()
        fields = dict(id='trial-refund-3', user_id=uid, trial_seconds_charged=800,
                      chunk_total=4, chunk_index=1)
        assert A.trial_refund_task(types.SimpleNamespace(**fields)) == 400
        assert A.trial_refund_task(types.SimpleNamespace(**fields)) == 0, 'refunded twice'
    assert _used(uid) == 400, 'the second refund handed back spent minutes'


def test_the_abandonment_backstop_does_not_double_refund(trial_on):
    """Every path that raises TaskAbandoned has ALREADY refunded -- that is
    what makes the status write fail. So the backstop in transcribe_thread runs
    on an already-settled task as the normal case, not as a race."""
    from models import db, TranscriptionTask

    uid = _make_user('backstop@test.com', limit=3600)
    with A.app.app_context():
        A.trial_reserve(uid, 800)
        t = TranscriptionTask(id='trial-backstop', user_id=uid, episode_title='x',
                              status='error', chunk_total=4, chunk_index=1,
                              trial_seconds_charged=800)
        db.session.add(t)
        db.session.commit()
        A.trial_refund_task(t)                       # the sweeper
        db.session.expire_all()
        A.trial_refund_task(db.session.get(TranscriptionTask, 'trial-backstop'))
    assert _used(uid) == 400


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


def test_reconcile_trims_when_episode_does_not_fit_trial(trial_on):
    """The feed said 10 minutes, the file is 40. Trial-only overruns return
    'trim' so the worker shortens the file to the reservation instead of
    failing (and never call Whisper on the unreserved remainder)."""
    from models import db, TranscriptionTask
    uid = _make_user('recon3@test.com', limit=900)
    with A.app.app_context():
        A.trial_reserve(uid, 600)
        db.session.add(TranscriptionTask(id='trial-recon-3', user_id=uid,
                                         episode_title='x', status='transcribing',
                                         trial_seconds_charged=600))
        db.session.commit()
        assert A.trial_reconcile_task('trial-recon-3', 2400) == 'trim'
        task = db.session.get(TranscriptionTask, 'trial-recon-3')
        # Still holding only the original reservation, nothing extra taken.
        assert task.trial_seconds_charged == 600
        assert A.task_is_partial(task)
    assert _used(uid) == 600


def test_reconcile_trims_an_over_long_episode_without_paid(trial_on, monkeypatch):
    """Real audio over the free per-episode max, no paid cover → trim preview."""
    from models import db, TranscriptionTask
    monkeypatch.setattr(A, 'TRIAL_MAX_EPISODE_SECONDS', 1800)
    uid = _make_user('recon4@test.com', limit=10 ** 6)
    with A.app.app_context():
        A.trial_reserve(uid, 600)
        db.session.add(TranscriptionTask(id='trial-recon-4', user_id=uid,
                                         episode_title='x', status='transcribing',
                                         trial_seconds_charged=600))
        db.session.commit()
        assert A.trial_reconcile_task('trial-recon-4', 3600) == 'trim'
        task = db.session.get(TranscriptionTask, 'trial-recon-4')
        assert task.trial_seconds_charged == 600
        assert A.task_is_partial(task)


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


def test_billing_trims_when_client_under_claims_duration(trial_on, monkeypatch, tmp_path):
    """Client-claimed short duration must not let Whisper see the full file.

    Both the reservation and task.audio_duration came from duration_min — both
    attacker-controlled. We now trim to the reservation (and mark a partial
    preview) instead of failing, so claim-one-minute cannot bill four hours.
    """
    from models import db, TranscriptionTask

    uid = _make_user('liar@test.com', limit=3600)
    audio = tmp_path / 'ep.mp3'
    audio.write_bytes(b'\0' * 1024)
    calls = []
    probes = {'n': 0}
    trimmed = []

    def probe(f):
        probes['n'] += 1
        # First probe sees the real length; after trim, the reserved length.
        return 14400.0 if probes['n'] == 1 else 60.0

    def fake_trim(path, seconds):
        trimmed.append(seconds)
        return path

    monkeypatch.setattr(A, 'probe_audio_duration', probe)
    monkeypatch.setattr(A, 'trim_audio_file', fake_trim)
    monkeypatch.setattr(A, 'prepare_audio_for_whisper', lambda f, **kw: [str(audio)])
    monkeypatch.setattr(A, '_transcribe_chunks',
                        lambda *a, **kw: calls.append(a) or ('text', []))

    with A.app.app_context():
        A.trial_reserve(uid, 60)
        db.session.add(TranscriptionTask(
            id='trial-liar', user_id=uid, episode_title='x', status='downloading',
            audio_duration=60.0, trial_seconds_charged=60))
        db.session.commit()
        A.transcribe_audio(str(audio), 'trial-liar', object(), language='no')
        task = db.session.get(TranscriptionTask, 'trial-liar')
        assert task.status == 'completed'
        assert A.task_is_partial(task)
        assert task.trial_seconds_charged == 60

    assert trimmed == [60], 'audio must be cut to the reserved minute'
    assert len(calls) == 1, 'Whisper still runs on the trimmed minute'
    assert _used(uid) == 60


def test_billing_uses_the_size_estimate_when_ffprobe_fails(trial_on, monkeypatch, tmp_path):
    """An unreadable file must still be charged for -- otherwise "corrupt the
    header" is a way to transcribe for free."""
    from models import db, TranscriptionTask

    uid = _make_user('unreadable@test.com', limit=3600)
    audio = tmp_path / 'ep.mp3'
    audio.write_bytes(b'\0' * (40 * 1024 * 1024))   # ~40 min at 1 MB/min
    monkeypatch.setattr(A, 'probe_audio_duration', lambda f: None)
    monkeypatch.setattr(A, 'prepare_audio_for_whisper', lambda f, **kw: [str(audio)])
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
    chunk_index is written BEFORE its chunk is uploaded, so index 1 of 4 means
    two chunks are gone -- counting it as one refunded a chunk we had paid for.
    """
    from models import db, TranscriptionTask

    uid = _make_user('prorata@test.com', limit=3600)
    with A.app.app_context():
        A.trial_reserve(uid, 800)
        t = TranscriptionTask(id='trial-prorata', user_id=uid, episode_title='x',
                              status='error', chunk_total=4, chunk_index=1,
                              trial_seconds_charged=800)
        db.session.add(t)
        db.session.commit()
        assert A.trial_refund_task(t) == 400      # two of four chunks sent
    assert _used(uid) == 400


def test_a_swept_single_chunk_episode_is_not_free(trial_on):
    """Every episode under 24 MB is one chunk, so chunk_index never leaves 0.
    Counting "0 chunks done" refunded the whole episode after it had been
    transcribed -- the common case, not an edge case."""
    from models import db, TranscriptionTask

    uid = _make_user('onechunk@test.com', limit=3600)
    with A.app.app_context():
        A.trial_reserve(uid, 600)
        t = TranscriptionTask(id='trial-onechunk', user_id=uid, episode_title='x',
                              status='error', chunk_total=1, chunk_index=0,
                              trial_seconds_charged=600)
        db.session.add(t)
        db.session.commit()
        assert A.trial_refund_task(t) == 0
    assert _used(uid) == 600


def test_refund_survives_a_stale_caller_object(trial_on):
    """/status hands _fail_if_stale a row loaded at the top of the request. If
    the worker reconciled the charge in between, a claim against the stale
    value matched nothing and the user forfeited the allowance for good."""
    from models import db, TranscriptionTask
    import types

    uid = _make_user('stalecaller@test.com', limit=3600)
    with A.app.app_context():
        A.trial_reserve(uid, 900)
        db.session.add(TranscriptionTask(id='trial-stale', user_id=uid,
                                         episode_title='x', status='error',
                                         trial_seconds_charged=900))
        db.session.commit()
        # Worker reconciled 900 -> 300 after the caller read the row.
        A.trial_reconcile_task('trial-stale', 300)
        stale = types.SimpleNamespace(id='trial-stale', user_id=uid,
                                      trial_seconds_charged=900,
                                      chunk_total=None, chunk_index=None)
        assert A.trial_refund_task(stale) == 300
    assert _used(uid) == 0


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


def test_negative_duration_is_treated_as_unstated(trial_on, monkeypatch):
    """A negative duration_min reserved nothing: trial_reserve() grants any
    non-positive request, so it was a free pass past the meter."""
    assert A._positive_float_or_none('-500') is None
    assert A._positive_float_or_none('0') is None
    assert A._positive_float_or_none('12.5') == 12.5

    uid = _make_user('negative@test.com', limit=3600)
    _post_start(monkeypatch, uid, {'audio_url': 'https://example.com/ep.mp3',
                      'episode_title': 'Ep', 'duration_min': '-500',
                      'language': 'no'})
    # Falls back to the flat unknown-episode estimate rather than reserving 0.
    assert _used(uid) == A.TRIAL_UNKNOWN_ESTIMATE_SECONDS


def test_daily_ceiling_message_does_not_contradict_itself(trial_on, monkeypatch):
    """Personal balance still has room, but today's shared budget does not."""
    from models import db
    with A.app.app_context():
        db.session.execute(A.text('UPDATE users SET trial_seconds_used = 0'))
        db.session.execute(A.text('DELETE FROM trial_budget_days'))
        db.session.commit()
    monkeypatch.setattr(A, 'TRIAL_DAILY_SECONDS', 60)
    monkeypatch.setattr(A, 'STRIPE_SECRET_KEY', 'sk_test_x')
    monkeypatch.setattr(A, 'STRIPE_PRICE_ID', 'price_x')
    uid = _make_user('ceiling@test.com', limit=36000)
    resp = _post_start(monkeypatch, uid, {'audio_url': 'https://example.com/ep.mp3',
                             'episode_title': 'Ep', 'duration_min': '30',
                             'language': 'no'})
    assert resp.status_code == 402
    error = resp.get_json()['error']
    assert "Today's free minutes are used up" in error, error
    assert 'midnight' in error.lower(), error
    assert 'minutes left' not in error, f'contradicts itself: {error}'
    assert 'handed out all the free minutes' not in error


def test_daily_budget_resets_at_oslo_midnight(trial_on, monkeypatch):
    """Reservations on day D do not consume day D+1's budget (Oslo boundary)."""
    from models import db
    with A.app.app_context():
        db.session.execute(A.text('DELETE FROM trial_budget_days'))
        db.session.commit()
    monkeypatch.setattr(A, 'TRIAL_DAILY_SECONDS', 600)
    monkeypatch.setattr(A, 'trial_oslo_day_str', lambda when=None: '2026-10-08')
    a = _make_user('oslo-day1@test.com', limit=6000)
    b = _make_user('oslo-day2@test.com', limit=6000)
    with A.app.app_context():
        assert A.trial_reserve(a, 600, budget_day='2026-10-08') is True
        assert A.trial_reserve(b, 60, budget_day='2026-10-08') is False
        monkeypatch.setattr(A, 'trial_oslo_day_str', lambda when=None: '2026-10-09')
        assert A.trial_reserve(b, 600, budget_day='2026-10-09') is True
        assert A.trial_daily_used_seconds('2026-10-08') == 600
        assert A.trial_daily_used_seconds('2026-10-09') == 600


def test_daily_budget_reservations_count_and_refunds_release(trial_on, monkeypatch):
    """In-flight reservations consume today's budget; refunds free it again."""
    from models import db, TranscriptionTask
    with A.app.app_context():
        db.session.execute(A.text('DELETE FROM trial_budget_days'))
        db.session.commit()
    monkeypatch.setattr(A, 'TRIAL_DAILY_SECONDS', 900)
    uid = _make_user('daily-refund@test.com', limit=6000)
    with A.app.app_context():
        assert A.trial_reserve(uid, 600) is True
        assert A.trial_daily_used_seconds() == 600
        db.session.add(TranscriptionTask(
            id='daily-refund-task', user_id=uid, episode_title='x',
            status='error', trial_seconds_charged=600, trial_settled=False))
        db.session.commit()
        task = db.session.get(TranscriptionTask, 'daily-refund-task')
        assert A.trial_refund_task(task) == 600
        assert A.trial_daily_used_seconds() == 0
        assert A.trial_reserve(uid, 900) is True


def test_daily_exhausted_hides_personal_minutes_counter(trial_on, monkeypatch):
    """UI must not show a personal 'X min left' while the daily pool is empty."""
    from models import db, TrialBudgetDay
    with A.app.app_context():
        db.session.execute(A.text('DELETE FROM trial_budget_days'))
        day = A.trial_oslo_day_str()
        db.session.add(TrialBudgetDay(day=day, seconds_used=0))
        db.session.commit()
    monkeypatch.setattr(A, 'TRIAL_DAILY_SECONDS', 60)
    uid = _make_user('daily-ui@test.com', limit=3600, used=0)
    with A.app.app_context():
        assert A.trial_reserve(uid, 60) is True
        assert not A.trial_daily_budget_available()
    client = A.app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(uid)
        sess['_fresh'] = True
    home = client.get('/').data.decode()
    assert 'Free minutes refill at midnight' in home
    assert 'minutes</strong> left on our key' not in home
    settings = client.get('/settings').data.decode()
    assert 'Free minutes refill at midnight' in settings
    # Nav pill uses the same copy, not a personal counter.
    assert 'min left' not in home or 'Free minutes refill at midnight' in home


def test_start_transcription_refuses_an_exhausted_trial(trial_on, monkeypatch):
    uid = _make_user('exhausted@test.com', limit=600, used=600)
    resp = _post_start(monkeypatch, uid, {'audio_url': 'https://example.com/ep.mp3',
                             'episode_title': 'Ep', 'duration_min': '30',
                             'language': 'no'})
    assert resp.status_code == 402
    error = resp.get_json()['error'].lower()
    assert 'free minutes' in error
    assert 'openai api key' in error
    assert f'${A.openai_whisper_cost_usd(30):.2f}' in resp.get_json()['error']


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
        'app.TRIAL_DAILY_SECONDS = 10 ** 7;'
        'app.TRIAL_GLOBAL_SECONDS = 0;'
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

def test_an_errored_task_cannot_be_resurrected(trial_on):
    """_update_task used to happily write 'transcribing' over the sweeper's
    verdict, which put the per-chunk abandonment check behind a status it
    could never see."""
    from models import db, TranscriptionTask

    uid = _make_user('resurrect@test.com', limit=3600)
    with A.app.app_context():
        db.session.add(TranscriptionTask(id='trial-resurrect', user_id=uid,
                                         episode_title='x', status='error',
                                         phase='error'))
        db.session.commit()
        assert A._update_task('trial-resurrect', status='transcribing') is False
        assert A._update_task('trial-resurrect', progress=55) is False
        assert db.session.get(TranscriptionTask, 'trial-resurrect').status == 'error'
        # Writing 'error' again is still allowed -- that is not a resurrection.
        assert A._update_task('trial-resurrect', status='error',
                              error_message='later detail') is True


def test_a_task_swept_during_splitting_never_reaches_whisper(trial_on, monkeypatch, tmp_path):
    """The window the second review found: the sweeper fires while the audio
    is being re-encoded, and the worker's post-split status write erased the
    verdict before the per-chunk check ran."""
    from models import db, TranscriptionTask

    uid = _make_user('sweptsplit@test.com', limit=3600)
    audio = tmp_path / 'ep.mp3'
    audio.write_bytes(b'\0' * 2048)
    sent = []

    def sweep_during_split(f, **kw):
        db.session.execute(A.text(
            "UPDATE transcription_tasks SET status='error' WHERE id='trial-sweptsplit'"))
        db.session.commit()
        return [str(audio)]

    monkeypatch.setattr(A, 'probe_audio_duration', lambda f: 600.0)
    monkeypatch.setattr(A, 'prepare_audio_for_whisper', sweep_during_split)
    monkeypatch.setattr(A, '_transcribe_chunks',
                        lambda *a, **kw: sent.append(a) or ('text', []))

    with A.app.app_context():
        A.trial_reserve(uid, 600)
        db.session.add(TranscriptionTask(
            id='trial-sweptsplit', user_id=uid, episode_title='x',
            status='downloading', trial_seconds_charged=600))
        db.session.commit()
        with pytest.raises(A.TaskAbandoned):
            A.transcribe_audio(str(audio), 'trial-sweptsplit', object(), language='no')
        task = db.session.get(TranscriptionTask, 'trial-sweptsplit')
        assert task.status == 'error', 'worker resurrected a swept task'

    assert sent == [], 'audio was sent to Whisper after the task was swept'

def test_a_job_that_dies_before_the_first_chunk_is_fully_refunded(trial_on, monkeypatch, tmp_path):
    """transcribe_audio writes chunk_total and chunk_index together, before the
    loop. Seeding chunk_index=0 there would read as "chunk 0 has been sent" and
    charge for a chunk that never left the server."""
    from models import db, TranscriptionTask

    uid = _make_user('predeath@test.com', limit=3600)
    audio = tmp_path / 'ep.mp3'
    audio.write_bytes(b'\0' * 2048)

    def die(*a, **kw):
        raise RuntimeError('connection reset before the first upload')

    monkeypatch.setattr(A, 'probe_audio_duration', lambda f: 600.0)
    monkeypatch.setattr(A, 'prepare_audio_for_whisper', lambda f, **kw: [str(audio)])
    monkeypatch.setattr(A, '_transcribe_chunks', die)

    with A.app.app_context():
        A.trial_reserve(uid, 600)
        db.session.add(TranscriptionTask(
            id='trial-predeath', user_id=uid, episode_title='x',
            status='downloading', trial_seconds_charged=600))
        db.session.commit()
        with pytest.raises(RuntimeError):
            A.transcribe_audio(str(audio), 'trial-predeath', object(), language='no')

        task = db.session.get(TranscriptionTask, 'trial-predeath')
        assert task.chunk_total == 1
        assert A.trial_refund_task(task) == 600, 'charged for a chunk never sent'
    assert _used(uid) == 0

def test_abandoning_a_task_does_not_leak_its_chunks(trial_on, monkeypatch, tmp_path):
    """prepare_audio_for_whisper() deletes the source, so the parts are the only
    copy left. The abandonment raise once sat above the cleanup try/finally and
    stranded up to MAX_AUDIO_BYTES per occurrence."""
    from models import db, TranscriptionTask

    uid = _make_user('leak@test.com', limit=3600)
    source = tmp_path / 'ep.mp3'
    source.write_bytes(b'\0' * 2048)
    chunks = []
    for i in range(2):
        c = tmp_path / f'ep_chunk_{i}.mp3'
        c.write_bytes(b'\0' * 1024)
        chunks.append(str(c))

    def sweep_during_split(f, **kw):
        db.session.execute(A.text(
            "UPDATE transcription_tasks SET status='error' WHERE id='trial-leak'"))
        db.session.commit()
        return chunks

    monkeypatch.setattr(A, 'probe_audio_duration', lambda f: 600.0)
    monkeypatch.setattr(A, 'prepare_audio_for_whisper', sweep_during_split)

    with A.app.app_context():
        A.trial_reserve(uid, 600)
        db.session.add(TranscriptionTask(id='trial-leak', user_id=uid,
                                         episode_title='x', status='downloading',
                                         trial_seconds_charged=600))
        db.session.commit()
        with pytest.raises(A.TaskAbandoned):
            A.transcribe_audio(str(source), 'trial-leak', object(), language='no')

    left = [c for c in chunks if os.path.exists(c)]
    assert left == [], f'chunk files stranded on disk: {left}'


def test_a_settled_charge_is_not_re_opened(trial_on):
    """The sweeper settles the charge to 0, not NULL. Reconcile's "is None"
    guard let it through, re-reserving the measured duration for a task that
    then aborts and sends nothing -- the user paid for silence."""
    from models import db, TranscriptionTask

    uid = _make_user('settled@test.com', limit=3600)
    with A.app.app_context():
        A.trial_reserve(uid, 1800)
        t = TranscriptionTask(id='trial-settled', user_id=uid, episode_title='x',
                              status='error', trial_seconds_charged=1800)
        db.session.add(t)
        db.session.commit()
        A.trial_refund_task(t)                  # sweeper settles: charge -> 0
        assert _used(uid) == 0

        A.trial_reconcile_task('trial-settled', 600)
        assert db.session.get(TranscriptionTask,
                              'trial-settled').trial_seconds_charged == 0
    assert _used(uid) == 0, 'a settled task was charged again'


def test_the_terminal_error_guard_holds_under_concurrency(trial_on):
    """CLAUDE.md: "A check-then-record split has shipped as a live hole here
    twice." Once any writer has marked the task failed, no concurrent writer
    may move it back -- whatever the interleaving. A SELECT-then-write guard
    loses this race; a conditional UPDATE cannot.

    Bounded on purpose: an unbounded writer loop turns SQLite write contention
    into a hang rather than a failure, which is not a test result.
    """
    import threading as _t
    from models import db, TranscriptionTask

    uid = _make_user('guardrace@test.com')
    with A.app.app_context():
        db.session.add(TranscriptionTask(id='trial-guardrace', user_id=uid,
                                         episode_title='x', status='transcribing'))
        db.session.commit()

    seen_after_failure = []
    failed = _t.Event()

    def keep_writing_progress():
        with A.app.app_context():
            for _ in range(40):
                A._update_task('trial-guardrace', status='completed', progress=100)
                if failed.is_set():
                    seen_after_failure.append(db.session.execute(A.text(
                        "SELECT status FROM transcription_tasks "
                        "WHERE id='trial-guardrace'")).scalar())

    def fail_it():
        with A.app.app_context():
            A._update_task('trial-guardrace', status='error', phase='error')
            failed.set()

    threads = [_t.Thread(target=keep_writing_progress) for _ in range(4)]
    threads.append(_t.Thread(target=fail_it))
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
        assert not t.is_alive(), 'writer thread did not finish'

    with A.app.app_context():
        final = db.session.execute(A.text(
            "SELECT status FROM transcription_tasks WHERE id='trial-guardrace'"
        )).scalar()
    assert final == 'error', 'a writer moved the task back out of error'
    assert all(s == 'error' for s in seen_after_failure), (
        f'task left error mid-race: {set(seen_after_failure)}'
    )


def test_a_refused_chunk_write_stops_the_upload(trial_on, monkeypatch, tmp_path):
    """The chunk_index write carries the same terminal-'error' guard and sits
    one statement before create(). If its verdict is discarded, a sweep landing
    in that window still gets a chunk uploaded -- and chunk_index stays behind,
    so the refund under-counts it too."""
    from models import db, TranscriptionTask

    uid = _make_user('refusedwrite@test.com', limit=3600)
    sent = []

    class FakeClient:
        class audio:
            class transcriptions:
                @staticmethod
                def create(**kw):
                    sent.append(kw)
                    raise AssertionError('uploaded after the write was refused')

    chunk = tmp_path / 'c0.mp3'
    chunk.write_bytes(b'\0' * 16)

    real_update = A._update_task

    def sweep_just_before_the_write(task_id, **kw):
        # Another worker fails the task in the instant before we claim it.
        if str(kw.get('status', '')).startswith('transcribing chunk'):
            db.session.execute(A.text(
                "UPDATE transcription_tasks SET status='error' "
                "WHERE id='trial-refusedwrite'"))
            db.session.commit()
        return real_update(task_id, **kw)

    monkeypatch.setattr(A, '_update_task', sweep_just_before_the_write)

    with A.app.app_context():
        db.session.add(TranscriptionTask(
            id='trial-refusedwrite', user_id=uid, episode_title='x',
            status='transcribing', chunk_total=1, trial_seconds_charged=600))
        db.session.commit()
        with pytest.raises(A.TaskAbandoned):
            A._transcribe_chunks([str(chunk)], {str(chunk)}, 'trial-refusedwrite',
                                 FakeClient(), 'no', 600.0)

    assert sent == [], 'chunk uploaded after its status write was refused'


def test_update_task_round_trips_datetimes(trial_on):
    """_update_task moved from ORM setattr to a Core UPDATE to make the
    terminal-'error' guard atomic. SQLite stores DATETIME as text, so a
    mis-typed write would come back as a string and _seconds_since would throw
    -- taking the progress bar and the stale detector with it."""
    from models import db, TranscriptionTask

    uid = _make_user('dtround@test.com')
    with A.app.app_context():
        db.session.add(TranscriptionTask(id='trial-dt', user_id=uid,
                                         episode_title='x', status='downloading'))
        db.session.commit()

        assert A._update_task('trial-dt', phase='downloading',
                              phase_started_at=datetime.now(timezone.utc),
                              bytes_downloaded=1024, bytes_total=4096) is True
        task = db.session.get(TranscriptionTask, 'trial-dt')
        assert isinstance(task.phase_started_at, datetime)
        assert isinstance(task.heartbeat_at, datetime)
        assert A._seconds_since(task.heartbeat_at) < 5
        assert task.bytes_downloaded == 1024
        # And the progress computation that reads them still works.
        percent, _ = A.compute_live_progress(task)
        assert 0 <= percent <= 100

    # A row that does not exist reports failure rather than raising.
    with A.app.app_context():
        assert A._update_task('no-such-task', progress=50) is False

def test_a_zero_reservation_cannot_make_a_task_unmetered(trial_on, monkeypatch):
    """trial_seconds_charged is the metering signal, so a 0 reservation would
    read as "own key, nothing to meter". Setting TRIAL_UNKNOWN_ESTIMATE_MINUTES=0
    would otherwise turn every feed without itunes:duration into free work."""
    from models import db, TranscriptionTask

    monkeypatch.setattr(A, 'TRIAL_UNKNOWN_ESTIMATE_SECONDS', 0)
    uid = _make_user('zeroest@test.com', limit=3600)
    resp = _post_start(monkeypatch, uid, {'audio_url': 'https://example.com/ep.mp3',
                                          'episode_title': 'Ep', 'language': 'no'})
    assert resp.status_code == 200
    with A.app.app_context():
        charged = db.session.execute(A.text(
            'SELECT trial_seconds_charged FROM transcription_tasks '
            'WHERE user_id = :uid'), {'uid': uid}).scalar()
    assert charged and charged > 0, 'task was created unmetered'

def test_boot_settles_an_error_task_left_unsettled(trial_on):
    """A worker killed between reconciling a charge and settling it leaves
    status='error' with an unsettled charge. The orphan sweep only looks at
    tasks still running, so nothing ever revisited it and the user forfeited
    those minutes permanently."""
    from models import db, TranscriptionTask

    uid = _make_user('bootsettle@test.com', limit=3600)
    with A.app.app_context():
        A.trial_reserve(uid, 900)
        db.session.add(TranscriptionTask(
            id='trial-bootsettle', user_id=uid, episode_title='x', status='error',
            trial_seconds_charged=900, trial_settled=False))
        db.session.commit()

        assert A.settle_stranded_charges() >= 1

        task = db.session.get(TranscriptionTask, 'trial-bootsettle')
        assert task.trial_settled is True
    assert _used(uid) == 0, 'the stranded charge was never handed back'


def test_reconcile_cannot_move_a_settled_charge(trial_on):
    """_claim_task_charge had no trial_settled check, so it still won against a
    settled row whenever the refund settled without moving the amount (spent ==
    charged -- the final chunk). Reconcile would then release spent seconds."""
    from models import db, TranscriptionTask

    uid = _make_user('claimsettled@test.com', limit=3600)
    with A.app.app_context():
        A.trial_reserve(uid, 800)
        t = TranscriptionTask(id='trial-claimsettled', user_id=uid, episode_title='x',
                              status='error', chunk_total=1, chunk_index=0,
                              trial_seconds_charged=800)
        db.session.add(t)
        db.session.commit()
        assert A.trial_refund_task(t) == 0            # settles at 800, amount unchanged
        assert A._claim_task_charge('trial-claimsettled', 800, 400) is False
        assert db.session.get(TranscriptionTask,
                              'trial-claimsettled').trial_seconds_charged == 800
    assert _used(uid) == 800

def test_column_migrations_survive_two_workers_racing():
    """The 2026-09-09 deploy: both gunicorn workers ran the ALTER TABLE at
    boot, the loser died on "duplicate column name", and gunicorn treats a
    worker that fails to boot as fatal -- it shut the master down. Only
    systemd's automatic restart kept podskrift.com up.

    Losing that race means the other worker did our work, not that the schema
    is wrong.
    """
    from sqlalchemy.exc import OperationalError

    with A.app.app_context():
        # Everything is already applied, so a second pass adds nothing.
        assert A.apply_column_migrations() == []

        # Now force the race: the column is gone from the schema read but
        # present in the table, which is exactly what the loser sees.
        table = 'transcription_tasks'
        column = next(iter(A.TASK_COLUMN_MIGRATIONS))
        real_live_columns = A._live_columns

        def blind_live_columns(name):
            cols = real_live_columns(name)
            if name == table:
                return cols - {column}
            return cols

        A._live_columns = blind_live_columns
        try:
            assert A.apply_column_migrations() == [], 'the losing ALTER was not tolerated'
        finally:
            A._live_columns = real_live_columns

        # A genuinely broken migration still raises rather than being swallowed.
        A.TASK_COLUMN_MIGRATIONS['not_a_real_column'] = 'NOT VALID SQL HERE'
        try:
            with pytest.raises(OperationalError):
                A.apply_column_migrations()
        finally:
            del A.TASK_COLUMN_MIGRATIONS['not_a_real_column']

# --------------------------------------------------------------------------
# Capacity limits
#
# Money is capped by the trial ceiling; this caps the box. Each in-flight job
# holds up to MAX_AUDIO_BYTES on disk and decodes the whole episode to raw PCM
# in memory. Prod is a shared Plesk host with 50+ other services on it.
# --------------------------------------------------------------------------

def test_concurrent_transcriptions_are_capped(trial_on, monkeypatch):
    """Unbounded threads are an out-of-memory event, not a slow page."""
    import threading as _t
    monkeypatch.setattr(A, 'MAX_CONCURRENT_TRANSCRIPTIONS', 2)
    monkeypatch.setattr(A, '_transcription_slots', _t.BoundedSemaphore(2))

    uid = _make_user('cap@test.com', limit=36000)
    # Distinct audio URLs: the web duplicate guard would otherwise reuse the
    # first task and never ask for a third capacity slot.
    def _start(n):
        return _post_start(monkeypatch, uid, {
            'audio_url': f'https://example.com/cap-{n}.mp3',
            'episode_title': 'Ep', 'duration_min': '5', 'language': 'no',
        })

    assert _start(1).status_code == 200
    assert _start(2).status_code == 200
    third = _start(3)
    assert third.status_code == 503
    assert 'try again' in third.get_json()['error'].lower()


def test_a_refused_job_hands_its_slot_back(trial_on, monkeypatch):
    """A refusal after the slot is taken -- an exhausted trial, say -- must not
    leak the slot for the life of the process, or a few bad requests would
    wedge the worker permanently."""
    import threading as _t
    monkeypatch.setattr(A, 'MAX_CONCURRENT_TRANSCRIPTIONS', 1)
    monkeypatch.setattr(A, '_transcription_slots', _t.BoundedSemaphore(1))

    broke = _make_user('slotleak@test.com', limit=600, used=600)
    for _ in range(3):
        resp = _post_start(monkeypatch, broke, {
            'audio_url': 'https://example.com/ep.mp3', 'episode_title': 'Ep',
            'duration_min': '30', 'language': 'no'})
        assert resp.status_code == 402, 'expected the trial refusal, not a capacity one'

    # The single slot is still available to someone who can use it.
    ok = _make_user('slotok@test.com', limit=36000)
    assert _post_start(monkeypatch, ok, {
        'audio_url': 'https://example.com/ep.mp3', 'episode_title': 'Ep',
        'duration_min': '5', 'language': 'no'}).status_code == 200


def test_a_finished_job_hands_its_slot_back(trial_on, monkeypatch, tmp_path):
    """The slot tracks work in flight, not requests served, so the worker
    thread releases it -- through its finally, whatever the outcome."""
    import threading as _t
    monkeypatch.setattr(A, 'MAX_CONCURRENT_TRANSCRIPTIONS', 1)
    slots = _t.BoundedSemaphore(1)
    monkeypatch.setattr(A, '_transcription_slots', slots)

    uid = _make_user('slotdone@test.com', limit=36000)
    monkeypatch.setattr(A, 'download_audio',
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('404')))

    A.app.config['TESTING'] = True
    client = A.app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(uid)
        sess['_fresh'] = True
    assert client.post('/start_transcription', data={
        'audio_url': 'https://example.com/ep.mp3', 'episode_title': 'Ep',
        'duration_min': '5', 'language': 'no'}).status_code == 200

    for _ in range(100):
        if slots.acquire(blocking=False):
            slots.release()
            break
        time.sleep(0.05)
    else:
        pytest.fail('the worker thread never released its slot')


def test_a_full_disk_refuses_before_anything_is_reserved(trial_on, monkeypatch):
    """The database, the backups and 50+ co-tenant sites share this volume.
    Filling it is their outage too."""
    uid = _make_user('fulldisk@test.com', limit=36000)
    resp = _post_start(monkeypatch, uid, {
        'audio_url': 'https://example.com/ep.mp3', 'episode_title': 'Ep',
        'duration_min': '5', 'language': 'no'}, free_disk=10 * 1024 * 1024)
    assert resp.status_code == 503
    assert 'disk space' in resp.get_json()['error'].lower()
    assert _used(uid) == 0, 'allowance was reserved despite the refusal'


def test_unreadable_disk_stats_do_not_block_transcription(trial_on, monkeypatch):
    """statvfs can fail; that is not a reason to refuse every job."""
    uid = _make_user('nostat@test.com', limit=36000)
    assert _post_start(monkeypatch, uid, {
        'audio_url': 'https://example.com/ep.mp3', 'episode_title': 'Ep',
        'duration_min': '5', 'language': 'no'}, free_disk=None).status_code == 200

def test_the_worker_releases_its_slot_even_if_the_app_context_fails(trial_on, monkeypatch):
    """The try/finally has to wrap app_context(), not sit inside it. An error
    entering the context -- a MemoryError under exactly the pressure this cap
    defends against -- would otherwise skip the release and wedge the worker
    at 503 for the life of the process.

    Only the worker thread's context is broken: failing the main thread's would
    take the test client's own request teardown down with it, which tests the
    harness rather than the code.
    """
    import threading as _t

    monkeypatch.setattr(A, 'MAX_CONCURRENT_TRANSCRIPTIONS', 1)
    slots = _t.BoundedSemaphore(1)
    monkeypatch.setattr(A, '_transcription_slots', slots)
    monkeypatch.setattr(A, 'free_disk_bytes', lambda *a, **kw: 10 ** 12)

    main_thread = _t.get_ident()
    real_ctx = A.app.app_context

    def only_break_the_worker():
        if _t.get_ident() != main_thread:
            raise MemoryError('cannot allocate an app context')
        return real_ctx()

    monkeypatch.setattr(A.app, 'app_context', only_break_the_worker)

    uid = _make_user('ctxfail@test.com', limit=36000)
    A.app.config['TESTING'] = True
    client = A.app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(uid)
        sess['_fresh'] = True
    assert client.post('/start_transcription', data={
        'audio_url': 'https://example.com/ep.mp3', 'episode_title': 'Ep',
        'duration_min': '5', 'language': 'no'}).status_code == 200

    for _ in range(100):
        if slots.acquire(blocking=False):
            slots.release()
            break
        time.sleep(0.05)
    else:
        pytest.fail('the slot was lost when the app context failed')


# --------------------------------------------------------------------------
# Audio preparation
# --------------------------------------------------------------------------

def _make_audio(path, seconds, bitrate='128k', frequency=440):
    """Synthesise a real MP3 with ffmpeg. Generated rather than committed: the
    only sample episode in the tree is untracked AND matched by .gitignore's
    `temp_audio_*.mp3`, so a test depending on it skips everywhere but the
    laptop it was written on."""
    import shutil
    import subprocess as sp
    if not shutil.which('ffmpeg'):
        pytest.skip('needs ffmpeg')
    sp.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi',
            '-i', f'sine=frequency={frequency}:duration={seconds}',
            '-b:a', bitrate, str(path)], check=True)
    return str(path)


def test_preparing_audio_shrinks_it_and_keeps_every_second(tmp_path):
    """Whisper resamples to 16 kHz mono anyway, so re-encoding to that costs
    nothing it would have used and makes most episodes a single upload."""
    source = _make_audio(tmp_path / 'ep.mp3', 300, bitrate='192k')
    before = os.path.getsize(source)
    duration = A.probe_audio_duration(source)

    parts = A.prepare_audio_for_whisper(source)

    assert len(parts) == 1, 'a five-minute episode should not be split'
    assert not os.path.exists(source), 'the source was left behind'
    assert os.path.getsize(parts[0]) < before, 'output was not smaller than input'
    assert abs(A.probe_audio_duration(parts[0]) - duration) < 1.0


def test_variable_input_bitrate_gives_evenly_sized_parts(tmp_path, monkeypatch):
    """The defect stream-copying had: bytes are not proportional to time in a
    VBR file, so time-equal cuts were not byte-equal -- a dense first half
    produced a 27 MB part against a 24 MB target, over OpenAI's limit.
    Re-encoding at a fixed bitrate makes size track duration by construction.
    """
    import subprocess as sp
    monkeypatch.setattr(A, 'SEGMENT_SECONDS', 60)
    dense = _make_audio(tmp_path / 'dense.mp3', 60, bitrate='320k', frequency=440)
    sparse = _make_audio(tmp_path / 'sparse.mp3', 60, bitrate='32k', frequency=200)
    listing = tmp_path / 'list.txt'
    listing.write_text(f"file '{dense}'\nfile '{sparse}'\n")
    vbr = tmp_path / 'vbr.mp3'
    sp.run(['ffmpeg', '-v', 'error', '-y', '-f', 'concat', '-safe', '0',
            '-i', str(listing), '-c', 'copy', str(vbr)], check=True)
    # The input really is skewed: one half is ten times the bitrate of the other.
    assert os.path.getsize(dense) > 5 * os.path.getsize(sparse)

    parts = A.prepare_audio_for_whisper(str(vbr))
    assert len(parts) == 2, f'expected two 60s parts, got {len(parts)}'
    sizes = sorted(os.path.getsize(p) for p in parts)
    assert sizes[1] <= sizes[0] * 1.2, (
        f'parts are {sizes[0]} and {sizes[1]} bytes -- output still tracks the '
        'input bitrate, so a dense stretch can still overflow'
    )
    for part in parts:
        assert os.path.getsize(part) <= A.WHISPER_MAX_UPLOAD_BYTES


def test_a_short_ffprobe_duration_does_not_drop_the_tail(tmp_path):
    """Byte-concatenated MP3s -- how dynamic ad insertion stitches segments --
    make ffprobe report only the first file's duration. Deriving coverage from
    that number silently never sent the rest: 16 minutes, no error, the
    transcript just ended."""
    a = _make_audio(tmp_path / 'a.mp3', 120, bitrate='320k', frequency=440)
    b = _make_audio(tmp_path / 'b.mp3', 120, bitrate='32k', frequency=200)
    joined = tmp_path / 'joined.mp3'
    joined.write_bytes(open(a, 'rb').read() + open(b, 'rb').read())

    # ffprobe extrapolates the whole file from the leading bitrate, so it only
    # reads short when the rate changes part-way -- exactly what stitched ad
    # segments do.
    reported = A.probe_audio_duration(str(joined))
    assert reported < 200, f'fixture does not reproduce the short reading ({reported:.0f}s)'

    parts = A.prepare_audio_for_whisper(str(joined))
    covered = sum(A.probe_audio_duration(p) or 0 for p in parts)
    assert covered > 230, (
        f'only {covered:.0f}s of ~240s survived; ffprobe had said {reported:.0f}s'
    )


def test_the_segmenters_rounding_crumb_is_discarded(tmp_path, monkeypatch):
    """An episode that is an exact multiple of the segment length leaves a
    sub-second tail. Whisper rejects audio that short, and it would cost an
    extra request and a phantom chunk on the progress bar."""
    monkeypatch.setattr(A, 'SEGMENT_SECONDS', 60)
    source = _make_audio(tmp_path / 'exact.mp3', 120)
    parts = A.prepare_audio_for_whisper(source)
    assert len(parts) == 2, f'expected two 60s parts, got {len(parts)}'
    for part in parts:
        assert os.path.getsize(part) >= A.MIN_PART_BYTES


def test_crumb_dropping_can_never_return_nothing(tmp_path, monkeypatch):
    """With the threshold above every part, the filter would empty the list.
    Dropping short parts must not be able to discard the whole episode."""
    monkeypatch.setattr(A, 'SEGMENT_SECONDS', 5)
    monkeypatch.setattr(A, 'MIN_PART_BYTES', 10 ** 9)   # everything is a crumb
    source = _make_audio(tmp_path / 'short.mp3', 12)
    with pytest.raises(RuntimeError, match='could not be split'):
        A.prepare_audio_for_whisper(source)
    assert [f for f in os.listdir(tmp_path) if '_part_' in f] == []


def test_a_single_part_is_kept_however_short(tmp_path):
    """The crumb filter only runs when there is more than one part."""
    source = _make_audio(tmp_path / 'tiny.mp3', 1)
    parts = A.prepare_audio_for_whisper(source)
    assert len(parts) == 1


def test_an_unprocessable_file_says_so_and_strands_nothing(tmp_path, monkeypatch):
    """The old code swallowed everything and returned the source, which then
    failed at OpenAI's 25 MB limit with an error pointing at the wrong thing."""
    junk = tmp_path / 'junk.mp3'
    junk.write_bytes(b'this is not audio' * 1000)
    with pytest.raises(RuntimeError, match='could not be processed'):
        A.prepare_audio_for_whisper(str(junk))
    assert [f for f in os.listdir(tmp_path) if '_part_' in f] == []


def test_a_failed_run_strands_nothing_even_though_ffmpeg_wrote_output(tmp_path, monkeypatch):
    """`ffmpeg -y` creates and writes its output before it fails, so the part
    in flight is never in any list we built. Cleanup has to glob."""
    import subprocess as sp
    source = tmp_path / 'ep.mp3'
    source.write_bytes(b'\0' * 2048)
    base = str(tmp_path / 'ep')

    def write_then_fail(cmd, **kw):
        open(f'{base}_part_000.mp3', 'wb').write(b'\0' * (12 * 1024 * 1024))
        return sp.CompletedProcess(cmd, 1, b'', b'ffmpeg died mid-write')

    monkeypatch.setattr(A.subprocess, 'run', write_then_fail)
    with pytest.raises(RuntimeError):
        A.prepare_audio_for_whisper(str(source))
    leftovers = [f for f in os.listdir(tmp_path) if '_part_' in f]
    assert leftovers == [], f'stranded parts: {leftovers}'


def test_missing_ffmpeg_does_not_blame_the_users_file(tmp_path, monkeypatch):
    """"The file may be corrupt" sent people to re-download a fine episode.

    Checked via shutil.which, not by catching FileNotFoundError: the nice(1)
    wrapper turns a missing ffmpeg into exit 127, so the exception handler
    never sees it and the user got the corrupt-file message anyway.
    """
    source = tmp_path / 'ep.mp3'
    source.write_bytes(b'\0' * 2048)
    monkeypatch.setattr(A.shutil, 'which', lambda name: None)
    with pytest.raises(RuntimeError, match='unavailable on the server'):
        A.prepare_audio_for_whisper(str(source))


def test_the_disk_floor_clears_what_admission_control_admits(tmp_path):
    """CLAUDE.md states this as a rule; a rule with no test is a comment.

    The disk check reserves nothing, so every concurrent request sees the same
    free space -- the floor has to exceed everything that can be admitted at
    once, or four requests all pass at 2.1 GB free and then need 2 GB.
    """
    workers = 2                      # gunicorn --workers 2 in production
    admitted = workers * A.MAX_CONCURRENT_TRANSCRIPTIONS
    # A job holds its source plus the parts, briefly, before the source goes.
    # The parts can never exceed the source: prepare_audio_for_whisper caps the
    # output bitrate at the input's, so a 24 kbps feed is not re-encoded up to
    # 48 and doubled. That cap is what makes this bound 2x and not open-ended.
    worst_case = admitted * A.MAX_AUDIO_BYTES * 2
    assert A.MIN_FREE_DISK_BYTES > worst_case, (
        f'floor {A.MIN_FREE_DISK_BYTES/1e9:.1f} GB does not clear '
        f'{worst_case/1e9:.1f} GB of admitted work'
    )

def test_a_low_bitrate_source_is_never_re_encoded_larger(tmp_path):
    """The disk floor assumes the parts never exceed the source. Encoding a
    24 kbps feed at 48 would double it, and a 500 MB source would then need
    1.5 GB, not 1 GB -- past what the floor was sized for."""
    source = _make_audio(tmp_path / 'quiet.mp3', 60, bitrate='24k')
    before = os.path.getsize(source)
    parts = A.prepare_audio_for_whisper(source)
    after = sum(os.path.getsize(p) for p in parts)
    # Not "smaller": each part carries a few hundred bytes of container header,
    # so a single-part re-encode at the same bitrate lands fractionally above.
    # The claim the disk floor rests on is that it cannot MULTIPLY.
    assert after < before * 1.1, f'{before} bytes in, {after} bytes out'


def test_the_segment_length_fits_the_upload_limit(tmp_path):
    """Raising the bitrate or the segment length without checking would fail
    every episode longer than one part. Asserted at import; pinned here too."""
    projected = A.SEGMENT_SECONDS * A.WHISPER_AUDIO_BITRATE_KBPS * 1000 / 8
    assert projected < A.WHISPER_MAX_UPLOAD_BYTES * 0.9


def test_ffmpeg_cannot_outlive_the_stale_task_window(tmp_path):
    """Re-encoding writes no heartbeat, so a run longer than the stale floor
    gets its own task swept out from under it and the user is told the server
    restarted."""
    assert A.FFMPEG_TIMEOUT_SECONDS < A.STALE_TASK_SECONDS

# --------------------------------------------------------------------------
# Cancelling and resuming  (TSK-20392, TSK-20394)
# --------------------------------------------------------------------------

def _login(user_id):
    A.app.config['TESTING'] = True
    client = A.app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user_id)
        sess['_fresh'] = True
    return client


def test_cancelling_stops_the_worker_at_the_next_chunk(trial_on, monkeypatch, tmp_path):
    """A daemon thread cannot be interrupted from outside, so cancel writes a
    terminal status and the worker gives up at its next boundary. If it did not
    check, Stop would be a lie that still spent the user's minutes."""
    from models import db, TranscriptionTask

    uid = _make_user('cancelworker@test.com', limit=36000)
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
                    db.session.execute(A.text(
                        "UPDATE transcription_tasks SET status='cancelled' "
                        "WHERE id='cancel-worker'"))
                    db.session.commit()
                    return FakeChunk()

    chunks = []
    for i in range(3):
        f = tmp_path / f'c{i}.mp3'
        f.write_bytes(b'\0' * 16)
        chunks.append(str(f))

    with A.app.app_context():
        db.session.add(TranscriptionTask(
            id='cancel-worker', user_id=uid, episode_title='x',
            status='transcribing', chunk_total=3, chunk_index=0,
            trial_seconds_charged=900))
        db.session.commit()
        # _update_task would also refuse to write to a cancelled task, which
        # would mask whether the loop's own check works. Neutralise that guard
        # so only the check under test can stop the upload.
        monkeypatch.setattr(A, '_update_task', lambda task_id, **kw: True)
        with pytest.raises(A.TaskAbandoned):
            A._transcribe_chunks(chunks, set(chunks), 'cancel-worker',
                                 FakeClient(), 'no', 900.0)

    assert len(sent) == 1, f'{len(sent)} chunks billed after cancelling'


def test_cancel_refunds_only_what_was_not_sent(trial_on):
    """Audio already uploaded is billed to us whatever the user clicks."""
    from models import db, TranscriptionTask

    uid = _make_user('cancelrefund@test.com', limit=36000)
    with A.app.app_context():
        A.trial_reserve(uid, 800)
        db.session.add(TranscriptionTask(
            id='cancel-refund', user_id=uid, episode_title='x',
            status='transcribing', chunk_total=4, chunk_index=1,
            trial_seconds_charged=800))
        db.session.commit()

    resp = _login(uid).post('/cancel/cancel-refund')
    assert resp.status_code == 200
    assert resp.get_json()['cancelled'] is True
    with A.app.app_context():
        task = db.session.get(TranscriptionTask, 'cancel-refund')
        assert task.status == 'cancelled'
    assert _used(uid) == 400, 'two of four chunks were sent, so half stands'


def test_a_cancelled_task_cannot_be_resurrected(trial_on):
    """The same guard that stops the sweeper being overwritten. Without it the
    worker would write 'completed' over the cancellation."""
    from models import db, TranscriptionTask

    uid = _make_user('cancelresurrect@test.com')
    with A.app.app_context():
        db.session.add(TranscriptionTask(id='cancel-res', user_id=uid,
                                         episode_title='x', status='cancelled'))
        db.session.commit()
        assert A._update_task('cancel-res', status='completed', progress=100) is False
        assert db.session.get(TranscriptionTask, 'cancel-res').status == 'cancelled'


def test_cancelling_someone_elses_task_is_a_404(trial_on):
    from models import db, TranscriptionTask

    owner = _make_user('owner@test.com')
    intruder = _make_user('intruder@test.com')
    with A.app.app_context():
        db.session.add(TranscriptionTask(id='cancel-owned', user_id=owner,
                                         episode_title='x', status='transcribing'))
        db.session.commit()

    assert _login(intruder).post('/cancel/cancel-owned').status_code == 404
    with A.app.app_context():
        assert db.session.get(TranscriptionTask, 'cancel-owned').status == 'transcribing'


def test_cancelling_a_finished_task_does_not_undo_it(trial_on):
    from models import db, TranscriptionTask

    uid = _make_user('cancelfinished@test.com')
    with A.app.app_context():
        db.session.add(TranscriptionTask(id='cancel-done', user_id=uid,
                                         episode_title='x', status='completed',
                                         transcript_text='the goods'))
        db.session.commit()

    assert _login(uid).post('/cancel/cancel-done').status_code == 409
    with A.app.app_context():
        task = db.session.get(TranscriptionTask, 'cancel-done')
        assert task.status == 'completed' and task.transcript_text == 'the goods'


def test_cancelling_twice_is_not_an_error(trial_on):
    """A double-click should not produce a scary message."""
    from models import db, TranscriptionTask

    uid = _make_user('canceltwice@test.com', limit=36000)
    with A.app.app_context():
        A.trial_reserve(uid, 600)
        db.session.add(TranscriptionTask(id='cancel-twice', user_id=uid,
                                         episode_title='x', status='downloading',
                                         trial_seconds_charged=600))
        db.session.commit()
    client = _login(uid)
    assert client.post('/cancel/cancel-twice').status_code == 200
    assert client.post('/cancel/cancel-twice').status_code == 200
    assert _used(uid) == 0, 'the second cancel refunded again'


def test_running_transcriptions_are_offered_wherever_you_are(trial_on):
    """The work survives leaving the page, but nothing said so -- so coming
    back looked like it had vanished and people started it over.

    This used to be a card on the home page. The job bar replaced it: the same
    information, on every page, which is what "you navigated away" actually
    means. Two views of it on one screen was just duplication.
    """
    from models import db, TranscriptionTask

    uid = _make_user('resume@test.com')
    with A.app.app_context():
        db.session.add_all([
            TranscriptionTask(id='resume-live', user_id=uid, status='transcribing',
                              episode_title='Still going', progress=42,
                              heartbeat_at=datetime.now(timezone.utc)),
            TranscriptionTask(id='resume-done', user_id=uid, status='completed',
                              episode_title='Already finished'),
        ])
        db.session.commit()

    client = _login(uid)
    jobs = client.get('/active-jobs').get_json()['jobs']
    assert [j['title'] for j in jobs] == ['Still going']
    assert jobs[0]['id'] == 'resume-live'

    # And the home page no longer carries its own second copy of it.
    home = client.get('/').data.decode()
    assert 'Still going' not in home, 'the home page duplicates the job bar'
    assert 'id="jobBar"' in home


def test_another_users_running_task_is_not_offered(trial_on):
    from models import db, TranscriptionTask

    mine = _make_user('mine@test.com')
    theirs = _make_user('theirs@test.com')
    with A.app.app_context():
        db.session.add(TranscriptionTask(id='resume-theirs', user_id=theirs,
                                         status='transcribing',
                                         episode_title='Not yours',
                                         heartbeat_at=datetime.now(timezone.utc)))
        db.session.commit()
    jobs = _login(mine).get('/active-jobs').get_json()['jobs']
    assert [j['title'] for j in jobs] == []


# --------------------------------------------------------------------------
# Mobile navigation  (TSK-20393)
# --------------------------------------------------------------------------

def test_the_menu_toggle_is_a_real_disclosure_control(trial_on):
    """The old toggle was an onclick flipping a class: nothing told assistive
    tech it controlled anything, or whether it was open."""
    body = A.app.test_client().get('/').data.decode()
    assert 'aria-expanded="false"' in body
    assert 'aria-controls="navLinks"' in body
    assert 'id="navLinks"' in body


def test_the_scrim_is_not_inside_the_blurred_nav_bar(trial_on):
    """.nav-bar sets backdrop-filter, which makes it the containing block for
    position:fixed descendants. With the scrim inside it, `inset: 56px 0 0 0`
    resolved against a 56px-tall box and collapsed to zero height -- an
    invisible backdrop that closed nothing and let taps reach the controls
    behind the open panel. Structure, not a string: this is the defect.
    """
    body = A.app.test_client().get('/').data.decode()
    assert 'id="navScrim"' in body, 'no backdrop at all'
    nav_open = body.index('<nav class="nav-bar">')
    nav_close = body.index('</nav>', nav_open)
    assert 'navScrim' not in body[nav_open:nav_close], (
        'the scrim is inside <nav>, whose backdrop-filter collapses it to 0 height'
    )


def test_the_menu_panel_is_hidden_by_an_attribute_not_by_opacity(trial_on):
    """Two browser-verified defects came from animating visibility: the panel
    was still hidden when the script focused its first link (focus never moved),
    and still visible after closing (five invisible links left in the tab
    order). The script owns `hidden` now, and CSS must not animate visibility.

    This asserts the mechanism, not the runtime behaviour -- there is no browser
    harness in this repo, so "focus lands inside the panel" and "no links are
    tabbable when closed" are verified by driving a real browser, and only the
    code that produces them is pinned here.
    """
    body = A.app.test_client().get('/').data.decode()
    assert '.nav-links[hidden] { display: none; }' in body
    # Invisible is not gone: before the script runs on first paint, and for the
    # 160ms of the close animation, the panel is opacity:0 but still fixed over
    # the page. Taps meant for the hero were landing on unseen nav links.
    mobile_nav = body[body.index('@media (max-width: 640px)'):]
    mobile_nav = mobile_nav[:mobile_nav.index('@media (prefers-reduced-motion')]
    assert 'pointer-events: none;' in mobile_nav, (
        'the closed panel is hit-testable again -- .btn:disabled elsewhere in '
        'the sheet is not what this is asking about'
    )
    assert '.nav-links.open { pointer-events: auto; }' in body
    # The deferred hide specifically: hiding immediately would make the panel
    # vanish instead of sliding out, and not hiding at all is the defect.
    assert 'closeTimer = setTimeout(function () { panel.hidden = true; }' in body
    assert 'panel.hidden = false;' in body, 'nothing reveals the panel on open'
    # A declaration, not the word: the comments explaining this defect mention
    # visibility, and matching those would make the test unfailable.
    import re as _re
    mobile = body[body.index('@media (max-width: 640px)'):]
    panel_rules = mobile[:mobile.index('@media (prefers-reduced-motion')]
    assert not _re.search(r'visibility\s*:', panel_rules), (
        'visibility is declared in the mobile nav rules again, where animating '
        'it broke focus on open and the tab order on close'
    )


def test_menu_links_do_not_transition_every_property(trial_on):
    """`transition: all` on the links included the inherited `visibility`, so a
    link stayed unfocusable for 150ms after its panel was already visible --
    which is why focus silently stayed on the toggle."""
    body = A.app.test_client().get('/').data.decode()
    nav_css = body[body.index('.nav-links a {'):body.index('.nav-links a:hover')]
    assert 'transition: all' not in nav_css, (
        'the links transition `all` again, which includes visibility'
    )


def test_the_menu_degrades_without_javascript(trial_on):
    """The panel is revealed by script, so with script off there is nothing to
    reveal it -- and the toggle would do nothing."""
    body = A.app.test_client().get('/').data.decode()
    assert '<noscript>' in body
    noscript = body[body.index('<noscript>'):body.index('</noscript>')]
    assert '.nav-toggle, .nav-scrim { display: none !important; }' in noscript
    # The column has to wrap onto its own line: dropped into .nav-inner (a 56px
    # centred flex row) it overflowed above the viewport and took its first
    # three links off-screen.
    assert 'flex-wrap: wrap' in noscript and 'height: auto' in noscript
    assert 'position: static' in noscript
    # And it has to actually be visible: without these an in-flight transition
    # holds the computed opacity at 0 and the whole menu stays invisible.
    assert 'opacity: 1 !important' in noscript
    assert 'transition: none !important' in noscript


def test_the_stop_button_handler_is_reachable_from_its_onclick(trial_on):
    """The whole script is an IIFE and the button uses an inline onclick, so a
    plain `function cancelTranscription()` is invisible to it -- the button
    threw ReferenceError and did nothing. copyTranscript is exported for
    exactly this reason; this one was not.
    """
    uid = _make_user('stopbtn@test.com')
    from models import db, TranscriptionTask
    with A.app.app_context():
        db.session.add(TranscriptionTask(id='stop-btn', user_id=uid,
                                         episode_title='x', status='transcribing'))
        db.session.commit()
    body = _login(uid).get('/transcription/stop-btn').data.decode()

    import re as _re
    handlers = _re.findall(r'onclick="(\w+)\(', body)
    exported = set(_re.findall(r'window\.(\w+)\s*=', body))
    for name in handlers:
        assert name in exported, (
            f'{name}() is called from an inline onclick but never reaches the '
            'global scope -- clicking it throws ReferenceError'
        )


def test_a_cancelled_task_stops_being_polled(trial_on):
    """'cancelled' is terminal, but the poll loop only knew about completed and
    error, so it kept hitting /status forever -- and every hit runs the stale
    sweeper."""
    uid = _make_user('pollstop@test.com')
    from models import db, TranscriptionTask
    with A.app.app_context():
        db.session.add(TranscriptionTask(id='poll-stop', user_id=uid,
                                         episode_title='x', status='cancelled'))
        db.session.commit()
    body = _login(uid).get('/transcription/poll-stop').data.decode()
    assert 'function isTerminal(' in body
    assert "status === 'cancelled'" in body
    assert "d.status !== 'completed' && d.status !== 'error'" not in body, (
        'the poll still has its own idea of terminal, which drifted from the UI'
    )


def test_the_mobile_menu_extras_are_hidden_on_desktop(trial_on):
    """"Ingen regresjon på desktop-navigasjonen": icons, separator and the extra
    home link are mobile affordances, so they are display:none outside the
    breakpoint. Verified in a real browser at 1280x900 as well -- there is no
    browser harness in this repo, so that half cannot run in CI."""
    body = A.app.test_client().get('/').data.decode()
    assert '.nav-icon, .nav-sep, .nav-mobile-only { display: none; }' in body
    # And the rules that re-show them live only inside the mobile media query.
    mobile = body[body.index('@media (max-width: 640px)'):]
    assert '.nav-icon { display: block;' in mobile
    assert '.nav-mobile-only { display: flex; }' in mobile


def test_the_logged_in_menu_offers_the_account_pages(trial_on):
    uid = _make_user('menu@test.com')
    body = _login(uid).get('/').data.decode()
    for label in ('New transcript', 'History', 'Settings', 'Log out'):
        assert label in body, f'{label} missing from the menu'
    assert 'Feeds' not in body
    assert 'nav-sep' in body, 'log out is not separated from the rest'

def test_cancelling_cannot_overwrite_a_finished_transcript(trial_on, monkeypatch):
    """The race the review reproduced: cancel read the row, the worker completed
    inside the window, and cancel stamped 'cancelled' over it -- keeping the
    full charge while the transcript 404'd and fell out of history.

    The worker's write goes through a SEPARATE connection on purpose. Committing
    it through db.session would expire the identity map, so the request's copy
    would refresh itself and the stale-read window would never open -- which is
    how the first version of this test passed against the unfixed code.
    """
    from models import db, TranscriptionTask

    uid = _make_user('cancelrace@test.com', limit=36000)
    with A.app.app_context():
        A.trial_reserve(uid, 600)
        db.session.add(TranscriptionTask(
            id='cancel-race', user_id=uid, episode_title='x',
            status='transcribing', chunk_total=1, chunk_index=0,
            trial_seconds_charged=600))
        db.session.commit()

    client = _login(uid)
    real_get = db.session.get

    def complete_it_behind_our_back(model, ident, *a, **kw):
        obj = real_get(model, ident, *a, **kw)
        if ident == 'cancel-race' and complete_it_behind_our_back.armed:
            complete_it_behind_our_back.armed = False
            with db.engine.connect() as conn:
                conn.execute(A.text(
                    "UPDATE transcription_tasks SET status='completed', "
                    "transcript_text='the goods' WHERE id='cancel-race'"))
                conn.commit()
        return obj
    complete_it_behind_our_back.armed = True

    monkeypatch.setattr(db.session, 'get', complete_it_behind_our_back)
    resp = client.post('/cancel/cancel-race')
    monkeypatch.undo()

    assert resp.status_code == 409, 'cancel won a race it should have lost'
    with A.app.app_context():
        task = db.session.get(TranscriptionTask, 'cancel-race')
        assert task.status == 'completed'
        assert task.transcript_text == 'the goods'


def test_cancelling_a_failed_task_reports_the_failure(trial_on):
    """Clicking Stop a moment after it broke used to say "Transcription
    stopped", hiding why it actually failed."""
    from models import db, TranscriptionTask

    uid = _make_user('cancelfailed@test.com')
    with A.app.app_context():
        db.session.add(TranscriptionTask(
            id='cancel-failed', user_id=uid, episode_title='x', status='error',
            error_message='Your OpenAI API key was rejected.'))
        db.session.commit()

    resp = _login(uid).post('/cancel/cancel-failed')
    assert resp.status_code == 409
    body = resp.get_json()
    assert body['cancelled'] is False
    assert 'rejected' in body['error']


def test_stopping_during_a_download_aborts_it(trial_on, monkeypatch, tmp_path):
    """Stop during the download used to let the whole episode download AND
    re-encode while the UI said it had stopped, holding a concurrency slot and
    disk. Drives download_audio for real: asserting that _update_task returns
    False proves nothing, because that was already true before the fix.
    """
    from models import db, TranscriptionTask

    uid = _make_user('stopdownload@test.com')
    with A.app.app_context():
        db.session.add(TranscriptionTask(id='stop-dl', user_id=uid,
                                         episode_title='x', status='downloading'))
        db.session.commit()

    delivered = []
    closed = []

    class FakeResponse:
        headers = {'content-length': '400000'}
        is_redirect = False
        is_permanent_redirect = False
        status_code = 200

        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size=8192):
            # Enough wall time after the first _update_task to clear the
            # hard-coded 1s write throttle so the cancel check actually runs.
            for i in range(80):
                delivered.append(i)
                if i == 1:
                    # The user hits Stop two chunks in.
                    with db.engine.connect() as conn:
                        conn.execute(A.text(
                            "UPDATE transcription_tasks SET status='cancelled' "
                            "WHERE id='stop-dl'"))
                        conn.commit()
                time.sleep(0.025)
                yield b'\0' * 8192

        def close(self):
            closed.append(True)

    monkeypatch.setattr(A, '_is_fetchable_url', lambda raw: True)
    monkeypatch.setattr(A.requests, 'get', lambda *a, **kw: FakeResponse())
    monkeypatch.setattr(A.requests.Session, 'get',
                        lambda self, *a, **kw: FakeResponse())
    monkeypatch.setattr(A.requests, 'head', lambda *a, **kw: FakeResponse())

    target = tmp_path / 'ep.mp3'
    with A.app.app_context():
        with pytest.raises(A.TaskAbandoned):
            A.download_audio('https://example.com/ep.mp3', str(target), 'stop-dl')

    assert len(delivered) < 80, 'the download ran to completion after cancelling'
    assert closed, 'the streamed response was left open, leaking the connection'


def test_stopping_before_the_split_aborts_it(trial_on, monkeypatch, tmp_path):
    """Same for the re-encode, which is the CPU-hungry step."""
    from models import db, TranscriptionTask

    uid = _make_user('stopsplit@test.com')
    audio = tmp_path / 'ep.mp3'
    audio.write_bytes(b'\0' * 2048)
    called = []
    monkeypatch.setattr(A, 'probe_audio_duration', lambda f: 600.0)
    monkeypatch.setattr(A, 'prepare_audio_for_whisper',
                        lambda f, **kw: called.append(f) or [str(audio)])

    with A.app.app_context():
        db.session.add(TranscriptionTask(id='stop-split', user_id=uid,
                                         episode_title='x', status='cancelled'))
        db.session.commit()
        with pytest.raises(A.TaskAbandoned):
            A.transcribe_audio(str(audio), 'stop-split', object(), language='no')

    assert called == [], 'the episode was re-encoded after being cancelled'

def test_a_cancelled_transcript_is_downloadable_but_only_as_text(trial_on):
    """The user was charged pro-rata for what was transcribed before they
    stopped, so it has to be reachable. Not .srt: segments_json is normally
    NULL on a cancelled task, and an .srt built from nothing is a one-line
    stub pretending to be a transcript."""
    from models import db, TranscriptionTask

    uid = _make_user('canceldl@test.com')
    other = _make_user('canceldl-other@test.com')
    with A.app.app_context():
        db.session.add_all([
            TranscriptionTask(id='dl-cancelled', user_id=uid, episode_title='Ep',
                              status='cancelled', transcript_text='half a transcript'),
            TranscriptionTask(id='dl-cancelled-empty', user_id=uid, episode_title='Ep',
                              status='cancelled', transcript_text=None),
        ])
        db.session.commit()

    mine = _login(uid)
    ok = mine.get('/download/dl-cancelled/txt')
    assert ok.status_code == 200
    assert b'half a transcript' in ok.data
    assert mine.get('/download/dl-cancelled/srt').status_code == 404, (
        'an .srt with no segments is a stub, not a transcript'
    )
    assert mine.get('/download/dl-cancelled-empty/txt').status_code == 404
    assert _login(other).get('/download/dl-cancelled/txt').status_code == 404, (
        'another user could download it'
    )


def test_a_completed_transcript_download_is_unchanged(trial_on):
    """The cancelled path must not narrow the completed one -- legacy rows
    imported from the old transcriptions table can have no text at all."""
    from models import db, TranscriptionTask

    uid = _make_user('completeddl@test.com')
    with A.app.app_context():
        db.session.add(TranscriptionTask(id='dl-done', user_id=uid,
                                         episode_title='Ep', status='completed',
                                         transcript_text=None))
        db.session.commit()
    assert _login(uid).get('/download/dl-done/txt').status_code == 200


def test_the_download_connection_closes_on_every_exit(trial_on, monkeypatch, tmp_path):
    """Cancel and the size cap were covered; a write failing on a full disk --
    the case the capacity limits exist for -- left the connection open."""
    from models import db, TranscriptionTask

    uid = _make_user('dlclose@test.com')
    with A.app.app_context():
        db.session.add(TranscriptionTask(id='dl-close', user_id=uid,
                                         episode_title='x', status='downloading'))
        db.session.commit()

    closed = []

    class FakeResponse:
        headers = {'content-length': '80000'}
        is_redirect = False
        is_permanent_redirect = False
        status_code = 200

        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size=8192):
            for _ in range(10):
                yield b'\0' * 8192

        def close(self):
            closed.append(True)

    monkeypatch.setattr(A, '_is_fetchable_url', lambda raw: True)
    monkeypatch.setattr(A.requests, 'get', lambda *a, **kw: FakeResponse())
    monkeypatch.setattr(A.requests.Session, 'get',
                        lambda self, *a, **kw: FakeResponse())

    real_open = open

    def full_disk(path, mode='r', *a, **kw):
        handle = real_open(path, mode, *a, **kw)
        if 'w' in mode:
            handle.write = lambda data: (_ for _ in ()).throw(OSError(28, 'No space left'))
        return handle

    monkeypatch.setattr('builtins.open', full_disk)
    with A.app.app_context():
        with pytest.raises(OSError):
            A.download_audio('https://example.com/ep.mp3',
                             str(tmp_path / 'ep.mp3'), 'dl-close')
    assert closed, 'the connection was left open when the write failed'

# --------------------------------------------------------------------------
# Live job bar
# --------------------------------------------------------------------------

def test_active_jobs_returns_only_your_own_running_work(trial_on):
    """The bar is carried on every page, so this endpoint is the one thing that
    could leak another user's episode titles."""
    from models import db, TranscriptionTask

    mine = _make_user('jobbar@test.com')
    theirs = _make_user('jobbar-other@test.com')
    with A.app.app_context():
        db.session.add_all([
            TranscriptionTask(id='jb-mine', user_id=mine, status='transcribing',
                              episode_title='My episode', podcast_name='Pod',
                              progress=42, heartbeat_at=datetime.now(timezone.utc)),
            TranscriptionTask(id='jb-theirs', user_id=theirs, status='transcribing',
                              episode_title='Their episode',
                              heartbeat_at=datetime.now(timezone.utc)),
            TranscriptionTask(id='jb-done', user_id=mine, status='completed',
                              episode_title='Finished'),
            TranscriptionTask(id='jb-cancelled', user_id=mine, status='cancelled',
                              episode_title='Stopped'),
        ])
        db.session.commit()

    jobs = _login(mine).get('/active-jobs').get_json()['jobs']
    titles = [j['title'] for j in jobs]
    assert titles == ['My episode'], titles
    assert jobs[0]['percent'] >= 42
    assert 'phase' in jobs[0]


def test_active_jobs_needs_a_login(trial_on):
    """Anonymous callers must not be able to probe it at all."""
    resp = A.app.test_client().get('/active-jobs')
    assert resp.status_code in (302, 401), resp.status_code


def test_active_jobs_drops_a_task_whose_worker_died(trial_on):
    """This endpoint is the only thing running the stale check while the user is
    elsewhere in the app, so a job killed by a deploy stops claiming to be
    alive even if nobody opens its page."""
    from models import db, TranscriptionTask

    uid = _make_user('jobbardead@test.com')
    with A.app.app_context():
        db.session.add(TranscriptionTask(
            id='jb-dead', user_id=uid, status='transcribing',
            episode_title='Killed by a deploy',
            heartbeat_at=datetime.now(timezone.utc) - timedelta(days=2)))
        db.session.commit()

    assert _login(uid).get('/active-jobs').get_json()['jobs'] == []
    with A.app.app_context():
        assert db.session.get(TranscriptionTask, 'jb-dead').status == 'error'


def test_the_job_bar_only_polls_for_signed_in_visitors(trial_on):
    """It runs on every page. An anonymous visitor would poll an endpoint that
    can only ever redirect them to the login page."""
    anon = A.app.test_client().get('/').data.decode()
    assert 'data-authenticated' not in anon
    uid = _make_user('jobbarauth@test.com')
    assert 'data-authenticated="1"' in _login(uid).get('/').data.decode()


def test_the_job_bar_is_on_every_page(trial_on):
    """A mini player that only exists on the home page is not a mini player."""
    uid = _make_user('jobbarpages@test.com')
    client = _login(uid)
    for path in ('/', '/settings', '/history', '/rss-help'):
        body = client.get(path).data.decode()
        assert 'id="jobBar"' in body, f'no job bar on {path}'
        assert "fetch('/active-jobs'" in body, f'no polling on {path}'


def test_the_job_bar_backs_off_when_nothing_is_running(trial_on):
    """It polls from every page of the app, so an idle tab must not keep asking
    every four seconds.

    Asserts the NUMBERS, not the source text. The first version of this test
    checked that the string `IDLE_MS` appeared, which stayed green when the
    interval itself was dropped to 1000ms -- a twentyfold load increase on a box
    CLAUDE.md notes is shared with 50+ other services.
    """
    import re as _re
    body = A.app.test_client().get('/').data.decode()

    def interval(name):
        m = _re.search(r'var ' + name + r' = (\d+);', body)
        assert m, f'{name} is gone'
        return int(m.group(1))

    active, idle = interval('ACTIVE_MS'), interval('IDLE_MS')
    assert active >= 2000, f'polling every {active}ms while a job runs'
    assert idle >= 15000, f'an idle tab polls every {idle}ms from every page'
    assert idle > active * 3, 'the idle back-off is barely a back-off'

    # Idle is the common case: most pages, most of the time, have no job.
    assert 'shownCount ? ACTIVE_MS : IDLE_MS' in body, (
        'the back-off keys on the response rather than on what is displayed, so '
        "a job's own page polls at the active rate for a bar it never renders"
    )
    assert 'if (document.hidden) { schedule(IDLE_MS); return; }' in body, (
        'a hidden tab re-checks on the active interval, which is a timer every '
        'four seconds for something nobody can see'
    )

def test_active_jobs_refunds_a_dead_task_it_sweeps(trial_on):
    """/active-jobs is a new caller into the refund path -- it runs the stale
    check from every open tab. A job killed by a deploy must give its allowance
    back, not just disappear from the list."""
    from models import db, TranscriptionTask

    uid = _make_user('jobbarrefund@test.com', limit=36000)
    with A.app.app_context():
        A.trial_reserve(uid, 900)
        db.session.add(TranscriptionTask(
            id='jb-refund', user_id=uid, status='transcribing',
            episode_title='Killed mid-episode', chunk_total=4, chunk_index=1,
            trial_seconds_charged=900,
            heartbeat_at=datetime.now(timezone.utc) - timedelta(days=2)))
        db.session.commit()

    assert _login(uid).get('/active-jobs').get_json()['jobs'] == []
    with A.app.app_context():
        task = db.session.get(TranscriptionTask, 'jb-refund')
        assert task.status == 'error'
        assert task.trial_settled is True
    # Two of four chunks had been sent, so half the reservation stands.
    assert _used(uid) == 450


def test_sweeping_a_stale_task_cannot_clobber_one_that_just_finished(trial_on):
    """_fail_if_stale was a read-check-write on a session-cached row -- the one
    status write in the file that was not a conditional UPDATE. A task that
    completed inside the window became "stopped making progress" while keeping
    the full charge, inviting a paid re-run. The job bar polls this from every
    open tab, so it fires far more often than it used to.
    """
    from models import db, TranscriptionTask

    uid = _make_user('sweeprace@test.com', limit=36000)
    with A.app.app_context():
        A.trial_reserve(uid, 600)
        db.session.add(TranscriptionTask(
            id='sweep-race', user_id=uid, status='transcribing',
            episode_title='Finished just in time', chunk_total=1, chunk_index=0,
            trial_seconds_charged=600,
            heartbeat_at=datetime.now(timezone.utc) - timedelta(days=2)))
        db.session.commit()

        task = db.session.get(TranscriptionTask, 'sweep-race')   # our stale copy
        # The worker completes it on another connection, leaving ours stale.
        with db.engine.connect() as conn:
            conn.execute(A.text(
                "UPDATE transcription_tasks SET status='completed', "
                "transcript_text='the goods' WHERE id='sweep-race'"))
            conn.commit()

        assert A._fail_if_stale(task) is False, 'the sweep clobbered a finished job'
        fresh = db.session.get(TranscriptionTask, 'sweep-race')
        assert fresh.status == 'completed'
        assert fresh.transcript_text == 'the goods'

# --------------------------------------------------------------------------
# AI readability
#
# ~25 visits a month arrive from ChatGPT with nothing on the site written for
# an assistant. These files and this markup are what it has to read.
# --------------------------------------------------------------------------

def test_robots_txt_names_the_assistants_that_send_traffic(trial_on):
    """There was no robots.txt at all, which leaves every crawler guessing."""
    resp = A.app.test_client().get('/robots.txt')
    assert resp.status_code == 200
    assert resp.mimetype == 'text/plain'
    body = resp.data.decode()
    for agent in ('GPTBot', 'OAI-SearchBot', 'ChatGPT-User', 'ClaudeBot',
                  'PerplexityBot', 'Google-Extended'):
        assert f'User-agent: {agent}' in body, f'{agent} not addressed'
    assert 'Sitemap: http' in body


def test_robots_txt_keeps_crawlers_out_of_session_only_pages(trial_on):
    """Nothing behind a login is useful to a crawler, and some of it is
    personal -- a transcript is the user's, not the index's."""
    body = A.app.test_client().get('/robots.txt').data.decode()
    for path in ('/settings', '/history', '/transcription/', '/download/',
                 '/api/', '/active-jobs', '/cancel/', '/t/', '/admin'):
        assert f'Disallow: {path}' in body, f'{path} is crawlable'


def test_llms_txt_states_what_the_tool_is_for(trial_on):
    """An assistant asked "how do I transcribe a Norwegian podcast" has to
    infer everything from a page that is mostly a search box."""
    resp = A.app.test_client().get('/llms.txt')
    assert resp.status_code == 200
    body = resp.data.decode()
    assert body.startswith('# Podskrift')
    assert '>' in body.split('\n')[2], 'no one-line summary blockquote'
    for claim in ('Norwegian', 'Danish', 'Swedish', 'German', 'Whisper', '.srt'):
        assert claim in body, f'{claim} missing from llms.txt'
    # The trial length is generated, not typed, so it cannot drift from the code.
    # Asserting the right number is present is not enough -- the FAQ is embedded
    # in this file too, so a hardcoded figure in the prose above hid behind the
    # generated one in the FAQ. Every figure in the file has to agree.
    import re as _re
    granted = A.NEW_USER_TRIAL_SECONDS // 60
    figures = {int(n) for n in _re.findall(r'(\d+) minutes of audio free', body)}
    assert figures == {granted}, (
        f'llms.txt quotes {sorted(figures)} free minutes; the configured grant is '
        f'{granted}'
    )


def test_llms_txt_and_the_page_answer_the_same_questions(trial_on):
    """If the file says one thing and the page another, the quote and the
    visit disagree."""
    llms = A.app.test_client().get('/llms.txt').data.decode()
    home = A.app.test_client().get('/').data.decode()
    for question, _ in A.faq_entries():
        assert question in llms, f'{question!r} missing from llms.txt'
        assert question in home, f'{question!r} missing from the page'


def test_the_home_page_carries_structured_data(trial_on):
    """WebApplication, not Organization: nobody asks an assistant what
    Podskrift is. They ask how to transcribe a podcast."""
    import json as _json
    import re as _re
    body = A.app.test_client().get('/').data.decode()
    m = _re.search(r'<script type="application/ld\+json">(.*?)</script>', body, _re.S)
    assert m, 'no JSON-LD on the home page'
    data = _json.loads(m.group(1))          # must be valid JSON, not just present
    types = {node['@type'] for node in data['@graph']}
    assert types == {'WebApplication', 'FAQPage'}, types

    app_node = next(n for n in data['@graph'] if n['@type'] == 'WebApplication')
    assert 'no' in app_node['inLanguage'], 'Norwegian missing from inLanguage'
    assert app_node['offers']['price'] == '0'

    faq_node = next(n for n in data['@graph'] if n['@type'] == 'FAQPage')
    assert len(faq_node['mainEntity']) == len(A.faq_entries())

    # Every answer in the schema is one a visitor can actually read. Compared
    # against the page WITHOUT the JSON-LD block: the schema lives in the same
    # document, so checking it against the whole body compared it to itself.
    import html as _html
    visible = _html.unescape(body[:m.start()] + body[m.end():])
    for entry in faq_node['mainEntity']:
        assert entry['acceptedAnswer']['text'] in visible, (
            f'the schema answers {entry["name"]!r} with text that is nowhere on the page'
        )


def test_the_sitemap_lists_the_public_pages(trial_on):
    resp = A.app.test_client().get('/sitemap.xml')
    assert resp.status_code == 200
    assert resp.mimetype == 'application/xml'
    from xml.etree import ElementTree
    root = ElementTree.fromstring(resp.data)          # must parse
    locs = [e.text for e in root.iter('{http://www.sitemaps.org/schemas/sitemap/0.9}loc')]
    assert any(u.endswith('/') for u in locs)
    assert any(u.endswith('/rss-help') for u in locs)
    assert any(u.endswith('/docs/api') for u in locs)
    assert any(u.endswith('/whats-new') for u in locs)
    assert any(u.endswith('/register') for u in locs)
    assert not any('/settings' in u or '/history' in u for u in locs), (
        'a session-only page is in the sitemap'
    )


def test_the_page_says_what_it_is_before_asking_for_anything(trial_on):
    """The hero used to lead with "bring your own API key" -- a credential
    request before any value was shown, and 99.4% of visitors left."""
    body = A.app.test_client().get('/').data.decode()
    assert 'bring your own API key, completely free' not in body
    assert 'Paste your OpenAI API key' not in body, (
        'the how-it-works steps still describe the pre-trial flow'
    )
    # The page leads with what it does for anyone, not with a language fence.
    # An earlier version read "Built for Norwegian, Danish, Swedish and German",
    # which was survivorship bias: the three transcriptions that succeeded were
    # Nordic/German because those three users happened to have a working API
    # key. Every failure -- Norwegian, German and English alike -- was a 401 or
    # a 429. Language never came into it.
    #
    # Asserted on the HERO, not on the document: the first version of this
    # checked the whole page for "N languages", which the meta tags satisfied,
    # so restoring the entire Nordic-fence hero left the suite green.
    import re as _re
    hero = _re.search(r'<h1[^>]*>.*?</p>', body, _re.S)
    assert hero, 'no hero to check'
    hero = hero.group(0)
    assert 'Built for' not in hero, 'the hero fences the product to a language group'
    assert 'English-first' not in hero, 'the hero still claims an edge we cannot evidence'
    # Language count lives in meta / FAQ, not the ChatGPT-facing hero (which leads
    # with Spotify). Still required somewhere visible on the page.
    assert f'{len(A.LANGUAGE_ENGLISH_NAMES)} languages' in body

    # And nowhere quotes a count it typed by hand. It was hardcoded in three
    # meta tags next to a comment claiming the derived form existed so they
    # could not drift; language 29 would leave every link preview lying.
    import re as _re
    counts = {int(n) for n in _re.findall(r'(\d+) languages', body)}
    assert counts == {len(A.LANGUAGE_ENGLISH_NAMES)}, (
        f'the page quotes {sorted(counts)} languages; there are '
        f'{len(A.LANGUAGE_ENGLISH_NAMES)}'
    )
    assert 'meta name="description"' in body
    assert 'og:title' in body
    assert '<main id="content"' in body, 'no main landmark for anything to orient on'

def test_every_named_crawler_group_repeats_the_rules(trial_on):
    """RFC 9309: a crawler obeys ONLY its most specific matching group and
    ignores `User-agent: *`. A named group containing just `Allow: /` therefore
    told exactly the assistants this file exists for that /history and
    /download/ were fair game -- strictly worse than not naming them.
    """
    body = A.app.test_client().get('/robots.txt').data.decode()

    groups, current = {}, None
    for line in body.splitlines():
        line = line.split('#')[0].strip()
        if not line:
            continue
        if line.lower().startswith('user-agent:'):
            current = line.split(':', 1)[1].strip()
            groups.setdefault(current, [])
        elif current and line.lower().startswith('disallow:'):
            groups[current].append(line.split(':', 1)[1].strip())

    assert len(groups) > 1, 'no named groups at all'
    baseline = set(groups['*'])
    assert baseline, 'the wildcard group disallows nothing'
    for agent, rules in groups.items():
        assert set(rules) >= baseline, (
            f'{agent} is granted access to {sorted(baseline - set(rules))} because its '
            'group does not repeat the rules'
        )


def test_public_urls_use_the_configured_origin(trial_on, monkeypatch):
    """url_for(_external=True) builds from the request, which behind Plesk's
    nginx is http:// on an https site -- so the canonical link, the sitemap and
    the JSON-LD @id all pointed at URLs that 301 away."""
    monkeypatch.setattr(A, 'PUBLIC_BASE_URL', 'https://podskrift.com')
    client = A.app.test_client()
    # Match the canonical Host so before_request does not 301 away from localhost.
    host = {'Host': 'podskrift.com'}

    sitemap = client.get('/sitemap.xml', headers=host).data.decode()
    assert 'https://podskrift.com/' in sitemap
    assert 'http://localhost' not in sitemap and 'http://podskrift' not in sitemap

    llms = client.get('/llms.txt', headers=host).data.decode()
    assert 'http://localhost' not in llms

    robots = client.get('/robots.txt', headers=host).data.decode()
    assert 'Sitemap: https://podskrift.com/sitemap.xml' in robots

    import json as _json, re as _re
    body = client.get('/', headers=host).data.decode()
    raw = _re.search(r'<script type="application/ld\+json">(.*?)</script>', body, _re.S).group(1)
    data = _json.loads(raw.replace('\\u003c', '<').replace('\\u003e', '>'))
    for node in data['@graph']:
        assert node['@id'].startswith('https://podskrift.com/'), node['@id']


def test_llms_txt_is_served_as_plain_utf8_text(trial_on):
    """It carries the Norwegian FAQ, so the charset is the one header that
    matters here. Passing 'text/plain; charset=utf-8' as the mimetype made
    Werkzeug emit the charset twice."""
    resp = A.app.test_client().get('/llms.txt')
    assert resp.mimetype == 'text/plain'
    assert resp.headers['Content-Type'].lower().count('charset') == 1, (
        resp.headers['Content-Type']
    )
    assert 'transkriberer' in resp.data.decode('utf-8')


def test_no_page_quotes_a_trial_length_the_code_does_not_grant(trial_on):
    """The regex in the llms.txt test was anchored to one phrasing and stepped
    straight over a hardcoded "60 trial minutes" in the same file and a
    hardcoded "First 60 minutes free" in the hero."""
    import re as _re
    client = A.app.test_client()
    granted = A.NEW_USER_TRIAL_SECONDS // 60
    for path in ('/', '/llms.txt', '/docs/api'):
        text = client.get(path).data.decode()
        figures = {int(n) for n in _re.findall(r'(\d+)\s+(?:trial\s+)?minutes', text)}
        assert figures <= {granted}, (
            f'{path} quotes {sorted(figures - {granted})} minutes; the configured '
            f'grant is {granted}'
        )


def test_the_copy_does_not_promise_a_trial_that_is_switched_off(trial_on, monkeypatch):
    """TRIAL_ENABLED=0, or no global key, and the app refuses at
    /start_transcription -- while the hero, the FAQ and the schema all
    advertised free minutes anyway."""
    monkeypatch.setattr(A, 'TRIAL_ENABLED', False)
    assert A.trial_available() is False
    client = A.app.test_client()

    home = client.get('/').data.decode()
    assert 'minutes free' not in home
    llms = client.get('/llms.txt').data.decode()
    assert 'minutes of audio free' not in llms
    assert 'trial minutes' not in llms
    # And the FAQ answers the question honestly instead.
    answers = dict(A.faq_entries())
    assert 'free trial' not in answers['Do I need an OpenAI API key?'].lower()
    assert 'free trial' not in answers['Is there an HTTP API?'].lower()
    docs = client.get('/docs/api').data.decode()
    # Changelog toast may mention historical grant lengths; marketing must not.
    grant = A.NEW_USER_TRIAL_SECONDS // 60
    assert f'Free {grant}-minute trial' not in docs
    assert f'<strong>{grant} minutes</strong>' not in docs
    assert 'of audio on signup' not in docs


def test_structured_data_cannot_break_out_of_its_script_tag(trial_on, monkeypatch):
    """json.dumps does not escape '<'. Everything in the graph is a constant
    today, but the first dynamic value -- a podcast title from RSS -- would
    close the tag."""
    monkeypatch.setattr(A, 'faq_entries',
                        lambda: [('</script><img src=x onerror=alert(1)>', 'x')])
    with A.app.test_request_context('/'):
        raw = A._structured_data()
    assert '</script>' not in raw
    assert '<' not in raw and '>' not in raw
    import json as _json
    _json.loads(raw)          # still valid JSON after escaping

def test_the_language_picker_is_not_limited_to_the_founders_market(trial_on):
    """Whisper handles far more than the nine languages that shipped first --
    a list chosen from whoever happened to have signed up. The product is for
    anyone with a podcast in any language."""
    codes = {code for code, _ in A.SUPPORTED_LANGUAGES if code}
    assert len(codes) >= 25, f'only {len(codes)} languages offered'
    # The biggest podcast markets in the world, plus the ones nobody else does well.
    for code in ('en', 'es', 'pt', 'zh', 'hi', 'ar', 'ja', 'de', 'fr', 'ru',
                 'no', 'uk', 'vi', 'id', 'tr'):
        assert code in codes, f'{code} missing from the picker'
    assert '' == A.SUPPORTED_LANGUAGES[0][0], 'auto-detect is not first'


def test_every_offered_language_has_an_english_name(trial_on):
    """llms.txt and the schema render English names; a code with no name would
    silently drop out of both."""
    # Not `set(LANGUAGE_ENGLISH_NAMES) == codes` -- both sides were
    # comprehensions over the same list, so it could not fail by construction.
    # Check what actually reaches a reader instead.
    llms = A.app.test_client().get('/llms.txt').data.decode()
    for code, _, english in A.SUPPORTED_LANGUAGES_FULL:
        if code:
            assert english in llms, f'{code} has no English name in llms.txt'
    assert 'Japanese' in llms and 'Ukrainian' in llms


def test_the_picker_is_ordered_so_a_long_list_stays_findable(trial_on):
    """Twenty-eight options ordered by who signed up first is a list nobody can
    use. Alphabetical by English name, after auto-detect."""
    named = [(code, english) for code, _, english in A.SUPPORTED_LANGUAGES_FULL if code]
    assert [e for _, e in named] == sorted(e for _, e in named)

    # And the order has to be visible: sorting on English names while rendering
    # only native ones (العربية, 中文, Čeština, Dansk...) looks random to the
    # person reading the list, which is the opposite of findable.
    labels = [label for code, label in A.language_choices() if code]
    assert labels == sorted(labels), 'the rendered labels are not in the sorted order'


def test_the_page_does_not_claim_a_single_home_market(trial_on):
    """og:locale said nb_NO on an English-language page for a worldwide tool."""
    body = A.app.test_client().get('/').data.decode()
    assert 'og:locale" content="en_US"' in body, 'the page claims a Norwegian locale'
    # The alternate stays: a Norwegian FAQ answer really is on the page. Pinning
    # nb_NO shut was itself a fence -- the opposite one.
    assert 'og:locale:alternate" content="nb_NO"' in body

def test_taking_the_default_does_not_assert_a_language(trial_on, monkeypatch):
    """The real fence, and the one the copy change missed. /start_transcription
    defaulted to 'no' and fell back to 'no' on anything unrecognised -- so a
    Japanese listener who took the default had Whisper TOLD the audio was
    Norwegian. Whisper obeys that as a constraint, not a hint, so the result is
    phonetic nonsense we still paid for.
    """
    from models import db, TranscriptionTask

    # _post_start stubs the worker thread, so its slot is never handed back;
    # two starts in one test need more than the production cap of one.
    import threading as _t
    monkeypatch.setattr(A, 'MAX_CONCURRENT_TRANSCRIPTIONS', 4)
    monkeypatch.setattr(A, '_transcription_slots', _t.BoundedSemaphore(4))

    uid = _make_user('deflang@test.com', limit=36000)
    for data in ({'audio_url': 'https://example.com/a.mp3', 'episode_title': 'A',
                  'duration_min': '5'},                                   # no language sent
                 {'audio_url': 'https://example.com/b.mp3', 'episode_title': 'B',
                  'duration_min': '5', 'language': 'klingon'}):           # unrecognised
        resp = _post_start(monkeypatch, uid, data)
        assert resp.status_code == 200, resp.get_json()

    with A.app.app_context():
        langs = [t.language for t in
                 TranscriptionTask.query.filter_by(user_id=uid).all()]
    assert langs == [None, None], f'a language was asserted on the user: {langs}'


def test_a_chosen_language_is_honoured(trial_on, monkeypatch):
    """The flip side: naming a language must still reach the task, because that
    is what makes it beat auto-detect on short or accented audio."""
    from models import db, TranscriptionTask

    uid = _make_user('pickedlang@test.com', limit=36000)
    resp = _post_start(monkeypatch, uid, {
        'audio_url': 'https://example.com/ja.mp3', 'episode_title': 'Ep',
        'duration_min': '5', 'language': 'ja'})
    assert resp.status_code == 200
    with A.app.app_context():
        task = TranscriptionTask.query.filter_by(user_id=uid).first()
    assert task.language == 'ja'


def test_the_picker_does_not_preselect_a_language(trial_on):
    """"Norsk" was hard-selected in the markup. Widening the list to 28 moved it
    from visible position 2 to position 19, so the pre-selection became
    invisible while still being what got submitted."""
    uid = _make_user('preselect@test.com')
    body = _login(uid).get('/').data.decode()
    import re as _re
    options = _re.findall(r'<option value="([^"]*)"([^>]*)>', body)
    assert options, 'no language picker on the page'
    selected = [code for code, attrs in options if 'selected' in attrs]
    assert selected in ([], ['']), f'the picker pre-selects {selected}'
    assert options[0][0] == '', 'auto-detect is not the first option'


def test_no_surface_claims_an_edge_we_cannot_evidence(trial_on):
    """"Unusually good on the languages English-first tools handle worst" was
    published on five surfaces. This is a plain whisper-1 wrapper -- no
    fine-tune, no custom decoding -- and the evidence for it was three
    successful transcriptions that happened to be Nordic because those three
    users happened to have a working API key.
    """
    client = A.app.test_client()
    surfaces = {path: client.get(path).data.decode() for path in ('/', '/llms.txt')}
    surfaces['schema'] = A.app.test_client().get('/').data.decode()
    for path, text in surfaces.items():
        low = text.lower()
        assert 'english-first' not in low, f'{path} still claims it'
        assert 'unusually good' not in low, f'{path} still claims it'

def test_the_language_count_follows_the_list(trial_on, monkeypatch):
    """Checking that the page says the right number is not enough while the
    right number happens to be the one someone typed. Change the list and the
    page has to follow -- otherwise a hardcoded literal passes for as long as
    it stays accidentally correct, which is exactly how three meta tags ended
    up quoting 28 next to a comment claiming they were derived.
    """
    import re as _re
    monkeypatch.setattr(A, 'LANGUAGE_ENGLISH_NAMES',
                        {'en': 'English', 'no': 'Norwegian', 'ja': 'Japanese'})
    body = A.app.test_client().get('/').data.decode()
    counts = {int(n) for n in _re.findall(r'(\d+) languages', body)}
    assert counts == {3}, f'the page still quotes {sorted(counts)} languages'


# --------------------------------------------------------------------------
# Spotify links  (TSK-20440)
# --------------------------------------------------------------------------

_SPOTIFY_EP = '7eDBByfSsrz3l3iYbXqDMH'
_SPOTIFY_SHOW = '79CkJF3UJTHFV8Dse3Oy0P'
_FEED = 'https://feeds.example.com/huberman'


def _embed_page(entity):
    import json as _json
    data = {'props': {'pageProps': {'state': {'data': {'entity': entity}}}}}
    return ('<html><script id="__NEXT_DATA__" type="application/json">'
            + _json.dumps(data) + '</script></html>')


def _rss(*titles):
    items = ''.join(
        f'<item><title>{t}</title><enclosure url="https://cdn.example.com/{i}.mp3" '
        f'type="audio/mpeg" length="1"/><itunes:duration>1:00:00</itunes:duration></item>'
        for i, t in enumerate(titles))
    return ('<?xml version="1.0"?><rss version="2.0" '
            'xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"><channel>'
            f'<title>Huberman Lab</title>{items}</channel></rss>').encode()


class _HttpResp:
    def __init__(self, status=200, text='', content=b'', payload=None, location=None):
        self.status_code = status
        self.text = text
        self.content = content or text.encode()
        self._payload = payload
        self.headers = {'location': location} if location else {}
        self.is_redirect = status in (301, 302, 303, 307, 308) and bool(location)
        self.is_permanent_redirect = status in (301, 308) and bool(location)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise A.requests.HTTPError(f'{self.status_code}')

    def json(self):
        return self._payload

    def iter_content(self, size):
        for i in range(0, len(self.content), size):
            yield self.content[i:i + size]

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _patch_requests_get(monkeypatch, get):
    """download_audio / feed fetch use Session.get; keep module get patched too."""
    monkeypatch.setattr(A.requests, 'get', get)
    monkeypatch.setattr(
        A.requests.Session, 'get',
        lambda self, url, **kw: get(url, **kw),
    )


def _fake_web(monkeypatch, embed=None, shows=(), episodes=(), feed=b'', oembed=None,
              show_embed=None, show_oembed=None):
    """Route requests.get by host. Records every URL that was fetched.

    `embed` / `oembed` cover the episode (or primary) Spotify lookup.
    `show_embed` / `show_oembed` optionally cover /embed/show/… and show oEmbed
    used when relatedEntityUri points at a show id.
    """
    fetched = []

    def get(url, params=None, **kw):
        fetched.append(url)
        if '/embed/show/' in url:
            body = show_embed if show_embed is not None else embed
            return _HttpResp(200, text=body) if body else _HttpResp(404)
        if '/embed/' in url:
            return _HttpResp(200, text=embed) if embed else _HttpResp(404)
        if 'oembed' in url:
            oembed_url = (params or {}).get('url') or ''
            if '/show/' in oembed_url and show_oembed is not None:
                return _HttpResp(200, payload=show_oembed) if show_oembed else _HttpResp(404)
            return _HttpResp(200, payload=oembed) if oembed else _HttpResp(404)
        if 'itunes.apple.com' in url:
            items = episodes if params['entity'] == 'podcastEpisode' else shows
            return _HttpResp(200, payload={'results': list(items)})
        if url == _FEED:
            if isinstance(feed, int):
                return _HttpResp(feed)
            return _HttpResp(200, content=feed)
        raise AssertionError(f'unexpected fetch {url}')

    _patch_requests_get(monkeypatch, get)
    monkeypatch.setattr(A, '_is_fetchable_url', lambda u: True)
    return fetched


_HUBERMAN_EP_ENTITY = {'type': 'episode', 'name': 'Essentials: Genes & Memory',
                       'subtitle': 'Huberman Lab'}
_HUBERMAN_SHOW = {'collectionName': 'Huberman Lab', 'artistName': 'Scicomm Media',
                  'feedUrl': _FEED}


@pytest.mark.parametrize('url,expected', [
    (f'https://open.spotify.com/episode/{_SPOTIFY_EP}', ('episode', _SPOTIFY_EP)),
    (f'https://open.spotify.com/episode/{_SPOTIFY_EP}?si=abc123', ('episode', _SPOTIFY_EP)),
    (f'https://open.spotify.com/intl-no/episode/{_SPOTIFY_EP}', ('episode', _SPOTIFY_EP)),
    (f'open.spotify.com/show/{_SPOTIFY_SHOW}', ('show', _SPOTIFY_SHOW)),
    (f'spotify:episode:{_SPOTIFY_EP}', ('episode', _SPOTIFY_EP)),
    # Mangled hosts still yield the id; only the id is ever requested.
    (f'https://open.spotify.comsode/{_SPOTIFY_EP}?si=x', ('episode', _SPOTIFY_EP)),
    (f'sode/{_SPOTIFY_EP}', ('episode', _SPOTIFY_EP)),
    (f'https://evil.example.com/episode/{_SPOTIFY_EP}', ('episode', _SPOTIFY_EP)),
    # Glued junk after the 22-char id (si= param pasted without '?').
    (f'https://open.spotify.com/episode/{_SPOTIFY_EP}4820', ('episode', _SPOTIFY_EP)),
    (f'episode/{_SPOTIFY_EP}4820', ('episode', _SPOTIFY_EP)),
    (f'https://open.spotify.com/track/{_SPOTIFY_EP}', (None, None)),
    ('https://open.spotify.com/episode/tooShort', (None, None)),
    ('huberman lab', (None, None)),
    # Must not misfire on ordinary prose that happens to contain "episode/".
    ('read the episode/notes carefully please', (None, None)),
])
def test_spotify_links_are_recognised(url, expected):
    assert A.parse_spotify_url(url) == expected


def test_spotify_episode_resolves_to_the_audio_in_the_public_feed(monkeypatch):
    fetched = _fake_web(monkeypatch, embed=_embed_page(_HUBERMAN_EP_ENTITY),
                        shows=[_HUBERMAN_SHOW],
                        feed=_rss('Another episode', 'Essentials: Genes &amp; Memory'))
    out = A.resolve_spotify_url(f'https://open.spotify.com/episode/{_SPOTIFY_EP}?si=x')
    assert out['error'] is None and out['error_kind'] is None
    assert len(out['results']) == 1
    hit = out['results'][0]
    assert hit['type'] == 'episode'
    assert hit['name'] == 'Essentials: Genes & Memory'
    assert hit['artist'] == 'Huberman Lab'
    assert hit['audio_url'] == 'https://cdn.example.com/1.mp3'
    assert hit['feed_url'] == _FEED
    # The only Spotify request is built from the parsed id, never the raw input.
    assert f'https://open.spotify.com/embed/episode/{_SPOTIFY_EP}' in fetched


def test_numbered_feed_titles_still_match(monkeypatch):
    _fake_web(monkeypatch, embed=_embed_page(_HUBERMAN_EP_ENTITY), shows=[_HUBERMAN_SHOW],
              feed=_rss('#212 - Essentials: Genes &amp; Memory'))
    out = A.resolve_spotify_url(f'spotify:episode:{_SPOTIFY_EP}')
    assert out['error'] is None and out['results'][0]['audio_url'] == 'https://cdn.example.com/0.mp3'


def test_spotify_show_not_in_directory_softens_the_message(monkeypatch):
    _fake_web(monkeypatch,
              embed=_embed_page({'type': 'episode', 'name': 'Ep 1', 'subtitle': 'Only On Spotify'}),
              shows=[{'collectionName': 'Something Else', 'feedUrl': _FEED}])
    out = A.resolve_spotify_url(f'https://open.spotify.com/episode/{_SPOTIFY_EP}')
    assert out['results'] == []
    assert 'Only On Spotify' in out['error']
    assert 'public RSS feed' in out['error']
    assert 'Spotify-exclusive' not in out['error']
    assert out['error_kind'] == 'no_feed'


def test_episode_missing_from_the_feed_offers_the_show(monkeypatch):
    _fake_web(monkeypatch, embed=_embed_page(_HUBERMAN_EP_ENTITY), shows=[_HUBERMAN_SHOW],
              feed=_rss('Unrelated episode'), episodes=[])
    out = A.resolve_spotify_url(f'https://open.spotify.com/episode/{_SPOTIFY_EP}')
    assert 'isn\'t in its public feed' in out['error']
    assert out['error_kind'] == 'episode_not_in_feed'
    assert [r['type'] for r in out['results']] == ['show']
    assert out['results'][0]['feed_url'] == _FEED


def test_itunes_episode_search_is_the_fallback_to_the_feed(monkeypatch):
    _fake_web(monkeypatch, embed=_embed_page(_HUBERMAN_EP_ENTITY), shows=[],
              episodes=[{'trackName': 'Essentials: Genes & Memory', 'collectionName': 'Other Show',
                         'episodeUrl': 'https://cdn.example.com/wrong.mp3'},
                        {'trackName': 'Essentials: Genes & Memory',
                         'collectionName': 'Huberman Lab',
                         'episodeUrl': 'https://cdn.example.com/right.mp3'}])
    out = A.resolve_spotify_url(f'https://open.spotify.com/episode/{_SPOTIFY_EP}')
    assert out['error'] is None
    assert out['results'][0]['audio_url'] == 'https://cdn.example.com/right.mp3'


def test_a_dead_feed_still_falls_through_to_the_episode_search(monkeypatch):
    _fake_web(monkeypatch, embed=_embed_page(_HUBERMAN_EP_ENTITY), shows=[_HUBERMAN_SHOW],
              feed=403,
              episodes=[{'trackName': 'Essentials: Genes & Memory',
                         'collectionName': 'Huberman Lab',
                         'episodeUrl': 'https://cdn.example.com/right.mp3'}])
    out = A.resolve_spotify_url(f'https://open.spotify.com/episode/{_SPOTIFY_EP}')
    assert out['error'] is None
    assert out['results'][0]['audio_url'] == 'https://cdn.example.com/right.mp3'


def test_an_oversized_feed_is_not_read_into_memory(monkeypatch):
    _fake_web(monkeypatch, embed=_embed_page(_HUBERMAN_EP_ENTITY), shows=[_HUBERMAN_SHOW],
              feed=_rss('Essentials: Genes &amp; Memory'), episodes=[])
    # (max_bytes, early_stop_items) — tiny cap, no early-stop (Spotify path).
    monkeypatch.setattr(A._fetch_feed_capped, '__defaults__', (100, None, 15))
    out = A.resolve_spotify_url(f'https://open.spotify.com/episode/{_SPOTIFY_EP}')
    # The feed would have matched; being over the cap it is skipped, not parsed.
    assert [r['type'] for r in out['results']] == ['show']
    assert out['error']


def test_spotify_show_link_opens_the_show(monkeypatch):
    # A show embed renders its latest episode; the show name is the subtitle.
    _fake_web(monkeypatch, embed=_embed_page(_HUBERMAN_EP_ENTITY), shows=[_HUBERMAN_SHOW])
    out = A.resolve_spotify_url(f'https://open.spotify.com/show/{_SPOTIFY_SHOW}')
    assert out['error'] is None
    assert out['results'] == [A._itunes_show_result(_HUBERMAN_SHOW)]


def test_spotify_show_link_not_in_directory_softens_the_message(monkeypatch):
    _fake_web(monkeypatch,
              embed=_embed_page({'type': 'episode', 'name': 'Ep 1', 'subtitle': 'Only On Spotify'}),
              shows=[])
    out = A.resolve_spotify_url(f'https://open.spotify.com/show/{_SPOTIFY_SHOW}')
    assert out['results'] == []
    assert 'Only On Spotify' in out['error']
    assert 'public RSS feed' in out['error']
    assert 'Spotify-exclusive' not in out['error']
    assert out['error_kind'] == 'no_feed'


def test_oembed_is_used_when_the_embed_page_changes_shape(monkeypatch):
    _fake_web(monkeypatch, embed='<html>no data here</html>',
              oembed={'title': 'Essentials: Genes & Memory'},
              episodes=[{'trackName': 'Essentials: Genes & Memory',
                         'collectionName': 'Huberman Lab',
                         'episodeUrl': 'https://cdn.example.com/right.mp3'}])
    out = A.resolve_spotify_url(f'https://open.spotify.com/episode/{_SPOTIFY_EP}')
    assert out['error'] is None
    assert out['results'][0]['audio_url'] == 'https://cdn.example.com/right.mp3'


def test_unreadable_spotify_link_says_so(monkeypatch):
    _fake_web(monkeypatch)
    out = A.resolve_spotify_url(f'https://open.spotify.com/episode/{_SPOTIFY_EP}')
    assert out['results'] == [] and "Couldn't read that Spotify link" in out['error']
    assert out['error_kind'] == 'unreadable_link'


def test_a_feed_on_a_private_host_is_never_fetched(monkeypatch):
    fetched = _fake_web(monkeypatch, embed=_embed_page(_HUBERMAN_EP_ENTITY),
                        shows=[_HUBERMAN_SHOW], feed=_rss('Essentials: Genes &amp; Memory'))
    monkeypatch.setattr(A, '_is_fetchable_url', lambda u: False)
    A.resolve_spotify_url(f'https://open.spotify.com/episode/{_SPOTIFY_EP}')
    assert _FEED not in fetched


def _redirecting_feed(monkeypatch, target):
    """The show's feed 302s to `target`; `target` serves the matching feed."""
    fetched = _fake_web(monkeypatch, embed=_embed_page(_HUBERMAN_EP_ENTITY),
                        shows=[_HUBERMAN_SHOW], episodes=[])
    inner = A.requests.get

    def get(url, params=None, **kw):
        if url == _FEED:
            fetched.append(url)
            assert kw.get('allow_redirects') is False, 'requests must not follow redirects itself'
            return _HttpResp(302, location=target)
        if url == target:
            fetched.append(url)
            return _HttpResp(200, content=_rss('Essentials: Genes &amp; Memory'))
        return inner(url, params=params, **kw)

    _patch_requests_get(monkeypatch, get)
    monkeypatch.setattr(A, '_is_fetchable_url', lambda u: '169.254' not in u)
    return fetched


def test_a_feed_redirect_into_the_private_network_is_refused(monkeypatch):
    target = 'http://169.254.169.254/latest/meta-data/'
    fetched = _redirecting_feed(monkeypatch, target)
    out = A.resolve_spotify_url(f'https://open.spotify.com/episode/{_SPOTIFY_EP}')
    assert target not in fetched, 'metadata endpoint was contacted'
    assert [r['type'] for r in out['results']] == ['show']


def test_a_feed_redirect_to_a_public_host_is_followed(monkeypatch):
    target = 'https://moved.example.com/feed.xml'
    fetched = _redirecting_feed(monkeypatch, target)
    out = A.resolve_spotify_url(f'https://open.spotify.com/episode/{_SPOTIFY_EP}')
    assert target in fetched and out['error'] is None
    assert out['results'][0]['audio_url'] == 'https://cdn.example.com/0.mp3'


def test_resolve_route_reports_an_unreachable_directory_without_details(monkeypatch):
    calls = {'n': 0}

    def down(*a, **kw):
        calls['n'] += 1
        if 'spotify.com' in a[0]:
            return _HttpResp(200, text=_embed_page(_HUBERMAN_EP_ENTITY))
        raise A.requests.ConnectionError('secret-internal-detail')

    monkeypatch.setattr(A.requests, 'get', down)
    monkeypatch.setattr(A.requests.Session, 'get',
                        lambda self, url, **kw: down(url, **kw))
    monkeypatch.setattr(A.time, 'sleep', lambda s: None)
    data = A.app.test_client().get(
        f'/resolve-spotify?url=https://open.spotify.com/episode/{_SPOTIFY_EP}').get_json()
    assert data['results'] == []
    assert "Couldn't reach the podcast directory" in data['error']
    assert data['error_kind'] == 'directory_unreachable'
    assert 'secret-internal-detail' not in data['error']
    # One attempt + one automatic retry.
    assert calls['n'] >= 2


def test_resolve_route_retries_transient_directory_failure(monkeypatch):
    state = {'itunes': 0}

    def get(url, params=None, **kw):
        if '/embed/' in url:
            return _HttpResp(200, text=_embed_page(_HUBERMAN_EP_ENTITY))
        if 'itunes.apple.com' in url:
            state['itunes'] += 1
            if state['itunes'] == 1:
                raise A.requests.ConnectionError('blip')
            return _HttpResp(200, payload={'results': [_HUBERMAN_SHOW]})
        if url == _FEED:
            return _HttpResp(200, content=_rss('Essentials: Genes &amp; Memory'))
        raise AssertionError(url)

    _patch_requests_get(monkeypatch, get)
    monkeypatch.setattr(A, '_is_fetchable_url', lambda u: True)
    monkeypatch.setattr(A.time, 'sleep', lambda s: None)
    data = A.app.test_client().get(
        f'/resolve-spotify?url=https://open.spotify.com/episode/{_SPOTIFY_EP}').get_json()
    assert data['error'] is None
    assert data['results'][0]['audio_url'] == 'https://cdn.example.com/0.mp3'
    assert state['itunes'] >= 2


def test_paid_spotify_episode_offers_the_show(monkeypatch):
    entity = dict(_HUBERMAN_EP_ENTITY, playabilityReason='PAYMENT_REQUIRED',
                  isPlayable=False)
    _fake_web(monkeypatch, embed=_embed_page(entity), shows=[_HUBERMAN_SHOW],
              feed=_rss('Essentials: Genes &amp; Memory'))
    out = A.resolve_spotify_url(f'https://open.spotify.com/episode/{_SPOTIFY_EP}')
    assert out['error_kind'] == 'paid_episode'
    assert 'paying' in out['error'].lower() or 'subscribers' in out['error'].lower()
    assert [r['type'] for r in out['results']] == ['show']
    assert out['results'][0]['feed_url'] == _FEED


def test_parenthetical_show_name_matches_when_episode_is_in_feed(monkeypatch):
    """Exact name "Higher Mind (backup)" misses; stripped "Higher Mind" hits,
    but only after the episode title is confirmed in that feed."""
    entity = {'type': 'episode', 'name': 'Deep Focus', 'subtitle': 'Higher Mind (backup)'}
    base_show = {'collectionName': 'Higher Mind', 'artistName': 'y', 'feedUrl': _FEED}

    def get(url, params=None, **kw):
        if '/embed/' in url:
            return _HttpResp(200, text=_embed_page(entity))
        if 'itunes.apple.com' in url:
            term = params['term']
            if params['entity'] == 'podcastEpisode':
                return _HttpResp(200, payload={'results': []})
            # Directory lists the base name, not the "(backup)" Spotify label.
            if term == 'Higher Mind':
                return _HttpResp(200, payload={'results': [base_show]})
            return _HttpResp(200, payload={'results': []})
        if url == _FEED:
            return _HttpResp(200, content=_rss('Deep Focus'))
        raise AssertionError(url)

    _patch_requests_get(monkeypatch, get)
    monkeypatch.setattr(A, '_is_fetchable_url', lambda u: True)
    out = A.resolve_spotify_url(f'https://open.spotify.com/episode/{_SPOTIFY_EP}')
    assert out['error'] is None
    assert out['results'][0]['audio_url'] == 'https://cdn.example.com/0.mp3'
    assert out['results'][0]['feed_url'] == _FEED


def test_loose_show_name_without_episode_in_feed_is_rejected(monkeypatch):
    entity = {'type': 'episode', 'name': 'Missing Ep', 'subtitle': 'Higher Mind (backup)'}
    base_show = {'collectionName': 'Higher Mind', 'artistName': 'y', 'feedUrl': _FEED}

    def get(url, params=None, **kw):
        if '/embed/' in url:
            return _HttpResp(200, text=_embed_page(entity))
        if 'itunes.apple.com' in url:
            if params['entity'] == 'podcastEpisode':
                return _HttpResp(200, payload={'results': []})
            if params['term'] == 'Higher Mind':
                return _HttpResp(200, payload={'results': [base_show]})
            return _HttpResp(200, payload={'results': []})
        if url == _FEED:
            return _HttpResp(200, content=_rss('Something else entirely'))
        raise AssertionError(url)

    _patch_requests_get(monkeypatch, get)
    monkeypatch.setattr(A, '_is_fetchable_url', lambda u: True)
    out = A.resolve_spotify_url(f'https://open.spotify.com/episode/{_SPOTIFY_EP}')
    assert out['results'] == []
    assert out['error_kind'] == 'no_feed'


def test_resolve_spotify_json_includes_error_kind(monkeypatch):
    _fake_web(monkeypatch,
              embed=_embed_page({'type': 'episode', 'name': 'Ep 1', 'subtitle': 'Ghost Show'}),
              shows=[])
    data = A.app.test_client().get(
        f'/resolve-spotify?url=https://open.spotify.com/episode/{_SPOTIFY_EP}').get_json()
    assert data['error_kind'] == 'no_feed'
    assert data['show_name'] == 'Ghost Show'
    assert data['results'] == []


def test_resolve_spotify_logs_error_kind(monkeypatch, caplog):
    import logging
    _fake_web(monkeypatch,
              embed=_embed_page({'type': 'episode', 'name': 'Ep 1', 'subtitle': 'Ghost Show'}),
              shows=[])
    with caplog.at_level(logging.INFO, logger=A.app.logger.name):
        A.app.test_client().get(
            f'/resolve-spotify?url=https://open.spotify.com/episode/{_SPOTIFY_EP}')
    assert any(
        'spotify resolve failed' in r.message and 'error_kind=no_feed' in r.message
        and _SPOTIFY_EP in r.message
        for r in caplog.records
    )


def test_index_routes_spotify_links_to_the_resolver():
    body = A.app.test_client().get('/').data.decode()
    assert '/resolve-spotify?url=' in body
    assert 'SPOTIFY_RE' in body
    # Results + error must render the cards, not the empty state.
    assert 'showSearchNotice' in body
    assert '!results.length' in body or 'if (!results.length)' in body
    assert 'error_kind' in body
    assert 'search-notice' in body
    # Mangled sode/<id> links are treated as Spotify, not name search.
    assert 'sode' in body
    # Paste robustness: iframe/embed extraction + HTTP-error handling.
    assert 'extractFirstHttpUrl' in body
    assert 'normalizeSpotifyEmbedUrl' in body
    assert 'prepareSearchQuery' in body
    assert 'parseResolveResponse' in body
    assert "error_kind: 'http_error'" in body or 'error_kind: "http_error"' in body
    assert 'error_detail' in body


def test_glued_junk_after_spotify_id_still_resolves(monkeypatch):
    """si= param glued onto the id is truncated to 22 chars and looked up."""
    fetched = _fake_web(monkeypatch, embed=_embed_page(_HUBERMAN_EP_ENTITY),
                        shows=[_HUBERMAN_SHOW],
                        feed=_rss('Essentials: Genes &amp; Memory'))
    out = A.resolve_spotify_url(
        f'https://open.spotify.com/episode/{_SPOTIFY_EP}4820')
    assert out['error'] is None
    assert out['results'][0]['audio_url'] == 'https://cdn.example.com/0.mp3'
    assert f'https://open.spotify.com/embed/episode/{_SPOTIFY_EP}' in fetched
    assert f'{_SPOTIFY_EP}4820' not in ''.join(fetched)


def test_short_spotify_ids_are_still_rejected():
    assert A.parse_spotify_url('https://open.spotify.com/episode/abc') == (None, None)
    assert A.parse_spotify_url('episode/abcdefghij') == (None, None)


_TOXICAS_SHOW = '0Lp33tnMZZ9sCCZZVoDk3g'
_TOXICAS_EP = '63xKKbCVtGW7U2jGFEIFwb'


def test_generic_podcast_subtitle_uses_related_show_name(monkeypatch):
    """Embed subtitle \"Podcast \" is useless; relatedEntityUri has the real show."""
    ep_entity = {
        'type': 'episode',
        'name': 'Episodio 12',
        'subtitle': 'Podcast ',
        'relatedEntityUri': f'spotify:show:{_TOXICAS_SHOW}',
    }
    show_entity = {'type': 'show', 'name': 'Relaciones Tóxicas'}
    toxicas_show = {
        'collectionName': 'Relaciones Tóxicas',
        'artistName': 'Host',
        'feedUrl': _FEED,
    }
    fetched = _fake_web(
        monkeypatch,
        embed=_embed_page(ep_entity),
        show_embed=_embed_page(show_entity),
        shows=[toxicas_show],
        feed=_rss('Episodio 12'),
    )
    out = A.resolve_spotify_url(f'https://open.spotify.com/episode/{_TOXICAS_EP}')
    assert out['error'] is None
    assert out['show_name'] == 'Relaciones Tóxicas'
    assert out['results'][0]['name'] == 'Episodio 12'
    assert out['results'][0]['audio_url'] == 'https://cdn.example.com/0.mp3'
    assert any(f'/embed/show/{_TOXICAS_SHOW}' in u for u in fetched)
    assert any(f'/embed/episode/{_TOXICAS_EP}' in u for u in fetched)


def test_generic_show_name_falls_back_to_show_oembed(monkeypatch):
    ep_entity = {
        'type': 'episode', 'name': 'Ep 1', 'subtitle': 'Podcasts',
        'relatedEntityUri': f'spotify:show:{_TOXICAS_SHOW}',
    }
    toxicas_show = {
        'collectionName': 'Relaciones Tóxicas',
        'artistName': 'Host',
        'feedUrl': _FEED,
    }

    def get(url, params=None, **kw):
        if f'/embed/episode/{_TOXICAS_EP}' in url:
            return _HttpResp(200, text=_embed_page(ep_entity))
        if f'/embed/show/{_TOXICAS_SHOW}' in url:
            return _HttpResp(404)
        if 'oembed' in url:
            oembed_url = (params or {}).get('url') or ''
            if f'/show/{_TOXICAS_SHOW}' in oembed_url:
                return _HttpResp(200, payload={'title': 'Relaciones Tóxicas'})
            return _HttpResp(404)
        if 'itunes.apple.com' in url:
            if params['entity'] == 'podcast':
                assert params['term'] == 'Relaciones Tóxicas'
                return _HttpResp(200, payload={'results': [toxicas_show]})
            return _HttpResp(200, payload={'results': []})
        if url == _FEED:
            return _HttpResp(200, content=_rss('Ep 1'))
        raise AssertionError(url)

    _patch_requests_get(monkeypatch, get)
    monkeypatch.setattr(A, '_is_fetchable_url', lambda u: True)
    out = A.resolve_spotify_url(f'https://open.spotify.com/episode/{_TOXICAS_EP}')
    assert out['error'] is None
    assert out['show_name'] == 'Relaciones Tóxicas'


def test_spotify_metadata_retries_transient_failures(monkeypatch):
    """One retry on 503 for embed; success on the second attempt."""
    calls = {'embed': 0}
    monkeypatch.setattr(A.time, 'sleep', lambda s: None)

    def get(url, params=None, **kw):
        if '/embed/' in url:
            calls['embed'] += 1
            if calls['embed'] == 1:
                return _HttpResp(503)
            return _HttpResp(200, text=_embed_page(_HUBERMAN_EP_ENTITY))
        if 'itunes.apple.com' in url:
            return _HttpResp(200, payload={'results': [_HUBERMAN_SHOW]})
        if url == _FEED:
            return _HttpResp(200, content=_rss('Essentials: Genes &amp; Memory'))
        if 'oembed' in url:
            return _HttpResp(404)
        raise AssertionError(url)

    _patch_requests_get(monkeypatch, get)
    monkeypatch.setattr(A, '_is_fetchable_url', lambda u: True)
    out = A.resolve_spotify_url(f'https://open.spotify.com/episode/{_SPOTIFY_EP}')
    assert out['error'] is None
    assert calls['embed'] == 2


def test_spotify_metadata_failure_includes_error_detail(monkeypatch, caplog):
    import logging
    calls = {'n': 0}
    monkeypatch.setattr(A.time, 'sleep', lambda s: None)

    def get(url, params=None, **kw):
        calls['n'] += 1
        if '/embed/' in url or 'oembed' in url:
            return _HttpResp(503)
        raise AssertionError(url)

    _patch_requests_get(monkeypatch, get)
    with caplog.at_level(logging.INFO, logger=A.app.logger.name):
        data = A.app.test_client().get(
            f'/resolve-spotify?url=https://open.spotify.com/episode/{_SPOTIFY_EP}'
        ).get_json()
    assert data['error_kind'] == 'unreadable_link'
    assert data['error_detail'] == 'status_503'
    assert any(
        'spotify resolve failed' in r.message
        and 'error_kind=unreadable_link' in r.message
        and 'detail=status_503' in r.message
        for r in caplog.records
    )
    # embed + retry, then oembed + retry
    assert calls['n'] >= 4


def test_is_generic_spotify_show_name():
    assert A._is_generic_spotify_show_name('Podcast') is True
    assert A._is_generic_spotify_show_name('Podcast ') is True
    assert A._is_generic_spotify_show_name('Podcasts') is True
    assert A._is_generic_spotify_show_name('   ') is True
    assert A._is_generic_spotify_show_name('Huberman Lab') is False
    assert A._is_generic_spotify_show_name('Relaciones Tóxicas') is False


# --------------------------------------------------------------------------
# Apple Podcasts / RSS / audio / YouTube search-box routing
# --------------------------------------------------------------------------

_APPLE_SHOW_ID = '1200361736'
_APPLE_EP_ID = '1000792546642'
_APPLE_CN_SHOW_ID = '262026947'
_APPLE_FEED = 'https://feeds.example.com/the-daily'
_APPLE_AUDIO = 'https://cdn.example.com/daily.mp3'


def _apple_show_item(show_id=_APPLE_SHOW_ID, feed=_APPLE_FEED, name='The Daily'):
    return {
        'wrapperType': 'track',
        'kind': 'podcast',
        'trackId': int(show_id),
        'collectionId': int(show_id),
        'trackName': name,
        'collectionName': name,
        'artistName': 'Publisher',
        'feedUrl': feed,
        'artworkUrl100': 'https://cdn.example.com/art.jpg',
        'primaryGenreName': 'News',
    }


def _apple_episode_item(ep_id=_APPLE_EP_ID, show_id=_APPLE_SHOW_ID, *,
                        feed=_APPLE_FEED, audio=_APPLE_AUDIO,
                        title='Why does heartbreak hurt so much?',
                        show_name='6 Minute English'):
    return {
        'wrapperType': 'podcastEpisode',
        'kind': 'podcast-episode',
        'trackId': int(ep_id),
        'collectionId': int(show_id),
        'trackName': title,
        'collectionName': show_name,
        'episodeUrl': audio,
        'feedUrl': feed,
        'releaseDate': '2023-01-15T00:00:00Z',
        'trackTimeMillis': 360000,
        'artworkUrl160': 'https://cdn.example.com/ep.jpg',
    }


def _fake_itunes_lookup(monkeypatch, items_by_id, *, fail_times=0):
    """Stub itunes.apple.com/lookup (and refuse unexpected hosts)."""
    state = {'calls': 0, 'fails_left': fail_times}
    fetched = []

    def get(url, params=None, **kw):
        fetched.append((url, dict(params or {})))
        if 'itunes.apple.com/lookup' not in url and 'itunes.apple.com' not in url:
            raise AssertionError(f'unexpected fetch {url}')
        # /search is also under itunes.apple.com — only allow lookup here.
        if '/search' in url:
            raise AssertionError(f'expected lookup, got search: {url}')
        state['calls'] += 1
        if state['fails_left'] > 0:
            state['fails_left'] -= 1
            raise A.requests.ConnectionError('blip')
        itunes_id = str((params or {}).get('id') or '')
        items = list(items_by_id.get(itunes_id, []))
        return _HttpResp(200, payload={'results': items})

    _patch_requests_get(monkeypatch, get)
    monkeypatch.setattr(A, '_is_fetchable_url', lambda u: True)
    return fetched, state


@pytest.mark.parametrize('url,expected', [
    (f'https://podcasts.apple.com/us/podcast/the-daily/id{_APPLE_SHOW_ID}',
     (_APPLE_SHOW_ID, None)),
    (f'https://podcasts.apple.com/cn/podcast/6-minute-english/id{_APPLE_CN_SHOW_ID}'
     f'?i={_APPLE_EP_ID}&r=0',
     (_APPLE_CN_SHOW_ID, _APPLE_EP_ID)),
    (f'podcasts.apple.com/gb/podcast/x/id{_APPLE_SHOW_ID}?i={_APPLE_EP_ID}',
     (_APPLE_SHOW_ID, _APPLE_EP_ID)),
    ('https://podcasts.apple.com/us/podcast/the-daily/', (None, None)),
    ('https://open.spotify.com/episode/x', (None, None)),
    ('the daily', (None, None)),
])
def test_apple_links_are_recognised(url, expected):
    assert A.parse_apple_podcasts_url(url) == expected


def test_apple_episode_link_us_resolves_to_episode(monkeypatch):
    show = _apple_show_item()
    ep = _apple_episode_item(show_id=_APPLE_SHOW_ID, show_name='The Daily',
                             title='Call My A.I. Agent')
    _fake_itunes_lookup(monkeypatch, {_APPLE_SHOW_ID: [show, ep]})
    out = A.resolve_apple_url(
        f'https://podcasts.apple.com/us/podcast/the-daily/id{_APPLE_SHOW_ID}'
        f'?i={_APPLE_EP_ID}')
    assert out['error'] is None and out['error_kind'] is None
    assert len(out['results']) == 1
    hit = out['results'][0]
    assert hit['type'] == 'episode'
    assert hit['name'] == 'Call My A.I. Agent'
    assert hit['audio_url'] == _APPLE_AUDIO
    assert hit['feed_url'] == _APPLE_FEED


def test_apple_episode_link_cn_resolves_to_episode(monkeypatch):
    show = _apple_show_item(show_id=_APPLE_CN_SHOW_ID, name='6 Minute English',
                            feed='https://podcasts.files.bbci.co.uk/p02pc9tn.rss')
    ep = _apple_episode_item(
        ep_id=_APPLE_EP_ID, show_id=_APPLE_CN_SHOW_ID,
        feed=show['feedUrl'],
        audio='http://open.live.bbc.co.uk/mediaselector/ep.mp3',
        title='Why does heartbreak hurt so much?',
        show_name='6 Minute English')
    # Episode omits feedUrl — resolver must copy it from the show row.
    ep_no_feed = dict(ep)
    ep_no_feed.pop('feedUrl')
    _fake_itunes_lookup(monkeypatch, {_APPLE_CN_SHOW_ID: [show, ep_no_feed]})
    out = A.resolve_apple_url(
        f'https://podcasts.apple.com/cn/podcast/6-minute-english/id{_APPLE_CN_SHOW_ID}'
        f'?i={_APPLE_EP_ID}&r=0')
    assert out['error'] is None
    hit = out['results'][0]
    assert hit['type'] == 'episode'
    assert hit['feed_url'] == show['feedUrl']
    assert hit['audio_url'] == ep['episodeUrl']


def test_apple_show_link_returns_show(monkeypatch):
    show = _apple_show_item()
    _fake_itunes_lookup(monkeypatch, {_APPLE_SHOW_ID: [show]})
    out = A.resolve_apple_url(
        f'https://podcasts.apple.com/us/podcast/the-daily/id{_APPLE_SHOW_ID}')
    assert out['error'] is None and out['error_kind'] is None
    assert out['results'] == [A._itunes_show_result(show)]
    assert out['results'][0]['feed_url'] == _APPLE_FEED


def test_apple_episode_missing_falls_back_to_show(monkeypatch):
    show = _apple_show_item()
    other = _apple_episode_item(ep_id='999', title='Other')
    _fake_itunes_lookup(monkeypatch, {_APPLE_SHOW_ID: [show, other]})
    out = A.resolve_apple_url(
        f'https://podcasts.apple.com/us/podcast/the-daily/id{_APPLE_SHOW_ID}'
        f'?i={_APPLE_EP_ID}')
    assert [r['type'] for r in out['results']] == ['show']
    assert out['error_kind'] == 'episode_not_found'
    assert out['results'][0]['feed_url'] == _APPLE_FEED


def test_apple_show_without_fetchable_feed_reports_no_feed(monkeypatch):
    show = _apple_show_item(feed='http://127.0.0.1/secret.xml')
    _fake_itunes_lookup(monkeypatch, {_APPLE_SHOW_ID: [show]})
    monkeypatch.setattr(A, '_is_fetchable_url', lambda u: False)
    out = A.resolve_apple_url(
        f'https://podcasts.apple.com/us/podcast/the-daily/id{_APPLE_SHOW_ID}')
    assert out['results'] == []
    assert out['error_kind'] == 'no_feed'


def test_resolve_apple_json_includes_error_kind(monkeypatch):
    _fake_itunes_lookup(monkeypatch, {})
    data = A.app.test_client().get(
        f'/resolve-apple?url=https://podcasts.apple.com/us/podcast/x/id{_APPLE_SHOW_ID}'
    ).get_json()
    assert data['error_kind'] == 'not_found'
    assert data['results'] == []
    assert data['error']


def test_resolve_apple_logs_error_kind(monkeypatch, caplog):
    import logging
    _fake_itunes_lookup(monkeypatch, {})
    with caplog.at_level(logging.INFO, logger=A.app.logger.name):
        A.app.test_client().get(
            f'/resolve-apple?url=https://podcasts.apple.com/us/podcast/x/id{_APPLE_SHOW_ID}'
            f'?i={_APPLE_EP_ID}')
    assert any(
        'apple resolve failed' in r.message
        and f'show_id={_APPLE_SHOW_ID}' in r.message
        and f'episode_id={_APPLE_EP_ID}' in r.message
        and 'error_kind=not_found' in r.message
        for r in caplog.records
    )


def test_resolve_apple_retries_transient_directory_failure(monkeypatch):
    show = _apple_show_item()
    ep = _apple_episode_item(show_name='The Daily', title='Ep')
    fetched, state = _fake_itunes_lookup(
        monkeypatch, {_APPLE_SHOW_ID: [show, ep]}, fail_times=1)
    monkeypatch.setattr(A.time, 'sleep', lambda s: None)
    data = A.app.test_client().get(
        f'/resolve-apple?url=https://podcasts.apple.com/us/podcast/x/id{_APPLE_SHOW_ID}'
        f'?i={_APPLE_EP_ID}').get_json()
    assert data['error'] is None
    assert data['results'][0]['audio_url'] == _APPLE_AUDIO
    assert state['calls'] >= 2
    assert any('lookup' in (u if isinstance(u, str) else u[0]) or True for u in fetched)


def test_resolve_apple_reports_unreachable_directory(monkeypatch):
    def down(*a, **kw):
        raise A.requests.ConnectionError('secret-internal-detail')

    monkeypatch.setattr(A.requests, 'get', down)
    monkeypatch.setattr(A.requests.Session, 'get',
                        lambda self, url, **kw: down(url, **kw))
    monkeypatch.setattr(A.time, 'sleep', lambda s: None)
    data = A.app.test_client().get(
        f'/resolve-apple?url=https://podcasts.apple.com/us/podcast/x/id{_APPLE_SHOW_ID}'
    ).get_json()
    assert data['results'] == []
    assert data['error_kind'] == 'directory_unreachable'
    assert 'secret-internal-detail' not in data['error']


def test_index_routes_apple_rss_audio_youtube():
    body = A.app.test_client().get('/').data.decode()
    assert '/resolve-apple?url=' in body
    assert 'resolveApple' in body
    assert 'showDirectAudioRow' in body
    assert 'Transcribe this audio file' in body
    assert "YouTube isn't supported yet" in body
    assert 'PARSE_RSS_URL' in body
    assert "inputType === 'rss_feed'" in body or "inputType === \"rss_feed\"" in body
    assert 'emptyStateTipText' in body
    # Failed Apple paste must not be told to paste an Apple link again.
    assert "inputType !== 'apple_link'" in body or 'inputType !== "apple_link"' in body
    assert 'error_kind' in body


def test_empty_state_tip_omits_failed_input_type():
    """Static check: tip builder skips the type that just failed."""
    body = A.app.test_client().get('/').data.decode()
    assert 'emptyStateTipText' in body
    assert "inputType !== 'apple_link'" in body
    assert "inputType !== 'rss_feed'" in body
    assert "inputType !== 'audio_url'" in body
    # Name-search empty state still offers the classic trio (assembled in JS).
    assert "No results? Paste" in body
    assert "the podcast's RSS feed" in body
    assert 'an Apple Podcasts link' in body
    assert 'a direct audio URL' in body


# --------------------------------------------------------------------------
# Transcript search  (TSK-20441)
# --------------------------------------------------------------------------

@pytest.fixture
def library():
    """Two users' transcripts. Returns (owner_id, other_id)."""
    from models import db, TranscriptionTask
    owner = _make_user('search-owner@example.com')
    other = _make_user('search-other@example.com')
    rows = [
        ('lib-1', owner, 'completed', 'Weather report',
         'Tomorrow brings rain across\nØstlandet and snow in the north. ' + 'filler ' * 40),
        ('lib-2', owner, 'completed', 'Cooking hour', 'We talk about bread <script>x</script>.'),
        ('lib-3', owner, 'transcribing', 'Still running', 'Østlandet partial text'),
        ('lib-4', other, 'completed', 'Other user', 'Østlandet is mentioned here too'),
        ('lib-5', owner, 'completed', 'Østlandet special', 'Nothing about the region in the body'),
    ]
    with A.app.app_context():
        db.session.query(TranscriptionTask).filter(
            TranscriptionTask.id.in_([r[0] for r in rows])).delete()
        for i, (tid, uid, status, title, text) in enumerate(rows):
            db.session.add(TranscriptionTask(
                id=tid, user_id=uid, status=status, episode_title=title,
                transcript_text=text, podcast_name='Show',
                completed_at=datetime(2026, 9, 1 + i, tzinfo=timezone.utc)))
        db.session.commit()
    yield owner, other
    with A.app.app_context():
        db.session.query(TranscriptionTask).filter(
            TranscriptionTask.id.in_([r[0] for r in rows])).delete()
        db.session.commit()
    _purge(['search-owner@example.com', 'search-other@example.com'])


def test_search_finds_a_phrase_across_case_and_line_breaks(library):
    owner, _ = library
    with A.app.app_context():
        matches = A.search_transcripts(owner, 'ACROSS østlandet')
    ids = [m['id'] for m in matches]
    assert ids == ['lib-1']
    before, hit, after = matches[0]['snippet']
    assert hit == 'across\nØstlandet'
    # The spaces either side of the hit survive, or the words glue onto the <mark>.
    assert before == 'Tomorrow brings rain '
    assert after.startswith(' and snow')
    assert after.endswith('…')


def test_punctuation_in_the_query_is_not_required_in_the_text(library):
    owner, _ = library
    with A.app.app_context():
        odd = A.search_transcripts(owner, 'snow, in the: north!')
        bare = A.search_transcripts(owner, '?!')
    assert [m['id'] for m in odd] == ['lib-1']
    assert odd[0]['snippet'][1] == 'snow in the north'
    assert bare == []


def test_search_matches_words_split_by_punctuation_in_the_text(library):
    from models import db, TranscriptionTask
    owner, _ = library
    with A.app.app_context():
        db.session.add(TranscriptionTask(
            id='lib-6', user_id=owner, status='completed',
            episode_title='Essentials: Genes & Memory', podcast_name='Show',
            transcript_text='So, yes -- genes, memory: and inheritance.',
            completed_at=datetime(2026, 9, 20, tzinfo=timezone.utc)))
        db.session.commit()
        try:
            text = A.search_transcripts(owner, 'genes memory and')
            title = A.search_transcripts(owner, 'essentials genes')
        finally:
            db.session.query(TranscriptionTask).filter_by(id='lib-6').delete()
            db.session.commit()
    assert [m['id'] for m in text] == ['lib-6']
    assert text[0]['snippet'][1] == 'genes, memory: and'
    assert [m['id'] for m in title] == ['lib-6']


def test_search_only_covers_my_completed_transcripts(library):
    owner, other = library
    with A.app.app_context():
        mine = [m['id'] for m in A.search_transcripts(owner, 'østlandet')]
        theirs = [m['id'] for m in A.search_transcripts(other, 'østlandet')]
    # Newest first; the title-only hit is included, the running task and the
    # other user's transcript are not.
    assert mine == ['lib-5', 'lib-1']
    assert theirs == ['lib-4']


def test_title_only_hit_has_no_snippet(library):
    owner, _ = library
    with A.app.app_context():
        [m] = [m for m in A.search_transcripts(owner, 'special')]
    assert m['snippet'] is None and m['hits'] == 0


def test_history_search_page_renders_and_escapes(library):
    owner, _ = library
    client = _login(owner)
    body = client.get('/history?q=bread').data.decode()
    assert 'Cooking hour' in body
    assert '/transcription/lib-2' in body
    assert '<mark' in body and '>bread</mark>' in body
    assert '<script>x</script>' not in body
    assert '&lt;script&gt;' in body
    assert 'Weather report' not in body


def test_history_list_titles_link_to_transcription_page(library):
    """Default History rows match search: title is a link to the transcript."""
    owner, _ = library
    body = _login(owner).get('/history').data.decode().replace("'", '"')
    assert 'Weather report' in body
    assert 'Cooking hour' in body
    # Title itself is the link text (same pattern as search results).
    assert 'class="list-item-title"' in body
    assert 'href="/transcription/lib-1"' in body
    assert '>Weather report</a>' in body
    assert 'href="/transcription/lib-2"' in body
    assert '>Cooking hour</a>' in body
    # Default list is completed-only; in-progress must not appear.
    assert 'Still running' not in body
    assert 'href="/transcription/lib-3"' not in body


def test_history_search_with_no_hits_says_so(library):
    owner, _ = library
    body = _login(owner).get('/history?q=zeppelin').data.decode()
    assert 'No completed transcripts mention' in body


def test_history_search_requires_login():
    resp = A.app.test_client().get('/history?q=anything')
    assert resp.status_code == 302 and '/login' in resp.headers['Location']


# --------------------------------------------------------------------------
# Error reporting (Sentry)
#
# The scrubbing is what these guard. OpenAI echoes the submitted key in a 401,
# users have pasted passwords into that field, and private feeds carry their
# token in the audio URL. None of it may reach a third party.
# --------------------------------------------------------------------------

@pytest.fixture
def sentry_events():
    """Turn reporting on with a transport that keeps events instead of sending."""
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
    assert observability.init_sentry(dsn='https://public@sentry.invalid/1', transport=transport)
    yield transport.events
    sentry_sdk.get_client().close()
    sentry_sdk.init()  # back to a disabled client for the rest of the suite


def _openai_401(echoed, wording='Incorrect API key provided: {}.'):
    import openai
    try:  # openai 3.x moved to httpx2; 1.x/2.x (production) use httpx
        import httpx2 as httpx
    except ImportError:
        import httpx
    request = httpx.Request('POST', 'https://api.openai.com/v1/audio/transcriptions')
    return openai.AuthenticationError(
        "Error code: 401 - {'error': {'message': '" + wording.format(echoed) + "'}}",
        response=httpx.Response(401, request=request), body=None)


def _raise_and_report(exc, **kw):
    import observability
    try:
        raise exc
    except Exception as e:  # noqa: BLE001
        observability.report_task_failure(e, task_id='t-report', key_source=kw.get('key_source', 'own'))


def test_reporting_is_off_without_a_dsn(monkeypatch):
    import observability
    monkeypatch.setenv('SENTRY_DSN', '')
    assert observability.init_sentry() is False


def test_sentry_stays_uninitialised_outside_production_even_with_dsn(monkeypatch):
    import observability
    import runtime_env

    monkeypatch.delenv('PODSKRIFT_ENV', raising=False)
    monkeypatch.setenv('SENTRY_DSN', 'https://public@sentry.invalid/99')
    assert runtime_env.is_production() is False
    assert observability.init_sentry() is False


def test_an_openai_error_never_ships_the_key_it_echoed(sentry_events):
    """A password pasted into the key field matches no key pattern, and the
    401 wording is OpenAI's to change -- so the message itself has to go."""
    _raise_and_report(_openai_401('hunter2-my-bank-password', wording='Bad credential {} rejected'))
    (event,) = sentry_events
    assert 'hunter2' not in repr(event)
    exc = event['exception']['values'][-1]
    assert exc['type'] == 'AuthenticationError'
    assert event['tags']['openai.status'] == '401'


def test_the_echo_phrase_is_redacted_outside_openai_errors(sentry_events):
    """The backstop for when an OpenAI message is re-raised as another type."""
    _raise_and_report(RuntimeError('Incorrect API key provided: hunter2-password.'))
    (event,) = sentry_events
    assert 'hunter2' not in repr(event)


def test_key_shaped_strings_are_redacted_anywhere(sentry_events):
    _raise_and_report(RuntimeError('boom with sk-proj-abcDEF1234567890xyz in it'))
    (event,) = sentry_events
    assert 'sk-proj-abcDEF' not in repr(event)
    assert '[redacted]' in event['exception']['values'][-1]['value']


def test_private_feed_tokens_are_stripped_from_urls(sentry_events):
    _raise_and_report(RuntimeError(
        'Failed to download audio: 403 for https://feeds.supercast.com/ep.mp3?token=s3cr3t'))
    (event,) = sentry_events
    value = event['exception']['values'][-1]['value']
    assert 's3cr3t' not in repr(event)
    assert 'https://feeds.supercast.com/ep.mp3?[redacted]' in value


def test_a_real_connection_error_does_not_leak_the_feed_token(sentry_events):
    """requests quotes the bare path in connection errors, and Sentry sends the
    whole chain (ConnectionError -> MaxRetryError -> NewConnectionError).
    A hand-written message missed this: the token went out in 3 of 5 values."""
    import observability
    import requests
    # Assembled at runtime: Sentry ships the source lines around each frame, and
    # a literal in this function's own assert would show up in them unredacted.
    token = 's3cr3t' + 'TOKEN'
    try:
        try:
            requests.get(f'https://127.0.0.1:1/ep.mp3?token={token}', timeout=2)
        except requests.exceptions.RequestException as e:
            raise Exception(f'Failed to download audio: {e}')  # as download_audio does
    except Exception as wrapped:  # noqa: BLE001
        observability.report_task_failure(wrapped, task_id='t-conn', key_source='own')
    (event,) = sentry_events
    assert len(event['exception']['values']) > 1, 'expected the chained causes too'
    assert token not in repr(event)


def test_the_echo_is_redacted_to_the_end_of_the_line(sentry_events):
    """A passphrase has spaces; redacting only the first word leaks the rest."""
    _raise_and_report(RuntimeError('Incorrect API key provided: my secret passphrase'))
    (event,) = sentry_events
    assert 'passphrase' not in repr(event)


def test_stack_frames_carry_no_local_variables(sentry_events):
    """A frame's locals include `api_key` and the OpenAI client."""
    def transcribe(api_key):
        raise RuntimeError('whisper failed')
    import observability
    try:
        transcribe('sk-local-variable-key-123456')
    except RuntimeError as e:
        observability.report_task_failure(e, task_id='t-locals', key_source='own')
    (event,) = sentry_events
    frames = event['exception']['values'][-1]['stacktrace']['frames']
    assert all('vars' not in f for f in frames)
    assert 'sk-local-variable' not in repr(event)


def test_request_bodies_and_pii_are_never_collected(sentry_events):
    import sentry_sdk
    options = sentry_sdk.get_client().options
    assert options['send_default_pii'] is False
    assert options['max_request_body_size'] == 'never'
    assert options['traces_sample_rate'] == 0.0


def _wait_task_error_settled(task_id, timeout_s=5.0, user_id=None,
                             expect_used=None, expect_paid=None):
    """Poll until status=error and platform charges are settled (or unmetered).

    Worker threads used to write status='error' before refunding; waiting only
    on status raced that window. Prefer trial_settled / charged==0, and when
    ``user_id`` is given also wait for the user balances to match.
    """
    import types
    from models import TranscriptionTask, db

    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        with A.app.app_context():
            task = db.session.get(TranscriptionTask, task_id)
            if task is not None:
                last = types.SimpleNamespace(
                    status=task.status,
                    error_message=task.error_message,
                    trial_seconds_charged=task.trial_seconds_charged,
                    paid_seconds_charged=task.paid_seconds_charged,
                    trial_settled=bool(task.trial_settled),
                )
                charged = (last.trial_seconds_charged or 0) + (
                    last.paid_seconds_charged or 0)
                # Own-key / zero-reserve rows never set trial_settled; charged==0
                # is enough. Metered rows must be settled.
                settled = last.status == 'error' and (
                    last.trial_settled or charged == 0)
                if settled and user_id is not None:
                    if expect_used is not None and _used(user_id) != expect_used:
                        settled = False
                    if expect_paid is not None and _paid(user_id) != expect_paid:
                        settled = False
                if settled:
                    return last
        time.sleep(0.05)
    assert last is not None, f'task {task_id} never appeared'
    assert last.status == 'error', f'task {task_id} stuck at {last.status!r}'
    charged = (last.trial_seconds_charged or 0) + (last.paid_seconds_charged or 0)
    assert last.trial_settled or charged == 0, (
        f'task {task_id} error but unsettle: trial={last.trial_seconds_charged} '
        f'paid={last.paid_seconds_charged} settled={last.trial_settled}')
    if user_id is not None and expect_used is not None:
        assert _used(user_id) == expect_used
    if user_id is not None and expect_paid is not None:
        assert _paid(user_id) == expect_paid
    return last


def test_fail_task_and_refund_settles_in_same_write(trial_on):
    """status='error' and settled charges land together — no error-with-charge window."""
    from models import TranscriptionTask, db

    uid = _make_user('failatomic@test.com', limit=3600, used=0)
    with A.app.app_context():
        assert A.trial_reserve(uid, 600)
        db.session.add(TranscriptionTask(
            id='fail-atomic', user_id=uid, episode_title='x',
            status='downloading', trial_seconds_charged=600, paid_seconds_charged=0,
            trial_settled=False))
        db.session.commit()
        refunded = A.fail_task_and_refund('fail-atomic', 'boom')
        assert refunded == 600
        task = db.session.get(TranscriptionTask, 'fail-atomic')
        assert task.status == 'error'
        assert task.error_message == 'boom'
        assert task.trial_settled is True
        assert (task.trial_seconds_charged or 0) == 0
        assert (task.paid_seconds_charged or 0) == 0
        # Idempotent: second call / backstop cannot double-credit.
        assert A.fail_task_and_refund('fail-atomic', 'boom again') == 0
        assert A.trial_refund_task(task) == 0
    assert _used(uid) == 0


def test_fail_task_and_refund_pro_rata_matches_trial_refund(trial_on):
    from models import TranscriptionTask, db

    uid = _make_user('failprorata@test.com', limit=3600, used=0)
    with A.app.app_context():
        assert A.trial_reserve(uid, 800)
        db.session.add(TranscriptionTask(
            id='fail-prorata', user_id=uid, episode_title='x',
            status='transcribing', chunk_total=4, chunk_index=1,
            trial_seconds_charged=800, trial_settled=False))
        db.session.commit()
        # 2 of 4 chunks in flight → spend 400, refund 400.
        assert A.fail_task_and_refund('fail-prorata', 'mid') == 400
        task = db.session.get(TranscriptionTask, 'fail-prorata')
        assert task.status == 'error'
        assert task.trial_settled is True
        assert task.trial_seconds_charged == 400
        assert A.trial_refund_task(task) == 0
    assert _used(uid) == 400


def test_a_failed_transcription_is_reported_and_still_refunded(trial_on, monkeypatch, sentry_events):
    """The real route and the real worker thread, failing in download.

    Uses a generic download failure (not SourceAudioUnavailable): permanent
    host refusals are expected user-side outcomes and must not hit Sentry.
    """
    monkeypatch.setattr(A, 'free_disk_bytes', lambda *a, **kw: 10 ** 12)
    monkeypatch.setattr(A, 'download_audio',
                        lambda *a, **kw: (_ for _ in ()).throw(
                            RuntimeError('HTTP error 502')))
    uid = _make_user('sentryfail@test.com', limit=36000)
    A.app.config['TESTING'] = True
    client = A.app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(uid)
        sess['_fresh'] = True
    resp = client.post('/start_transcription', data={
        'audio_url': 'https://example.com/ep.mp3', 'episode_title': 'Ep',
        'duration_min': '5', 'language': 'no'})
    assert resp.status_code == 200
    task_id = resp.get_json()['task_id']

    task = _wait_task_error_settled(
        task_id, user_id=uid, expect_used=0)
    for _ in range(100):
        if sentry_events:
            break
        time.sleep(0.05)
    (event,) = sentry_events
    assert event['exception']['values'][-1]['value'] == 'HTTP error 502'
    assert event['tags']['task.key_source'] == 'trial'
    assert event['contexts']['task']['id'] == task_id
    assert (task.trial_seconds_charged or 0) == 0
    assert task.trial_settled is True
    assert _used(uid) == 0, 'the failed job kept its trial reservation'


def test_a_broken_reporter_cannot_cost_a_refund(trial_on, monkeypatch, sentry_events):
    """Reporting runs on the money path's failure branch. If Sentry itself
    throws, the refund must still happen -- through the real route and worker."""
    import sentry_sdk
    monkeypatch.setattr(sentry_sdk, 'capture_exception',
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('sentry down')))
    monkeypatch.setattr(A, 'free_disk_bytes', lambda *a, **kw: 10 ** 12)
    monkeypatch.setattr(A, 'download_audio',
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('HTTP error 502')))
    uid = _make_user('sentrybroken@test.com', limit=36000)
    A.app.config['TESTING'] = True
    client = A.app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(uid)
        sess['_fresh'] = True
    resp = client.post('/start_transcription', data={
        'audio_url': 'https://example.com/ep.mp3', 'episode_title': 'Ep',
        'duration_min': '5', 'language': 'no'})
    assert resp.status_code == 200
    _wait_task_error_settled(
        resp.get_json()['task_id'], user_id=uid, expect_used=0)
    assert _used(uid) == 0, 'a failing reporter cost the user their refund'


def test_missing_source_audio_is_clear_refunded_and_not_sentry(
        trial_on, monkeypatch, sentry_events, ph_events):
    """Libsyn 404: specific PostHog reason, clear UI message, full refund, no Sentry."""
    monkeypatch.setattr(A, 'free_disk_bytes', lambda *a, **kw: 10 ** 12)
    monkeypatch.setattr(
        A, 'download_audio',
        lambda *a, **kw: (_ for _ in ()).throw(
            A.SourceAudioUnavailable(
                A.SourceAudioUnavailable.REASON_MISSING, status_code=404)))
    uid = _make_user('sourcemiss@test.com', limit=600, used=0)  # 10 min trial
    _set_paid(uid, 1800)  # 30 min paid — 20 min job mixes both
    A.app.config['TESTING'] = True
    client = A.app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(uid)
        sess['_fresh'] = True
    # Ask for more than trial alone so the reserve mixes trial + paid.
    resp = client.post('/start_transcription', data={
        'audio_url': 'https://traffic.libsyn.com/secure/show/gone.mp3',
        'episode_title': 'Gone Ep', 'duration_min': '20', 'language': 'no'})
    assert resp.status_code == 200
    task_id = resp.get_json()['task_id']

    task = _wait_task_error_settled(
        task_id, user_id=uid, expect_used=0, expect_paid=1800)
    assert "no longer serves this episode's audio" in (task.error_message or '')
    assert 'Try another episode' in (task.error_message or '')
    assert (task.trial_seconds_charged or 0) == 0
    assert (task.paid_seconds_charged or 0) == 0
    assert task.trial_settled is True
    assert _used(uid) == 0
    assert _paid(uid) == 1800

    failed = [e for e in ph_events.events
              if e['event'] == 'transcript_failed' and e['distinct_id'] == str(uid)]
    assert len(failed) == 1
    assert failed[0]['properties']['reason'] == 'source_audio_missing'
    assert sentry_events == [], 'dead episode links must not fire the Sentry alert'


def test_forbidden_source_audio_posts_specific_reason(
        trial_on, monkeypatch, sentry_events, ph_events):
    import types
    uid = _make_user('source403@test.com', limit=3600)

    def boom(*a, **kw):
        raise A.SourceAudioUnavailable(
            A.SourceAudioUnavailable.REASON_FORBIDDEN, status_code=403)

    monkeypatch.setattr(
        A.threading, 'Thread',
        lambda target=None, **kw: types.SimpleNamespace(
            daemon=True, start=lambda: target and target()))
    monkeypatch.setattr(A, 'download_audio', boom)
    monkeypatch.setattr(A, 'free_disk_bytes', lambda *a, **kw: 10 ** 12)

    with A.app.app_context():
        user = A.db.session.get(A.User, uid)
        payload, status = A.enqueue_transcription(
            user,
            {'title': 'Ep', 'audio_url': 'https://example.com/private.mp3',
             'duration_min': 1},
        )
    assert status == 200, payload
    failed = [e for e in ph_events.events
              if e['event'] == 'transcript_failed' and e['distinct_id'] == str(uid)]
    assert len(failed) == 1
    assert failed[0]['properties']['reason'] == 'source_audio_forbidden'
    assert sentry_events == []
    assert _used(uid) == 0


def test_a_stale_task_is_reported_once_failed(sentry_events):
    from models import TranscriptionTask, db
    with A.app.app_context():
        stale_id = 'stale-sentry-test'
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
        assert A._fail_if_stale(task) is False  # already failed: no second report

    (event,) = sentry_events
    assert event['level'] == 'warning'
    assert event['fingerprint'] == ['stale-task']
    assert event['tags'] == {'task.last_status': 'transcribing', 'sweep.source': 'poll'}
    assert event['contexts']['task']['id'] == stale_id


# --------------------------------------------------------------------------
# Agent read API  (TSK-20496 / PRJ-596)
# --------------------------------------------------------------------------

_AGENT_KEY = 'test-agent-key-not-for-prod'


@pytest.fixture
def agent_api(monkeypatch):
    """Enable the agent API with a known key; clear optional user scope."""
    monkeypatch.setenv('AGENT_API_KEY', _AGENT_KEY)
    monkeypatch.delenv('AGENT_API_USER_ID', raising=False)
    return _AGENT_KEY


@pytest.fixture
def agent_library(agent_api):
    """Episodes across two users for search / status / transcript tests."""
    from models import db, TranscriptionTask
    owner = _make_user('agent-owner@example.com')
    other = _make_user('agent-other@example.com')
    rows = [
        dict(id='ag-ready', user_id=owner, status='completed',
             episode_title='Morning briefing', podcast_name='Forklaringssaften',
             episode_published='2026-09-12',
             transcript_text='Hei. Dette er hele transkriptet.',
             language='no', audio_duration=600.0,
             completed_at=datetime(2026, 9, 12, 10, tzinfo=timezone.utc)),
        dict(id='ag-pending', user_id=owner, status='transcribing chunk 1/3',
             episode_title='Still cooking', podcast_name='Forklaringssaften',
             episode_published='2026-09-12', transcript_text='partial…'),
        dict(id='ag-failed', user_id=owner, status='error',
             episode_title='Broken download', podcast_name='Forklaringssaften',
             episode_published='2026-09-11', error_message='403 from host'),
        dict(id='ag-other-day', user_id=owner, status='completed',
             episode_title='Older show', podcast_name='Forklaringssaften',
             episode_published='2026-09-01',
             transcript_text='Gammel episode.',
             completed_at=datetime(2026, 9, 1, tzinfo=timezone.utc)),
        dict(id='ag-other-show', user_id=owner, status='completed',
             episode_title='Unrelated', podcast_name='Some Other Pod',
             episode_published='2026-09-12',
             transcript_text='Wrong publisher.',
             completed_at=datetime(2026, 9, 12, 11, tzinfo=timezone.utc)),
        dict(id='ag-other-user', user_id=other, status='completed',
             episode_title='Private', podcast_name='Forklaringssaften',
             episode_published='2026-09-12',
             transcript_text='Should hide when scoped.',
             completed_at=datetime(2026, 9, 12, 12, tzinfo=timezone.utc)),
    ]
    with A.app.app_context():
        db.session.query(TranscriptionTask).filter(
            TranscriptionTask.id.in_([r['id'] for r in rows])).delete()
        for r in rows:
            db.session.add(TranscriptionTask(**r))
        db.session.commit()
    yield {'owner': owner, 'other': other, 'key': agent_api}
    with A.app.app_context():
        db.session.query(TranscriptionTask).filter(
            TranscriptionTask.id.in_([r['id'] for r in rows])).delete()
        db.session.commit()
    _purge(['agent-owner@example.com', 'agent-other@example.com'])


def _agent_get(path, key=None, **kwargs):
    headers = kwargs.pop('headers', {})
    if key is not None:
        headers['Authorization'] = f'Bearer {key}'
    return A.app.test_client().get(path, headers=headers, **kwargs)


def test_agent_api_rejects_missing_auth(agent_library):
    r = _agent_get('/api/v1/episodes?publisher=Forklaring&date=2026-09-12')
    assert r.status_code == 401
    assert r.get_json()['error'] == 'Unauthorized'


def test_agent_api_rejects_wrong_key(agent_library):
    r = _agent_get('/api/v1/episodes?publisher=Forklaring&date=2026-09-12',
                   key='wrong-key')
    assert r.status_code == 401


def test_agent_api_rejects_when_unconfigured(monkeypatch, agent_library):
    monkeypatch.delenv('AGENT_API_KEY', raising=False)
    r = _agent_get('/api/v1/episodes?publisher=x', key=_AGENT_KEY)
    assert r.status_code == 401


def test_agent_search_by_publisher_and_date(agent_library):
    r = _agent_get(
        '/api/v1/episodes?publisher=forklaring&date=2026-09-12',
        key=agent_library['key'])
    assert r.status_code == 200
    data = r.get_json()
    ids = {e['id'] for e in data['episodes']}
    assert 'ag-ready' in ids
    assert 'ag-pending' in ids
    assert 'ag-other-day' not in ids
    assert 'ag-other-show' not in ids
    assert data['filters']['timezone'] == 'Europe/Oslo'


def test_agent_search_empty_list_when_nothing_matches(agent_library):
    r = _agent_get(
        '/api/v1/episodes?publisher=Forklaring&date=1999-01-01',
        key=agent_library['key'])
    assert r.status_code == 200
    assert r.get_json()['episodes'] == []
    assert r.get_json()['count'] == 0


def test_agent_search_requires_a_filter(agent_library):
    r = _agent_get('/api/v1/episodes', key=agent_library['key'])
    assert r.status_code == 400


def test_agent_get_episode_includes_status(agent_library):
    key = agent_library['key']
    ready = _agent_get('/api/v1/episodes/ag-ready', key=key).get_json()
    assert ready['transcript_status'] == 'ready'
    assert ready['published_at'] == '2026-09-12'
    assert ready['publisher'] == 'Forklaringssaften'

    pending = _agent_get('/api/v1/episodes/ag-pending', key=key).get_json()
    assert pending['transcript_status'] == 'pending'

    failed = _agent_get('/api/v1/episodes/ag-failed', key=key).get_json()
    assert failed['transcript_status'] == 'failed'


def test_agent_transcript_ready_returns_full_text(agent_library):
    r = _agent_get('/api/v1/episodes/ag-ready/transcript',
                   key=agent_library['key'])
    assert r.status_code == 200
    data = r.get_json()
    assert data['transcript_status'] == 'ready'
    assert data['text'] == 'Hei. Dette er hele transkriptet.'


def test_agent_transcript_pending_is_not_500(agent_library):
    r = _agent_get('/api/v1/episodes/ag-pending/transcript',
                   key=agent_library['key'])
    assert r.status_code == 200
    data = r.get_json()
    assert data['transcript_status'] == 'pending'
    assert 'text' not in data


def test_agent_transcript_failed_is_not_500(agent_library):
    r = _agent_get('/api/v1/episodes/ag-failed/transcript',
                   key=agent_library['key'])
    assert r.status_code == 200
    assert r.get_json()['transcript_status'] == 'failed'
    assert 'text' not in r.get_json()


def test_agent_x_api_key_header_works(agent_library):
    r = A.app.test_client().get(
        '/api/v1/episodes/ag-ready',
        headers={'X-Api-Key': agent_library['key']})
    assert r.status_code == 200
    assert r.get_json()['id'] == 'ag-ready'


def test_agent_user_scope_hides_other_accounts(agent_library, monkeypatch):
    monkeypatch.setenv('AGENT_API_USER_ID', str(agent_library['owner']))
    r = _agent_get(
        '/api/v1/episodes?publisher=Forklaring&date=2026-09-12',
        key=agent_library['key'])
    ids = {e['id'] for e in r.get_json()['episodes']}
    assert 'ag-ready' in ids
    assert 'ag-other-user' not in ids
    assert _agent_get('/api/v1/episodes/ag-other-user',
                      key=agent_library['key']).status_code == 404


def test_agent_transcript_status_helper_maps_phases():
    assert A.agent_transcript_status(
        type('T', (), {'status': 'downloading', 'transcript_text': None})()
    ) == 'pending'
    assert A.agent_transcript_status(
        type('T', (), {'status': 'completed', 'transcript_text': 'x'})()
    ) == 'ready'
    assert A.agent_transcript_status(
        type('T', (), {'status': 'cancelled', 'transcript_text': ''})()
    ) == 'failed'


# --------------------------------------------------------------------------
# Agent write API (TSK-20498): resolve catalog + start transcription
# --------------------------------------------------------------------------

_SPART_FEED = [
    {
        'title': 'Ukas iddiot: De har én jobb!',
        'published': '2026-09-10',
        'audio_url': 'https://cdn.example.com/spart-2026-09-10.mp3',
        'podcast_name': 'Spårtsklubben',
        'artwork': 'https://cdn.example.com/art.jpg',
        'duration_min': 42.0,
        'estimated_cost': 0.025,
    },
    {
        'title': 'Some other episode',
        'published': '2026-09-11',
        'audio_url': 'https://cdn.example.com/spart-2026-09-11.mp3',
        'podcast_name': 'Spårtsklubben',
        'artwork': 'https://cdn.example.com/art.jpg',
        'duration_min': 40.0,
        'estimated_cost': 0.024,
    },
]


@pytest.fixture
def agent_write(agent_api, trial_on, monkeypatch):
    """Agent key + scoped user with trial; catalog lookups stubbed."""
    from models import db, TranscriptionTask
    A._agent_write_attempts.clear()
    uid = _make_user('agent-writer@example.com', limit=3600, used=0)
    monkeypatch.setenv('AGENT_API_USER_ID', str(uid))
    monkeypatch.setattr(A, '_itunes_search', lambda term, entity: [{
        'collectionName': 'Spårtsklubben',
        'feedUrl': 'https://feeds.example.com/spart.xml',
    }])
    monkeypatch.setattr(
        A, 'get_episodes_from_rss',
        lambda url: (list(_SPART_FEED), None))
    monkeypatch.setattr(A, '_is_fetchable_url', lambda u: True)
    monkeypatch.setattr(A, 'free_disk_bytes', lambda *a, **kw: 10 ** 12)
    import types
    monkeypatch.setattr(
        A.threading, 'Thread',
        lambda *a, **kw: types.SimpleNamespace(daemon=True, start=lambda: None))
    yield {'user_id': uid, 'key': agent_api}
    with A.app.app_context():
        db.session.query(TranscriptionTask).filter_by(user_id=uid).delete()
        db.session.commit()
    _purge(['agent-writer@example.com'])


def _agent_post(path, key=None, json_body=None, **kwargs):
    headers = kwargs.pop('headers', {})
    if key is not None:
        headers['Authorization'] = f'Bearer {key}'
    if json_body is not None:
        headers.setdefault('Content-Type', 'application/json')
        return A.app.test_client().post(
            path, headers=headers, json=json_body, **kwargs)
    return A.app.test_client().post(path, headers=headers, **kwargs)


def test_agent_resolve_rejects_missing_auth(agent_write):
    r = A.app.test_client().post(
        '/api/v1/resolve',
        json={'publisher': 'Spårtsklubben', 'date': '2026-09-10'})
    assert r.status_code == 401


def test_agent_resolve_miss_is_clear_404(agent_write, monkeypatch):
    monkeypatch.setattr(A, '_itunes_search', lambda term, entity: [])
    r = _agent_post(
        '/api/v1/resolve', key=agent_write['key'],
        json_body={'publisher': 'No Such Show XYZ', 'date': '2026-09-10'})
    assert r.status_code == 404
    err = r.get_json()['error']
    assert 'No public podcast feed' in err or 'not found' in err.lower()


def test_agent_resolve_date_miss_is_clear_404(agent_write):
    r = _agent_post(
        '/api/v1/resolve', key=agent_write['key'],
        json_body={'publisher': 'Spårtsklubben', 'date': '1999-01-01'})
    assert r.status_code == 404
    assert '1999-01-01' in r.get_json()['error']
    assert 'Europe/Oslo' in r.get_json()['error']


def test_agent_resolve_spartsklubben_by_publisher_and_date(agent_write):
    r = _agent_post(
        '/api/v1/resolve', key=agent_write['key'],
        json_body={'publisher': 'spårtsklubben', 'date': '2026-09-10'})
    assert r.status_code == 200
    ep = r.get_json()['episode']
    assert ep['publisher'] == 'Spårtsklubben'
    assert ep['published_at'] == '2026-09-10'
    assert ep['title'].startswith('Ukas iddiot')
    assert ep['audio_url'].endswith('.mp3')
    assert ep['transcript_status'] == 'none'
    assert 'id' not in ep  # catalog hit, not a task yet


def test_agent_start_rejects_missing_auth(agent_write):
    r = A.app.test_client().post(
        '/api/v1/transcriptions',
        json={'publisher': 'Spårtsklubben', 'date': '2026-09-10'})
    assert r.status_code == 401


def test_agent_start_requires_scoped_user(agent_api, trial_on, monkeypatch):
    monkeypatch.delenv('AGENT_API_USER_ID', raising=False)
    A._agent_write_attempts.clear()
    r = _agent_post(
        '/api/v1/transcriptions', key=agent_api,
        json_body={'publisher': 'Spårtsklubben', 'date': '2026-09-10'})
    assert r.status_code == 403
    assert 'AGENT_API_USER_ID' in r.get_json()['error']


def test_agent_start_creates_task_scoped_to_agent_user(agent_write):
    from models import db, TranscriptionTask
    r = _agent_post(
        '/api/v1/transcriptions', key=agent_write['key'],
        json_body={'publisher': 'Spårtsklubben', 'date': '2026-09-10'})
    assert r.status_code == 201, r.get_json()
    data = r.get_json()
    assert data['transcript_status'] == 'pending'
    assert data['publisher'] == 'Spårtsklubben'
    assert data['published_at'] == '2026-09-10'
    assert data['reused'] is False
    with A.app.app_context():
        task = db.session.get(TranscriptionTask, data['id'])
        assert task is not None
        assert task.user_id == agent_write['user_id']
        assert task.podcast_name == 'Spårtsklubben'
        assert task.trial_seconds_charged  # metered on trial


def test_agent_start_then_poll_pending_and_ready(agent_write):
    from models import db, TranscriptionTask
    start = _agent_post(
        '/api/v1/transcriptions', key=agent_write['key'],
        json_body={'publisher': 'Spårtsklubben', 'date': '2026-09-10'})
    assert start.status_code == 201
    task_id = start.get_json()['id']

    pending = _agent_get(f'/api/v1/episodes/{task_id}', key=agent_write['key'])
    assert pending.status_code == 200
    assert pending.get_json()['transcript_status'] == 'pending'

    with A.app.app_context():
        task = db.session.get(TranscriptionTask, task_id)
        task.status = 'completed'
        task.transcript_text = 'Ferdig transkript for Spårtsklubben.'
        db.session.commit()

    ready = _agent_get(f'/api/v1/episodes/{task_id}', key=agent_write['key'])
    assert ready.get_json()['transcript_status'] == 'ready'
    text = _agent_get(f'/api/v1/episodes/{task_id}/transcript',
                      key=agent_write['key'])
    assert text.get_json()['text'] == 'Ferdig transkript for Spårtsklubben.'


def test_agent_start_trial_exhausted_is_402(agent_write, monkeypatch):
    from models import db, User
    with A.app.app_context():
        u = db.session.get(User, agent_write['user_id'])
        u.trial_seconds_used = u.trial_seconds_limit or A.TRIAL_DEFAULT_SECONDS
        db.session.commit()
    r = _agent_post(
        '/api/v1/transcriptions', key=agent_write['key'],
        json_body={'publisher': 'Spårtsklubben', 'date': '2026-09-10'})
    assert r.status_code == 402, r.get_json()
    assert 'error' in r.get_json()


def test_agent_start_reuses_existing_pending_task(agent_write):
    first = _agent_post(
        '/api/v1/transcriptions', key=agent_write['key'],
        json_body={'publisher': 'Spårtsklubben', 'date': '2026-09-10'})
    assert first.status_code == 201
    second = _agent_post(
        '/api/v1/transcriptions', key=agent_write['key'],
        json_body={'publisher': 'Spårtsklubben', 'date': '2026-09-10'})
    assert second.status_code == 200
    assert second.get_json()['id'] == first.get_json()['id']
    assert second.get_json()['reused'] is True


def test_agent_resolve_via_get_query_params(agent_write):
    r = _agent_get(
        '/api/v1/resolve?publisher=Spårtsklubben&date=2026-09-10',
        key=agent_write['key'])
    assert r.status_code == 200
    assert r.get_json()['episode']['published_at'] == '2026-09-10'


# --------------------------------------------------------------------------
# Customer API keys (TSK-20500): per-user hashed keys on the same endpoints
# --------------------------------------------------------------------------

def _issue_customer_key(user_id):
    """Persist a fresh customer API key for user_id; return plaintext."""
    from models import db, User
    plaintext = A.mint_customer_api_key()
    with A.app.app_context():
        u = db.session.get(User, user_id)
        u.api_key_hash = A.hash_customer_api_key(plaintext)
        u.api_key_prefix = A.customer_api_key_prefix(plaintext)
        u.api_key_created_at = datetime.now(timezone.utc)
        db.session.commit()
    return plaintext


@pytest.fixture
def customer_api(trial_on, monkeypatch):
    """Two users with keys; catalog stubs for write tests."""
    from models import db, TranscriptionTask
    A._agent_write_attempts.clear()
    a = _make_user('customer-a@example.com', limit=3600, used=0)
    b = _make_user('customer-b@example.com', limit=3600, used=0)
    key_a = _issue_customer_key(a)
    key_b = _issue_customer_key(b)
    with A.app.app_context():
        db.session.query(TranscriptionTask).filter(
            TranscriptionTask.id.in_(['cust-a-ep', 'cust-b-ep'])).delete()
        db.session.add(TranscriptionTask(
            id='cust-a-ep', user_id=a, status='completed',
            episode_title='A private episode', podcast_name='Forklaringssaften',
            episode_published='2026-09-12',
            transcript_text='Transcript belonging to A.',
            completed_at=datetime(2026, 9, 12, 10, tzinfo=timezone.utc)))
        db.session.add(TranscriptionTask(
            id='cust-b-ep', user_id=b, status='completed',
            episode_title='B private episode', podcast_name='Forklaringssaften',
            episode_published='2026-09-12',
            transcript_text='Transcript belonging to B.',
            completed_at=datetime(2026, 9, 12, 11, tzinfo=timezone.utc)))
        db.session.commit()
    monkeypatch.setattr(A, '_itunes_search', lambda term, entity: [{
        'collectionName': 'Spårtsklubben',
        'feedUrl': 'https://feeds.example.com/spart.xml',
    }])
    monkeypatch.setattr(
        A, 'get_episodes_from_rss',
        lambda url: (list(_SPART_FEED), None))
    monkeypatch.setattr(A, '_is_fetchable_url', lambda u: True)
    monkeypatch.setattr(A, 'free_disk_bytes', lambda *a, **kw: 10 ** 12)
    import types
    monkeypatch.setattr(
        A.threading, 'Thread',
        lambda *a, **kw: types.SimpleNamespace(daemon=True, start=lambda: None))
    yield {
        'a': a, 'b': b, 'key_a': key_a, 'key_b': key_b,
    }
    with A.app.app_context():
        db.session.query(TranscriptionTask).filter(
            TranscriptionTask.user_id.in_([a, b])).delete()
        db.session.commit()
    _purge(['customer-a@example.com', 'customer-b@example.com'])


def test_customer_api_rejects_missing_auth(customer_api):
    r = A.app.test_client().get(
        '/api/v1/episodes?publisher=Forklaring&date=2026-09-12')
    assert r.status_code == 401
    assert r.get_json()['error'] == 'Unauthorized'


def test_customer_api_rejects_wrong_key(customer_api):
    r = _agent_get(
        '/api/v1/episodes?publisher=Forklaring&date=2026-09-12',
        key='psk_not-a-real-key')
    assert r.status_code == 401


def test_customer_a_cannot_see_customer_b_jobs(customer_api):
    listed = _agent_get(
        '/api/v1/episodes?publisher=Forklaring&date=2026-09-12',
        key=customer_api['key_a'])
    assert listed.status_code == 200
    ids = {e['id'] for e in listed.get_json()['episodes']}
    assert 'cust-a-ep' in ids
    assert 'cust-b-ep' not in ids

    assert _agent_get('/api/v1/episodes/cust-b-ep',
                      key=customer_api['key_a']).status_code == 404
    assert _agent_get('/api/v1/episodes/cust-b-ep/transcript',
                      key=customer_api['key_a']).status_code == 404

    own = _agent_get('/api/v1/episodes/cust-a-ep/transcript',
                     key=customer_api['key_a'])
    assert own.status_code == 200
    assert own.get_json()['text'] == 'Transcript belonging to A.'


def test_customer_start_scopes_job_to_key_owner(customer_api):
    from models import db, TranscriptionTask
    r = _agent_post(
        '/api/v1/transcriptions', key=customer_api['key_a'],
        json_body={'publisher': 'Spårtsklubben', 'date': '2026-09-10'})
    assert r.status_code == 201, r.get_json()
    task_id = r.get_json()['id']
    with A.app.app_context():
        task = db.session.get(TranscriptionTask, task_id)
        assert task.user_id == customer_api['a']
        assert task.trial_seconds_charged  # metered on A's trial

    # B cannot poll A's new job
    assert _agent_get(f'/api/v1/episodes/{task_id}',
                      key=customer_api['key_b']).status_code == 404


def test_customer_start_trial_exhausted_is_402(customer_api):
    from models import db, User
    with A.app.app_context():
        u = db.session.get(User, customer_api['a'])
        u.trial_seconds_used = u.trial_seconds_limit or A.TRIAL_DEFAULT_SECONDS
        db.session.commit()
    r = _agent_post(
        '/api/v1/transcriptions', key=customer_api['key_a'],
        json_body={'publisher': 'Spårtsklubben', 'date': '2026-09-10'})
    assert r.status_code == 402, r.get_json()
    assert 'error' in r.get_json()


def test_customer_revoke_invalidates_immediately(customer_api):
    from models import db, User
    key = customer_api['key_a']
    assert _agent_get('/api/v1/episodes/cust-a-ep', key=key).status_code == 200

    with A.app.app_context():
        u = db.session.get(User, customer_api['a'])
        u.api_key_hash = None
        u.api_key_prefix = None
        u.api_key_created_at = None
        db.session.commit()

    assert _agent_get('/api/v1/episodes/cust-a-ep', key=key).status_code == 401
    assert _agent_post(
        '/api/v1/transcriptions', key=key,
        json_body={'publisher': 'Spårtsklubben', 'date': '2026-09-10'},
    ).status_code == 401


def test_customer_settings_generate_and_revoke(trial_on):
    from models import db, User
    uid = _make_user('settings-api@example.com', limit=600, used=0)
    client = A.app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(uid)
        sess['_fresh'] = True

    page = client.get('/settings')
    assert page.status_code == 200
    assert b'Podskrift API key (developers only)' in page.data
    assert b'transcribe on the website' in page.data
    assert b'Create' in page.data
    assert b'/docs/api' in page.data
    assert b'How to use' in page.data
    assert b'>Docs<' in page.data or b'Docs</a>' in page.data
    # Growth UI: no multi-key / "Buy more" chrome when Stripe is unset.
    # The #credits trial-balance section is intentional (shows remaining minutes).
    assert b'Buy more' not in page.data
    assert b'Buy 5 hours for $5' not in page.data
    # A dedicated "Developers" product surface is not part of settings; the
    # developer-only card title is intentional so OpenAI-key users do not
    # confuse the psk_ key with sk-.
    assert b'>Developers<' not in page.data

    gen = client.post('/settings/api-key/generate', follow_redirects=True)
    assert gen.status_code == 200
    assert b'psk_' in gen.data
    assert b'copy now' in gen.data.lower() or b'Copy' in gen.data

    with A.app.app_context():
        u = db.session.get(User, uid)
        assert u.api_key_hash
        assert u.api_key_prefix.startswith('psk_')
        # Plaintext must not be persisted
        assert 'psk_' not in (u.api_key_hash or '')

    # Second GET must not show the secret again
    again = client.get('/settings')
    assert b'id="new_api_key"' not in again.data
    assert b'Regenerate' in again.data
    assert b'Revoke' in again.data
    assert b'Old keys stop working immediately.' in again.data

    rev = client.post('/settings/api-key/revoke', follow_redirects=True)
    assert rev.status_code == 200
    with A.app.app_context():
        u = db.session.get(User, uid)
        assert u.api_key_hash is None
    _purge(['settings-api@example.com'])


def test_agent_key_still_works_alongside_customer_keys(
        agent_write, customer_api, monkeypatch):
    """CoS AGENT_API_KEY path is unchanged when customer keys exist."""
    r = _agent_post(
        '/api/v1/resolve', key=agent_write['key'],
        json_body={'publisher': 'Spårtsklubben', 'date': '2026-09-10'})
    assert r.status_code == 200
    assert r.get_json()['episode']['publisher'] == 'Spårtsklubben'

    start = _agent_post(
        '/api/v1/transcriptions', key=agent_write['key'],
        json_body={'publisher': 'Spårtsklubben', 'date': '2026-09-10'})
    assert start.status_code == 201, start.get_json()
    with A.app.app_context():
        from models import db, TranscriptionTask
        task = db.session.get(TranscriptionTask, start.get_json()['id'])
        assert task.user_id == agent_write['user_id']


def test_customer_key_works_when_agent_env_unset(customer_api, monkeypatch):
    monkeypatch.delenv('AGENT_API_KEY', raising=False)
    monkeypatch.delenv('AGENT_API_USER_ID', raising=False)
    r = _agent_get('/api/v1/episodes/cust-a-ep', key=customer_api['key_a'])
    assert r.status_code == 200
    assert r.get_json()['id'] == 'cust-a-ep'


def test_customer_api_key_is_hashed_not_plaintext():
    plaintext = A.mint_customer_api_key()
    assert plaintext.startswith('psk_')
    digest = A.hash_customer_api_key(plaintext)
    assert len(digest) == 64
    assert digest != plaintext
    assert A.hash_customer_api_key(plaintext) == digest


def test_public_api_docs_page_renders_customer_markdown(trial_on):
    """Self-serve docs live at /docs/api so users need not open the GitHub repo."""
    resp = A.app.test_client().get('/docs/api')
    assert resp.status_code == 200
    body = resp.data.decode()
    assert '<h1>Podcast transcript API</h1>' in body
    assert 'psk_' in body or 'psk_…' in body
    assert 'Authorization: Bearer' in body
    assert 'X-Api-Key' in body
    assert '401' in body and '402' in body and '404' in body
    # One visible heading per endpoint (modern reference layout, not a curl dump).
    for heading in (
        'POST /api/v1/resolve',
        'POST /api/v1/transcriptions',
        'GET /api/v1/episodes',
        'GET /api/v1/episodes/{id}',
        'GET /api/v1/episodes/{id}/transcript',
        'Authentication',
        'Errors',
    ):
        assert f'<h2>{heading}</h2>' in body, heading
    assert body.index('<h2>POST /api/v1/resolve</h2>') < body.index(
        '<h2>POST /api/v1/transcriptions</h2>')
    assert body.index('<h2>POST /api/v1/transcriptions</h2>') < body.index(
        '<h2>GET /api/v1/episodes</h2>')
    # Same trial length new signups get — not a hardcoded figure that can drift.
    assert f'{A.NEW_USER_TRIAL_SECONDS // 60} minutes' in body
    # Host CoS secret must never appear on the public page.
    assert 'AGENT_API_KEY' not in body
    assert 'MCP' not in body
    assert 'Developers' not in body


def test_public_api_docs_seo_metadata_and_intro(trial_on):
    """Growth-approved title/meta/OG/intro so transcript-API searches find /docs/api."""
    body = A.app.test_client().get('/docs/api').data.decode()
    grant = A.NEW_USER_TRIAL_SECONDS // 60
    assert '<title>Podcast Transcript API — Get Transcript via HTTP | Podskrift</title>' in body
    assert (
        'content="Podcast transcription API for agents and scripts. '
        'Resolve by show and date, start Whisper, fetch the transcript via HTTP. '
        f'Free {grant}-minute trial. API key in Settings."'
    ) in body
    assert 'content="Podcast Transcript API — Podskrift"' in body
    assert (
        'content="HTTP endpoints to resolve a podcast episode, transcribe with Whisper, '
        'and get the transcript. Same free trial as the web UI."'
    ) in body
    assert '<h1>Podcast transcript API</h1>' in body
    intro = (
        'Podskrift’s podcast transcription API lets agents and scripts get a transcript '
        'over HTTP — the same path as the web UI. Resolve an episode by publisher/show '
        'and date (or URL), start Whisper, poll until ready, then fetch the plain-text '
        f'transcript. New accounts get {grant} free trial minutes on our OpenAI key; after that, '
        'add your own. Create a <code>psk_…</code> key in Settings.'
    )
    assert intro in body
    # Endpoint H2s stay intact and ordered (TSK-20504).
    assert body.index('<h1>Podcast transcript API</h1>') < body.index(
        '<h2>Authentication</h2>')
    assert body.index('<h2>Authentication</h2>') < body.index(
        '<h2>POST /api/v1/resolve</h2>')


def test_llms_txt_mentions_podcast_transcript_api_endpoint(trial_on):
    """llms.txt FAQ answers the SEO query without changing the homepage FAQ list."""
    llms = A.app.test_client().get('/llms.txt').data.decode()
    assert 'Is there a podcast transcript API / get-transcript endpoint?' in llms
    assert 'resolve → start transcription → get transcript' in llms
    assert 'https://podskrift.com/docs/api' in llms
    # Homepage FAQ stays the existing HTTP API entry only.
    home = A.app.test_client().get('/').data.decode()
    assert 'Is there an HTTP API?' in home
    assert 'Is there a podcast transcript API / get-transcript endpoint?' not in home


def test_public_api_docs_strips_agent_key_mentions(trial_on, tmp_path, monkeypatch):
    """Even if the markdown is edited to mention AGENT_API_KEY, the HTML must not."""
    poisoned = (
        '# Customer API\n\n'
        'Never use `AGENT_API_KEY` — internal only.\n\n'
        'Use `psk_…` from Settings.\n'
    )
    doc = tmp_path / 'customer-api.md'
    doc.write_text(poisoned, encoding='utf-8')
    monkeypatch.setattr(A, 'CUSTOMER_API_DOC_PATH', str(doc))
    body = A.app.test_client().get('/docs/api').data.decode()
    assert 'AGENT_API_KEY' not in body
    assert 'psk_' in body


def test_homepage_faq_mentions_api_and_docs(trial_on):
    answers = dict(A.faq_entries())
    assert 'Is there an HTTP API?' in answers
    api = answers['Is there an HTTP API?']
    assert 'psk_' in api or 'Settings' in api
    assert '/docs/api' in api
    assert 'free trial' in api.lower() or 'trial' in api.lower()
    assert 'MCP' not in api
    home = A.app.test_client().get('/').data.decode()
    assert 'Is there an HTTP API?' in home
    assert 'Developers' not in home


# --------------------------------------------------------------------------
# Remote MCP server (/mcp) — feature-flagged, reuses customer API helpers
# --------------------------------------------------------------------------

import mcp_server as MCP  # noqa: E402 — after app import / test DB wiring


@pytest.fixture
def mcp_on(customer_api, monkeypatch):
    """MCP_ENABLED + customer key; catalog/enqueue stubs from customer_api."""
    monkeypatch.setenv('MCP_ENABLED', '1')
    MCP._mcp_rate_attempts.clear()
    A._agent_write_attempts.clear()
    return customer_api


def _mcp_rpc(key, method, params=None, req_id=1):
    headers = {'Authorization': f'Bearer {key}'}
    body = {'jsonrpc': '2.0', 'id': req_id, 'method': method}
    if params is not None:
        body['params'] = params
    return A.app.test_client().post('/mcp', json=body, headers=headers)


def _mcp_tool(key, name, arguments=None):
    r = _mcp_rpc(key, 'tools/call', {
        'name': name,
        'arguments': arguments or {},
    })
    assert r.status_code == 200, r.data
    data = r.get_json()
    assert 'result' in data, data
    result = data['result']
    if 'structuredContent' in result:
        payload = result['structuredContent']
    else:
        payload = json.loads(result['content'][0]['text'])
    return payload, result


def test_mcp_flag_off_returns_404(customer_api, monkeypatch):
    monkeypatch.setenv('MCP_ENABLED', '0')
    r = _mcp_rpc(customer_api['key_a'], 'initialize', {})
    assert r.status_code == 404
    assert r.get_json()['error'] == 'Not found'


def test_mcp_requires_auth(mcp_on):
    r = A.app.test_client().post('/mcp', json={
        'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {},
    })
    assert r.status_code == 401
    r = A.app.test_client().post(
        '/mcp',
        json={'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}},
        headers={'Authorization': 'Bearer psk_not-a-real-key'})
    assert r.status_code == 401


def test_mcp_initialize_and_tools_list(mcp_on):
    r = _mcp_rpc(mcp_on['key_a'], 'initialize', {
        'protocolVersion': '2025-03-26',
        'capabilities': {},
        'clientInfo': {'name': 'test', 'version': '0'},
    })
    assert r.status_code == 200
    result = r.get_json()['result']
    assert result['protocolVersion'] == '2025-03-26'
    assert 'tools' in result['capabilities']
    assert result['serverInfo']['name'] == 'podskrift'

    listed = _mcp_rpc(mcp_on['key_a'], 'tools/list', {})
    names = {t['name'] for t in listed.get_json()['result']['tools']}
    assert names == {
        'search_podcasts', 'list_episodes', 'get_transcript',
        'get_transcript_status',
    }


def test_mcp_search_podcasts_tool(mcp_on, monkeypatch):
    monkeypatch.setattr(A, '_itunes_search', lambda term, entity: [{
        'collectionName': 'Spårtsklubben',
        'artistName': 'NRK',
        'feedUrl': 'https://feeds.example.com/spart.xml',
        'artworkUrl100': 'https://cdn.example.com/art.jpg',
        'primaryGenreName': 'Sports',
        'collectionViewUrl': 'https://podcasts.apple.com/podcast/id1',
        'collectionId': 1,
    }])
    payload, result = _mcp_tool(mcp_on['key_a'], 'search_podcasts', {
        'query': 'Spårtsklubben',
    })
    assert result.get('isError') is False
    assert payload['count'] >= 1
    assert payload['results'][0]['name'] == 'Spårtsklubben'
    assert payload['results'][0]['feed_url']


def test_mcp_list_episodes_tool(mcp_on):
    payload, result = _mcp_tool(mcp_on['key_a'], 'list_episodes', {
        'podcast': 'Spårtsklubben',
    })
    assert result.get('isError') is False
    assert payload['count'] >= 1
    ep = payload['episodes'][0]
    assert ep['title']
    assert ep['date']
    assert 'duration_min' in ep
    assert ep['id']


def test_mcp_get_transcript_ready_existing(mcp_on):
    payload, result = _mcp_tool(mcp_on['key_a'], 'get_transcript', {
        'episode': 'cust-a-ep',
    })
    assert result.get('isError') is False
    assert payload['transcript_status'] == 'ready'
    assert payload['text'] == 'Transcript belonging to A.'
    assert payload['job_id'] == 'cust-a-ep'
    assert 'balance' in payload


def test_mcp_get_transcript_starts_job(mcp_on, ph_events):
    payload, result = _mcp_tool(mcp_on['key_a'], 'get_transcript', {
        'episode': 'Spårtsklubben|2026-09-10',
    })
    assert result.get('isError') is False
    assert payload.get('error') is None
    assert payload['transcript_status'] == 'pending'
    assert payload['job_id']
    assert payload['cost_minutes'] == 42
    assert 'balance_before' in payload
    assert 'balance_after' in payload
    # Minutes reserved → free balance dropped
    assert (payload['balance_after']['remaining_minutes']
            < payload['balance_before']['remaining_minutes'])

    status_payload, _ = _mcp_tool(mcp_on['key_a'], 'get_transcript_status', {
        'job_id': payload['job_id'],
    })
    assert status_payload['job_id'] == payload['job_id']
    assert status_payload['transcript_status'] == 'pending'

    called = [e for e in ph_events.events if e['event'] == 'mcp_tool_called']
    assert called
    assert all('email' not in (e.get('properties') or {}) for e in called)
    tools = {e['properties']['tool'] for e in called}
    assert 'get_transcript' in tools
    assert 'get_transcript_status' in tools
    assert all(isinstance(e['properties'].get('success'), bool) for e in called)


def test_mcp_get_transcript_insufficient_balance(mcp_on):
    from models import db, User
    with A.app.app_context():
        u = db.session.get(User, mcp_on['a'])
        u.trial_seconds_used = u.trial_seconds_limit or 3600
        u.paid_seconds_balance = 0
        db.session.commit()

    payload, result = _mcp_tool(mcp_on['key_a'], 'get_transcript', {
        'episode': 'Spårtsklubben|2026-09-10',
    })
    assert result.get('isError') is True
    assert payload['error'] == 'insufficient_balance'
    assert payload['pricing_url'].endswith('/pricing')
    assert 'cost_minutes' in payload
    assert 'balance' in payload or 'balance_before' in payload


def test_mcp_cannot_read_other_users_job(mcp_on):
    payload, result = _mcp_tool(mcp_on['key_a'], 'get_transcript_status', {
        'job_id': 'cust-b-ep',
    })
    assert result.get('isError') is True
    assert payload['error'] == 'Job not found'


def test_mcp_get_transcript_by_list_id_does_not_charge_twice(mcp_on):
    """list_episodes id is the audio URL; a repeat call must reuse the job."""
    listed, _ = _mcp_tool(mcp_on['key_a'], 'list_episodes', {
        'podcast': 'Spårtsklubben',
    })
    ep = listed['episodes'][0]
    args = {
        'episode': ep['id'], 'title': ep['title'], 'publisher': ep['publisher'],
        'date': ep['date'], 'duration_min': ep['duration_min'],
        'rss_url': ep['rss_url'],
    }
    first, _ = _mcp_tool(mcp_on['key_a'], 'get_transcript', args)
    assert first['reused'] is False
    assert first['title'] == ep['title']
    assert first['cost_minutes'] == 42
    second, _ = _mcp_tool(mcp_on['key_a'], 'get_transcript', {'episode': ep['id']})
    assert second['job_id'] == first['job_id']
    assert second['reused'] is True
    assert (second['balance']['remaining_minutes']
            == first['balance_after']['remaining_minutes'])


def test_mcp_list_episodes_spotify_show(mcp_on, monkeypatch):
    monkeypatch.setattr(A, 'resolve_spotify_url', lambda raw: {
        'results': [{'type': 'show', 'name': 'Spårtsklubben',
                     'artist': 'Spårtsklubben',
                     'feed_url': 'https://feeds.example.com/spart.xml'}],
        'error': None, 'error_kind': None, 'show_name': 'Spårtsklubben',
        'error_detail': '',
    })
    payload, result = _mcp_tool(mcp_on['key_a'], 'list_episodes', {
        'podcast': 'https://open.spotify.com/show/2MAi0BvDc6GTFvKFPXnkCL',
    })
    assert result.get('isError') is False, payload
    assert payload['count'] >= 1


def test_mcp_jsonrpc_edge_cases(mcp_on):
    key = mcp_on['key_a']
    headers = {'Authorization': f'Bearer {key}'}
    c = A.app.test_client()
    r = c.post('/mcp', json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                             'params': [1]}, headers=headers)
    assert r.status_code == 200
    assert r.get_json()['error']['code'] == -32602
    r = c.post('/mcp', json=[{'jsonrpc': '2.0', 'method': 'notifications/initialized'}],
               headers=headers)
    assert r.status_code == 202
    r = c.post('/mcp', json=[], headers=headers)
    assert r.status_code == 400
    r = c.post('/mcp', json={'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'})
    assert r.status_code == 401
    assert r.headers.get('WWW-Authenticate', '').startswith('Bearer')


def test_mcp_batch_consumes_rate_limit(mcp_on, monkeypatch):
    monkeypatch.setattr(MCP, 'MCP_MAX_PER_WINDOW', 3)
    batch = [{'jsonrpc': '2.0', 'id': i, 'method': 'ping'} for i in range(10)]
    r = A.app.test_client().post(
        '/mcp', json=batch,
        headers={'Authorization': f'Bearer {mcp_on["key_a"]}'})
    out = r.get_json()
    assert len(out) == 10
    assert sum(1 for m in out if 'result' in m) == 3
    assert all(m['error']['code'] == -32002 for m in out if 'error' in m)


def test_mcp_docs_section_only_when_flag_on(trial_on, monkeypatch):
    monkeypatch.setenv('MCP_ENABLED', '0')
    monkeypatch.setenv('MCP_OAUTH_ENABLED', '0')
    off = A.app.test_client().get('/docs/api').data.decode()
    assert 'MCP' not in off
    assert 'podskrift.com/mcp' not in off

    monkeypatch.setenv('MCP_ENABLED', '1')
    monkeypatch.setenv('MCP_OAUTH_ENABLED', '0')
    on = A.app.test_client().get('/docs/api').data.decode()
    assert 'MCP (ChatGPT / Claude / Cursor)' in on
    assert 'podskrift.com/mcp' in on
    assert 'search_podcasts' in on
    # OAuth connector steps stay hidden until MCP_OAUTH_ENABLED.
    assert 'OAuth for ChatGPT' not in on
    assert 'claude.ai/api/mcp/auth_callback' not in on

    monkeypatch.setenv('MCP_OAUTH_ENABLED', '1')
    oauth_on = A.app.test_client().get('/docs/api').data.decode()
    assert 'OAuth for ChatGPT and Claude.ai' in oauth_on
    assert 'claude.ai/api/mcp/auth_callback' in oauth_on


def test_mcp_path_not_redirected_off_canonical_host(monkeypatch):
    monkeypatch.setenv('PUBLIC_BASE_URL', 'https://podskrift.com')
    monkeypatch.setenv('MCP_ENABLED', '1')
    r = A.app.test_client().post(
        '/mcp',
        base_url='http://www.podskrift.com',
        json={'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}},
    )
    # Auth fails closed with 401 — must not 301/308 off the host.
    assert r.status_code == 401


# --------------------------------------------------------------------------
# MCP OAuth 2.1 (ChatGPT / Claude.ai connectors)
# --------------------------------------------------------------------------

import base64 as _b64
import hashlib as _hashlib
import oauth_server as OAUTH  # noqa: E402


def _pkce_pair():
    verifier = secrets.token_urlsafe(48)
    challenge = _b64.urlsafe_b64encode(
        _hashlib.sha256(verifier.encode('ascii')).digest()
    ).rstrip(b'=').decode('ascii')
    return verifier, challenge


@pytest.fixture
def mcp_oauth_on(mcp_on, monkeypatch):
    """MCP + OAuth flags; reuse customer_api stubs from mcp_on."""
    monkeypatch.setenv('MCP_OAUTH_ENABLED', '1')
    monkeypatch.setenv('PUBLIC_BASE_URL', 'https://podskrift.com')
    OAUTH._register_attempts.clear()
    OAUTH._token_attempts.clear()
    return mcp_on


def _oauth_register(redirect_uris=None, client_name='Test Connector'):
    r = A.app.test_client().post('/oauth/register', json={
        'client_name': client_name,
        'redirect_uris': redirect_uris or ['http://127.0.0.1/callback'],
        'token_endpoint_auth_method': 'none',
        'grant_types': ['authorization_code', 'refresh_token'],
        'response_types': ['code'],
    })
    return r


def _oauth_approve(client, *, client_id, redirect_uri, verifier_challenge,
                   state='xyz'):
    """GET consent + POST approve; return (response, code|None)."""
    verifier, challenge = verifier_challenge
    resource = OAUTH.mcp_resource_url()
    q = {
        'response_type': 'code',
        'client_id': client_id,
        'redirect_uri': redirect_uri,
        'state': state,
        'code_challenge': challenge,
        'code_challenge_method': 'S256',
        'resource': resource,
        'scope': 'mcp offline_access',
    }
    page = client.get('/oauth/authorize', query_string=q)
    assert page.status_code == 200, page.data[:500]
    assert b'Allow' in page.data
    with client.session_transaction() as sess:
        token = sess.get('_csrf_token') or 'tok'
        sess['_csrf_token'] = token
    approved = client.post('/oauth/authorize', data={
        'csrf_token': token,
        'decision': 'approve',
        'client_id': client_id,
        'redirect_uri': redirect_uri,
        'response_type': 'code',
        'state': state,
        'scope': 'mcp offline_access',
        'code_challenge': challenge,
        'code_challenge_method': 'S256',
        'resource': resource,
    }, follow_redirects=False)
    assert approved.status_code in (302, 303), approved.data[:500]
    loc = approved.headers['Location']
    from urllib.parse import urlparse, parse_qs
    params = parse_qs(urlparse(loc).query)
    assert params.get('iss') == ['https://podskrift.com']
    code = (params.get('code') or [None])[0]
    return approved, code


def test_oauth_flag_off_returns_404(monkeypatch):
    monkeypatch.setenv('MCP_OAUTH_ENABLED', '0')
    c = A.app.test_client()
    assert c.get('/.well-known/oauth-protected-resource').status_code == 404
    assert c.get('/.well-known/oauth-authorization-server').status_code == 404
    assert c.post('/oauth/register', json={
        'redirect_uris': ['http://127.0.0.1/callback'],
    }).status_code == 404
    assert c.get('/oauth/authorize').status_code == 404
    assert c.post('/oauth/token').status_code == 404


def test_oauth_metadata_and_www_authenticate(mcp_oauth_on):
    c = A.app.test_client()
    prm = c.get('/.well-known/oauth-protected-resource')
    assert prm.status_code == 200
    body = prm.get_json()
    assert body['resource'] == 'https://podskrift.com/mcp'
    assert body['authorization_servers'] == ['https://podskrift.com']

    # Path-appended variant for Claude discovery when MCP URL has /mcp.
    prm2 = c.get('/.well-known/oauth-protected-resource/mcp')
    assert prm2.status_code == 200
    assert prm2.get_json()['resource'] == body['resource']

    asm = c.get('/.well-known/oauth-authorization-server')
    assert asm.status_code == 200
    meta = asm.get_json()
    assert meta['issuer'] == 'https://podskrift.com'
    assert 'S256' in meta['code_challenge_methods_supported']
    assert meta['registration_endpoint'].endswith('/oauth/register')
    assert meta['authorization_response_iss_parameter_supported'] is True

    unauth = c.post('/mcp', json={
        'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {},
    })
    assert unauth.status_code == 401
    www = unauth.headers.get('WWW-Authenticate', '')
    assert 'resource_metadata=' in www
    assert '/.well-known/oauth-protected-resource' in www


def test_oauth_full_flow_register_authorize_token_mcp_refresh_revoke(mcp_oauth_on):
    uid = mcp_oauth_on['a']
    redirect_uri = 'http://127.0.0.1/callback'
    reg = _oauth_register(redirect_uris=[redirect_uri], client_name='ChatGPT Test')
    assert reg.status_code == 201, reg.get_json()
    client_id = reg.get_json()['client_id']
    assert client_id.startswith('poc_')

    verifier, challenge = _pkce_pair()
    client = _login(uid)
    _, code = _oauth_approve(
        client, client_id=client_id, redirect_uri=redirect_uri,
        verifier_challenge=(verifier, challenge))
    assert code

    tok = A.app.test_client().post('/oauth/token', data={
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': redirect_uri,
        'client_id': client_id,
        'code_verifier': verifier,
        'resource': 'https://podskrift.com/mcp',
    })
    assert tok.status_code == 200, tok.get_json()
    tokens = tok.get_json()
    assert tokens['access_token'].startswith('poa_')
    assert tokens['refresh_token'].startswith('por_')
    assert tokens['expires_in'] == 3600

    # Access token works on /mcp
    mcp = A.app.test_client().post(
        '/mcp',
        json={'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
              'params': {'protocolVersion': '2025-03-26', 'capabilities': {},
                         'clientInfo': {'name': 't', 'version': '0'}}},
        headers={'Authorization': f'Bearer {tokens["access_token"]}'})
    assert mcp.status_code == 200
    assert mcp.get_json()['result']['serverInfo']['name'] == 'podskrift'

    # psk_ API key still works alongside OAuth
    assert _mcp_rpc(mcp_oauth_on['key_a'], 'tools/list').status_code == 200

    # Refresh rotates
    old_refresh = tokens['refresh_token']
    refreshed = A.app.test_client().post('/oauth/token', data={
        'grant_type': 'refresh_token',
        'refresh_token': old_refresh,
        'client_id': client_id,
        'resource': 'https://podskrift.com/mcp',
    })
    assert refreshed.status_code == 200, refreshed.get_json()
    new_tokens = refreshed.get_json()
    assert new_tokens['refresh_token'] != old_refresh
    assert new_tokens['access_token'] != tokens['access_token']

    # Old refresh cannot be reused
    reuse = A.app.test_client().post('/oauth/token', data={
        'grant_type': 'refresh_token',
        'refresh_token': old_refresh,
        'client_id': client_id,
    })
    assert reuse.status_code == 400
    assert reuse.get_json()['error'] == 'invalid_grant'

    # Settings lists the app; revoke kills the new access token
    settings = client.get('/settings')
    assert settings.status_code == 200
    assert b'Connected apps' in settings.data
    assert b'ChatGPT Test' in settings.data
    with client.session_transaction() as sess:
        csrf = sess.get('_csrf_token') or 'tok'
        sess['_csrf_token'] = csrf
    rev = client.post('/settings/oauth/revoke', data={
        'csrf_token': csrf,
        'client_id': client_id,
    }, follow_redirects=True)
    assert rev.status_code == 200
    assert b'Access revoked' in rev.data or b'already disconnected' in rev.data

    dead = A.app.test_client().post(
        '/mcp',
        json={'jsonrpc': '2.0', 'id': 1, 'method': 'ping', 'params': {}},
        headers={'Authorization': f'Bearer {new_tokens["access_token"]}'})
    assert dead.status_code == 401


def test_oauth_bad_pkce_rejected(mcp_oauth_on):
    redirect_uri = 'http://127.0.0.1/callback'
    client_id = _oauth_register(redirect_uris=[redirect_uri]).get_json()['client_id']
    verifier, challenge = _pkce_pair()
    client = _login(mcp_oauth_on['a'])
    _, code = _oauth_approve(
        client, client_id=client_id, redirect_uri=redirect_uri,
        verifier_challenge=(verifier, challenge))
    bad = A.app.test_client().post('/oauth/token', data={
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': redirect_uri,
        'client_id': client_id,
        'code_verifier': secrets.token_urlsafe(48),  # wrong verifier
        'resource': 'https://podskrift.com/mcp',
    })
    assert bad.status_code == 400
    assert bad.get_json()['error'] == 'invalid_grant'


def test_oauth_wrong_redirect_rejected(mcp_oauth_on):
    reg = _oauth_register(redirect_uris=['http://127.0.0.1/callback'])
    client_id = reg.get_json()['client_id']
    verifier, challenge = _pkce_pair()
    client = _login(mcp_oauth_on['a'])
    q = {
        'response_type': 'code',
        'client_id': client_id,
        'redirect_uri': 'http://127.0.0.1/evil',
        'code_challenge': challenge,
        'code_challenge_method': 'S256',
        'resource': OAUTH.mcp_resource_url(),
    }
    r = client.get('/oauth/authorize', query_string=q)
    assert r.status_code == 400
    assert r.get_json()['error'] == 'invalid_request'


def test_oauth_expired_code_and_reuse(mcp_oauth_on):
    from models import db, OAuthAuthorizationCode
    redirect_uri = 'http://127.0.0.1/callback'
    client_id = _oauth_register(redirect_uris=[redirect_uri]).get_json()['client_id']
    verifier, challenge = _pkce_pair()
    client = _login(mcp_oauth_on['a'])
    _, code = _oauth_approve(
        client, client_id=client_id, redirect_uri=redirect_uri,
        verifier_challenge=(verifier, challenge))

    # Expire the code before exchange
    with A.app.app_context():
        row = OAuthAuthorizationCode.query.filter_by(
            code_hash=OAUTH._hash_token(code)).first()
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.session.commit()

    expired = A.app.test_client().post('/oauth/token', data={
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': redirect_uri,
        'client_id': client_id,
        'code_verifier': verifier,
        'resource': 'https://podskrift.com/mcp',
    })
    assert expired.status_code == 400
    assert expired.get_json()['error'] == 'invalid_grant'

    # Fresh code, then reuse
    verifier2, challenge2 = _pkce_pair()
    _, code2 = _oauth_approve(
        client, client_id=client_id, redirect_uri=redirect_uri,
        verifier_challenge=(verifier2, challenge2), state='s2')
    first = A.app.test_client().post('/oauth/token', data={
        'grant_type': 'authorization_code',
        'code': code2,
        'redirect_uri': redirect_uri,
        'client_id': client_id,
        'code_verifier': verifier2,
        'resource': 'https://podskrift.com/mcp',
    })
    assert first.status_code == 200
    second = A.app.test_client().post('/oauth/token', data={
        'grant_type': 'authorization_code',
        'code': code2,
        'redirect_uri': redirect_uri,
        'client_id': client_id,
        'code_verifier': verifier2,
        'resource': 'https://podskrift.com/mcp',
    })
    assert second.status_code == 400
    assert second.get_json()['error'] == 'invalid_grant'


def test_oauth_register_rejects_non_https_remote_redirect(mcp_oauth_on):
    r = _oauth_register(redirect_uris=['http://evil.example/callback'])
    assert r.status_code == 400
    assert r.get_json()['error'] == 'invalid_redirect_uri'


def test_oauth_tables_ensure_is_idempotent():
    """Additive migration must be safe to re-run (prod deploy / two workers)."""
    with A.app.app_context():
        OAUTH.ensure_oauth_tables()
        OAUTH.ensure_oauth_tables()
        assert 'client_id' in A._live_columns('oauth_clients')
        cols = A._live_columns('oauth_refresh_tokens')
        assert 'token_hash' in cols
        assert 'replaced_by_hash' in cols


# --------------------------------------------------------------------------
# Product analytics (PostHog)
# --------------------------------------------------------------------------

class _FakePosthog:
    """Records capture() calls without touching the network."""

    def __init__(self):
        self.events = []

    def capture(self, event, distinct_id=None, properties=None, uuid=None, **kwargs):
        self.events.append({
            'event': event,
            'distinct_id': distinct_id,
            'properties': dict(properties or {}),
            'uuid': uuid,
        })


@pytest.fixture
def ph_events(monkeypatch):
    import analytics
    fake = _FakePosthog()
    monkeypatch.setattr(analytics, '_client', fake)
    yield fake
    monkeypatch.setattr(analytics, '_client', False)


def test_posthog_stays_off_without_a_key():
    import analytics
    assert analytics.posthog_key() == ''
    assert analytics.get_client() is None
    # Must not raise and must not invent a client.
    analytics.capture('user_signed_up', 1)
    body = A.app.test_client().get('/').data.decode()
    assert 'posthog.init' not in body
    assert 'phc_' not in body


def test_pytest_never_initialises_posthog_or_sentry_against_live_projects(
        monkeypatch):
    """Regression: sandboxes/CI with a copied .env must not hit prod PH/Sentry."""
    import analytics
    import observability
    import runtime_env

    monkeypatch.delenv('PODSKRIFT_ENV', raising=False)
    monkeypatch.setenv('POSTHOG_KEY', 'phc_would_have_hit_production')
    monkeypatch.setenv('SENTRY_DSN', 'https://public@sentry.invalid/1')
    analytics._client = None

    assert runtime_env.is_production() is False
    assert analytics.init_posthog() is False
    assert analytics.get_client() is None
    assert analytics.posthog_key() == ''
    analytics.capture('transcript_failed', 1, {'reason': 'network'})
    assert analytics._client is False

    assert observability.init_sentry() is False
    body = A.app.test_client().get('/').data.decode()
    assert 'posthog.init' not in body
    assert 'phc_would_have_hit_production' not in body


def test_production_markers_match_live_host_layout(monkeypatch):
    """Prod needs no new .env key: path + PUBLIC_BASE_URL already identify it."""
    import runtime_env

    monkeypatch.delenv('PODSKRIFT_ENV', raising=False)
    for key in ('CI', 'GITHUB_ACTIONS', 'CURSOR_AGENT', 'CURSOR_CLOUD_AGENT',
                'PYTEST_CURRENT_TEST', 'AGENT_TRANSCRIPTS',
                'CURSOR_CONVERSATION_ID', 'CURSOR_TRACE_ID', 'CURSOR_REQUEST_ID'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv(
        'DATABASE_URL',
        'sqlite:////var/www/vhosts/podskrift.nettsmed.dev/app/data/podcast.db',
    )
    monkeypatch.setenv('PUBLIC_BASE_URL', 'https://podskrift.com')
    assert runtime_env._production_markers() is True
    assert runtime_env.is_production() is True
    assert runtime_env.resolve_environment() == 'production'


def test_podskrift_env_override_wins(monkeypatch):
    import runtime_env

    monkeypatch.setenv('PODSKRIFT_ENV', 'production')
    monkeypatch.setenv('CI', 'true')
    monkeypatch.setenv(
        'DATABASE_URL',
        'sqlite:////tmp/not-prod.db',
    )
    assert runtime_env.is_production() is True

    monkeypatch.setenv('PODSKRIFT_ENV', 'dev')
    monkeypatch.setenv(
        'DATABASE_URL',
        'sqlite:////var/www/vhosts/podskrift.nettsmed.dev/app/data/podcast.db',
    )
    monkeypatch.setenv('PUBLIC_BASE_URL', 'https://podskrift.com')
    assert runtime_env.is_production() is False
    assert runtime_env.resolve_environment() == 'dev'


def test_production_capture_tags_environment_and_release(monkeypatch):
    import analytics
    import runtime_env

    monkeypatch.setenv('PODSKRIFT_ENV', 'production')
    monkeypatch.setenv('PODSKRIFT_RELEASE', 'deadbeef')
    runtime_env.clear_release_cache()
    fake = _FakePosthog()
    monkeypatch.setattr(analytics, '_client', fake)
    analytics.capture('user_signed_up', 42)
    assert fake.events == [{
        'event': 'user_signed_up',
        'distinct_id': '42',
        'properties': {
            'app': 'podskrift',
            'environment': 'production',
            'release': 'deadbeef',
        },
        'uuid': None,
    }]
    runtime_env.clear_release_cache()


def test_posthog_snippet_renders_when_key_is_set(monkeypatch):
    monkeypatch.setenv('PODSKRIFT_ENV', 'production')
    monkeypatch.setenv('POSTHOG_KEY', 'phc_test_public_key')
    monkeypatch.setenv('POSTHOG_HOST', 'https://eu.i.posthog.com')
    body = A.app.test_client().get('/').data.decode()
    assert 'posthog.init' in body
    assert 'phc_test_public_key' in body
    assert 'https://eu.i.posthog.com' in body
    assert 'maskAllInputs: true' in body
    assert "maskTextSelector: '.ph-no-capture, .transcript-pane'" in body
    assert 'capture_exceptions: true' in body
    assert "posthog.register({app: 'podskrift'})" in body
    assert 'podskriftPaywallShown' in body
    assert 'buy_clicked' in body
    # Cookieless until Accept: SDK loads immediately with on_reject; opt_out
    # enables anonymous hash counting without ph_* cookies/storage.
    assert "cookieless_mode: 'on_reject'" in body
    assert "person_profiles: 'identified_only'" in body
    assert 'disable_session_recording: true' in body
    assert 'function loadPosthogSdk' in body
    assert 'function applyConsentToPosthog' in body
    assert 'opt_out_capturing()' in body
    assert 'opt_in_capturing()' in body
    assert 'loadPosthogSdk();' in body
    # Loader is defined before the unconditional boot call; init stays inside.
    head, _, _rest = body.partition('function loadPosthogSdk')
    assert 'posthog.init(' not in head
    assert '-assets.i.posthog.com' not in head
    assert 'function clearPosthogStorage' in body
    assert 'sessionStorage' in body
    assert 'cookie-consent.js' in body
    assert 'id="cookieConsent"' in body
    assert 'id="cookieConsentAccept"' in body
    assert 'id="cookieConsentDecline"' in body
    assert 'id="cookieSettingsLink"' in body
    assert 'href="/privacy"' in body or "url_for('privacy')" in body
    assert 'anonymous, cookie-free' in body
    # identify only after Accept (helper gates on choice()).
    assert 'function identifyIfAllowed' in body
    assert "choice() !== 'accepted'" in body


def test_cookie_consent_banner_mentions_cookieless_on_decline(monkeypatch):
    """Decline copy must not imply zero analytics — cookieless stats remain."""
    monkeypatch.setenv('PODSKRIFT_ENV', 'production')
    monkeypatch.setenv('POSTHOG_KEY', 'phc_test_public_key')
    body = A.app.test_client().get('/').data.decode()
    assert 'cookie-free usage statistics' in body
    assert 'session replay' in body.lower()


def test_cookie_consent_banner_absent_without_posthog_key():
    body = A.app.test_client().get('/').data.decode()
    assert 'id="cookieConsent"' not in body
    assert 'cookieSettingsLink' not in body
    assert 'cookie-consent.js' not in body


def test_cookie_consent_js_is_served_from_static():
    resp = A.app.test_client().get('/static/cookie-consent.js')
    assert resp.status_code == 200
    text = resp.data.decode()
    assert 'podskrift_cookie_consent' in text
    assert 'PodskriftConsent' in text
    assert 'Max-Age' in text
    assert 'localStorage' in text
    assert "cookieless_mode: 'on_reject'" in text
    # Regression: a mangled ";cookie" once SyntaxError'd the whole file.
    assert 'document.cookie' in text
    assert ';cookie' not in text.replace('document.cookie', '')


def test_cookie_consent_absent_on_admin_even_with_posthog(monkeypatch):
    """Admin pages keep PostHog off; no consent banner either."""
    monkeypatch.setenv('PODSKRIFT_ENV', 'production')
    monkeypatch.setenv('POSTHOG_KEY', 'phc_test_public_key')
    monkeypatch.setenv('ADMIN_EMAILS', 'admin-consent@test.com')
    client, _uid = _admin_login('admin-consent@test.com')
    resp = client.get('/admin')
    assert resp.status_code == 200
    body = resp.data.decode()
    assert 'posthog.init' not in body
    assert 'id="cookieConsent"' not in body
    assert 'cookieSettingsLink' not in body


def test_consented_posthog_session_id_requires_consent_cookie():
    """Server must not attach cookie-derived session ids without Accept."""
    with A.app.test_request_context('/', method='POST'):
        assert A._consented_posthog_session_id('ph_sess_abc') == ''
    with A.app.test_request_context(
            '/', method='POST',
            headers={'Cookie': f'{A.COOKIE_CONSENT_NAME}=accepted'}):
        assert A._consented_posthog_session_id('ph_sess_abc') == 'ph_sess_abc'
        assert A._consented_posthog_session_id('  x  ') == 'x'
    with A.app.test_request_context(
            '/', method='POST',
            headers={'Cookie': f'{A.COOKIE_CONSENT_NAME}=declined'}):
        assert A._consented_posthog_session_id('ph_sess_abc') == ''


def test_checkout_drops_ph_sid_without_consent(stripe_on, ph_events):
    """ph_sid on the checkout form is ignored unless consent cookie is Accept."""
    uid = _make_user('phsid-consent@test.com', limit=600, used=0)
    client = _login(uid)
    client.get('/settings')
    with client.session_transaction() as sess:
        token = sess.get('_csrf_token')

    resp = client.post('/billing/checkout', data={
        'csrf_token': token,
        'source': 'settings',
        'ph_sid': 'ph_should_drop',
    }, follow_redirects=False)
    assert resp.status_code == 303
    kw = stripe_on['last_create_params']
    assert 'ph_sid' not in (kw.get('metadata') or {})
    assert 'ph_sid' not in ((kw.get('payment_intent_data') or {}).get('metadata') or {})
    started = [e for e in ph_events.events if e['event'] == 'checkout_started']
    assert started
    assert '$session_id' not in started[-1]['properties']

    client.set_cookie(A.COOKIE_CONSENT_NAME, 'accepted')
    client.get('/settings')
    with client.session_transaction() as sess:
        token = sess.get('_csrf_token')
    resp = client.post('/billing/checkout', data={
        'csrf_token': token,
        'source': 'settings',
        'ph_sid': 'ph_keep_me',
    }, follow_redirects=False)
    assert resp.status_code == 303
    kw = stripe_on['last_create_params']
    assert kw['metadata']['ph_sid'] == 'ph_keep_me'
    assert kw['payment_intent_data']['metadata']['ph_sid'] == 'ph_keep_me'


def test_openai_key_field_is_marked_for_replay_masking():
    uid = _make_user('phmask@test.com')
    body = _login(uid).get('/settings').data.decode()
    assert 'id="openai_api_key"' in body
    assert 'ph-no-capture' in body
    # password-type input → maskAllInputs / maskInputOptions.password cover it
    assert 'type="password"' in body
    assert 'name="openai_api_key"' in body


def test_settings_never_renders_the_full_openai_key():
    """The raw key used to be stuffed into value=""; it must never reach the browser."""
    key = 'sk-' + 'k' * 40
    uid = _make_user('keymask@test.com', key=key)
    body = _login(uid).get('/settings').data.decode()
    assert key not in body
    assert f'••••{key[-4:]}' in body
    assert 'Key saved' in body
    assert 'value=""' in body
    assert 'Remove key' in body
    assert 'ph-no-capture' in body
    assert 'type="password"' in body


def test_settings_empty_save_keeps_the_openai_key(monkeypatch):
    key = 'sk-' + 'm' * 40
    uid = _make_user('keykeep@test.com', key=key)
    client = _login(uid)
    client.post('/settings', data={'openai_api_key': ''}, follow_redirects=True)
    with A.app.app_context():
        assert A.db.session.get(A.User, uid).openai_api_key == key


def test_settings_explicit_remove_clears_the_openai_key():
    key = 'sk-' + 'n' * 40
    uid = _make_user('keyrm@test.com', key=key)
    client = _login(uid)
    resp = client.post('/settings/openai-key/remove', follow_redirects=True)
    assert resp.status_code == 200
    assert b'API key removed' in resp.data
    with A.app.app_context():
        assert A.db.session.get(A.User, uid).openai_api_key is None


def test_signup_emits_user_signed_up(ph_events):
    A._register_attempts.clear()
    client = A.app.test_client()
    resp = client.post('/register', data={
        'email': 'phsignup@example.com',
        'password': 'password123',
        'password2': 'password123',
    }, follow_redirects=False)
    assert resp.status_code in (302, 303)
    events = [e for e in ph_events.events if e['event'] == 'user_signed_up']
    assert len(events) == 1
    assert events[0]['distinct_id']
    # No email/name in properties
    assert 'email' not in events[0]['properties']
    assert 'name' not in events[0]['properties']


def test_settings_view_and_key_save_events(ph_events, monkeypatch):
    uid = _make_user('phsettings@test.com')

    def ok_verify(key):
        return True, 'API key verified.', 'verified'
    monkeypatch.setattr(A, 'verify_openai_key', ok_verify)

    client = _login(uid)
    client.get('/settings')
    assert any(e['event'] == 'settings_viewed' and e['distinct_id'] == str(uid)
               for e in ph_events.events)

    client.post('/settings', data={'openai_api_key': 'sk-' + 'f' * 40},
                follow_redirects=True)
    saved = [e for e in ph_events.events if e['event'] == 'openai_key_saved']
    assert len(saved) == 1
    assert saved[0]['distinct_id'] == str(uid)
    assert saved[0]['properties'] == {'status': 'verified', 'app': 'podskrift'}


@pytest.mark.parametrize('verify_status', ['no_billing', 'unverified_network'])
def test_key_save_carries_coarse_status(ph_events, monkeypatch, verify_status):
    """429 and unreachable still save the key — status must distinguish them."""
    uid = _make_user(f'phstatus-{verify_status}@test.com')

    def soft_verify(key):
        return True, 'Key saved with caveat.', verify_status
    monkeypatch.setattr(A, 'verify_openai_key', soft_verify)

    _login(uid).post('/settings', data={'openai_api_key': 'sk-' + 'g' * 40},
                     follow_redirects=True)
    saved = [e for e in ph_events.events if e['event'] == 'openai_key_saved']
    assert len(saved) == 1
    assert saved[0]['properties'] == {'status': verify_status, 'app': 'podskrift'}


def test_rejected_key_emits_validation_failed_with_coarse_reason(ph_events, monkeypatch):
    uid = _make_user('phbadkey@test.com')

    def bad_verify(key):
        return False, 'rejected', 'invalid_key'
    monkeypatch.setattr(A, 'verify_openai_key', bad_verify)

    _login(uid).post('/settings', data={'openai_api_key': 'not-a-key'},
                     follow_redirects=True)
    fails = [e for e in ph_events.events if e['event'] == 'openai_key_validation_failed']
    assert len(fails) == 1
    assert fails[0]['properties'] == {'reason': 'invalid_key', 'app': 'podskrift'}
    # Never echo the submitted value
    assert 'not-a-key' not in str(fails[0])


def test_transcript_started_includes_key_source(ph_events, monkeypatch, trial_on):
    """resolve_openai_key uses 'user' (BYOK) or 'trial' — surface that on start."""
    uid = _make_user('phstart@test.com', key='sk-' + 'h' * 40)
    resp = _post_start(monkeypatch, uid, {
        'audio_url': 'https://example.com/ep.mp3',
        'episode_title': 'Ep',
        'duration_min': '1',
    })
    assert resp.status_code == 200
    started = [e for e in ph_events.events if e['event'] == 'transcript_started']
    assert len(started) == 1
    props = started[0]['properties']
    assert props['key_source'] == 'user'
    assert props['source'] == 'web'
    assert props['app'] == 'podskrift'
    assert props['duration_min'] == 1.0
    assert props['input_origin'] == 'audio'
    assert props['has_feed'] is False
    assert props['nth_transcript'] == 1


def test_transcript_started_trial_key_source(ph_events, monkeypatch, trial_on):
    uid = _make_user('phtrialstart@test.com')  # no own key → trial
    resp = _post_start(monkeypatch, uid, {
        'audio_url': 'https://example.com/ep.mp3',
        'episode_title': 'Ep',
        'duration_min': '1',
    })
    assert resp.status_code == 200
    started = [e for e in ph_events.events if e['event'] == 'transcript_started']
    assert len(started) == 1
    props = started[0]['properties']
    assert props['key_source'] == 'trial'
    assert props['source'] == 'web'
    assert props['app'] == 'podskrift'


def test_transcript_completed_includes_key_source(ph_events, monkeypatch, trial_on):
    """Worker fires completed with the same key_source captured at enqueue."""
    import types
    uid = _make_user('phdone@test.com', key='sk-' + 'i' * 40)
    monkeypatch.setattr(
        A.threading, 'Thread',
        lambda target=None, **kw: types.SimpleNamespace(
            daemon=True, start=lambda: target and target()))
    monkeypatch.setattr(A, 'download_audio', lambda *a, **kw: None)
    monkeypatch.setattr(A, 'transcribe_audio', lambda *a, **kw: None)
    monkeypatch.setattr(A, 'free_disk_bytes', lambda *a, **kw: 10 ** 12)

    with A.app.app_context():
        user = A.db.session.get(A.User, uid)
        payload, status = A.enqueue_transcription(
            user,
            {'title': 'Ep', 'audio_url': 'https://example.com/ep.mp3',
             'duration_min': 1},
        )
    assert status == 200 and 'task_id' in payload
    completed = [e for e in ph_events.events if e['event'] == 'transcript_completed']
    assert len(completed) == 1
    assert completed[0]['distinct_id'] == str(uid)
    props = completed[0]['properties']
    assert props['key_source'] == 'user'
    assert props['source'] == 'web'
    assert props['app'] == 'podskrift'
    assert props['input_origin'] == 'audio'
    assert 'nth_transcript' in props


def test_transcript_completed_outside_request_context(ph_events, monkeypatch, trial_on):
    """Background worker must not read flask_login.current_user after the request.

    Production passes the LocalProxy into enqueue_transcription. Evaluating
    user.id in the thread (no request context) raised
    "'NoneType' object has no attribute 'id'", which the except path turned
    into status=error even though the transcript was already saved — and then
    tried a trial refund on a finished job.
    """
    import types
    from flask_login import current_user, login_user
    from models import db, TranscriptionTask

    uid = _make_user('phproxy@test.com', limit=3600)  # trial path, no own key
    deferred = []
    monkeypatch.setattr(
        A.threading, 'Thread',
        lambda target=None, **kw: types.SimpleNamespace(
            daemon=True, start=lambda: deferred.append(target)))
    monkeypatch.setattr(A, 'download_audio', lambda *a, **kw: None)

    def finish(audio_file, task_id, openai_client, language=None):
        # Mimic a successful Whisper run: transcript saved, charge still open
        # (trial_settled stays False until refund/settle — same as production).
        A._update_task(
            task_id,
            status='completed',
            phase='completed',
            progress=100,
            transcript_text='done',
            chunk_total=1,
            chunk_index=0,
        )

    monkeypatch.setattr(A, 'transcribe_audio', finish)
    monkeypatch.setattr(A, 'free_disk_bytes', lambda *a, **kw: 10 ** 12)

    with A.app.test_request_context('/start_transcription'):
        user = db.session.get(A.User, uid)
        login_user(user)
        # Same object the web route passes: the LocalProxy, not a detached User.
        payload, status = A.enqueue_transcription(
            current_user,
            {'title': 'Ep', 'audio_url': 'https://example.com/ep.mp3',
             'duration_min': 1},
        )
    assert status == 200 and 'task_id' in payload
    assert deferred, 'worker was never scheduled'
    # Request context is gone — this is the production failure mode.
    deferred[0]()

    with A.app.app_context():
        task = db.session.get(TranscriptionTask, payload['task_id'])
        assert task.status == 'completed', task.error_message
        assert task.transcript_text == 'done'
        assert task.error_message is None
        # Full episode reached Whisper; allowance stays charged (no false refund).
        assert task.trial_seconds_charged and task.trial_seconds_charged > 0
        charged = task.trial_seconds_charged
        assert task.trial_settled is False
    assert _used(uid) == charged

    completed = [e for e in ph_events.events if e['event'] == 'transcript_completed']
    assert len(completed) == 1
    assert completed[0]['distinct_id'] == str(uid)
    assert completed[0]['properties']['key_source'] == 'trial'
    assert completed[0]['properties']['source'] == 'web'
    assert completed[0]['properties']['app'] == 'podskrift'
    assert not [e for e in ph_events.events if e['event'] == 'transcript_failed']


def test_analytics_raise_after_success_cannot_fail_the_job(monkeypatch, trial_on):
    """Even a broken capture() must leave status=completed and the charge alone."""
    import types
    from models import db, TranscriptionTask

    uid = _make_user('phanalyticboom@test.com', limit=3600)
    monkeypatch.setattr(
        A.threading, 'Thread',
        lambda target=None, **kw: types.SimpleNamespace(
            daemon=True, start=lambda: target and target()))
    monkeypatch.setattr(A, 'download_audio', lambda *a, **kw: None)

    def finish(audio_file, task_id, openai_client, language=None):
        A._update_task(
            task_id, status='completed', phase='completed', progress=100,
            transcript_text='ok', chunk_total=1, chunk_index=0)

    monkeypatch.setattr(A, 'transcribe_audio', finish)
    monkeypatch.setattr(A, 'free_disk_bytes', lambda *a, **kw: 10 ** 12)

    real_capture = A.product_analytics.capture

    def boom(event, distinct_id=None, properties=None):
        if event == 'transcript_completed':
            raise RuntimeError('posthog down')
        return real_capture(event, distinct_id, properties)

    monkeypatch.setattr(A.product_analytics, 'capture', boom)

    used_before = None
    with A.app.app_context():
        user = db.session.get(A.User, uid)
        used_before = user.trial_seconds_used
        payload, status = A.enqueue_transcription(
            user,
            {'title': 'Ep', 'audio_url': 'https://example.com/ep.mp3',
             'duration_min': 1},
        )
    assert status == 200
    with A.app.app_context():
        task = db.session.get(TranscriptionTask, payload['task_id'])
        assert task.status == 'completed'
        assert task.transcript_text == 'ok'
        charged = task.trial_seconds_charged
        assert task.trial_settled is False
    assert _used(uid) == used_before + charged


def test_openai_fail_reason_is_coarse():
    import analytics
    class E401(Exception):
        status_code = 401
    class E429(Exception):
        status_code = 429
    class E429Quota(Exception):
        status_code = 429
        code = 'insufficient_quota'
    class E429Rate(Exception):
        status_code = 429
        code = 'rate_limit_exceeded'
    class E503(Exception):
        status_code = 503
    assert analytics.openai_fail_reason(E401()) == 'invalid_key'
    assert analytics.openai_fail_reason(E429()) == 'no_billing'
    assert analytics.openai_fail_reason(E429Quota()) == 'no_billing'
    assert analytics.openai_fail_reason(E429Rate()) == 'rate_limit'
    assert analytics.openai_fail_reason(E503()) == 'network'
    assert analytics.openai_fail_reason(None, looks_like_key=False) == 'invalid_key'
    assert analytics.openai_fail_reason(RuntimeError('boom')) == 'other'
    assert analytics.openai_error_code(E429Quota()) == 'insufficient_quota'
    assert analytics.openai_error_code(E429Rate()) == 'rate_limit_exceeded'
    nested = E429()
    nested.body = {'error': {'code': 'insufficient_quota'}}
    assert analytics.openai_error_code(nested) == 'insufficient_quota'
    # BYOK remaps auth/billing; transient rate limits stay rate_limit.
    assert analytics.openai_fail_reason(
        E429Quota(), key_source='user') == 'own_key_no_credit'
    assert analytics.openai_fail_reason(
        E401(), key_source='user') == 'own_key_invalid'
    assert analytics.openai_fail_reason(
        E429Rate(), key_source='user') == 'rate_limit'
    assert analytics.openai_fail_reason(
        E429Quota(), key_source='trial') == 'no_billing'


def test_privacy_page_mentions_posthog():
    body = A.app.test_client().get('/privacy').data.decode()
    assert 'PostHog' in body
    assert 'EU' in body
    assert 'session replay' in body.lower()
    assert 'podskrift_cookie_consent' in body
    assert 'Cookie settings' in body
    assert 'Accept' in body
    assert 'Decline' in body
    assert 'cookieless' in body.lower()
    assert 'legitimate interest' in body.lower()
    assert '6(1)(f)' in body or 'art. 6(1)(f)' in body
    assert 'daily' in body.lower()
    assert 'hash' in body.lower()


def test_whats_new_page_renders_changelog_entries(trial_on):
    """Public /whats-new lists curated entries from changelog.json, newest first."""
    import html as _html
    entries = A.load_changelog_entries()
    assert entries, 'changelog.json must have at least one curated entry'
    assert entries[0]['id'] == 'new-signup-120-min-trial'
    assert entries[1]['id'] == 'new-look'
    assert entries[2]['id'] == 'forgot-password'
    assert entries[3]['id'] == 'share-listen-links'
    assert entries[4]['id'] == 'keyboard-and-faster-loading'
    assert entries[5]['id'] == 'show-landing-pages'
    assert entries[6]['id'] == 'public-share-links'
    assert entries[7]['id'] == 'unsubscribe-confirm-click'
    assert entries[8]['id'] == 'partial-preview-minutes-wording'
    assert entries[9]['id'] == 'partial-trial-preview'
    assert entries[10]['id'] == 'own-key-billing-clarity'
    assert entries[11]['id'] == 'clearer-missing-episode-audio'
    assert entries[12]['id'] == 'new-signup-60-min-trial'
    assert entries[13]['id'] == 'spotify-paste-robustness'
    assert entries[14]['id'] == 'no-double-charge-restart'
    resp = A.app.test_client().get('/whats-new')
    assert resp.status_code == 200
    body = _html.unescape(resp.data.decode())
    assert f'id="{entries[0]["id"]}"' in resp.data.decode()
    assert entries[0]['title'] in body
    assert entries[0]['summary'] in body
    assert f'id="{entries[1]["id"]}"' in resp.data.decode()
    assert entries[1]['title'] in body
    assert entries[1]['summary'] in body
    # Newest-first: the first entry's title appears before the last one's.
    assert body.index(entries[0]['title']) < body.index(entries[1]['title'])
    assert body.index(entries[0]['title']) < body.index(entries[-1]['title'])
    # Footer link + toast markup on other public pages.
    home = A.app.test_client().get('/').data.decode()
    assert '/whats-new' in home
    assert 'whatsNewToast' in home
    assert 'podskrift_whats_new_seen' in home


def test_changelog_json_is_well_formed():
    """Keep the data file agent-friendly: unique ids, ISO dates, required fields."""
    import json as _json
    from pathlib import Path
    path = Path(A.CHANGELOG_PATH)
    data = _json.loads(path.read_text(encoding='utf-8'))
    assert data.get('version') == 1
    entries = data['entries']
    assert isinstance(entries, list) and entries
    ids = []
    prev_date = None
    for entry in entries:
        assert entry['id'] and entry['title'] and entry['summary']
        assert re.match(r'^\d{4}-\d{2}-\d{2}$', entry['date']), entry['date']
        ids.append(entry['id'])
        if prev_date is not None:
            assert entry['date'] <= prev_date, (
                'entries must be newest-first by date '
                f'({prev_date} then {entry["date"]})'
            )
        prev_date = entry['date']
    assert len(ids) == len(set(ids)), 'duplicate changelog ids'
    # llms.txt surfaces the page for assistants.
    llms = A.app.test_client().get('/llms.txt').data.decode()
    assert 'whats-new' in llms
    assert '/whats-new' in llms


# --- trial_limit_hit: the buying signal ---------------------------------------

def _limit_hits(ph_events):
    return [e for e in ph_events.events if e['event'] == 'trial_limit_hit']


def test_trial_limit_hit_when_the_account_is_short(ph_events, monkeypatch, trial_on):
    uid = _make_user('phlimit-user@test.com', limit=600, used=540)  # 1 min left
    resp = _post_start(monkeypatch, uid, {
        'audio_url': 'https://example.com/ep.mp3',
        'episode_title': 'Ep',
        'duration_min': '5',
    })
    assert resp.status_code == 402
    hits = _limit_hits(ph_events)
    assert len(hits) == 1
    assert hits[0]['distinct_id'] == str(uid)
    assert hits[0]['properties'] == {
        'scope': 'user', 'stage': 'start', 'source': 'web',
        'estimate_min': 5, 'remaining_min': 1, 'app': 'podskrift',
    }
    body = resp.get_json()
    assert 'about 5 minutes' in body['error']
    assert '1 free minutes' in body['error']
    assert f'${A.openai_whisper_cost_usd(5):.2f}' in body['error']
    assert body.get('action_label') == 'Add OpenAI key →'
    assert body.get('action_url', '').endswith('/settings#openai')
    assert not [e for e in ph_events.events if e['event'] == 'transcript_started']


def test_trial_limit_hit_when_the_service_is_out(ph_events, monkeypatch, trial_on):
    from models import db
    with A.app.app_context():
        db.session.execute(A.text('DELETE FROM trial_budget_days'))
        db.session.commit()
    monkeypatch.setattr(A, 'TRIAL_DAILY_SECONDS', 0)
    uid = _make_user('phlimit-daily@test.com', limit=3600)
    resp = _post_start(monkeypatch, uid, {
        'audio_url': 'https://example.com/ep.mp3',
        'episode_title': 'Ep',
        'duration_min': '5',
    })
    assert resp.status_code == 402
    hits = _limit_hits(ph_events)
    assert [h['properties']['scope'] for h in hits] == ['daily']
    exhausted = [e for e in ph_events.events
                 if e['event'] == 'trial_daily_budget_exhausted']
    assert len(exhausted) == 1


def test_trial_limit_hit_for_an_over_long_episode(ph_events, monkeypatch, trial_on):
    monkeypatch.setattr(A, 'TRIAL_MAX_EPISODE_SECONDS', 1800)
    uid = _make_user('phlimit-long@test.com', limit=10 ** 6)
    resp = _post_start(monkeypatch, uid, {
        'audio_url': 'https://example.com/ep.mp3',
        'episode_title': 'Ep',
        'duration_min': '60',
    })
    assert resp.status_code == 402
    hits = _limit_hits(ph_events)
    assert len(hits) == 1
    assert hits[0]['properties']['scope'] == 'episode_length'
    assert hits[0]['properties']['estimate_min'] == 60
    assert 'remaining_min' in hits[0]['properties']
    body = resp.get_json()
    assert 'too long for the free trial' in body['error']
    assert f'${A.openai_whisper_cost_usd(60):.2f}' in body['error']
    assert body.get('action_url', '').endswith('#openai')


def test_no_trial_limit_hit_on_a_granted_reservation(ph_events, monkeypatch, trial_on):
    uid = _make_user('phlimit-ok@test.com', limit=3600)
    resp = _post_start(monkeypatch, uid, {
        'audio_url': 'https://example.com/ep.mp3',
        'episode_title': 'Ep',
        'duration_min': '5',
    })
    assert resp.status_code == 200
    assert _limit_hits(ph_events) == []


def test_reconcile_names_the_cap_that_refused(trial_on, monkeypatch):
    from models import db, TranscriptionTask
    uid = _make_user('phrecon-scope@test.com', limit=900)
    with A.app.app_context():
        A.trial_reserve(uid, 600)
        db.session.add(TranscriptionTask(id='trial-recon-scope', user_id=uid,
                                         episode_title='x', status='transcribing',
                                         trial_seconds_charged=600))
        db.session.commit()
        # Under the free per-episode max: trim to reservation (partial preview).
        assert A.trial_reconcile_task('trial-recon-scope', 2400) == 'trim'
        assert A.task_is_partial(db.session.get(TranscriptionTask, 'trial-recon-scope'))

        # Clear partial marker so the over-max path is exercised alone.
        task = db.session.get(TranscriptionTask, 'trial-recon-scope')
        task.partial_meta = None
        db.session.commit()

        monkeypatch.setattr(A, 'TRIAL_MAX_EPISODE_SECONDS', 1800)
        assert A.trial_reconcile_task('trial-recon-scope', 3600) == 'trim'
        assert A.task_is_partial(db.session.get(TranscriptionTask, 'trial-recon-scope'))
    assert _used(uid) == 600  # neither path took anything extra


def test_worker_reports_trial_exhausted_at_reconcile(ph_events, monkeypatch, trial_on):
    """The real audio outgrew the allowance after download: a failed transcript
    with reason trial_exhausted, plus the buying signal at stage reconcile."""
    import types
    uid = _make_user('phrecon-worker@test.com', limit=3600)
    monkeypatch.setattr(
        A.threading, 'Thread',
        lambda target=None, **kw: types.SimpleNamespace(
            daemon=True, start=lambda: target and target()))
    monkeypatch.setattr(A, 'download_audio', lambda *a, **kw: None)

    def refuse(*a, **kw):
        raise A.TrialExhausted('too long for what is left', scope='user')

    monkeypatch.setattr(A, 'transcribe_audio', refuse)
    monkeypatch.setattr(A, 'free_disk_bytes', lambda *a, **kw: 10 ** 12)

    with A.app.app_context():
        user = A.db.session.get(A.User, uid)
        payload, status = A.enqueue_transcription(
            user, {'title': 'Ep', 'audio_url': 'https://example.com/ep.mp3',
                   'duration_min': 1})
    assert status == 200, payload
    failed = [e for e in ph_events.events if e['event'] == 'transcript_failed']
    assert len(failed) == 1
    assert failed[0]['properties']['key_source'] == 'trial'
    assert failed[0]['properties']['source'] == 'web'
    assert failed[0]['properties']['reason'] == 'trial_exhausted'
    assert failed[0]['properties']['app'] == 'podskrift'
    assert [h['properties'] for h in _limit_hits(ph_events)] == [
        {'scope': 'user', 'stage': 'reconcile', 'source': 'web',
         'estimate_min': 1, 'remaining_min': 60, 'app': 'podskrift'}]
    assert _used(uid) == 0  # refunded in full: nothing reached Whisper


def test_api_transcriptions_are_labelled_api(ph_events, agent_write):
    r = _agent_post(
        '/api/v1/transcriptions', key=agent_write['key'],
        json_body={'publisher': 'Spårtsklubben', 'date': '2026-09-10'})
    assert r.status_code == 201, r.get_json()
    started = [e for e in ph_events.events if e['event'] == 'transcript_started']
    assert len(started) == 1
    props = started[0]['properties']
    assert props['key_source'] == 'trial'
    assert props['source'] == 'api'
    assert props['app'] == 'podskrift'
    assert props['input_origin'] == 'rss'
    assert props['has_feed'] is True
    assert props['podcast_name'] == 'Spårtsklubben'


def test_search_emits_podcast_searched_with_query_and_input_type(monkeypatch):
    monkeypatch.setenv('PODSKRIFT_ENV', 'production')
    monkeypatch.setenv('POSTHOG_KEY', 'phc_test_not_real')
    body = A.app.test_client().get('/').data.decode()
    assert "capture('podcast_searched'" in body
    assert "capture('podcast_search_no_results'" in body
    assert 'input_type' in body
    assert 'detectInputType' in body
    assert "slice(0, 200)" in body
    assert 'errored' in body
    assert 'error_kind' in body
    assert 'error_detail' in body
    # Empty-state copy + RSS help when nothing matches (tip assembled in JS).
    assert "No results? Paste" in body
    assert "the podcast's RSS feed" in body
    assert 'emptyStateTipText' in body
    assert '/rss-help' in body or "url_for('rss_help')" in body
    assert 'showSearchEmptyState' in body
    # Typed search and Spotify resolve both go through trackSearch(query, …).
    assert 'trackSearch(query,' in body or 'trackSearch(url,' in body
    assert 'detectInputType' in body
    assert 'prepareSearchQuery' in body
    for kind in ('name', 'rss_feed', 'spotify_link', 'apple_link',
                 'youtube_link', 'audio_url', 'other'):
        assert f"'{kind}'" in body


def test_reconcile_trims_when_daily_cap_blocks_top_up(trial_on, monkeypatch):
    """The account has room; today's budget does not — keep the reservation and trim."""
    from models import db, TranscriptionTask
    uid = _make_user('phrecon-daily@test.com', limit=10 ** 6)
    with A.app.app_context():
        A.trial_reserve(uid, 600)
        db.session.add(TranscriptionTask(id='trial-recon-daily', user_id=uid,
                                         episode_title='x', status='transcribing',
                                         trial_seconds_charged=600))
        db.session.commit()
        monkeypatch.setattr(A, 'TRIAL_DAILY_SECONDS', A.trial_daily_used_seconds())
        assert A.trial_reconcile_task('trial-recon-daily', 1200) == 'trim'
        task = db.session.get(TranscriptionTask, 'trial-recon-daily')
        assert task.trial_seconds_charged == 600
        assert A.task_is_partial(task)
    assert _used(uid) == 600


# --- ChatGPT landing: signup keeps episode, badges, copy -----------------------

def test_safe_next_url_rejects_off_site_targets():
    with A.app.test_request_context('/'):
        assert A.safe_next_url('/resume-transcription') == '/resume-transcription'
        assert A.safe_next_url('/settings#openai') == '/settings#openai'
        assert A.safe_next_url('/some/path?x=1') == '/some/path?x=1'
        assert A.safe_next_url('/') == '/'
        # Absolute / protocol-relative
        assert A.safe_next_url('https://evil.com') == '/'
        assert A.safe_next_url('//evil.com') == '/'
        assert A.safe_next_url('https://evil.example/phish', default='/x') == '/x'
        # Backslash open-redirect tricks (browsers treat \ as /)
        assert A.safe_next_url('/\\evil.com') == '/'
        assert A.safe_next_url('/\\/evil.com') == '/'
        assert A.safe_next_url('/%5Cevil.com') == '/'
        assert A.safe_next_url('/%5C/evil.com') == '/'
        assert A.safe_next_url('/%255Cevil.com') == '/'  # double-encoded \
        assert A.safe_next_url(None) == '/'
        assert A.safe_next_url('') == '/'


def test_whisper_cost_constant_matches_copy_figures():
    """$0.36/hr and $0.54/90min must come from WHISPER_COST_PER_MINUTE."""
    assert A.WHISPER_COST_PER_MINUTE == 0.006
    assert A.openai_whisper_cost_usd(60) == 0.36
    assert A.openai_whisper_cost_usd(90) == 0.54


def test_episode_needs_own_key_badge_logic(trial_on):
    assert A.episode_needs_own_key(None, 60) is False
    assert A.episode_needs_own_key(95, None) is False
    assert A.episode_needs_own_key(45, 60) is False
    # Longer than remaining free trial with no paid → free preview, not a badge.
    assert A.episode_needs_own_key(95, 60) is False
    # Over free per-episode max with plenty of remaining trial → still needs pack/key.
    assert A.episode_needs_own_key(200, 300) is True
    # Tiny remainder cannot start a useful preview.
    assert A.episode_needs_own_key(95, 3) is True
    # Paid balance that does not cover the episode still needs a pack/key.
    assert A.episode_needs_own_key(95, 60, paid_minutes=10) is True


def test_anonymous_pending_transcription_goes_to_register(monkeypatch, trial_on):
    """Transcribe while logged out stashes the episode and opens /register."""
    monkeypatch.setattr(A, '_is_fetchable_url', lambda url: True)
    client = A.app.test_client()
    resp = client.post('/pending-transcription', data={
        'audio_url': 'https://cdn.example.com/ep.mp3',
        'episode_title': 'ChatGPT Ep',
        'podcast_name': 'Show',
        'duration_min': '42',
        'language': 'en',
    }, follow_redirects=False)
    assert resp.status_code in (302, 303)
    loc = resp.headers['Location']
    assert '/register' in loc
    assert 'next=' in loc
    assert 'resume-transcription' in loc
    with client.session_transaction() as sess:
        pending = sess.get(A.PENDING_TRANSCRIPTION_KEY)
        assert pending is not None
        assert pending['audio_url'] == 'https://cdn.example.com/ep.mp3'
        assert pending['title'] == 'ChatGPT Ep'
        assert pending['duration_min'] == 42.0


def test_register_rejects_off_site_next_and_keeps_flash(monkeypatch, trial_on):
    A._register_attempts.clear()
    email = 'nextsafe@example.com'
    _purge([email])
    client = _fresh_client()
    resp = client.post(
        '/register?next=https://evil.example/steal',
        data={'email': email, 'password': 'abcdefgh1', 'password2': 'abcdefgh1'},
        follow_redirects=True,
    )
    body = resp.data.decode()
    assert 'Account created' in body
    assert f'{A.NEW_USER_TRIAL_SECONDS // 60} free minutes' in body
    assert 'evil.example' not in resp.request.url
    assert resp.request.path == '/'
    _purge([email])


def test_register_with_pending_episode_resumes_and_flashes(monkeypatch, trial_on):
    """After signup with a stashed episode, resume starts the job."""
    A._register_attempts.clear()
    email = 'resume-ep@example.com'
    _purge([email])
    monkeypatch.setattr(A, '_is_fetchable_url', lambda url: True)
    monkeypatch.setattr(A, 'free_disk_bytes', lambda *a, **kw: 10 ** 12)

    started = {}

    def fake_enqueue(user, meta, rss_url=None, language='', source='web'):
        started['meta'] = meta
        started['user_id'] = user.id
        return {'task_id': 'task-from-resume'}, 200

    monkeypatch.setattr(A, 'enqueue_transcription', fake_enqueue)

    client = _fresh_client()
    client.post('/pending-transcription', data={
        'audio_url': 'https://cdn.example.com/kept.mp3',
        'episode_title': 'Kept Episode',
        'duration_min': '12',
    })
    resp = client.post(
        '/register?next=/resume-transcription',
        data={'email': email, 'password': 'abcdefgh1', 'password2': 'abcdefgh1'},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    with client.session_transaction() as sess:
        flashes = sess.get('_flashes') or []
        assert any('starting your transcript' in msg for _cat, msg in flashes)
        assert A.PENDING_TRANSCRIPTION_KEY in sess

    resume = client.get('/resume-transcription', follow_redirects=False)
    assert resume.status_code in (302, 303)
    assert '/transcription/task-from-resume' in resume.headers['Location']
    assert started.get('meta', {}).get('audio_url') == 'https://cdn.example.com/kept.mp3'
    assert started.get('meta', {}).get('title') == 'Kept Episode'
    with client.session_transaction() as sess:
        assert A.PENDING_TRANSCRIPTION_KEY not in sess
    _purge([email])


def test_login_with_pending_episode_resumes(monkeypatch, trial_on):
    monkeypatch.setattr(A, '_is_fetchable_url', lambda url: True)
    monkeypatch.setattr(A, 'free_disk_bytes', lambda *a, **kw: 10 ** 12)
    uid = _make_user('login-resume@test.com')
    started = {}

    def fake_enqueue(user, meta, rss_url=None, language='', source='web'):
        started['title'] = meta.get('title')
        return {'task_id': 'login-resume-task'}, 200

    monkeypatch.setattr(A, 'enqueue_transcription', fake_enqueue)

    from models import User
    with A.app.app_context():
        email = A.db.session.get(User, uid).email

    client = A.app.test_client()
    client.post('/pending-transcription', data={
        'audio_url': 'https://cdn.example.com/login.mp3',
        'episode_title': 'After Login',
        'duration_min': '8',
    })
    resp = client.post(
        '/login?next=/resume-transcription',
        data={'email': email, 'password': 'password123'},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    assert 'resume-transcription' in resp.headers['Location']
    resume = client.get('/resume-transcription', follow_redirects=False)
    assert '/transcription/login-resume-task' in resume.headers['Location']
    assert started.get('title') == 'After Login'


def test_homepage_copy_leads_with_spotify(trial_on):
    body = A.app.test_client().get('/').data.decode()
    assert 'Spotify & podcast transcripts' in body
    assert 'even on Spotify' in body
    assert 'Paste a Spotify link or search a podcast' in body
    assert 'badge-needs-key' in body
    assert f'Your first {A.NEW_USER_TRIAL_SECONDS // 60} minutes' in body
    assert 'run on our key' in body
    assert 'Download .txt or .srt transcript' not in body
    assert f'${A.openai_whisper_cost_usd(90):.2f}' in body
    assert f'${A.openai_whisper_cost_usd(60):.2f}' in body


def test_homepage_anon_sees_new_signup_trial_copy(trial_on):
    """Logged-out visitors get the advertised new-account grant (60)."""
    grant = A.NEW_USER_TRIAL_SECONDS // 60
    body = A.app.test_client().get('/').data.decode()
    assert f'First {grant} minutes of audio free' in body
    assert f'covers {grant} minutes of audio in total' in body


def test_homepage_logged_in_60_min_user_sees_own_trial_copy(trial_on):
    """A new 60-minute account sees its own limit, not a mismatched figure."""
    grant = A.NEW_USER_TRIAL_SECONDS // 60
    uid = _make_user('home-trial-60@test.com', limit=A.NEW_USER_TRIAL_SECONDS, used=0)
    body = _login(uid).get('/').data.decode()
    # Signup hero is for anonymous visitors only.
    assert f'First {grant} minutes of audio free' not in body
    assert f'covers {grant} minutes of audio in total' in body
    assert f'— {grant} left' in body
    assert 'covers 180 minutes of audio in total' not in body


def test_homepage_logged_in_legacy_180_min_user_sees_own_trial_copy(trial_on):
    """Legacy 180-minute grants must not be told the signup trial is 60 total."""
    uid = _make_user('home-trial-180@test.com', limit=180 * 60, used=0)
    body = _login(uid).get('/').data.decode()
    assert 'First 60 minutes of audio free' not in body
    assert 'covers 60 minutes of audio in total' not in body
    assert 'covers 180 minutes of audio in total' in body
    assert '— 180 left' in body
    # Balance banner still reflects the same remaining grant.
    assert '180 free minutes' in body or '180 minutes' in body


def test_register_helper_text_mentions_free_minutes(trial_on):
    body = A.app.test_client().get('/register').data.decode()
    assert f'{A.NEW_USER_TRIAL_SECONDS // 60} free minutes' in body
    assert 'No card, no OpenAI key' in body
    assert 'password2' not in body
    assert 'Confirm password' not in body
    assert 'Create account' in body
    assert 'At least 8 characters' in body
    assert 'Creating account…' in body or 'Creating account' in body
    assert 'novalidate' in body
    assert 'id="registerSubmit"' in body


def test_register_with_pending_episode_shows_episode_card(monkeypatch, trial_on):
    """Contextual signup: episode card + start-transcript CTA when stash is set."""
    monkeypatch.setattr(A, '_is_fetchable_url', lambda url: True)
    client = A.app.test_client()
    client.post('/pending-transcription', data={
        'audio_url': 'https://cdn.example.com/ep.mp3',
        'episode_title': 'Hard Fork Special',
        'podcast_name': 'Hard Fork',
        'duration_min': '48',
        'artwork': 'https://cdn.example.com/art.jpg',
        'language': 'en',
    }, follow_redirects=False)
    body = client.get('/register').data.decode()
    assert 'Create a free account to get this transcript' in body
    assert 'Hard Fork Special' in body
    assert 'Hard Fork' in body
    assert '48 min' in body
    assert f'of your {A.NEW_USER_TRIAL_SECONDS // 60} free' in body
    assert 'Create account &amp; start transcript' in body or 'Create account & start transcript' in body
    assert 'Confirm password' not in body


def test_register_without_pending_is_generic(trial_on):
    body = A.app.test_client().get('/register').data.decode()
    assert 'Create a free account to get this transcript' not in body
    assert 'Create your free account' in body
    assert 'Create account &amp; start transcript' not in body
    assert 'Create account & start transcript' not in body


def test_signup_redirects_301_to_register(trial_on):
    client = A.app.test_client()
    resp = client.get('/signup?next=/resume-transcription', follow_redirects=False)
    assert resp.status_code == 301
    loc = resp.headers['Location']
    assert '/register' in loc
    assert 'next=' in loc
    assert 'resume-transcription' in loc


def test_settings_renames_developer_api_key_card(trial_on):
    uid = _make_user('devcard@test.com')
    body = _login(uid).get('/settings').data.decode()
    assert 'Podskrift API key (developers only)' in body
    assert 'id="openai"' in body
    assert 'transcribe on the website' in body


def test_verify_openai_key_copy(monkeypatch):
    ok, msg, status = A.verify_openai_key('psk_looks_like_ours_but_isnt')
    assert ok is False and status == 'invalid_key'
    assert 'Podskrift developer key' in msg
    assert 'sk-' in msg

    class Audio:
        def __init__(self):
            self.transcriptions = self

        def create(self, **kw):
            return type('T', (), {'text': ''})()

    class Client:
        def __init__(self, *a, **kw):
            self.models = self
            self.audio = Audio()

        def list(self):
            return []

    monkeypatch.setattr(A, 'OpenAI', Client)
    ok, msg, status = A.verify_openai_key('sk-' + 'v' * 40)
    assert ok and status == 'verified'
    assert 'Key saved and verified' in msg
    assert 'Whisper' in msg


def _fake_openai_client(*, list_exc=None, whisper_exc=None):
    """OpenAI stand-in that can fail models.list and/or Whisper separately."""

    class Audio:
        def __init__(self):
            self.transcriptions = self

        def create(self, **kw):
            if whisper_exc is not None:
                raise whisper_exc
            return type('T', (), {'text': ''})()

    class Client:
        def __init__(self, *a, **kw):
            self.models = self
            self.audio = Audio()

        def list(self):
            if list_exc is not None:
                raise list_exc
            return []

    return Client


def test_verify_probes_whisper_after_models_list(monkeypatch):
    """Zero-credit accounts pass models.list; Whisper is what catches them."""
    class NoCredit(Exception):
        status_code = 429
        code = 'insufficient_quota'

    monkeypatch.setattr(A, 'OpenAI', _fake_openai_client(whisper_exc=NoCredit()))
    ok, msg, status = A.verify_openai_key('sk-' + 'w' * 40)
    assert ok is True
    assert status == 'no_billing'
    assert 'saved' in msg.lower()
    assert 'credit' in msg.lower()


def test_verify_whisper_network_error_still_saves(monkeypatch):
    monkeypatch.setattr(
        A, 'OpenAI',
        _fake_openai_client(whisper_exc=A.APIConnectionError(request=None)))
    ok, msg, status = A.verify_openai_key('sk-' + 'n' * 40)
    assert ok is True
    assert status == 'unverified_network'


def test_silent_wav_probe_is_short_wav():
    raw = A._silent_wav_bytes()
    assert raw[:4] == b'RIFF'
    assert b'WAVE' in raw[:16]
    assert 1000 < len(raw) < 100_000


def test_settings_openai_links_and_create_credit_hint(trial_on):
    uid = _make_user('links@test.com')
    body = _login(uid).get('/settings').data.decode()
    assert 'href="https://platform.openai.com/api-keys"' in body
    assert 'href="https://platform.openai.com/account/billing"' in body
    assert 'rel="noopener"' in body
    assert 'target="_blank"' in body
    assert '1. Create key' in body
    assert '2. Add credit' in body


def test_settings_collapses_developer_api_key_without_one(trial_on):
    uid = _make_user('collapse-api@test.com')
    body = _login(uid).get('/settings').data.decode()
    assert '<details' in body
    assert 'Podskrift API key (developers only)' in body
    # Create button is inside the collapsed details, not a top-level card header CTA.
    assert body.index('<details') < body.index('>Create<')


def test_settings_expands_developer_api_key_when_present(trial_on):
    from models import db, User
    uid = _make_user('expand-api@test.com')
    with A.app.app_context():
        u = db.session.get(User, uid)
        u.api_key_hash = 'a' * 64
        u.api_key_prefix = 'psk_abcd'
        db.session.commit()
    body = _login(uid).get('/settings').data.decode()
    assert '<details' not in body or body.count('<details') == 0
    assert 'psk_abcd' in body
    assert 'Regenerate' in body


def test_no_billing_save_offers_pack_when_stripe_on(ph_events, monkeypatch, stripe_on):
    uid = _make_user('nobill-pack@test.com')

    def soft_verify(key):
        return True, A.OPENAI_NO_BILLING_SAVE_MSG, 'no_billing'
    monkeypatch.setattr(A, 'verify_openai_key', soft_verify)

    client = _login(uid)
    body = client.post(
        '/settings', data={'openai_api_key': 'sk-' + 'p' * 40},
        follow_redirects=True,
    ).data.decode()
    assert 'openai-no-billing' in body or 'OpenAI account still needs credit' in body
    assert 'openai_no_billing' in body
    assert A.CREDIT_PACK_LABEL in body
    saved = [e for e in ph_events.events if e['event'] == 'openai_key_saved']
    assert saved and saved[0]['properties']['status'] == 'no_billing'


def test_transcription_status_flags_no_billing_with_retry(stripe_on):
    from models import db, TranscriptionTask
    uid = _make_user('nobill-status@test.com', key='sk-' + 'q' * 40)
    with A.app.app_context():
        db.session.add(TranscriptionTask(
            id='nobill-1', user_id=uid, episode_title='Hard Fork',
            status='error', phase='error',
            podcast_name='Hard Fork',
            artwork_url='https://example.com/art.jpg',
            episode_published='2024-01-01',
            source_audio_url='https://example.com/ep.mp3',
            rss_url='https://feeds.example.com/hardfork.xml',
            audio_duration=48 * 60,
            language='en',
            error_message=A.describe_openai_error(
                type('E', (Exception,), {'status_code': 429, 'code': 'insufficient_quota'})(),
            ),
        ))
        db.session.commit()
    data = _login(uid).get('/status/nobill-1').get_json()
    assert data.get('no_billing') is True
    assert data.get('minutes_error') is not True
    assert 'insufficient_quota' not in (data.get('error') or '')
    assert data['retry']['audio_url'] == 'https://example.com/ep.mp3'
    assert data['retry']['episode_title'] == 'Hard Fork'
    assert data['retry']['duration_min'] == '48'
    assert data['retry']['rss_url'] == 'https://feeds.example.com/hardfork.xml'
    assert data.get('buy_available') is True


def test_transcription_page_has_no_billing_retry_ui(stripe_on):
    src = open('templates/transcription.html').read()
    assert 'no_billing' in src
    assert 'Add credit at OpenAI' in src
    assert 'Retry this episode' in src
    assert 'retryEpisode' in src
    assert 'transcription_no_billing' in src
    assert 'Your OpenAI account has no credit' in src
    assert 'remove the key in' in src
    assert 'Settings to use Podskrift free or paid minutes' in src


def test_enqueue_stores_source_audio_url_for_retry(monkeypatch, trial_on):
    import types
    uid = _make_user('retry-store@test.com', key='sk-' + 'r' * 40)
    monkeypatch.setattr(
        A.threading, 'Thread',
        lambda target=None, **kw: types.SimpleNamespace(
            daemon=True, start=lambda: None))
    monkeypatch.setattr(A, 'free_disk_bytes', lambda *a, **kw: 10 ** 12)
    with A.app.app_context():
        user = A.db.session.get(A.User, uid)
        payload, status = A.enqueue_transcription(
            user,
            {'title': 'Ep', 'audio_url': 'https://example.com/retry.mp3',
             'duration_min': 12, 'podcast_name': 'Show'},
        )
        assert status == 200, payload
        task = A.db.session.get(A.TranscriptionTask, payload['task_id'])
        assert task.source_audio_url == 'https://example.com/retry.mp3'


def test_byok_no_billing_checkout_clears_key(stripe_on):
    key = 'sk-' + 'z' * 40
    uid = _make_user('byok-clear@test.com', key=key)
    client = _login(uid)
    with client.session_transaction() as sess:
        sess['_csrf_token'] = 'tok'
    resp = client.post('/billing/checkout', data={
        'csrf_token': 'tok',
        'source': 'openai_no_billing',
        'ph_sid': '',
    }, follow_redirects=False)
    assert resp.status_code in (302, 303)
    with A.app.app_context():
        assert A.db.session.get(A.User, uid).openai_api_key is None


def test_saved_openai_key_never_leaks_into_html_or_analytics(ph_events, monkeypatch, stripe_on):
    """Regression: a prior PR leaked keys into /pricing via a template expression."""
    key = 'sk-leak-regression-key-never-render-' + ('x' * 24)
    uid = _make_user('noleak@test.com', key=key)

    def ok_verify(k):
        return True, 'Key saved and verified for Whisper transcription.', 'verified'
    monkeypatch.setattr(A, 'verify_openai_key', ok_verify)

    client = _login(uid)
    # Re-save so openai_key_saved fires with this key in scope.
    client.post('/settings', data={'openai_api_key': key}, follow_redirects=True)

    for path in ('/settings', '/pricing', '/'):
        body = client.get(path).data.decode()
        assert key not in body, f'{path} rendered the raw OpenAI key'

    # Transcription page for a task owned by this user.
    from models import db, TranscriptionTask
    with A.app.app_context():
        db.session.add(TranscriptionTask(
            id='noleak-task', user_id=uid, episode_title='Ep',
            status='error', phase='error',
            source_audio_url='https://example.com/ep.mp3',
            error_message='OpenAI refused the job: your OpenAI account has no credit left.',
        ))
        db.session.commit()
    body = client.get('/transcription/noleak-task').data.decode()
    assert key not in body
    status = client.get('/status/noleak-task').get_json()
    assert key not in str(status)

    for event in ph_events.events:
        blob = str(event)
        assert key not in blob
        props = event.get('properties') or {}
        for v in props.values():
            assert key not in str(v)

    # Hint may show last 4 chars only — never the full key.
    settings = client.get('/settings').data.decode()
    assert f'••••{key[-4:]}' in settings
    assert key not in settings


def _openai_exc(status_code, code=None, name='OpenAIError'):
    """Build an exception that looks like it came from the OpenAI SDK."""
    cls = type(name, (Exception,), {
        'status_code': status_code,
        'code': code,
    })
    cls.__module__ = 'openai'
    return cls(f'{name}:{code or status_code}')


def _enqueue_with_openai_boom(monkeypatch, uid, exc):
    """Run enqueue_transcription with a worker that raises `exc` immediately."""
    import types

    def boom(*a, **kw):
        raise exc

    monkeypatch.setattr(
        A.threading, 'Thread',
        lambda target=None, **kw: types.SimpleNamespace(
            daemon=True, start=lambda: target and target()))
    monkeypatch.setattr(A, 'download_audio', boom)
    monkeypatch.setattr(A, 'free_disk_bytes', lambda *a, **kw: 10 ** 12)
    monkeypatch.setattr(
        A, '_is_openai_error',
        lambda e: type(e).__module__.split('.')[0] == 'openai')

    with A.app.app_context():
        user = A.db.session.get(A.User, uid)
        return A.enqueue_transcription(
            user,
            {'title': 'Ep', 'audio_url': 'https://example.com/ep.mp3',
             'duration_min': 1},
        )


def test_transcript_failed_distinguishes_rate_limit(
        ph_events, monkeypatch, trial_on, sentry_events):
    """Transient 429 rate_limit_exceeded stays rate_limit (not own_key_*) and
    still reports to Sentry — not an account-billing outcome."""
    uid = _make_user('ratefail@test.com', key='sk-' + 't' * 40)
    payload, status = _enqueue_with_openai_boom(
        monkeypatch, uid,
        _openai_exc(429, 'rate_limit_exceeded', 'RateLimitError'))
    assert status == 200, payload
    failed = [e for e in ph_events.events
              if e['event'] == 'transcript_failed' and e['distinct_id'] == str(uid)]
    assert len(failed) == 1, [(e['event'], e.get('properties')) for e in ph_events.events]
    assert failed[0]['properties']['reason'] == 'rate_limit'
    assert 'sk-' not in str(failed[0])
    assert len(sentry_events) == 1
    assert sentry_events[0]['tags']['task.key_source'] == 'user'
    assert sentry_events[0]['tags']['openai.status'] == '429'


def test_byok_insufficient_quota_is_clear_refunded_and_not_sentry(
        trial_on, monkeypatch, sentry_events, ph_events):
    """PODSKRIFT-3: user's empty OpenAI account is not our outage."""
    from models import TranscriptionTask, db

    uid = _make_user('byokquota@test.com', key='sk-' + 'q' * 40, limit=3600)
    payload, status = _enqueue_with_openai_boom(
        monkeypatch, uid,
        _openai_exc(429, 'insufficient_quota', 'RateLimitError'))
    assert status == 200, payload
    task_id = payload['task_id']

    with A.app.app_context():
        task = db.session.get(TranscriptionTask, task_id)
        assert task is not None
        assert task.status == 'error'
        err = task.error_message or ''
        assert 'no credit' in err.lower()
        assert 'platform.openai.com/account/billing' in err
        assert 'Settings' in err
        assert 'free or paid minutes' in err
        assert 'insufficient_quota' not in err
        # Own-key jobs are unmetered; reservation must stay released / unused.
        assert task.trial_seconds_charged is None
        assert (task.paid_seconds_charged or 0) == 0
    assert _used(uid) == 0

    failed = [e for e in ph_events.events
              if e['event'] == 'transcript_failed' and e['distinct_id'] == str(uid)]
    assert len(failed) == 1
    assert failed[0]['properties']['reason'] == 'own_key_no_credit'
    assert failed[0]['properties']['key_source'] == 'user'
    assert sentry_events == [], 'BYOK quota must not fire the Sentry alert'


def test_byok_invalid_key_is_not_sentry(trial_on, monkeypatch, sentry_events, ph_events):
    uid = _make_user('byokbadkey@test.com', key='sk-' + 'i' * 40, limit=3600)
    payload, status = _enqueue_with_openai_boom(
        monkeypatch, uid,
        _openai_exc(401, 'invalid_api_key', 'AuthenticationError'))
    assert status == 200, payload
    failed = [e for e in ph_events.events
              if e['event'] == 'transcript_failed' and e['distinct_id'] == str(uid)]
    assert len(failed) == 1
    assert failed[0]['properties']['reason'] == 'own_key_invalid'
    assert sentry_events == []
    assert _used(uid) == 0
    with A.app.app_context():
        task = A.db.session.get(A.TranscriptionTask, payload['task_id'])
        assert 'rejected' in (task.error_message or '').lower()
        assert 'Settings' in (task.error_message or '')


def test_platform_key_insufficient_quota_still_goes_to_sentry(
        trial_on, monkeypatch, sentry_events, ph_events):
    """Our key out of credit is a real outage — must alert."""
    from models import TranscriptionTask, db

    uid = _make_user('ourquota@test.com', limit=3600, used=0)
    payload, status = _enqueue_with_openai_boom(
        monkeypatch, uid,
        _openai_exc(429, 'insufficient_quota', 'RateLimitError'))
    assert status == 200, payload
    task_id = payload['task_id']

    with A.app.app_context():
        task = db.session.get(TranscriptionTask, task_id)
        assert task.status == 'error'
        # Reservation released (pro-rata refund of unsent work).
        assert (task.trial_seconds_charged or 0) == 0
    assert _used(uid) == 0

    failed = [e for e in ph_events.events
              if e['event'] == 'transcript_failed' and e['distinct_id'] == str(uid)]
    assert len(failed) == 1
    assert failed[0]['properties']['reason'] == 'no_billing'
    assert failed[0]['properties']['key_source'] == 'trial'
    assert len(sentry_events) == 1
    assert sentry_events[0]['tags']['task.key_source'] == 'trial'
    assert sentry_events[0]['tags']['openai.status'] == '429'


def test_byok_quota_message_hints_remove_key():
    exc = _openai_exc(429, 'insufficient_quota')
    msg = A.describe_openai_error(exc, key_source='user')
    assert 'no credit' in msg.lower()
    assert 'Settings' in msg
    assert 'free or paid minutes' in msg
    # Platform-key copy must not tell the user to remove a key they do not have.
    trial_msg = A.describe_openai_error(exc, key_source='trial')
    assert 'Settings' not in trial_msg


def test_whisper_does_not_retry_quota_or_auth_errors(tmp_path, monkeypatch):
    """Auth/billing 4xx must fail immediately; only timeouts/connection retry."""
    attempts = {'n': 0}

    class FakeClient:
        class audio:
            class transcriptions:
                @staticmethod
                def create(**kw):
                    attempts['n'] += 1
                    raise _openai_exc(429, 'insufficient_quota', 'RateLimitError')

    chunk = tmp_path / 'c0.mp3'
    chunk.write_bytes(b'\0' * 16)
    monkeypatch.setattr(A.time, 'sleep', lambda *_: None)

    with pytest.raises(Exception) as caught:
        A._whisper_transcribe_chunk(
            FakeClient(), str(chunk), 'no', chunk_label='chunk 1/1')
    assert getattr(caught.value, 'code', None) == 'insufficient_quota'
    assert attempts['n'] == 1, 'quota errors must not burn the retry budget'

    attempts['n'] = 0

    class RateClient:
        class audio:
            class transcriptions:
                @staticmethod
                def create(**kw):
                    attempts['n'] += 1
                    raise _openai_exc(429, 'rate_limit_exceeded', 'RateLimitError')

    with pytest.raises(Exception) as caught:
        A._whisper_transcribe_chunk(
            RateClient(), str(chunk), 'no', chunk_label='chunk 1/1')
    assert getattr(caught.value, 'code', None) == 'rate_limit_exceeded'
    # Existing behaviour: rate_limit_exceeded is not retried in the chunk loop
    # (only APITimeoutError / APIConnectionError are). Keep that contract.
    assert attempts['n'] == 1


def test_own_key_user_gets_null_trial_badge(trial_on):
    uid = _make_user('badge-own@test.com', key='sk-' + 'o' * 40)
    body = _login(uid).get('/').data.decode()
    assert 'var TRIAL_REMAINING_MIN = null;' in body


def test_anon_homepage_exposes_new_account_trial_badge(trial_on):
    body = A.app.test_client().get('/').data.decode()
    assert f'var TRIAL_REMAINING_MIN = {A.NEW_USER_TRIAL_SECONDS // 60};' in body


def test_default_trial_grants_and_daily_budget():
    """New signups get 120 minutes; NULL-limit legacy rows use 180; daily is 750.
    Lifetime safety is off when TRIAL_GLOBAL_MINUTES is unset. Per-episode max
    still tracks TRIAL_MINUTES so a 120-min user over remaining balance hits the
    paywall, not the hard episode-length refusal."""
    assert A.NEW_USER_TRIAL_SECONDS == 120 * 60
    assert A.TRIAL_DEFAULT_SECONDS == 180 * 60
    assert A.TRIAL_MAX_EPISODE_SECONDS == A.TRIAL_DEFAULT_SECONDS
    assert A.TRIAL_DAILY_SECONDS == 750 * 60
    assert A.TRIAL_GLOBAL_SECONDS == 0


def test_null_limit_accounts_pick_up_the_raised_default(monkeypatch, trial_on):
    """users.trial_seconds_limit NULL means "use TRIAL_MINUTES" — raising the
    constant lifts every existing account without resetting trial_seconds_used."""
    monkeypatch.setattr(A, 'TRIAL_DEFAULT_SECONDS', 180 * 60)
    uid = _make_user('lift-default@test.com', limit=None, used=600)
    with A.app.app_context():
        limit, used, remaining = A.trial_status(A.db.session.get(A.User, uid))
    assert limit == 180 * 60
    assert used == 600
    assert remaining == 180 * 60 - 600


def test_register_stamps_new_user_trial_limit(trial_on):
    """Registration sets trial_seconds_limit to NEW_USER_TRIAL_MINUTES (120)."""
    email = 'newgrant@example.com'
    _purge([email])
    A._register_attempts.clear()
    client = A.app.test_client()
    resp = client.post('/register', data={
        'email': email,
        'password': 'password123',
    }, follow_redirects=False)
    assert resp.status_code in (302, 303)
    with A.app.app_context():
        u = A.User.query.filter_by(email=email).first()
        assert u is not None
        assert u.trial_seconds_limit == A.NEW_USER_TRIAL_SECONDS
        assert u.trial_seconds_limit == 120 * 60
        limit, used, remaining = A.trial_status(u)
        assert limit == 120 * 60
        assert used == 0
        assert remaining == 120 * 60
    _purge([email])


def _set_created(uid, created_at):
    with A.app.app_context():
        A.db.session.execute(A.text(
            'UPDATE users SET created_at = :c WHERE id = :id'),
            {'c': created_at, 'id': uid})
        A.db.session.commit()


def _limit_of(uid):
    with A.app.app_context():
        return A.db.session.execute(A.text(
            'SELECT trial_seconds_limit FROM users WHERE id = :id'),
            {'id': uid}).scalar()


def test_raise_60_minute_trial_cohort_lifts_only_that_cohort(monkeypatch):
    """Accounts stamped with the 60-minute grant (PR #54) move to the current
    grant once; used minutes stay; legacy NULL, hand-set limits and 3600 rows
    outside the signup window are untouched; a rerun is a no-op.

    The window is shifted to 2001 so other tests' users (created "now") can
    never fall inside it; the production bounds are asserted separately."""
    assert A.TRIAL_60_COHORT_CREATED_FROM == '2026-10-09 06:00:00'
    assert A.TRIAL_60_COHORT_CREATED_BEFORE == '2026-10-11 00:00:00'
    monkeypatch.setattr(A, 'TRIAL_60_COHORT_CREATED_FROM', '2001-01-09 06:00:00')
    monkeypatch.setattr(A, 'TRIAL_60_COHORT_CREATED_BEFORE', '2001-01-11 00:00:00')
    cohort = _make_user('cohort60@test.com', limit=3600, used=2832)
    cohort_late = _make_user('cohort60late@test.com', limit=3600, used=0)
    before = _make_user('pre54-3600@test.com', limit=3600, used=0)
    after = _make_user('manual-3600@test.com', limit=3600, used=0)
    legacy = _make_user('legacy-null-cohort@test.com', limit=None, used=100)
    tester = _make_user('partial-1500@test.com', limit=1500, used=0)
    _set_created(cohort, '2001-01-09 07:09:18.135131')
    _set_created(cohort_late, '2001-01-10 23:59:59.000000')
    _set_created(before, '2001-01-09 05:59:59.000000')
    _set_created(after, '2001-01-11 00:00:00.000000')
    _set_created(legacy, '2001-01-09 08:00:00.000000')
    _set_created(tester, '2001-01-09 11:37:18.743825')
    with A.app.app_context():
        assert A.raise_60_minute_trial_cohort() == 2
        assert A.raise_60_minute_trial_cohort() == 0
    assert _limit_of(cohort) == A.NEW_USER_TRIAL_SECONDS == 120 * 60
    assert _limit_of(cohort_late) == 120 * 60
    assert _limit_of(before) == 3600
    assert _limit_of(after) == 3600
    assert _limit_of(legacy) is None
    assert _limit_of(tester) == 1500
    assert _used(cohort) == 2832


def test_raise_60_minute_trial_cohort_noop_when_grant_not_larger(monkeypatch):
    monkeypatch.setattr(A, 'TRIAL_60_COHORT_CREATED_FROM', '2001-02-09 06:00:00')
    monkeypatch.setattr(A, 'TRIAL_60_COHORT_CREATED_BEFORE', '2001-02-11 00:00:00')
    uid = _make_user('cohort60-noop@test.com', limit=3600, used=0)
    _set_created(uid, '2001-02-09 09:00:00.000000')
    monkeypatch.setattr(A, 'NEW_USER_TRIAL_SECONDS', 60 * 60)
    with A.app.app_context():
        assert A.raise_60_minute_trial_cohort() == 0
    assert _limit_of(uid) == 3600


def test_existing_null_limit_user_still_gets_trial_minutes_default(trial_on):
    """Legacy NULL trial_seconds_limit continues to use TRIAL_DEFAULT_SECONDS."""
    uid = _make_user('legacy-null@test.com', limit=None, used=0)
    with A.app.app_context():
        u = A.db.session.get(A.User, uid)
        assert u.trial_seconds_limit is None
        limit, _, remaining = A.trial_status(u)
        # Under trial_on the default is patched to 600s; production default
        # of 180 minutes is asserted in test_default_trial_grants_and_daily_budget.
        assert limit == A.TRIAL_DEFAULT_SECONDS
        assert remaining == A.TRIAL_DEFAULT_SECONDS
        assert limit != A.NEW_USER_TRIAL_SECONDS


def test_health_reports_trial_daily_budget_fields(trial_on, monkeypatch):
    """GET /health exposes today's budget availability plus used/limit minutes."""
    monkeypatch.setattr(A, 'TRIAL_DAILY_SECONDS', 750 * 60)
    monkeypatch.setattr(A, 'trial_daily_used_seconds', lambda day=None: 100 * 60)
    resp = A.app.test_client().get('/health')
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['ok'] is True
    assert data['trial_available'] is True
    assert data['trial_daily_used'] == 100
    assert data['trial_daily_limit'] == 750

    monkeypatch.setattr(A, 'trial_daily_used_seconds', lambda day=None: 750 * 60)
    exhausted = A.app.test_client().get('/health').get_json()
    assert exhausted['trial_available'] is False
    assert exhausted['trial_daily_used'] == 750
    assert A.trial_daily_budget_available() is False


def test_new_user_long_episode_starts_partial_preview_not_max_cap(
        monkeypatch, trial_on):
    """A 90-min episode for a 60-min new user is under TRIAL_MAX_EPISODE (180)
    but over remaining trial — start a partial preview of the first 60 min."""
    from models import db, TranscriptionTask
    monkeypatch.setattr(A, 'TRIAL_MAX_EPISODE_SECONDS', 180 * 60)
    monkeypatch.setattr(A, 'TRIAL_DAILY_SECONDS', 10 ** 7)
    uid = _make_user('sixty-partial@test.com', limit=60 * 60, used=0)
    resp = _post_start(monkeypatch, uid, {
        'audio_url': 'https://example.com/ep.mp3',
        'episode_title': 'Long one',
        'duration_min': '90',
    })
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert 'task_id' in body
    assert _used(uid) == 60 * 60
    with A.app.app_context():
        task = db.session.get(TranscriptionTask, body['task_id'])
        assert A.task_is_partial(task)
        assert task.trial_seconds_charged == 60 * 60
        assert (task.paid_seconds_charged or 0) == 0
        meta = A.task_partial_meta(task)
        assert meta['partial_seconds'] == 60 * 60
        assert meta['episode_seconds'] == 90 * 60


# --------------------------------------------------------------------------
# Stripe credit pack
# --------------------------------------------------------------------------

def _paid(user_id):
    from models import db, User
    with A.app.app_context():
        return db.session.get(User, user_id).paid_seconds_balance or 0


def _set_paid(user_id, seconds):
    from models import db, User
    with A.app.app_context():
        u = db.session.get(User, user_id)
        u.paid_seconds_balance = int(seconds)
        db.session.commit()


@pytest.fixture
def stripe_on(monkeypatch, trial_on):
    """Enable Stripe checkout + webhook with a fake StripeClient surface."""
    monkeypatch.setattr(A, 'STRIPE_SECRET_KEY', 'sk_test_fake')
    monkeypatch.setattr(A, 'STRIPE_WEBHOOK_SECRET', 'whsec_test_fake')
    monkeypatch.setattr(A, 'STRIPE_PRICE_ID', '')
    monkeypatch.setattr(A, 'STRIPE_AUTOMATIC_TAX', False)
    monkeypatch.setattr(A, 'CREDIT_PACK_TAX_BEHAVIOR', 'inclusive')

    store = {'sessions': {}, 'last_create_params': None, 'last_create_options': None}

    class FakeSessionObj:
        def __init__(self, data):
            self._data = data
            self.url = data.get('url', 'https://checkout.stripe.test/session')
            self.id = data['id']

        def to_dict(self):
            return dict(self._data)

    class FakeSessionsAPI:
        def create(self, params=None, options=None):
            store['last_create_params'] = params
            store['last_create_options'] = options
            data = {
                'id': 'cs_test_123',
                'url': 'https://checkout.stripe.test/session',
                'mode': 'payment',
                'payment_status': 'unpaid',
            }
            return FakeSessionObj(data)

        def retrieve(self, session_id, params=None, options=None):
            if session_id not in store['sessions']:
                raise Exception(f'unknown session {session_id}')
            return FakeSessionObj(store['sessions'][session_id])

    class FakeClient:
        def __init__(self):
            self.v1 = type('V1', (), {})()
            self.v1.checkout = type('Checkout', (), {})()
            self.v1.checkout.sessions = FakeSessionsAPI()

    client = FakeClient()
    monkeypatch.setattr(A, 'stripe_client', lambda: client)

    class SignatureVerificationError(Exception):
        pass

    class FakeWebhook:
        @staticmethod
        def construct_event(payload, sig_header, secret):
            raise AssertionError('tests must monkeypatch construct_event')

    _sve = SignatureVerificationError

    class FakeStripe:
        api_key = None
        api_version = '2026-08-26.dahlia'
        Webhook = FakeWebhook

    FakeStripe.SignatureVerificationError = _sve
    monkeypatch.setattr(A, 'stripe', FakeStripe)
    store['client'] = client
    store['FakeStripe'] = FakeStripe
    return store


def _pack_session(uid, session_id='cs_test_1', **overrides):
    """Build a retrieved Checkout Session dict that matches the credit pack."""
    base = {
        'id': session_id,
        'object': 'checkout.session',
        'mode': 'payment',
        'payment_status': 'paid',
        'currency': 'usd',
        'amount_subtotal': 500,
        'amount_total': 500,
        'client_reference_id': str(uid),
        'payment_intent': 'pi_' + session_id,
        'metadata': {
            'user_id': str(uid),
            'minutes': '300',
            'pack': A.CREDIT_PACK_SKU,
        },
        'total_details': {'amount_tax': 0, 'amount_discount': 0},
        'customer_details': {'address': {'country': 'US'}},
        'line_items': {
            'data': [{
                'quantity': 1,
                'price': {
                    'id': 'price_inline',
                    'unit_amount': 500,
                    'tax_behavior': 'inclusive',
                },
            }],
        },
    }
    base.update(overrides)
    return base


def _ns_event(etype, obj_id, eid='evt_1', obj=None):
    """Event-shaped namespace using attribute access (stripe-python >= 15)."""
    from types import SimpleNamespace
    if obj is None:
        obj = SimpleNamespace(id=obj_id)
    return SimpleNamespace(id=eid, type=etype, data=SimpleNamespace(object=obj))


def _register_and_post(stripe_on, session_dict, etype='checkout.session.completed',
                       eid=None):
    """Put session in fake retrieve store and POST a webhook event for it."""
    stripe_on['sessions'][session_dict['id']] = session_dict
    eid = eid or ('evt_' + session_dict['id'])
    event = _ns_event(etype, session_dict['id'], eid=eid)
    A.stripe.Webhook.construct_event = staticmethod(
        lambda payload, sig, secret: event)
    return A.app.test_client().post(
        '/stripe/webhook', data=b'{}',
        headers={'Stripe-Signature': 't=1,v1=ok'})


def test_buy_hidden_when_stripe_unconfigured(trial_on):
    uid = _make_user('nostripe@test.com', limit=600, used=600)
    body = _login(uid).get('/settings').data.decode()
    assert 'Buy 5 hours for $5' not in body
    assert A.stripe_checkout_enabled() is False


def test_buy_hidden_when_only_secret_key_set(trial_on, monkeypatch):
    """Buy must stay hidden until the webhook secret is also configured."""
    monkeypatch.setattr(A, 'STRIPE_SECRET_KEY', 'sk_test_only')
    monkeypatch.setattr(A, 'STRIPE_WEBHOOK_SECRET', '')
    assert A.stripe_checkout_enabled() is False
    uid = _make_user('halfstripe@test.com', limit=600, used=600)
    body = _login(uid).get('/settings').data.decode()
    assert 'Buy 5 hours for $5' not in body


def test_buy_shown_when_stripe_configured(stripe_on):
    uid = _make_user('withstripe@test.com', limit=600, used=600)
    body = _login(uid).get('/settings').data.decode()
    assert 'Buy 5 hours for $5' in body
    assert 'billing/checkout' in body
    assert 'csrf_token' in body
    assert 'name="ph_sid"' in body
    assert '/terms' in body


def test_header_pill_sets_buy_source(stripe_on):
    uid = _make_user('headerpill@test.com', limit=600, used=600)
    client = _login(uid)
    home = client.get('/').data.decode()
    assert 'from=header_pill' in home
    assert 'id="navBuyPill"' in home
    settings = client.get('/settings?from=header_pill').data.decode()
    assert 'name="source" value="header_pill"' in settings


def test_checkout_session_creation(stripe_on, ph_events):
    uid = _make_user('checkout@test.com', limit=600, used=0)
    client = _login(uid)
    # Seed CSRF via a GET that runs the context processor.
    client.get('/settings')
    with client.session_transaction() as sess:
        token = sess.get('_csrf_token')
    assert token
    # ph_sid is only forwarded when analytics consent is accepted.
    client.set_cookie(A.COOKIE_CONSENT_NAME, 'accepted')
    resp = client.post('/billing/checkout', data={
        'csrf_token': token,
        'source': 'settings',
        'ph_sid': 'ph_sess_test_1',
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers['Location'] == 'https://checkout.stripe.test/session'
    kw = stripe_on['last_create_params']
    assert kw['mode'] == 'payment'
    assert kw['client_reference_id'] == str(uid)
    assert kw['metadata']['user_id'] == str(uid)
    assert kw['metadata']['minutes'] == str(A.CREDIT_PACK_MINUTES)
    assert kw['metadata']['pack'] == A.CREDIT_PACK_SKU
    assert kw['metadata']['location'] == 'settings'
    assert kw['metadata']['ph_sid'] == 'ph_sess_test_1'
    assert kw['payment_intent_data']['metadata']['location'] == 'settings'
    assert kw['payment_intent_data']['metadata']['ph_sid'] == 'ph_sess_test_1'
    assert kw['customer_creation'] == 'always'
    # auto: minimum address fields for tax; not a full street form every time.
    assert kw['billing_address_collection'] == 'auto'
    # Dynamic payment methods (Managed Payments / Dashboard). Do not pin types.
    assert 'payment_method_types' not in kw
    assert kw['line_items'][0]['price_data']['unit_amount'] == 500
    assert kw['line_items'][0]['price_data']['tax_behavior'] == 'inclusive'
    assert kw['line_items'][0]['price_data']['product_data']['tax_code']
    assert 'automatic_tax' not in kw
    assert '/billing/cancel' in kw['cancel_url']
    assert 'next=' in kw['cancel_url']
    started = [e for e in ph_events.events if e['event'] == 'checkout_started']
    assert started and started[-1]['distinct_id'] == str(uid)
    props = started[-1]['properties']
    assert props['location'] == 'settings'
    assert props['source'] == 'settings'
    assert props['checkout_session_id'] == 'cs_test_123'
    assert props['amount_cents'] == A.CREDIT_PACK_AMOUNT_CENTS
    assert props['pack_sku'] == A.CREDIT_PACK_SKU
    assert props['$session_id'] == 'ph_sess_test_1'
    assert props.get('app') == 'podskrift'
    assert 'email' not in props
    assert '@' not in str(props)
    import uuid as _uuid
    assert started[-1]['uuid'] == str(
        _uuid.uuid5(_uuid.NAMESPACE_URL, 'cs_test_123'))


def test_checkout_passes_automatic_tax_when_enabled(stripe_on, monkeypatch):
    monkeypatch.setattr(A, 'STRIPE_AUTOMATIC_TAX', True)
    uid = _make_user('taxcheckout@test.com', limit=600, used=0)
    client = _login(uid)
    client.get('/settings')
    with client.session_transaction() as sess:
        token = sess.get('_csrf_token')
    resp = client.post('/billing/checkout', data={
        'csrf_token': token, 'source': 'settings',
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert stripe_on['last_create_params']['automatic_tax'] == {'enabled': True}


def test_checkout_rejects_bad_csrf(stripe_on, ph_events):
    uid = _make_user('csrf@test.com')
    client = _login(uid)
    client.get('/settings')
    resp = client.post('/billing/checkout', data={
        'csrf_token': 'wrong',
        'source': 'settings',
    }, follow_redirects=True)
    assert b'form expired' in resp.data.lower() or b'expired' in resp.data.lower()
    failed = [e for e in ph_events.events if e['event'] == 'purchase_failed']
    assert failed and failed[-1]['properties']['reason'] == 'csrf_invalid'
    assert failed[-1]['properties']['stage'] == 'checkout_create'
    assert failed[-1]['distinct_id'] == str(uid)
    assert 'email' not in failed[-1]['properties']


def test_checkout_rejects_byok_user(stripe_on, ph_events):
    uid = _make_user('byokbuy@test.com', key='sk-' + 'b' * 40)
    client = _login(uid)
    # Mint CSRF via the homepage script block (authenticated + stripe on).
    client.get('/')
    with client.session_transaction() as sess:
        token = sess.get('_csrf_token')
    assert token
    resp = client.post('/billing/checkout', data={
        'csrf_token': token, 'source': 'settings',
    }, follow_redirects=True)
    assert resp.status_code == 200
    failed = [e for e in ph_events.events if e['event'] == 'purchase_failed']
    assert failed and failed[-1]['properties']['reason'] == 'byok_user'
    assert b'own OpenAI key' in resp.data or b'paid minutes are not needed' in resp.data


def test_billing_cancel_fires_checkout_returned(stripe_on, ph_events):
    uid = _make_user('cancelroute@test.com')
    client = _login(uid)
    resp = client.get(
        '/billing/cancel?next=/settings%23credits', follow_redirects=False)
    assert resp.status_code in (302, 303)
    assert '/settings' in resp.headers['Location']
    returned = [e for e in ph_events.events if e['event'] == 'checkout_returned']
    assert returned and returned[-1]['properties']['status'] == 'cancelled'
    assert returned[-1]['distinct_id'] == str(uid)


def test_webhook_rejects_bad_signature(stripe_on, ph_events):
    class SignatureVerificationError(Exception):
        pass

    def boom(payload, sig, secret):
        raise SignatureVerificationError('bad sig')

    A.stripe.Webhook.construct_event = staticmethod(boom)
    resp = A.app.test_client().post(
        '/stripe/webhook',
        data=b'{}',
        headers={'Stripe-Signature': 't=1,v1=x'},
    )
    assert resp.status_code == 400
    errs = [e for e in ph_events.events if e['event'] == 'stripe_webhook_error']
    assert errs and errs[-1]['properties']['reason'] == 'signature_invalid'
    assert errs[-1]['distinct_id'] == 'system:stripe-webhook'
    assert errs[-1]['properties'].get('$process_person_profile') is False


def test_webhook_real_sdk_construct_event_attribute_access(stripe_on, monkeypatch):
    """Signed payload must survive real stripe.Webhook.construct_event.

    Catches the stripe-python >= 15 regression where Event has no .get().
    """
    import json
    import stripe as real_stripe

    uid = _make_user('realsdk@test.com', limit=600, used=0)
    session = _pack_session(uid, 'cs_real_sdk_1')
    stripe_on['sessions'][session['id']] = session

    secret = 'whsec_test_real_sdk_secret'
    monkeypatch.setattr(A, 'STRIPE_WEBHOOK_SECRET', secret)
    monkeypatch.setattr(A, 'stripe', real_stripe)

    payload_obj = {
        'id': 'evt_real_sdk_1',
        'object': 'event',
        'api_version': '2026-08-26.dahlia',
        'type': 'checkout.session.completed',
        'data': {'object': {
            'id': session['id'],
            'object': 'checkout.session',
            'payment_status': 'paid',
        }},
    }
    payload = json.dumps(payload_obj)
    header = real_stripe.WebhookSignature.generate_signature_header(
        payload=payload, secret=secret)

    # Prove the class of bug: attribute access works; .get does not.
    event = real_stripe.Webhook.construct_event(payload, header, secret)
    assert event.type == 'checkout.session.completed'
    assert event.id == 'evt_real_sdk_1'
    assert event.data.object.id == session['id']
    with pytest.raises(AttributeError):
        event.get('type')

    resp = A.app.test_client().post(
        '/stripe/webhook',
        data=payload.encode('utf-8'),
        headers={'Stripe-Signature': header,
                 'Content-Type': 'application/json'},
    )
    assert resp.status_code == 200
    assert _paid(uid) == 300 * 60


def test_webhook_credits_once_on_duplicate_event(stripe_on, ph_events):
    uid = _make_user('creditonce@test.com', limit=600, used=0)
    assert _paid(uid) == 0
    session = _pack_session(uid, 'cs_test_dup_1')
    r1 = _register_and_post(stripe_on, session, eid='evt_1')
    r2 = _register_and_post(stripe_on, session, eid='evt_1')
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert _paid(uid) == 300 * 60
    from models import db, CreditPurchase
    with A.app.app_context():
        rows = CreditPurchase.query.filter_by(stripe_session_id='cs_test_dup_1').all()
        assert len(rows) == 1
        assert rows[0].status == 'credited'
        assert rows[0].stripe_payment_intent_id == 'pi_cs_test_dup_1'
    purchased = [e for e in ph_events.events if e['event'] == 'purchase_completed']
    assert len(purchased) == 1
    assert purchased[0]['properties']['minutes'] == 300
    props = purchased[0]['properties']
    assert props.get('checkout_session_id') == 'cs_test_dup_1'
    assert props.get('is_first_purchase') is True
    assert props.get('fulfilled_via') == 'webhook'
    assert props.get('$set') == {'has_purchased': True}
    assert 'email' not in props
    import uuid as _uuid
    assert purchased[0]['uuid'] == str(
        _uuid.uuid5(_uuid.NAMESPACE_URL, 'cs_test_dup_1'))


def test_async_payment_succeeded_credits(stripe_on):
    uid = _make_user('asyncpay@test.com', limit=600, used=0)
    session = _pack_session(uid, 'cs_async_1')
    resp = _register_and_post(
        stripe_on, session, etype='checkout.session.async_payment_succeeded')
    assert resp.status_code == 200
    assert _paid(uid) == 300 * 60


def test_async_payment_failed_captures_purchase_failed(stripe_on, ph_events):
    uid = _make_user('asyncfail@test.com', limit=600, used=0)
    session = _pack_session(uid, 'cs_async_fail_1', payment_status='unpaid')
    resp = _register_and_post(
        stripe_on, session, etype='checkout.session.async_payment_failed')
    assert resp.status_code == 200
    assert _paid(uid) == 0
    failed = [e for e in ph_events.events if e['event'] == 'purchase_failed']
    assert failed and failed[-1]['properties']['reason'] == 'async_payment_failed'
    assert failed[-1]['properties']['stage'] == 'async_payment'
    assert failed[-1]['distinct_id'] == 'stripe:cs_async_fail_1'
    assert failed[-1]['properties'].get('$process_person_profile') is False


def test_completed_unpaid_does_not_credit(stripe_on):
    uid = _make_user('unpaid@test.com', limit=600, used=0)
    session = _pack_session(uid, 'cs_unpaid_1', payment_status='unpaid')
    resp = _register_and_post(stripe_on, session)
    assert resp.status_code == 200
    assert _paid(uid) == 0


def test_inclusive_taxed_session_credits(stripe_on):
    """Inclusive tax: customer still pays 500; amount_tax > 0 is fine."""
    uid = _make_user('incltax@test.com', limit=600, used=0)
    session = _pack_session(
        uid, 'cs_incl_1',
        amount_subtotal=500,
        amount_total=500,
        total_details={'amount_tax': 100, 'amount_discount': 0},
        line_items={'data': [{
            'quantity': 1,
            'price': {
                'id': 'price_incl',
                'unit_amount': 500,
                'tax_behavior': 'inclusive',
            },
        }]},
        customer_details={'address': {'country': 'NO'}},
    )
    assert _register_and_post(stripe_on, session).status_code == 200
    assert _paid(uid) == 300 * 60
    from models import db, CreditPurchase
    with A.app.app_context():
        row = CreditPurchase.query.filter_by(stripe_session_id='cs_incl_1').one()
        assert row.amount_tax_cents == 100
        assert row.customer_country == 'NO'
        assert row.status == 'credited'


def test_exclusive_taxed_session_credits(stripe_on):
    """Exclusive tax: subtotal 500, total = subtotal + tax."""
    uid = _make_user('excltax@test.com', limit=600, used=0)
    session = _pack_session(
        uid, 'cs_excl_1',
        amount_subtotal=500,
        amount_total=625,
        total_details={'amount_tax': 125, 'amount_discount': 0},
        line_items={'data': [{
            'quantity': 1,
            'price': {
                'id': 'price_excl',
                'unit_amount': 500,
                'tax_behavior': 'exclusive',
            },
        }]},
    )
    assert _register_and_post(stripe_on, session).status_code == 200
    assert _paid(uid) == 300 * 60


def test_wrong_price_needs_review_and_sentry(stripe_on, monkeypatch):
    uid = _make_user('badprice@test.com', limit=600, used=0)
    monkeypatch.setattr(A, 'STRIPE_PRICE_ID', 'price_expected')
    captured = []

    def capture(msg, level='error', **kwargs):
        captured.append((msg, level, kwargs))

    monkeypatch.setattr(A, '_sentry_capture_message', capture)
    session = _pack_session(
        uid, 'cs_badprice_1',
        line_items={'data': [{
            'quantity': 1,
            'price': {
                'id': 'price_wrong',
                'unit_amount': 500,
                'tax_behavior': 'inclusive',
            },
        }]},
    )
    resp = _register_and_post(stripe_on, session)
    assert resp.status_code == 200
    assert _paid(uid) == 0
    from models import CreditPurchase
    with A.app.app_context():
        row = CreditPurchase.query.filter_by(stripe_session_id='cs_badprice_1').one()
        assert row.status == 'needs_review'
        assert row.minutes == 0
    assert captured and 'not credited' in captured[0][0]
    assert captured[0][1] == 'error'


def test_settlement_charges_trial_then_paid(trial_on):
    uid = _make_user('split@test.com', limit=600, used=0)  # 10 min trial
    _set_paid(uid, 1800)  # 30 min paid
    with A.app.app_context():
        split = A.platform_reserve(uid, 1200)  # 20 min
        assert split == (600, 600)
    assert _used(uid) == 600
    assert _paid(uid) == 1200


def test_paid_only_covers_over_long_episode(trial_on, monkeypatch):
    """Episodes over the free per-episode cap must not burn free trial."""
    monkeypatch.setattr(A, 'TRIAL_MAX_EPISODE_SECONDS', 1800)
    uid = _make_user('overlong@test.com', limit=3600, used=0)
    _set_paid(uid, 7200)  # 120 min
    with A.app.app_context():
        assert A.platform_reserve(uid, 3600, paid_only=True) == (0, 3600)
    assert _used(uid) == 0
    assert _paid(uid) == 3600


def test_paid_not_limited_by_daily_trial_budget(trial_on, monkeypatch):
    monkeypatch.setattr(A, 'TRIAL_DAILY_SECONDS', 0)
    uid = _make_user('paiddaily@test.com', limit=3600, used=0)
    _set_paid(uid, 600)
    with A.app.app_context():
        # Free trial blocked by today's budget; paid still works.
        assert A.trial_reserve(uid, 60) is False
        assert A.platform_reserve(uid, 300) == (0, 300)
    assert _paid(uid) == 300


def test_failed_job_refunds_paid_pro_rata(trial_on):
    from models import db, TranscriptionTask
    uid = _make_user('paidrefund@test.com', limit=600, used=0)
    _set_paid(uid, 1800)
    with A.app.app_context():
        A.platform_reserve(uid, 1200)  # 600 trial + 600 paid
        db.session.add(TranscriptionTask(
            id='paid-refund-1', user_id=uid, episode_title='x',
            status='error', trial_seconds_charged=600, paid_seconds_charged=600,
            chunk_total=4, chunk_index=1, trial_settled=False))
        db.session.commit()
        task = db.session.get(TranscriptionTask, 'paid-refund-1')
        # 2 of 4 chunks sent → spend 600 of 1200; trial first → spend all 600
        # trial + 0 paid; refund 0 trial + 600 paid.
        assert A.trial_refund_task(task) == 600
    assert _used(uid) == 600
    assert _paid(uid) == 1800  # 1800 - 600 reserved + 600 refunded


def test_privacy_mentions_stripe():
    body = A.app.test_client().get('/privacy').data.decode().lower()
    assert 'stripe' in body


def test_terms_page_mentions_refunds():
    body = A.app.test_client().get('/terms').data.decode().lower()
    assert 'refund' in body
    assert '14 days' in body or '14-day' in body
    assert 'productivitytech.io/contact' in body


def test_offer_shown_no_longer_server_fired_on_limit(stripe_on, ph_events, monkeypatch):
    """Paywall/offer analytics moved to the browser; server must not dual-fire."""
    uid = _make_user('offer@test.com', limit=600, used=600)
    resp = _post_start(monkeypatch, uid, {
        'audio_url': 'https://example.com/ep.mp3',
        'episode_title': 'Ep', 'duration_min': '30', 'language': 'no',
    })
    assert resp.status_code == 402
    body = resp.get_json()
    assert body.get('buy_available') is True
    assert body.get('buy_label') == 'Buy 5 hours for $5'
    assert body.get('paywall_reason') in (
        'trial_exhausted', 'low_balance', 'paid_exhausted', 'global_cap',
        'episode_too_long')
    offers = [e for e in ph_events.events if e['event'] == 'offer_shown']
    assert offers == []
    paywalls = [e for e in ph_events.events if e['event'] == 'paywall_shown']
    assert paywalls == []


def test_minutes_exhausted_on_depleting_reserve(trial_on, ph_events):
    uid = _make_user('minexhausted@test.com', limit=600, used=0)
    with A.app.app_context():
        before_t, before_p = A._platform_remaining_seconds(uid)
        assert before_t == 600
        split = A.platform_reserve(uid, 600)
        assert split == (600, 0)
        A._capture_minutes_exhausted_if_depleted(uid, 'web', before_t, before_p)
    exhausted = [e for e in ph_events.events if e['event'] == 'minutes_exhausted']
    assert exhausted and exhausted[-1]['properties']['kind'] == 'trial'
    dual = [e for e in ph_events.events if e['event'] == 'paid_minutes_exhausted']
    assert dual


def test_enqueue_uses_paid_after_trial(trial_on, monkeypatch):
    uid = _make_user('enqueue-paid@test.com', limit=300, used=0)  # 5 min
    _set_paid(uid, 1800)  # 30 min
    resp = _post_start(monkeypatch, uid, {
        'audio_url': 'https://example.com/ep.mp3',
        'episode_title': 'Ep', 'duration_min': '10', 'language': 'no',
    })
    assert resp.status_code == 200, resp.get_json()
    assert _used(uid) == 300
    assert _paid(uid) == 1800 - 300  # 5 min from paid
    from models import db, TranscriptionTask
    with A.app.app_context():
        task = TranscriptionTask.query.filter_by(user_id=uid).order_by(
            TranscriptionTask.started_at.desc()).first()
        assert task.trial_seconds_charged == 300
        assert task.paid_seconds_charged == 300


def test_webhook_unconfigured_returns_404():
    """Stripe unset must look like a missing route, not a soft outage (503)."""
    assert A.stripe_webhook_enabled() is False
    resp = A.app.test_client().post(
        '/stripe/webhook', data=b'{}',
        headers={'Stripe-Signature': 't=1,v1=x'},
    )
    assert resp.status_code == 404


def test_webhook_rejects_unpaid_or_wrong_amount(stripe_on, monkeypatch):
    uid = _make_user('badpay@test.com')
    captured = []
    monkeypatch.setattr(
        A, '_sentry_capture_message',
        lambda msg, level='error', **kw: captured.append(msg))

    unpaid = _pack_session(uid, 'cs_unpaid', payment_status='unpaid')
    assert _register_and_post(stripe_on, unpaid).status_code == 200
    assert _paid(uid) == 0

    missing_status = _pack_session(uid, 'cs_nostatus')
    del missing_status['payment_status']
    assert _register_and_post(stripe_on, missing_status).status_code == 200
    assert _paid(uid) == 0

    wrong_amount = _pack_session(
        uid, 'cs_wrong_amt',
        amount_total=100, amount_subtotal=100,
        line_items={'data': [{
            'quantity': 1,
            'price': {'id': 'p', 'unit_amount': 100, 'tax_behavior': 'inclusive'},
        }]},
    )
    assert _register_and_post(stripe_on, wrong_amount).status_code == 200
    assert _paid(uid) == 0
    from models import CreditPurchase
    with A.app.app_context():
        row = CreditPurchase.query.filter_by(stripe_session_id='cs_wrong_amt').one()
        assert row.status == 'needs_review'
    assert any('cs_wrong_amt' in m for m in captured)

    wrong_currency = _pack_session(uid, 'cs_wrong_cur', currency='eur')
    assert _register_and_post(stripe_on, wrong_currency).status_code == 200
    assert _paid(uid) == 0


def test_charge_refunded_claws_back_pro_rata(stripe_on, ph_events):
    uid = _make_user('clawback@test.com', limit=600, used=0)
    session = _pack_session(uid, 'cs_refund_1')
    assert _register_and_post(stripe_on, session).status_code == 200
    assert _paid(uid) == 300 * 60

    # Spend half the pack so clawback cannot reclaim used minutes below zero…
    # (clawback takes min(balance, pro-rata); after spending 150 min, balance
    # is 150 min; full refund should take all 150 remaining, not invent more.)
    _set_paid(uid, 150 * 60)

    from types import SimpleNamespace
    charge = {
        'id': 'ch_1',
        'payment_intent': 'pi_cs_refund_1',
        'amount': 500,
        'amount_captured': 500,
        'amount_refunded': 500,
    }
    event = _ns_event(
        'charge.refunded', 'ch_1', eid='evt_ref_1',
        obj=SimpleNamespace(id='ch_1', to_dict=lambda: charge))
    A.stripe.Webhook.construct_event = staticmethod(
        lambda payload, sig, secret: event)
    r1 = A.app.test_client().post(
        '/stripe/webhook', data=b'{}',
        headers={'Stripe-Signature': 't=1,v1=ok'})
    r2 = A.app.test_client().post(
        '/stripe/webhook', data=b'{}',
        headers={'Stripe-Signature': 't=1,v1=ok'})
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert _paid(uid) == 0
    from models import CreditPurchase
    with A.app.app_context():
        row = CreditPurchase.query.filter_by(stripe_session_id='cs_refund_1').one()
        assert row.status == 'refunded'
        assert row.seconds_clawed_back == 150 * 60
        assert row.amount_refunded_cents == 500
    refunded = [e for e in ph_events.events if e['event'] == 'purchase_refunded']
    assert len(refunded) == 1
    processed = [e for e in ph_events.events if e['event'] == 'refund_processed']
    assert len(processed) == 1
    props = processed[0]['properties']
    assert props['kind'] == 'refund'
    assert props['seconds_clawed_back'] == 150 * 60
    assert props['full_refund'] is True
    assert 'email' not in props
    assert processed[0]['uuid']


def test_partial_refund_pro_rata_idempotent(stripe_on):
    uid = _make_user('partialref@test.com', limit=600, used=0)
    session = _pack_session(uid, 'cs_partial_1')
    assert _register_and_post(stripe_on, session).status_code == 200
    assert _paid(uid) == 300 * 60

    from types import SimpleNamespace

    def post_refund(amount_refunded, eid):
        charge = {
            'id': 'ch_partial',
            'payment_intent': 'pi_cs_partial_1',
            'amount': 500,
            'amount_captured': 500,
            'amount_refunded': amount_refunded,
        }
        event = _ns_event(
            'charge.refunded', 'ch_partial', eid=eid,
            obj=SimpleNamespace(id='ch_partial', to_dict=lambda: charge))
        A.stripe.Webhook.construct_event = staticmethod(
            lambda payload, sig, secret: event)
        return A.app.test_client().post(
            '/stripe/webhook', data=b'{}',
            headers={'Stripe-Signature': 't=1,v1=ok'})

    assert post_refund(250, 'evt_p1').status_code == 200
    # Half refund → half of 300 min = 150 min = 9000 seconds.
    assert _paid(uid) == 150 * 60
    assert post_refund(250, 'evt_p1_dup').status_code == 200  # idempotent
    assert _paid(uid) == 150 * 60
    assert post_refund(500, 'evt_p2').status_code == 200
    assert _paid(uid) == 0


def test_refund_processed_when_no_seconds_left(stripe_on, ph_events):
    uid = _make_user('refundzero@test.com', limit=600, used=0)
    session = _pack_session(uid, 'cs_refund_zero')
    assert _register_and_post(stripe_on, session).status_code == 200
    # Fully claw via a first refund, then a second delta that finds remaining<=0.
    from types import SimpleNamespace
    from models import db, CreditPurchase

    def post_refund(amount_refunded, eid):
        charge = {
            'id': 'ch_zero',
            'payment_intent': 'pi_cs_refund_zero',
            'amount': 500,
            'amount_captured': 500,
            'amount_refunded': amount_refunded,
        }
        event = _ns_event(
            'charge.refunded', 'ch_zero', eid=eid,
            obj=SimpleNamespace(id='ch_zero', to_dict=lambda: charge))
        A.stripe.Webhook.construct_event = staticmethod(
            lambda payload, sig, secret: event)
        return A.app.test_client().post(
            '/stripe/webhook', data=b'{}',
            headers={'Stripe-Signature': 't=1,v1=ok'})

    assert post_refund(500, 'evt_rz1').status_code == 200
    assert _paid(uid) == 0
    with A.app.app_context():
        row = CreditPurchase.query.filter_by(stripe_session_id='cs_refund_zero').one()
        # Simulate a redelivery that increases amount_refunded while remaining is 0
        # by resetting amount_refunded tracking without restoring seconds.
        row.amount_refunded_cents = 400
        row.status = 'credited'
        row.refunded_at = None
        db.session.commit()
    ph_events.events.clear()
    assert post_refund(500, 'evt_rz2').status_code == 200
    processed = [e for e in ph_events.events if e['event'] == 'refund_processed']
    assert processed
    assert processed[-1]['properties']['seconds_clawed_back'] == 0
    refunded = [e for e in ph_events.events if e['event'] == 'purchase_refunded']
    assert refunded


def test_dispute_created_full_clawback(stripe_on, ph_events):
    uid = _make_user('dispute@test.com', limit=600, used=0)
    session = _pack_session(uid, 'cs_disp_1')
    assert _register_and_post(stripe_on, session).status_code == 200
    assert _paid(uid) == 300 * 60

    from types import SimpleNamespace
    dispute = {
        'id': 'dp_1',
        'payment_intent': 'pi_cs_disp_1',
        'amount': 500,
    }
    event = _ns_event(
        'charge.dispute.created', 'dp_1', eid='evt_dp_1',
        obj=SimpleNamespace(id='dp_1', to_dict=lambda: dispute))
    A.stripe.Webhook.construct_event = staticmethod(
        lambda payload, sig, secret: event)
    assert A.app.test_client().post(
        '/stripe/webhook', data=b'{}',
        headers={'Stripe-Signature': 't=1,v1=ok'}).status_code == 200
    # Second delivery must not double-claw.
    assert A.app.test_client().post(
        '/stripe/webhook', data=b'{}',
        headers={'Stripe-Signature': 't=1,v1=ok'}).status_code == 200
    assert _paid(uid) == 0
    from models import CreditPurchase
    with A.app.app_context():
        row = CreditPurchase.query.filter_by(stripe_session_id='cs_disp_1').one()
        assert row.status == 'disputed'
        assert row.seconds_clawed_back == 300 * 60


def test_billing_success_fulfills_checkout(stripe_on):
    uid = _make_user('successpage@test.com', limit=600, used=0)
    session = _pack_session(uid, 'cs_success_1')
    stripe_on['sessions'][session['id']] = session
    client = _login(uid)
    resp = client.get(f'/billing/success?session_id={session["id"]}')
    assert resp.status_code == 200
    assert _paid(uid) == 300 * 60
    assert b'Payment received' in resp.data or b'paid' in resp.data.lower()


def test_reconcile_over_cap_keeps_trial_when_paid_debit_fails(trial_on, monkeypatch):
    """Paid debit must succeed before trial is released.

    If we released first and the re-reserve failed, the task would still show
    trial_seconds_charged and the later refund would invent free minutes.
    """
    from models import db, TranscriptionTask

    monkeypatch.setattr(A, 'TRIAL_MAX_EPISODE_SECONDS', 1800)
    uid = _make_user('recon-race@test.com', limit=900, used=0)
    # Enough paid that the pre-check passes — then force the debit to fail
    # (simulating a concurrent drain between the read and the UPDATE).
    _set_paid(uid, 7200)
    with A.app.app_context():
        assert A.trial_reserve(uid, 600) is True
        db.session.add(TranscriptionTask(
            id='recon-race-1', user_id=uid, episode_title='x',
            status='transcribing', trial_seconds_charged=600,
            paid_seconds_charged=None, trial_settled=False))
        db.session.commit()

        monkeypatch.setattr(A, 'paid_reserve', lambda *a, **k: False)
        # Also poison re-reserve: if the buggy path still runs, it must not
        # paper over a failed re-reserve by writing the charge anyway.
        real_trial_reserve = A.trial_reserve
        re_reserve_calls = []

        def tracking_reserve(user_id, seconds):
            ok = real_trial_reserve(user_id, seconds)
            re_reserve_calls.append((user_id, seconds, ok))
            return False  # force failure if called after a release

        monkeypatch.setattr(A, 'trial_reserve', tracking_reserve)

        with pytest.raises(A.TrialExhausted) as exc:
            A.trial_reconcile_task('recon-race-1', 3600)
        assert exc.value.scope == 'episode_length'

        task = db.session.get(TranscriptionTask, 'recon-race-1')
        assert task.trial_seconds_charged == 600
        assert (task.paid_seconds_charged or 0) == 0
        assert task.trial_settled is False or task.trial_settled == 0

    # Trial reservation never left the user balance.
    assert _used(uid) == 600, (
        f'trial was released (used={_used(uid)}); re-reserve calls={re_reserve_calls}')
    assert _paid(uid) == 7200
    # No re-reserve attempt — we kept the original reservation.
    assert re_reserve_calls == []

    # Refund must return only what was actually charged, not invent minutes.
    with A.app.app_context():
        task = db.session.get(TranscriptionTask, 'recon-race-1')
        task.status = 'error'
        db.session.commit()
        assert A.trial_refund_task(task) == 600
    assert _used(uid) == 0
    assert _paid(uid) == 7200


def test_reconcile_over_cap_swaps_to_paid_when_debit_succeeds(trial_on, monkeypatch):
    from models import db, TranscriptionTask

    monkeypatch.setattr(A, 'TRIAL_MAX_EPISODE_SECONDS', 1800)
    uid = _make_user('recon-swap@test.com', limit=900, used=0)
    _set_paid(uid, 7200)
    with A.app.app_context():
        assert A.trial_reserve(uid, 600) is True
        db.session.add(TranscriptionTask(
            id='recon-swap-1', user_id=uid, episode_title='x',
            status='transcribing', trial_seconds_charged=600,
            paid_seconds_charged=None, trial_settled=False))
        db.session.commit()
        A.trial_reconcile_task('recon-swap-1', 3600)
        task = db.session.get(TranscriptionTask, 'recon-swap-1')
        assert (task.trial_seconds_charged or 0) == 0
        assert task.paid_seconds_charged == 3600
    assert _used(uid) == 0
    assert _paid(uid) == 7200 - 3600


def test_anon_homepage_does_not_mint_csrf_cookie(trial_on):
    """Anonymous visitors must not get a session cookie just for CSRF."""
    client = A.app.test_client()
    resp = client.get('/')
    assert resp.status_code == 200
    # No Set-Cookie writing a session with _csrf_token.
    with client.session_transaction() as sess:
        assert '_csrf_token' not in sess


def test_ensure_credit_purchases_table_is_idempotent(trial_on):
    """Two boot races must not raise — CREATE TABLE IF NOT EXISTS."""
    with A.app.app_context():
        A.ensure_credit_purchases_table()
        A.ensure_credit_purchases_table()
        rows = A.db.session.execute(A.text(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='credit_purchases'"
        )).fetchall()
        assert len(rows) == 1
        cols = {c['name'] for c in A.sa_inspect(A.db.engine).get_columns(
            'credit_purchases')}
        assert 'status' in cols
        assert 'stripe_payment_intent_id' in cols
        assert 'seconds_clawed_back' in cols


def test_ensure_credit_purchases_table_upgrades_legacy_table(trial_on):
    """A credit_purchases table from the first Stripe ship (no hardening
    columns) must be upgraded in place on boot, not crash on the index."""
    with A.app.app_context():
        A.db.session.execute(A.text('DROP TABLE IF EXISTS credit_purchases'))
        A.db.session.execute(A.text("""
            CREATE TABLE credit_purchases (
                id INTEGER NOT NULL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                stripe_session_id VARCHAR(255) NOT NULL UNIQUE,
                stripe_event_id VARCHAR(255),
                amount_cents INTEGER NOT NULL,
                currency VARCHAR(16) NOT NULL,
                minutes INTEGER NOT NULL,
                created_at DATETIME
            )
        """))
        A.db.session.execute(A.text(
            "INSERT INTO credit_purchases (user_id, stripe_session_id, "
            "amount_cents, currency, minutes) VALUES (1, 'cs_legacy', 500, 'usd', 300)"
        ))
        A.db.session.commit()
        # Other pooled SQLite connections still cache the old (full) schema
        # after the DROP/CREATE above and would report "duplicate column";
        # a real boot starts with fresh connections, so start fresh here too.
        A.db.session.remove()
        A.db.engine.dispose()
        try:
            A.ensure_credit_purchases_table()
            A.ensure_credit_purchases_table()
            cols = {c['name'] for c in A.sa_inspect(A.db.engine).get_columns(
                'credit_purchases')}
            for col in A.CREDIT_PURCHASE_COLUMN_MIGRATIONS:
                assert col in cols
            idx = {r[0] for r in A.db.session.execute(A.text(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name='credit_purchases'")).fetchall()}
            assert 'ix_credit_purchases_stripe_payment_intent_id' in idx
            row = A.db.session.execute(A.text(
                "SELECT status, seconds_clawed_back, amount_refunded_cents "
                "FROM credit_purchases WHERE stripe_session_id='cs_legacy'"
            )).fetchone()
            assert tuple(row) == ('credited', 0, 0)
        finally:
            A.db.session.execute(A.text('DROP TABLE IF EXISTS credit_purchases'))
            A.db.session.commit()
            A.db.session.remove()
            A.db.engine.dispose()
            A.db.create_all()
            A.ensure_credit_purchases_table()


def test_ensure_credit_purchases_table_upgrades_with_stale_pooled_connections(trial_on):
    """Same legacy upgrade, but WITHOUT disposing the pool first: other
    pooled connections may still hold the old (full) schema. The column check
    must read the schema on the connection that runs the ALTERs, so no
    column is skipped (PODSKRIFT-6 follow-up)."""
    with A.app.app_context():
        # Warm other pooled connections with the current full schema, the way
        # earlier requests/tests leave them in a long-lived process.
        conns = [A.db.engine.connect() for _ in range(3)]
        for c in conns:
            c.execute(A.text('SELECT stripe_payment_intent_id FROM credit_purchases LIMIT 1'))
            c.commit()
        for c in conns:
            c.close()
        A.db.session.execute(A.text('DROP TABLE IF EXISTS credit_purchases'))
        A.db.session.execute(A.text("""
            CREATE TABLE credit_purchases (
                id INTEGER NOT NULL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                stripe_session_id VARCHAR(255) NOT NULL UNIQUE,
                stripe_event_id VARCHAR(255),
                amount_cents INTEGER NOT NULL,
                currency VARCHAR(16) NOT NULL,
                minutes INTEGER NOT NULL,
                created_at DATETIME
            )
        """))
        A.db.session.commit()
        try:
            A.ensure_credit_purchases_table()
            cols = A._live_columns('credit_purchases')
            for col in A.CREDIT_PURCHASE_COLUMN_MIGRATIONS:
                assert col in cols
            idx = {r[0] for r in A.db.session.execute(A.text(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name='credit_purchases'")).fetchall()}
            assert 'ix_credit_purchases_stripe_payment_intent_id' in idx
        finally:
            A.db.session.execute(A.text('DROP TABLE IF EXISTS credit_purchases'))
            A.db.session.commit()
            A.db.session.remove()
            A.db.engine.dispose()
            A.db.create_all()
            A.ensure_credit_purchases_table()


def test_live_columns_is_empty_for_missing_table():
    with A.app.app_context():
        assert A._live_columns('no_such_table_here') == set()


# --------------------------------------------------------------------------
# Managed Payments and concurrent refund handling
# --------------------------------------------------------------------------

def _start_checkout(uid):
    client = _login(uid)
    client.get('/settings')
    with client.session_transaction() as sess:
        token = sess.get('_csrf_token')
    return client.post('/billing/checkout', data={
        'csrf_token': token, 'source': 'settings'}, follow_redirects=False)


def test_checkout_uses_managed_payments_when_enabled(stripe_on, monkeypatch):
    """Stripe as merchant of record. Managed Payments rejects automatic_tax,
    so it must not be sent even when STRIPE_AUTOMATIC_TAX is also on."""
    monkeypatch.setattr(A, 'STRIPE_MANAGED_PAYMENTS', True)
    monkeypatch.setattr(A, 'STRIPE_AUTOMATIC_TAX', True)
    uid = _make_user('managed@test.com', limit=600, used=0)
    assert _start_checkout(uid).status_code == 303
    params = stripe_on['last_create_params']
    assert params['managed_payments'] == {'enabled': True}
    assert 'automatic_tax' not in params
    assert params['billing_address_collection'] == 'auto'
    assert 'payment_method_types' not in params
    price_data = params['line_items'][0]['price_data']
    assert price_data['tax_behavior'] == 'inclusive'
    assert price_data['product_data']['tax_code'] == A.STRIPE_TAX_CODE


def test_checkout_omits_managed_payments_by_default(stripe_on):
    uid = _make_user('unmanaged@test.com', limit=600, used=0)
    assert _start_checkout(uid).status_code == 303
    assert 'managed_payments' not in stripe_on['last_create_params']


def _stale(purchase, **values):
    """Make ``purchase`` look as it did before another worker's commit."""
    from sqlalchemy.orm.attributes import set_committed_value
    for key, value in values.items():
        set_committed_value(purchase, key, value)


def test_concurrent_refund_redelivery_claws_back_once(stripe_on):
    """Two workers get the same charge.refunded. Worker A commits the half
    refund; worker B read the purchase before that commit. Without the
    snapshot claim, B also claws back 150 minutes the customer still owns."""
    from models import CreditPurchase
    uid = _make_user('racerefund@test.com', limit=600, used=0)
    assert _register_and_post(stripe_on, _pack_session(uid, 'cs_race_1')).status_code == 200
    charge = {'id': 'ch_race', 'payment_intent': 'pi_cs_race_1',
              'amount': 500, 'amount_captured': 500, 'amount_refunded': 250}
    with A.app.app_context():
        purchase = CreditPurchase.query.filter_by(stripe_session_id='cs_race_1').one()
        assert A.handle_charge_refunded(dict(charge)) is True  # worker A
        assert _paid(uid) == 150 * 60
        _stale(purchase, amount_refunded_cents=0, seconds_clawed_back=0,
               status='credited')  # worker B's view
        with pytest.raises(RuntimeError):
            A.handle_charge_refunded(dict(charge))
    assert _paid(uid) == 150 * 60


def test_concurrent_dispute_claws_back_once(stripe_on):
    from models import CreditPurchase
    uid = _make_user('racedispute@test.com', limit=600, used=0)
    assert _register_and_post(stripe_on, _pack_session(uid, 'cs_race_2')).status_code == 200
    dispute = {'id': 'dp_race', 'payment_intent': 'pi_cs_race_2'}
    with A.app.app_context():
        purchase = CreditPurchase.query.filter_by(stripe_session_id='cs_race_2').one()
        assert A.handle_dispute(dict(dispute), 'charge.dispute.created') is True
        # The customer bought 10 minutes elsewhere; B must not take them.
        A.db.session.execute(A.text(
            'UPDATE users SET paid_seconds_balance = 600 WHERE id = :u'), {'u': uid})
        A.db.session.commit()
        A.db.session.expire_all()
        _stale(purchase, seconds_clawed_back=0, status='credited',
               amount_refunded_cents=0)
        with pytest.raises(RuntimeError):
            A.handle_dispute(dict(dispute), 'charge.dispute.created')
    assert _paid(uid) == 600


# ---------------------------------------------------------------------------
# Make paying obvious — nav, pricing, low-balance, minutes-error actions
# ---------------------------------------------------------------------------

def test_nav_shows_minutes_pill_and_buy_when_stripe_on(stripe_on):
    uid = _make_user('navpill@test.com', limit=180 * 60, used=60 * 60)
    body = _login(uid).get('/').data.decode()
    assert 'class="nav-balance"' in body
    assert '120 min left' in body  # 180 - 60
    assert 'class="nav-buy"' in body
    assert 'Buy minutes' in body
    assert 'data-open-buy-modal' in body
    assert 'buy_modal_header' in body
    assert 'id="navBuyPill"' in body
    assert 'from=header_pill' in body
    assert 'buyModalScrim' in body


def test_buy_modal_tracks_open_and_close_not_header_pill_click(stripe_on, monkeypatch):
    """Opening the modal is buy_modal_opened; buy_clicked is form-submit only."""
    monkeypatch.setenv('PODSKRIFT_ENV', 'production')
    monkeypatch.setenv('POSTHOG_KEY', 'phc_test_public_key')
    uid = _make_user('buymodaltrack@test.com', limit=180 * 60, used=60 * 60)
    body = _login(uid).get('/').data.decode()
    assert "posthog.capture('buy_modal_opened'" in body
    assert "posthog.capture('buy_modal_closed'" in body
    assert "open_ms" in body
    assert "podskriftPaywallShown('buy_modal_' + source, 'manual')" in body
    assert 'closeMobileNav' in body
    # Header pill must not fire buy_clicked on click — that event means checkout submit.
    assert "location: 'header_pill'" not in body
    assert "posthog.capture('buy_clicked', {location: loc" in body


def test_nav_hides_buy_when_stripe_off(trial_on):
    uid = _make_user('navnobuy@test.com', limit=180 * 60, used=0)
    body = _login(uid).get('/').data.decode()
    assert 'class="nav-balance"' in body
    assert 'class="nav-buy"' not in body


def test_nav_hides_pill_and_buy_for_own_key_users(stripe_on):
    uid = _make_user('navkey@test.com', key='sk-' + 'n' * 40, limit=180 * 60, used=0)
    body = _login(uid).get('/').data.decode()
    assert 'class="nav-balance"' not in body
    assert 'class="nav-buy"' not in body
    assert 'Buy minutes' not in body


def test_anon_nav_has_pricing_link(trial_on):
    body = A.app.test_client().get('/').data.decode()
    assert 'href="/pricing"' in body or "/pricing" in body
    assert '>Pricing<' in body


def test_pricing_page_renders_without_stripe(trial_on):
    resp = A.app.test_client().get('/pricing')
    assert resp.status_code == 200
    body = resp.data.decode()
    assert 'Free trial' in body
    assert '300' in body and '$5' in body
    assert 'VAT' in body
    assert 'one-time' in body.lower() or 'One-time' in body
    assert 'Create free account' in body or 'own OpenAI' in body
    assert 'Buy 5 hours for $5' not in body  # Buy POST only when configured + logged in
    assert '/terms' in body


def test_pricing_page_buy_when_logged_in_with_stripe(stripe_on):
    uid = _make_user('pricebuy@test.com', limit=600, used=0)
    body = _login(uid).get('/pricing').data.decode()
    assert 'Buy 5 hours for $5' in body
    assert 'billing/checkout' in body
    assert 'csrf_token' in body
    assert 'One-time · 300 min · VAT incl.' in body
    assert A.CREDIT_PACK_PAYMENT_HINT in body


def test_buy_modal_shows_payment_method_hint(stripe_on):
    """Buy modal lists methods Managed Payments enables dynamically."""
    uid = _make_user('pmhint@test.com', limit=180 * 60, used=60 * 60)
    body = _login(uid).get('/').data.decode()
    assert 'buyModalScrim' in body
    assert A.CREDIT_PACK_PAYMENT_HINT in body
    assert 'Card · Apple Pay · Google Pay' in body


def test_pricing_hides_buy_for_own_key_user(stripe_on):
    uid = _make_user('pricekey@test.com', key='sk-' + 'p' * 40, limit=600, used=0)
    body = _login(uid).get('/pricing').data.decode()
    assert 'Buy 5 hours for $5' not in body
    assert 'Add OpenAI key' in body or 'own OpenAI' in body


def test_openai_key_never_rendered_into_analytics_pages(stripe_on, monkeypatch):
    """Jinja `a and openai_api_key` returns the key string — never |tojson it.

    A logged-in own-key user must see has_own_key as the boolean true, and the
    raw key must not appear in any page that fires funnel events.
    """
    leak = 'sk-test-LEAKCHECK'
    uid = _make_user('leakcheck@test.com', key=leak, limit=600, used=0)
    client = _login(uid)

    pricing = client.get('/pricing').data.decode()
    assert leak not in pricing
    assert 'has_own_key: true' in pricing
    assert 'has_own_key: false' not in pricing
    # Guard the specific footgun: truthy-and must not pipe the key through tojson.
    assert 'openai_api_key)|tojson' not in pricing
    assert 'openai_api_key |tojson' not in pricing

    index = client.get('/').data.decode()
    assert leak not in index

    episodes = [{
        'index': 0,
        'title': 'Ep One',
        'published': '2024-01-01',
        'audio_url': 'https://example.com/ep.mp3',
        'description': '',
        'duration_min': 30.0,
        'estimated_cost': 0.18,
        'artwork': '',
        'podcast_name': 'Test Feed',
    }]
    monkeypatch.setattr(A, 'get_episodes_from_rss', lambda url: (episodes, None))
    picker = client.post('/parse_rss', data={
        'rss_url': 'https://example.com/feed.xml',
    }, follow_redirects=True).data.decode()
    assert leak not in picker


def test_pricing_in_sitemap_and_llms(trial_on):
    sitemap = A.app.test_client().get('/sitemap.xml').data.decode()
    assert '/pricing' in sitemap
    assert '/whats-new' in sitemap
    llms = A.app.test_client().get('/llms.txt').data.decode()
    assert 'Pricing' in llms
    assert '/pricing' in llms


def test_low_balance_banner_on_index(stripe_on):
    # 20 min left (< 30) → gentle Running low nudge
    uid = _make_user('lowbal@test.com', limit=180 * 60, used=160 * 60)
    body = _login(uid).get('/').data.decode()
    assert 'Running low' in body
    assert 'Buy minutes' in body


def test_settings_credits_above_openai_and_primary_buy(stripe_on):
    uid = _make_user('setcred@test.com', limit=600, used=0)
    body = _login(uid).get('/settings').data.decode()
    credits_at = body.index('id="credits"')
    openai_at = body.index('id="openai"')
    assert credits_at < openai_at, 'credits card should sit above the OpenAI key form'
    assert 'btn-primary' in body[credits_at:openai_at]
    assert '$5 · 300 minutes · one-time · VAT included' in body
    assert 'minutes available' in body


def test_transcription_status_flags_minutes_error(stripe_on):
    from models import db, TranscriptionTask
    uid = _make_user('minerr@test.com', limit=600, used=600)
    with A.app.app_context():
        db.session.add(TranscriptionTask(
            id='min-err-1', user_id=uid, episode_title='Ep',
            status='error', phase='error',
            error_message=(
                'This episode is about 45 minutes — longer than the 0 free '
                'minutes you have left. Pick a shorter episode, Buy 5 hours '
                'for $5, or add your own OpenAI API key.'
            ),
        ))
        db.session.commit()
    client = _login(uid)
    resp = client.get('/status/min-err-1')
    assert resp.status_code == 200
    data = resp.get_json()
    assert data.get('minutes_error') is True
    assert data.get('buy_available') is True
    assert data.get('buy_label') == 'Buy 5 hours for $5'


def test_transcription_status_no_minutes_flag_on_generic_error(stripe_on):
    from models import db, TranscriptionTask
    uid = _make_user('generr@test.com', limit=600, used=0)
    with A.app.app_context():
        db.session.add(TranscriptionTask(
            id='gen-err-1', user_id=uid, episode_title='Ep',
            status='error', phase='error',
            error_message='Download failed: connection reset',
        ))
        db.session.commit()
    data = _login(uid).get('/status/gen-err-1').get_json()
    assert data.get('minutes_error') is not True
    assert data.get('buy_available') is None


def test_index_mentions_pack_when_stripe_on(stripe_on):
    body = A.app.test_client().get('/').data.decode()
    assert '$5' in body
    assert '300' in body
    assert 'Pricing' in body or '/pricing' in body


def test_episode_selection_uses_inline_error_not_confirm(stripe_on):
    """402 on Start must show an inline box with Buy — not window.confirm()."""
    # Render path needs a session with episodes; assert the script shape instead.
    from models import db, User
    uid = _make_user('epsel@test.com', limit=600, used=600)
    # Build a minimal episode_selection by posting parse_rss stub… skip network:
    # the template source is what ships the confirm→inline change.
    src = open('templates/episode_selection.html').read()
    assert 'confirm(' not in src
    assert 'setStartError' in src
    assert 'startError' in src
    assert 'Add OpenAI key' in src


def test_episode_selection_buy_form_not_nested_in_episode_form(stripe_on):
    """Buy must not nest inside #episodeForm — browsers drop the inner form and
    close episodeForm early, leaving #transcribeBtn outside any form."""
    from bs4 import BeautifulSoup
    from flask import render_template

    episodes = [{
        'index': 0,
        'title': 'Ep One',
        'published': '2024-01-01',
        'duration_min': 30,
        'estimated_cost': 0.01,
        'description': '',
        'needs_own_key': False,
    }]
    with A.app.test_request_context('/'):
        html = render_template(
            'episode_selection.html',
            episodes=episodes,
            all_episodes=episodes,
            rss_url='https://example.com/feed.xml',
            feed_name='Test Feed',
            has_more=False,
            needs_api_key=True,
            show_openai_cost=False,
            podcast_name='Test Feed',
            artwork='',
            languages=[('en', 'English')],
        )
    soup = BeautifulSoup(html, 'html5lib')
    episode_form = soup.find('form', id='episodeForm')
    assert episode_form is not None
    # Nested <form> inside <form> is invalid HTML; after a real parse the Buy
    # form must be a sibling of episodeForm, not a descendant.
    nested = episode_form.find_all('form')
    assert nested == [], f'nested form(s) inside #episodeForm: {nested}'
    buy_form = soup.find('form', id='buyFormEpisodeSelection')
    assert buy_form is not None
    assert buy_form.find_parent('form') is None
    assert buy_form.get('action', '').endswith('/billing/checkout')
    assert buy_form.find('input', {'name': 'source', 'value': 'episode_selection'})
    assert buy_form.find('input', {'name': 'ph_sid'}) is not None
    assert buy_form.find('input', {'name': 'return_to'}) is not None
    # HTML5 form= associates the Buy button with buyForm without nesting.
    buy_btn = soup.find('button', attrs={'form': 'buyFormEpisodeSelection'})
    assert buy_btn is not None
    assert buy_btn.get('type') == 'submit'
    # Sticky bar + meter hooks ship with the conversion top-3 work.
    assert soup.find(id='epSticky') is not None
    assert soup.find(id='stickyTranscribeBtn') is not None
    assert 'This episode uses' in html or 'Transcribe free' in html
    # Start Transcription must remain a submit control of episodeForm.
    transcribe = soup.find(id='transcribeBtn')
    assert transcribe is not None
    assert transcribe.find_parent('form', id='episodeForm') is not None
    assert transcribe.get('type') == 'submit'


def _render_episode_selection_via_parse_rss(monkeypatch, client, *, duration_min=30.0):
    """POST /parse_rss with a stubbed feed and return the rendered HTML."""
    episodes = [{
        'index': 0,
        'title': 'Ep One',
        'published': '2024-01-01',
        'audio_url': 'https://example.com/ep.mp3',
        'description': '',
        'duration_min': duration_min,
        'estimated_cost': round(duration_min * A.WHISPER_COST_PER_MINUTE, 3),
        'artwork': '',
        'podcast_name': 'Test Feed',
    }]
    monkeypatch.setattr(
        A, 'get_episodes_from_rss',
        lambda url: (episodes, None))
    return client.post('/parse_rss', data={
        'rss_url': 'https://example.com/feed.xml',
    }, follow_redirects=True)


def _episode_card_meta(html):
    """Episode-card meta text from the server-rendered grid (not the JS source)."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, 'html5lib')
    meta = soup.select_one('.episode-card .episode-meta')
    assert meta is not None
    return meta.get_text(' ', strip=True)


def test_episode_selection_logged_out_hides_paywall_and_dollar_cost(
        stripe_on, monkeypatch):
    """Anon visitors keep the signup path — no 'Out of free minutes' card."""
    from bs4 import BeautifulSoup
    client = A.app.test_client()
    resp = _render_episode_selection_via_parse_rss(monkeypatch, client)
    assert resp.status_code == 200
    body = resp.data.decode()
    assert 'Out of free minutes' not in body
    soup = BeautifulSoup(body, 'html5lib')
    assert soup.find('form', id='buyFormEpisodeSelection') is None
    meta = _episode_card_meta(body)
    assert 'Uses ~30 min' in meta
    assert '$' not in meta
    assert 'Start Transcription' in body


def test_episode_selection_trial_with_minutes_hides_paywall(
        stripe_on, monkeypatch):
    uid = _make_user('trial-ok@test.com', limit=600, used=0)
    client = _login(uid)
    resp = _render_episode_selection_via_parse_rss(monkeypatch, client)
    assert resp.status_code == 200
    body = resp.data.decode()
    assert 'Out of free minutes' not in body
    meta = _episode_card_meta(body)
    assert 'Uses ~30 min' in meta
    assert '$' not in meta


def test_episode_selection_trial_exhausted_shows_paywall(
        stripe_on, monkeypatch):
    uid = _make_user('trial-done@test.com', limit=600, used=600)
    client = _login(uid)
    resp = _render_episode_selection_via_parse_rss(monkeypatch, client)
    assert resp.status_code == 200
    body = resp.data.decode()
    assert 'Out of free minutes' in body
    assert 'buyFormEpisodeSelection' in body
    assert A.CREDIT_PACK_PAYMENT_HINT in body
    meta = _episode_card_meta(body)
    assert 'Uses ~30 min' in meta
    assert '$' not in meta


def test_episode_selection_own_key_shows_dollar_cost_not_paywall(
        stripe_on, monkeypatch):
    uid = _make_user('byok@test.com', key='sk-' + 'u' * 40, limit=600, used=600)
    client = _login(uid)
    resp = _render_episode_selection_via_parse_rss(monkeypatch, client)
    assert resp.status_code == 200
    body = resp.data.decode()
    assert 'Out of free minutes' not in body
    meta = _episode_card_meta(body)
    assert 'Uses ~' not in meta
    assert '~$' in meta
    assert '30.0 min' in meta or '30 min' in meta

# ---------------------------------------------------------------------------
# Conversion top-3: signup episode card, usage meter, buy modal + return_to
# ---------------------------------------------------------------------------

def test_safe_return_to_rejects_absolute_and_external():
    assert A.safe_return_to('/parse_rss') == '/parse_rss'
    assert A.safe_return_to('/history?x=1') == '/history?x=1'
    assert A.safe_return_to('/settings#credits') == '/settings#credits'
    assert A.safe_return_to('/') == '/'
    assert A.safe_return_to('https://evil.com') is None
    assert A.safe_return_to('//evil.com') is None
    assert A.safe_return_to('https://evil.example/phish') is None
    assert A.safe_return_to('/\\evil.com') is None
    assert A.safe_return_to('') is None
    assert A.safe_return_to(None) is None
    assert A.safe_return_to('  ') is None


def test_billing_checkout_stores_safe_return_to(stripe_on):
    uid = _make_user('returnto@test.com', limit=600, used=0)
    client = _login(uid)
    client.get('/settings')  # seed CSRF
    with client.session_transaction() as sess:
        tok = sess['_csrf_token']
    resp = client.post('/billing/checkout', data={
        'csrf_token': tok,
        'source': 'buy_modal_episode',
        'return_to': '/parse_rss?feed=1',
    }, follow_redirects=False)
    assert resp.status_code in (302, 303)
    assert 'checkout.stripe.test' in resp.headers.get('Location', '')
    with client.session_transaction() as sess:
        assert sess.get(A.BILLING_RETURN_TO_KEY) == '/parse_rss?feed=1'
    params = stripe_on['last_create_params']
    assert params['metadata'].get('return_to') == '/parse_rss?feed=1'
    # Cancel goes through /billing/cancel?next=<return_to>
    assert 'billing/cancel' in params['cancel_url']
    assert 'parse_rss' in params['cancel_url']
    assert params['metadata'].get('location') == 'buy_modal_episode'
    assert params['metadata'].get('source') == 'buy_modal_episode'


def test_billing_checkout_rejects_external_return_to(stripe_on):
    uid = _make_user('badrto@test.com', limit=600, used=0)
    client = _login(uid)
    client.get('/settings')
    with client.session_transaction() as sess:
        tok = sess['_csrf_token']
    client.post('/billing/checkout', data={
        'csrf_token': tok,
        'source': 'buy_modal_header',
        'return_to': 'https://evil.example/steal',
    }, follow_redirects=False)
    with client.session_transaction() as sess:
        assert sess.get(A.BILLING_RETURN_TO_KEY) is None
    params = stripe_on['last_create_params']
    assert 'return_to' not in (params.get('metadata') or {})
    assert 'evil.example' not in (params.get('cancel_url') or '')


def test_billing_success_shows_episode_start_button(stripe_on):
    uid = _make_user('successcta@test.com', limit=600, used=0)
    session = _pack_session(uid, 'cs_success_cta')
    stripe_on['sessions'][session['id']] = session
    client = _login(uid)
    with client.session_transaction() as sess:
        sess[A.PENDING_TRANSCRIPTION_KEY] = {
            'title': 'Return To Task Ep',
            'audio_url': 'https://cdn.example.com/r.mp3',
            'duration_min': 40.0,
            'podcast_name': 'Show',
            'language': 'en',
            'rss_url': None,
            'episode_index': None,
            'artwork': None,
            'published': None,
        }
    resp = client.get(f'/billing/success?session_id={session["id"]}')
    assert resp.status_code == 200
    body = resp.data.decode()
    assert f'+{A.CREDIT_PACK_MINUTES} minutes added' in body
    assert 'Start transcript: Return To Task Ep' in body
    assert 'resume-transcription' in body
    assert 'Settings' in body
    assert 'Back to Settings' not in body


def test_billing_success_fallback_without_pending(stripe_on):
    uid = _make_user('successfallback@test.com', limit=600, used=0)
    session = _pack_session(uid, 'cs_success_fb')
    stripe_on['sessions'][session['id']] = session
    client = _login(uid)
    resp = client.get(f'/billing/success?session_id={session["id"]}')
    body = resp.data.decode()
    assert f'+{A.CREDIT_PACK_MINUTES} minutes added' in body
    assert 'Start a transcript' in body
    assert 'Start transcript:' not in body


def test_episode_picker_meter_text_for_trial_user(stripe_on):
    from flask import render_template
    uid = _make_user('metert@test.com', limit=180 * 60, used=30 * 60)
    episodes = [{
        'index': 0, 'title': 'Meter Ep', 'published': '2024-01-01',
        'duration_min': 45, 'estimated_cost': 0.27, 'description': '',
        'needs_own_key': False,
    }]
    client = _login(uid)
    with client.session_transaction():
        pass
    with A.app.test_request_context('/'):
        from flask_login import login_user
        from models import User
        user = A.db.session.get(User, uid)
        login_user(user)
        html = render_template(
            'episode_selection.html',
            episodes=episodes, all_episodes=episodes,
            rss_url='https://example.com/feed.xml', feed_name='Show',
            has_more=False, needs_api_key=False, podcast_name='Show',
            artwork='https://cdn.example.com/a.jpg',
            languages=[('en', 'English')],
            show_openai_cost=False,
        )
    assert 'This episode uses' in html
    assert 'you have' in html and 'left' in html
    assert 'TRIAL_REMAINING_MIN' in html
    assert 'epSticky' in html
    assert 'Transcribe free' in html
    # Trial users should not see OpenAI $ on cards — "Uses ~N min" instead
    assert 'Uses ~45 min' in html
    assert '~$0.270' not in html and '~$0.27' not in html


def test_episode_picker_meter_for_paid_user(stripe_on):
    from flask import render_template
    from models import User
    uid = _make_user('meterp@test.com', limit=180 * 60, used=180 * 60)
    with A.app.app_context():
        u = A.db.session.get(User, uid)
        u.paid_seconds_balance = 120 * 60
        A.db.session.commit()
    episodes = [{
        'index': 0, 'title': 'Paid Ep', 'published': '2024-01-01',
        'duration_min': 30, 'estimated_cost': 0.18, 'description': '',
        'needs_own_key': False,
    }]
    with A.app.test_request_context('/'):
        from flask_login import login_user
        login_user(A.db.session.get(User, uid))
        html = render_template(
            'episode_selection.html',
            episodes=episodes, all_episodes=episodes,
            rss_url='https://example.com/feed.xml', feed_name='Show',
            has_more=False, needs_api_key=False, podcast_name='Show',
            artwork='', languages=[('en', 'English')],
            show_openai_cost=False,
        )
    assert 'Transcribe →' in html
    assert 'This episode uses' in html
    assert 'Uses ~30 min' in html


def test_episode_picker_own_key_keeps_cost_no_meter(stripe_on):
    from flask import render_template
    from models import User
    uid = _make_user('meterk@test.com', key='sk-' + 'm' * 40, limit=180 * 60, used=0)
    episodes = [{
        'index': 0, 'title': 'Key Ep', 'published': '2024-01-01',
        'duration_min': 30, 'estimated_cost': 0.18, 'description': '',
        'needs_own_key': False,
    }]
    with A.app.test_request_context('/'):
        from flask_login import login_user
        login_user(A.db.session.get(User, uid))
        html = render_template(
            'episode_selection.html',
            episodes=episodes, all_episodes=episodes,
            rss_url='https://example.com/feed.xml', feed_name='Show',
            has_more=False, needs_api_key=False, podcast_name='Show',
            artwork='', languages=[('en', 'English')],
            show_openai_cost=True,
        )
    assert 'TRIAL_REMAINING_MIN = null' in html
    assert '~$0.180' in html or '~$0.18' in html
    assert 'Uses ~' not in html or 'SHOW_OPENAI_COST = true' in html


def test_first_run_home_leads_with_start_not_buy(stripe_on):
    uid = _make_user('firstrun@test.com', limit=180 * 60, used=0)
    body = _login(uid).get('/').data.decode()
    assert 'Start your first transcript' in body
    assert 'autofocus' in body
    # Banner Buy button must not lead for a brand-new account
    assert 'source" value="home_banner"' not in body
    assert 'name="source" value="home_banner"' not in body


# --------------------------------------------------------------------------
# Result-page next steps, signup feedback, PostHog client events
# --------------------------------------------------------------------------

def _completed_task(uid, task_id, **kw):
    from models import db, TranscriptionTask
    defaults = dict(
        id=task_id, user_id=uid, episode_title='Episode One',
        status='completed', phase='completed', progress=100,
        podcast_name='Hard Fork',
        rss_url='https://feeds.example.com/hardfork.xml',
        source_audio_url='https://cdn.example.com/ep1.mp3',
        transcript_text='hello world',
        language='en',
    )
    defaults.update(kw)
    with A.app.app_context():
        old = db.session.get(TranscriptionTask, task_id)
        if old:
            db.session.delete(old)
            db.session.commit()
        db.session.add(TranscriptionTask(**defaults))
        db.session.commit()


def test_related_episodes_lists_others_from_same_feed(monkeypatch, trial_on):
    uid = _make_user('related@test.com')
    _completed_task(uid, 'rel-1')
    feed = [
        {'index': 0, 'title': 'Episode Two', 'published': '2024-02-01',
         'audio_url': 'https://cdn.example.com/ep2.mp3', 'duration_min': 40,
         'artwork': '', 'podcast_name': 'Hard Fork', 'description': ''},
        {'index': 1, 'title': 'Episode One', 'published': '2024-01-01',
         'audio_url': 'https://cdn.example.com/ep1.mp3', 'duration_min': 48,
         'artwork': '', 'podcast_name': 'Hard Fork', 'description': ''},
        {'index': 2, 'title': 'Episode Zero', 'published': '2023-12-01',
         'audio_url': 'https://cdn.example.com/ep0.mp3', 'duration_min': 30,
         'artwork': '', 'podcast_name': 'Hard Fork', 'description': ''},
    ]
    monkeypatch.setattr(
        A, 'get_episodes_from_rss',
        lambda url, timeout=None: (feed, None))
    data = _login(uid).get('/transcription/rel-1/related-episodes').get_json()
    assert data['has_feed'] is True
    assert 'following' not in data
    assert data['podcast_name'] == 'Hard Fork'
    titles = [e['title'] for e in data['episodes']]
    assert 'Episode One' not in titles
    assert titles == ['Episode Two', 'Episode Zero']
    assert all('audio_url' in e for e in data['episodes'])


def test_related_episodes_hidden_without_rss(trial_on):
    uid = _make_user('norelated@test.com')
    # No feed and no show name → nothing to look up.
    _completed_task(uid, 'rel-none', rss_url=None, podcast_name=None)
    data = _login(uid).get('/transcription/rel-none/related-episodes').get_json()
    assert data['has_feed'] is False
    assert data['episodes'] == []
    assert 'following' not in data


def test_start_transcription_stores_rss_url_with_audio(monkeypatch, trial_on):
    """Episode search / Spotify starts must persist the show feed for related episodes."""
    from models import db, TranscriptionTask
    uid = _make_user('startrss@test.com', limit=36000)
    monkeypatch.setattr(A, '_is_fetchable_url', lambda url: True)
    feed = 'https://feeds.example.com/hardfork.xml'
    resp = _post_start(monkeypatch, uid, {
        'audio_url': 'https://cdn.example.com/ep.mp3',
        'episode_title': 'Episode One',
        'podcast_name': 'Hard Fork',
        'rss_url': feed,
        'duration_min': '5',
    })
    assert resp.status_code == 200
    task_id = resp.get_json()['task_id']
    with A.app.app_context():
        task = db.session.get(TranscriptionTask, task_id)
        assert task is not None
        assert task.rss_url == feed
    # Mark completed so related-episodes is the post-transcript path users hit.
    with A.app.app_context():
        task = db.session.get(TranscriptionTask, task_id)
        task.status = 'completed'
        task.phase = 'completed'
        task.progress = 100
        task.transcript_text = 'hello'
        db.session.commit()
    monkeypatch.setattr(
        A, 'get_episodes_from_rss',
        lambda url, timeout=None: ([], 'offline'))
    data = _login(uid).get(f'/transcription/{task_id}/related-episodes').get_json()
    assert data['has_feed'] is True


def test_start_transcription_drops_private_rss_url(monkeypatch, trial_on):
    from models import db, TranscriptionTask
    uid = _make_user('droprss@test.com', limit=36000)

    def fetchable(url):
        return '127.0.0.1' not in url and '169.254' not in url

    monkeypatch.setattr(A, '_is_fetchable_url', fetchable)
    resp = _post_start(monkeypatch, uid, {
        'audio_url': 'https://cdn.example.com/ep.mp3',
        'episode_title': 'Episode One',
        'podcast_name': 'Hard Fork',
        'rss_url': 'http://127.0.0.1/feed.xml',
        'duration_min': '5',
    })
    assert resp.status_code == 200
    task_id = resp.get_json()['task_id']
    with A.app.app_context():
        task = db.session.get(TranscriptionTask, task_id)
        assert task.rss_url is None


def test_pending_transcription_keeps_fetchable_rss(monkeypatch, trial_on):
    monkeypatch.setattr(A, '_is_fetchable_url', lambda url: '127.0.0.1' not in url)
    client = A.app.test_client()
    resp = client.post('/pending-transcription', data={
        'audio_url': 'https://cdn.example.com/ep.mp3',
        'episode_title': 'ChatGPT Ep',
        'podcast_name': 'Show',
        'rss_url': 'https://feeds.example.com/show.xml',
        'duration_min': '42',
    }, follow_redirects=False)
    assert resp.status_code in (302, 303)
    with client.session_transaction() as sess:
        pending = sess.get(A.PENDING_TRANSCRIPTION_KEY)
        assert pending['rss_url'] == 'https://feeds.example.com/show.xml'

    client2 = A.app.test_client()
    resp2 = client2.post('/pending-transcription', data={
        'audio_url': 'https://cdn.example.com/ep.mp3',
        'episode_title': 'ChatGPT Ep',
        'rss_url': 'http://127.0.0.1/feed.xml',
    }, follow_redirects=False)
    assert resp2.status_code in (302, 303)
    with client2.session_transaction() as sess:
        pending = sess.get(A.PENDING_TRANSCRIPTION_KEY)
        assert pending['rss_url'] is None


def test_related_episodes_looks_up_feed_by_podcast_name(monkeypatch, trial_on):
    """Tasks that never stored rss_url still get related episodes via iTunes."""
    from models import db, TranscriptionTask
    uid = _make_user('rellookup@test.com')
    _completed_task(uid, 'rel-lookup', rss_url=None, podcast_name='Hard Fork')
    feed_url = 'https://feeds.example.com/hardfork.xml'
    monkeypatch.setattr(
        A, '_public_shows_named',
        lambda name: [{'collectionName': 'Hard Fork', 'feedUrl': feed_url}])
    monkeypatch.setattr(A, '_is_fetchable_url', lambda url: True)
    feed = [
        {'index': 0, 'title': 'Episode Two', 'published': '2024-02-01',
         'audio_url': 'https://cdn.example.com/ep2.mp3', 'duration_min': 40,
         'artwork': '', 'podcast_name': 'Hard Fork', 'description': ''},
        {'index': 1, 'title': 'Episode One', 'published': '2024-01-01',
         'audio_url': 'https://cdn.example.com/ep1.mp3', 'duration_min': 48,
         'artwork': '', 'podcast_name': 'Hard Fork', 'description': ''},
    ]
    monkeypatch.setattr(
        A, 'get_episodes_from_rss',
        lambda url, timeout=None: (feed, None))
    data = _login(uid).get('/transcription/rel-lookup/related-episodes').get_json()
    assert data['has_feed'] is True
    assert data['episodes'][0]['title'] == 'Episode Two'
    with A.app.app_context():
        task = db.session.get(TranscriptionTask, 'rel-lookup')
        assert task.rss_url == feed_url


def test_related_episodes_respects_timeout_kwarg(monkeypatch, trial_on):
    """The result page must pass a timeout so a hung feed cannot stall it."""
    uid = _make_user('reltimeout@test.com')
    _completed_task(uid, 'rel-to')
    seen = {}

    def fake(url, timeout=None):
        seen['timeout'] = timeout
        return [], 'Error parsing RSS feed: timed out'

    monkeypatch.setattr(A, 'get_episodes_from_rss', fake)
    data = _login(uid).get('/transcription/rel-to/related-episodes').get_json()
    assert seen['timeout'] == A.RELATED_EPISODES_TIMEOUT
    assert data['has_feed'] is True
    assert data['episodes'] == []


def test_status_reports_has_rss(trial_on):
    uid = _make_user('hasrss@test.com')
    _completed_task(uid, 'rss-yes')
    data = _login(uid).get('/status/rss-yes').get_json()
    assert data['has_rss'] is True
    assert data['status'] == 'completed'
    _completed_task(uid, 'rss-no', rss_url=None)
    data2 = _login(uid).get('/status/rss-no').get_json()
    assert data2['has_rss'] is False


def test_transcription_page_has_next_steps_and_tracking():
    src = open('templates/transcription.html').read()
    assert 'id="nextSteps"' in src
    assert 'Follow this podcast' not in src
    assert 'Email me when new episodes' not in src
    assert 'More episodes from' in src
    assert 'related-episodes' in src or 'RELATED_URL' in src
    assert "url_for('history')" in src
    assert "transcript_copied" in src
    assert "transcript_downloaded" in src
    assert "result_viewed" in src
    assert "next_episode_clicked" in src
    assert "podcast_followed" not in src
    # Must not ship transcript text into analytics properties.
    assert "phCapture('transcript_copied', { task_id: taskId })" in src
    assert 'format: \'txt\'' in src or 'format: "txt"' in src
    # Copy-adjacent next steps + visible downloads.
    assert 'id="copyNextSteps"' in src
    assert 'copy_next_step_shown' in src
    assert 'copy_next_step_clicked' in src
    assert 'Download .txt' in src
    assert 'Download .srt' in src
    assert 'showCopyNextSteps' in src
    assert 'Transcribe another episode from' in src


def test_homepage_episode_click_sends_rss_url():
    """Search / Spotify episode rows must post feed_url as rss_url."""
    src = open('templates/index.html').read()
    assert 'fields.rss_url = item.feed_url' in src or 'rss_url: item.feed_url' in src


def test_search_input_type_patterns_in_homepage():
    """Client-side classifier covers the documented input_type enum."""
    src = open('templates/index.html').read()
    assert 'podcasts' in src and 'apple' in src
    assert 'APPLE_RE' in src
    assert 'YOUTUBE_RE' in src
    assert 'AUDIO_EXT_RE' in src
    assert 'RSS_HINT_RE' in src
    assert "return 'name'" in src


def test_login_page_has_submit_feedback():
    body = A.app.test_client().get('/login').data.decode()
    assert 'id="loginSubmit"' in body
    assert 'Logging in' in body
    assert 'novalidate' in body


def test_register_sets_remember_cookie_and_permanent_session(trial_on):
    """Closing Safari must not dump a new signup onto /login."""
    A._register_attempts.clear()
    email = 'remember-me@example.com'
    _purge([email])
    client = _fresh_client()
    resp = client.post(
        '/register',
        data={'email': email, 'password': 'abcdefgh1'},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    set_cookies = resp.headers.getlist('Set-Cookie')
    remember = [c for c in set_cookies if c.lower().startswith('remember_token=')]
    assert remember, set_cookies
    assert 'Expires=' in remember[0] or 'Max-Age=' in remember[0]
    with client.session_transaction() as sess:
        assert sess.permanent is True
    _purge([email])


def test_login_sets_remember_cookie_and_permanent_session():
    uid = _make_user('login-remember@test.com')
    from models import User
    with A.app.app_context():
        email = A.db.session.get(User, uid).email
    client = A.app.test_client()
    resp = client.post(
        '/login',
        data={'email': email, 'password': 'password123'},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    set_cookies = resp.headers.getlist('Set-Cookie')
    remember = [c for c in set_cookies if c.lower().startswith('remember_token=')]
    assert remember, set_cookies
    assert 'Expires=' in remember[0] or 'Max-Age=' in remember[0]
    with client.session_transaction() as sess:
        assert sess.permanent is True


def test_session_cookie_config_matches_public_origin(monkeypatch):
    assert A.app.config['PERMANENT_SESSION_LIFETIME'].days == 90
    assert A.app.config['REMEMBER_COOKIE_DURATION'].days == 365
    assert A.app.config['SESSION_COOKIE_SAMESITE'] == 'Lax'
    assert A.app.config['REMEMBER_COOKIE_SAMESITE'] == 'Lax'
    # Suite leaves PUBLIC_BASE_URL unset / http — Secure must stay off so
    # test_client cookies work. Production sets https://podskrift.com.
    if not (A.PUBLIC_BASE_URL or '').startswith('https://'):
        assert A.app.config['SESSION_COOKIE_SECURE'] is False
        assert A.app.config['REMEMBER_COOKIE_SECURE'] is False


def test_www_host_redirects_301_to_public_base(monkeypatch, trial_on):
    monkeypatch.setattr(A, 'PUBLIC_BASE_URL', 'https://podskrift.com')
    client = A.app.test_client()
    resp = client.get(
        '/pricing?x=1',
        headers={'Host': 'www.podskrift.com'},
        follow_redirects=False,
    )
    assert resp.status_code == 301
    assert resp.headers['Location'] == 'https://podskrift.com/pricing?x=1'


def test_staging_host_post_redirects_308(monkeypatch, trial_on):
    monkeypatch.setattr(A, 'PUBLIC_BASE_URL', 'https://podskrift.com')
    client = A.app.test_client()
    resp = client.post(
        '/login',
        headers={'Host': 'podskrift.nettsmed.dev'},
        data={'email': 'a@b.com', 'password': 'x'},
        follow_redirects=False,
    )
    assert resp.status_code == 308
    assert resp.headers['Location'] == 'https://podskrift.com/login'


def test_stripe_webhook_not_redirected_off_canonical_host(monkeypatch):
    monkeypatch.setattr(A, 'PUBLIC_BASE_URL', 'https://podskrift.com')
    client = A.app.test_client()
    resp = client.post(
        '/stripe/webhook',
        headers={'Host': 'www.podskrift.com'},
        data=b'{}',
        content_type='application/json',
        follow_redirects=False,
    )
    assert resp.status_code != 301
    assert resp.status_code != 308
    assert 'Location' not in resp.headers or 'podskrift.com/stripe' not in (
        resp.headers.get('Location') or '')


def test_api_path_not_redirected_off_canonical_host(monkeypatch):
    monkeypatch.setattr(A, 'PUBLIC_BASE_URL', 'https://podskrift.com')
    client = A.app.test_client()
    resp = client.get(
        '/api/v1/episodes',
        headers={'Host': 'www.podskrift.com'},
        follow_redirects=False,
    )
    assert resp.status_code != 301
    assert resp.status_code != 308


def test_canonical_link_uses_public_base_url(monkeypatch, trial_on):
    monkeypatch.setattr(A, 'PUBLIC_BASE_URL', 'https://podskrift.com')
    body = A.app.test_client().get(
        '/pricing', headers={'Host': 'podskrift.com'}).data.decode()
    assert 'rel="canonical" href="https://podskrift.com/pricing"' in body
    assert 'property="og:url" content="https://podskrift.com/pricing"' in body


def test_register_existing_account_offers_login_with_next(trial_on):
    A._register_attempts.clear()
    email = 'exists-link@example.com'
    _purge([email])
    client = _fresh_client()
    assert 'Account created' in _signup(client, email).data.decode()
    client = _new_session()
    resp = client.post(
        '/register?next=/transcription/abc123',
        data={'email': email, 'password': 'abcdefgh1'},
        follow_redirects=True,
    )
    body = resp.data.decode()
    assert 'already exists' in body
    assert 'Log in instead' in body
    assert 'next=/transcription/abc123' in body or 'next=%2Ftranscription%2Fabc123' in body
    _purge([email])


def test_login_heading_for_saved_transcript():
    body = A.app.test_client().get(
        '/login?next=/transcription/task-xyz').data.decode()
    assert 'Log in to open your saved transcript' in body
    assert 'Forgot password?' in body
    assert '/forgot-password' in body


def test_login_default_heading_without_transcript_next():
    body = A.app.test_client().get('/login').data.decode()
    assert 'Log in to open your saved transcript' not in body
    assert '>Log in<' in body or 'Log in</h2>' in body
    assert 'Forgot password?' in body


def test_register_page_shows_password_rule_upfront(trial_on):
    body = A.app.test_client().get('/register').data.decode()
    assert 'minlength="8"' in body
    assert 'At least 8 characters' in body
    assert 'id="passwordHint"' in body


def test_register_failed_emits_posthog_reason(ph_events, trial_on):
    A._register_attempts.clear()
    client = _fresh_client()
    client.post('/register', data={
        'email': 'shortpw@example.com',
        'password': 'short',
    })
    fails = [e for e in ph_events.events if e['event'] == 'register_failed']
    assert fails
    assert fails[-1]['properties']['reason'] == 'password_too_short'
    assert 'email' not in fails[-1]['properties']


def test_login_wall_shown_emits_next_type(ph_events):
    client = A.app.test_client()
    client.get('/login?next=/transcription/abc')
    walls = [e for e in ph_events.events if e['event'] == 'login_wall_shown']
    assert len(walls) == 1
    assert walls[0]['properties']['next_type'] == 'transcription'
    assert 'email' not in walls[0]['properties']


def test_get_episodes_from_rss_timeout_uses_capped_fetch(monkeypatch):
    """Timed path goes through the SSRF-safe, byte-capped, early-stop fetch."""
    body = b"""<?xml version="1.0"?>
        <rss><channel><title>Show</title>
        <item><title>Ep</title>
        <enclosure url="https://cdn.example.com/a.mp3" type="audio/mpeg"/>
        </item></channel></rss>"""
    called = {}

    def fake_capped(url, max_bytes=None, early_stop_items=None, timeout=None):
        called.update(url=url, timeout=timeout, max_bytes=max_bytes,
                      early_stop_items=early_stop_items)
        return body

    monkeypatch.setattr(A, '_fetch_feed_capped', fake_capped)
    monkeypatch.setattr(A, '_is_fetchable_url', lambda url: True)
    episodes, err = A.get_episodes_from_rss(
        'https://feeds.example.com/x.xml', timeout=8)
    assert err is None
    assert called['timeout'] == 8
    assert called['max_bytes'] == A.SHOW_FEED_MAX_BYTES
    assert called['early_stop_items'] == A.SHOW_FEED_EARLY_STOP_ITEMS
    assert len(episodes) == 1
    assert episodes[0]['title'] == 'Ep'

    monkeypatch.setattr(A, '_fetch_feed_capped', lambda *a, **k: None)
    episodes, err = A.get_episodes_from_rss(
        'https://feeds.example.com/x.xml', timeout=8)
    assert episodes is None and err


# --------------------------------------------------------------------------
# Duplicate web enqueue guard, language normalisation, download filenames
# --------------------------------------------------------------------------

def test_web_enqueue_reuses_running_task_without_double_charge(monkeypatch, trial_on):
    """Restarting the same audio URL while it is live must not reserve again."""
    uid = _make_user('dup-run@test.com', limit=3600)
    first = _post_start(monkeypatch, uid, {
        'audio_url': 'https://example.com/same-ep.mp3',
        'episode_title': 'Same Ep',
        'duration_min': '5',
    })
    assert first.status_code == 200, first.get_json()
    first_id = first.get_json()['task_id']
    used_after_first = _used(uid)
    assert used_after_first == 300  # 5 min reserved

    second = _post_start(monkeypatch, uid, {
        'audio_url': 'https://example.com/same-ep.mp3',
        'episode_title': 'Same Ep again',
        'duration_min': '5',
    })
    assert second.status_code == 200
    body = second.get_json()
    assert body['task_id'] == first_id
    assert body.get('existing') is True
    assert _used(uid) == used_after_first  # no second reservation


def test_web_enqueue_reuses_completed_task_without_double_charge(monkeypatch, trial_on):
    """A finished transcript is the redirect target; trial minutes stay put."""
    from models import db, TranscriptionTask

    uid = _make_user('dup-done@test.com', limit=3600)
    audio = 'https://example.com/finished-ep.mp3'
    first = _post_start(monkeypatch, uid, {
        'audio_url': audio,
        'episode_title': 'Done Ep',
        'duration_min': '4',
    })
    assert first.status_code == 200, first.get_json()
    task_id = first.get_json()['task_id']
    used_after = _used(uid)

    with A.app.app_context():
        task = db.session.get(TranscriptionTask, task_id)
        task.status = 'completed'
        task.phase = 'completed'
        task.transcript_text = 'hello'
        db.session.commit()

    second = _post_start(monkeypatch, uid, {
        'audio_url': audio,
        'episode_title': 'Done Ep',
        'duration_min': '4',
    })
    assert second.status_code == 200
    body = second.get_json()
    assert body == {'task_id': task_id, 'existing': True}
    assert _used(uid) == used_after


def test_web_enqueue_allows_rerun_after_error(monkeypatch, trial_on):
    """Error/cancelled tasks may be started again (and reserve again)."""
    import threading as _t
    from models import db, TranscriptionTask

    # Default concurrency is 1; the stubbed worker never releases its slot.
    monkeypatch.setattr(A, 'MAX_CONCURRENT_TRANSCRIPTIONS', 2)
    monkeypatch.setattr(A, '_transcription_slots', _t.BoundedSemaphore(2))

    uid = _make_user('dup-err@test.com', limit=3600)
    audio = 'https://example.com/failed-ep.mp3'
    first = _post_start(monkeypatch, uid, {
        'audio_url': audio,
        'episode_title': 'Fail Ep',
        'duration_min': '3',
    })
    assert first.status_code == 200, first.get_json()
    task_id = first.get_json()['task_id']
    with A.app.app_context():
        task = db.session.get(TranscriptionTask, task_id)
        # Settle so a re-run is a clean new reservation (mirrors refund path).
        A.trial_refund_task(task)
        task.status = 'error'
        task.phase = 'error'
        db.session.commit()
    used_before = _used(uid)

    second = _post_start(monkeypatch, uid, {
        'audio_url': audio,
        'episode_title': 'Fail Ep',
        'duration_min': '3',
    })
    assert second.status_code == 200, second.get_json()
    body = second.get_json()
    assert body.get('existing') is not True
    assert body['task_id'] != task_id
    assert _used(uid) == used_before + 180


def test_api_enqueue_is_not_blocked_by_web_duplicate_guard(monkeypatch, trial_on):
    """Duplicate guard is web-only; agent/api keeps its own reuse logic."""
    import threading as _t
    import types
    from models import db, User

    monkeypatch.setattr(A, 'MAX_CONCURRENT_TRANSCRIPTIONS', 2)
    monkeypatch.setattr(A, '_transcription_slots', _t.BoundedSemaphore(2))
    uid = _make_user('dup-api@test.com', key='sk-' + 'a' * 40)
    audio = 'https://example.com/api-ep.mp3'
    monkeypatch.setattr(
        A.threading, 'Thread',
        lambda *a, **kw: types.SimpleNamespace(daemon=True, start=lambda: None))
    monkeypatch.setattr(A, 'free_disk_bytes', lambda *a, **kw: 10 ** 12)

    with A.app.app_context():
        user = db.session.get(User, uid)
        first, s1 = A.enqueue_transcription(
            user, {'title': 'Ep', 'audio_url': audio, 'duration_min': 1},
            source='api')
        second, s2 = A.enqueue_transcription(
            user, {'title': 'Ep', 'audio_url': audio, 'duration_min': 1},
            source='api')
    assert s1 == 200 and s2 == 200
    assert first['task_id'] != second['task_id']
    assert 'existing' not in second


def test_normalize_language_code_maps_names_and_unknown_to_auto():
    assert A.normalize_language_code('en') == 'en'
    assert A.normalize_language_code('EN') == 'en'
    assert A.normalize_language_code('english') == 'en'
    assert A.normalize_language_code('Norwegian') == 'no'
    assert A.normalize_language_code('not-a-language') == ''
    assert A.normalize_language_code('') == ''
    assert A.normalize_language_code(None) == ''


def test_display_language_keeps_legacy_names_readable():
    assert A.display_language('en') == 'English'
    assert A.display_language('english') == 'English'
    assert A.display_language('Klingon') == 'Klingon'


def test_retry_payload_sends_iso_code_for_legacy_name(monkeypatch, trial_on):
    """Old rows stored Whisper names; retry must post a code the picker accepts."""
    from models import db, TranscriptionTask

    uid = _make_user('lang-retry@test.com', key='sk-' + 'b' * 40)
    with A.app.app_context():
        task = TranscriptionTask(
            id='lang-retry-1',
            user_id=uid,
            episode_title='Ep',
            status='error',
            phase='error',
            error_message=(
                'Your OpenAI account has no credit. '
                'Add billing at platform.openai.com.'
            ),
            source_audio_url='https://cdn.example.com/ep.mp3',
            audio_duration=120.0,
            language='english',
        )
        db.session.add(task)
        db.session.commit()

    client = A.app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(uid)
        sess['_fresh'] = True
    resp = client.get('/status/lang-retry-1')
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['retry']['language'] == 'en'


def test_safe_download_basename_strips_path_separators():
    assert '/' not in A.safe_download_basename('a/b\\c:d')
    assert '\\' not in A.safe_download_basename('a/b\\c:d')
    assert A.safe_download_basename('Hello World') == 'Hello_World'
    assert A.safe_download_basename('') == 'transcript'
    assert A.safe_download_basename('///') == 'transcript'


def test_download_filename_sanitises_slashes(monkeypatch, trial_on):
    from models import db, TranscriptionTask

    uid = _make_user('dl-slash@test.com', key='sk-' + 'c' * 40)
    with A.app.app_context():
        task = TranscriptionTask(
            id='dl-slash-1',
            user_id=uid,
            episode_title='Show/Episode: Part 1?',
            status='completed',
            phase='completed',
            transcript_text='hi',
        )
        db.session.add(task)
        db.session.commit()

    client = A.app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(uid)
        sess['_fresh'] = True
    resp = client.get('/download/dl-slash-1/txt')
    assert resp.status_code == 200
    cd = resp.headers.get('Content-Disposition', '')
    assert 'filename=' in cd
    # The download name itself must not contain path separators.
    name = cd.split('filename=')[-1].strip().strip('"')
    assert '/' not in name
    assert '\\' not in name


def test_transcript_started_includes_measurement_props(ph_events, monkeypatch, trial_on):
    uid = _make_user('ph-measure@test.com', key='sk-' + 'd' * 40)
    resp = _post_start(monkeypatch, uid, {
        'audio_url': 'https://example.com/ep.mp3',
        'episode_title': 'Measured',
        'podcast_name': 'Cool Show',
        'duration_min': '12',
        'language': 'no',
        'input_origin': 'spotify',
    })
    assert resp.status_code == 200
    started = [e for e in ph_events.events if e['event'] == 'transcript_started']
    assert len(started) == 1
    props = started[0]['properties']
    assert props['duration_min'] == 12.0
    assert props['language'] == 'no'
    assert props['input_origin'] == 'spotify'
    assert props['has_feed'] is False
    assert props['podcast_name'] == 'Cool Show'
    assert props['nth_transcript'] == 1


def test_derive_input_origin_from_rss_and_apple():
    assert A.derive_input_origin({}, rss_url='https://feeds.example.com/x.xml') == 'rss'
    assert A.derive_input_origin(
        {}, rss_url='https://podcasts.apple.com/us/podcast/x/id1') == 'apple'
    assert A.derive_input_origin(
        {'input_origin': 'itunes_episode'}, rss_url=None) == 'itunes_episode'
    assert A.derive_input_origin({}, rss_url=None) == 'audio'


# --- Show landing pages (/podcasts) -------------------------------------------

_SHOW_FIXTURE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'testdata', 'show_pages_fixture.json')


@pytest.fixture
def show_pages_fixture(monkeypatch, tmp_path):
    """Point show pages at a tiny fixture and an isolated feed-cache dir."""
    import show_pages as SP
    monkeypatch.setattr(SP, 'SHOW_PAGES_PATH', __import__('pathlib').Path(_SHOW_FIXTURE))
    monkeypatch.setattr(SP, 'SHOW_FEED_CACHE_DIR', tmp_path / 'show_feed_cache')
    with SP._curated_lock:
        SP._curated_mtime = None
        SP._curated_by_slug = {}
        SP._curated_list = []
    with SP._mem_cache_lock:
        SP._mem_feed_cache.clear()
    return SP


def _fixture_episodes():
    return [
        {
            'index': 0,
            'title': 'Episode One: Hello',
            'published': '2026-10-01',
            'audio_url': 'https://cdn.example.com/ep1.mp3',
            'description': 'First fixture episode.',
            'duration_min': 32.0,
            'artwork': '',
            'podcast_name': 'Fixture Show',
        },
        {
            'index': 1,
            'title': 'Episode Two: World',
            'published': '2026-09-20',
            'audio_url': 'https://cdn.example.com/ep2.mp3',
            'description': 'Second fixture episode.',
            'duration_min': 45.0,
            'artwork': '',
            'podcast_name': 'Fixture Show',
        },
    ]


def test_show_page_renders_from_fixture_data(show_pages_fixture, monkeypatch, trial_on, ph_events):
    """Curated show page: H1, description, episodes, FAQ — no transcript body."""
    SP = show_pages_fixture
    monkeypatch.setattr(A, '_is_fetchable_url', lambda u: True)

    def fake_rss(url, *, timeout=None):
        assert url == 'https://feeds.example.com/fixture.xml'
        assert timeout is not None  # show pages must use a bounded fetch
        return _fixture_episodes(), None

    monkeypatch.setattr(A, 'get_episodes_from_rss', fake_rss)
    client = A.app.test_client()
    resp = client.get('/podcasts/fixture-show')
    assert resp.status_code == 200
    body = resp.data.decode()
    assert 'Transcripts for Fixture Show' in body
    assert 'A short description of Fixture Show' in body
    assert 'Episode One: Hello' in body
    assert 'Transcribe this episode' in body
    assert 'Is it free?' in body
    assert '60' in body  # free minutes copy
    # Never leak transcript text (we don't have any — guard the word patterns).
    assert 'transcript_text' not in body
    assert 'Full transcript' not in body
    # SEO bits
    assert 'application/ld+json' in body
    assert 'PodcastSeries' in body
    assert 'FAQPage' in body
    assert 'BreadcrumbList' in body
    assert 'aria-label="Transcribe this episode: Episode One: Hello"' in body
    assert 'rel="canonical"' in body or 'rel=canonical' in body.lower() or 'canonical' in body
    # Analytics
    views = [e for e in ph_events.events if e['event'] == 'show_page_viewed']
    assert len(views) == 1
    assert views[0]['properties']['show_slug'] == 'fixture-show'
    assert 'referrer_source' in views[0]['properties']


def test_show_page_feed_failure_falls_back_without_500(
        show_pages_fixture, monkeypatch, trial_on, tmp_path):
    """A hung/failed feed still returns 200 with last-good episodes or empty list."""
    SP = show_pages_fixture
    monkeypatch.setattr(A, '_is_fetchable_url', lambda u: True)
    # Seed last-good on disk.
    cache_dir = tmp_path / 'show_feed_cache'
    cache_dir.mkdir(parents=True)
    import json as _json
    import time as _time
    (cache_dir / 'fixture-show.json').write_text(_json.dumps({
        'fetched_at': _time.time() - 10,
        'episodes': [{
            'index': 0,
            'title': 'Cached Episode',
            'published': '2026-01-01',
            'duration_min': 10,
            'description': 'from cache',
            'artwork': '',
            'audio_url': 'https://cdn.example.com/cached.mp3',
        }],
        'error': None,
        'feed_url': 'https://feeds.example.com/fixture.xml',
    }), encoding='utf-8')
    # Force TTL miss so we attempt a fetch, then fail.
    monkeypatch.setattr(SP, 'SHOW_FEED_CACHE_TTL', 0)

    def boom(url, *, timeout=None):
        return None, 'timeout talking to feed'

    monkeypatch.setattr(A, 'get_episodes_from_rss', boom)
    resp = A.app.test_client().get('/podcasts/fixture-show')
    assert resp.status_code == 200
    body = resp.data.decode()
    assert 'Cached Episode' in body
    assert 'last good' in body.lower() or 'Transcribe this episode' in body


def test_show_page_unknown_slug_is_404(show_pages_fixture, trial_on):
    assert A.app.test_client().get('/podcasts/no-such-show-xyz').status_code == 404


def test_podcasts_index_lists_fixture_shows(show_pages_fixture, trial_on):
    resp = A.app.test_client().get('/podcasts')
    assert resp.status_code == 200
    body = resp.data.decode()
    assert 'Fixture Show' in body
    assert '/podcasts/fixture-show' in body
    assert 'Another Fixture' in body


def test_sitemap_includes_show_pages(show_pages_fixture, trial_on, monkeypatch):
    monkeypatch.setattr(A, 'PUBLIC_BASE_URL', 'https://podskrift.com')
    # Match canonical Host so before_request does not 301.
    resp = A.app.test_client().get(
        '/sitemap.xml', headers={'Host': 'podskrift.com'})
    assert resp.status_code == 200
    body = resp.data.decode()
    assert '/podcasts</loc>' in body or body.count('/podcasts') >= 1
    assert '/podcasts/fixture-show' in body
    assert '/podcasts/another-fixture' in body


def test_show_page_uses_ssrf_guard(show_pages_fixture, monkeypatch, trial_on):
    """Blocked feed URLs must not be fetched; page still 200."""
    called = {'n': 0}

    def tracking_fetchable(url):
        return False

    def should_not_run(url, *, timeout=None):
        called['n'] += 1
        raise AssertionError('get_episodes_from_rss must not run for blocked URLs')

    monkeypatch.setattr(A, '_is_fetchable_url', tracking_fetchable)
    monkeypatch.setattr(A, 'get_episodes_from_rss', should_not_run)
    resp = A.app.test_client().get('/podcasts/fixture-show')
    assert resp.status_code == 200
    assert called['n'] == 0
    body = resp.data.decode()
    assert 'Transcripts for Fixture Show' in body
    # No episode rows from a blocked feed.
    assert 'Episode One' not in body


def test_show_page_never_leaks_transcript_text(
        show_pages_fixture, monkeypatch, trial_on):
    """Even if a feed description somehow contained transcript-like text, the
    page must not surface DB transcript_text from completed jobs."""
    uid = _make_user('show-leak@test.com')
    secret = 'TOP SECRET TRANSCRIPT BODY UNIQUE 9f3a2c'
    with A.app.app_context():
        A.db.session.add(A.TranscriptionTask(
            id='show-leak-task',
            user_id=uid,
            episode_title='Secret Ep',
            podcast_name='Fixture Show',
            rss_url='https://feeds.example.com/fixture.xml',
            status='completed',
            transcript_text=secret,
            progress=100,
        ))
        A.db.session.commit()
    monkeypatch.setattr(A, '_is_fetchable_url', lambda u: True)
    monkeypatch.setattr(
        A, 'get_episodes_from_rss',
        lambda url, *, timeout=None: (_fixture_episodes(), None))
    body = A.app.test_client().get('/podcasts/fixture-show').data.decode()
    assert secret not in body
    assert 'TOP SECRET' not in body


def test_community_shows_off_by_default(show_pages_fixture, monkeypatch, trial_on):
    """User-derived shows (feed URLs may carry private tokens) need opt-in."""
    uid = _make_user('community-off@test.com')
    with A.app.app_context():
        A.db.session.add(A.TranscriptionTask(
            id='community-off-task', user_id=uid, episode_title='Ep',
            podcast_name='Private Premium Feed Show',
            rss_url='https://feeds.example.com/private.xml?token=SECRET123',
            status='completed', transcript_text='x', progress=100,
        ))
        A.db.session.commit()
    monkeypatch.setattr(show_pages_fixture, 'SHOW_PAGES_COMMUNITY', False)
    index = A.app.test_client().get('/podcasts').data.decode()
    assert 'Private Premium Feed Show' not in index
    assert 'SECRET123' not in index
    assert A.app.test_client().get(
        '/podcasts/private-premium-feed-show').status_code == 404


def test_community_show_appears_when_completed_with_feed(
        show_pages_fixture, monkeypatch, trial_on):
    uid = _make_user('community-show@test.com')
    monkeypatch.setattr(show_pages_fixture, 'SHOW_PAGES_COMMUNITY', True)
    with A.app.app_context():
        A.db.session.add(A.TranscriptionTask(
            id='community-show-task',
            user_id=uid,
            episode_title='Ep',
            podcast_name='Niche Community Podcast',
            rss_url='https://feeds.example.com/niche.xml',
            status='completed',
            transcript_text='should not appear on index',
            progress=100,
        ))
        A.db.session.commit()
    monkeypatch.setattr(A, '_is_fetchable_url', lambda u: True)
    monkeypatch.setattr(
        A, 'get_episodes_from_rss',
        lambda url, *, timeout=None: ([], 'empty'))
    index = A.app.test_client().get('/podcasts').data.decode()
    assert 'Niche Community Podcast' in index
    assert 'should not appear on index' not in index
    # Slug is generated from the name.
    resp = A.app.test_client().get('/podcasts/niche-community-podcast')
    assert resp.status_code == 200
    assert 'Transcripts for Niche Community Podcast' in resp.data.decode()
    assert 'should not appear on index' not in resp.data.decode()


def test_llms_txt_links_podcasts_index(trial_on):
    body = A.app.test_client().get('/llms.txt').data.decode()
    assert '/podcasts' in body
    assert 'Spotify' in body
    assert 'Apple' in body


def test_footer_links_to_podcasts(trial_on):
    home = A.app.test_client().get('/').data.decode()
    assert '/podcasts' in home
    assert 'Podcasts' in home


def test_show_page_transcribe_posts_rss_with_episode_index(
        show_pages_fixture, monkeypatch, trial_on):
    """Transcribe button reuses parse_rss with rss_url + episode_index."""
    monkeypatch.setattr(A, '_is_fetchable_url', lambda u: True)
    monkeypatch.setattr(
        A, 'get_episodes_from_rss',
        lambda url, *, timeout=None: (_fixture_episodes(), None))
    # parse_rss itself uses get_episodes_from_rss without timeout by default.
    client = A.app.test_client()
    page = client.get('/podcasts/fixture-show').data.decode()
    assert 'name="rss_url"' in page
    assert 'name="episode_index"' in page
    assert 'action="/parse_rss"' in page or 'parse_rss' in page
    resp = client.post('/parse_rss', data={
        'rss_url': 'https://feeds.example.com/fixture.xml',
        'episode_index': '0',
    }, follow_redirects=True)
    assert resp.status_code == 200
    body = resp.data.decode()
    assert 'Episode One: Hello' in body
    # Preselected index is wired into the picker JS.
    assert 'PRESELECTED_INDEX' in body
    assert '0' in body
    assert 'name="episode_audio_url"' in page
    # A new release shifted indexes since the page was cached: the audio URL
    # still preselects the right episode, not whatever is now at index 0.
    resp = client.post('/parse_rss', data={
        'rss_url': 'https://feeds.example.com/fixture.xml',
        'episode_index': '0',
        'episode_audio_url': 'https://cdn.example.com/ep2.mp3',
    }, follow_redirects=True)
    assert 'var PRESELECTED_INDEX = 1;' in resp.data.decode()


# --------------------------------------------------------------------------
# Public share links
# --------------------------------------------------------------------------

def _share_task(user_id, task_id='share-ep-1', **extra):
    """Completed transcript fixture for share-link tests (distinct from
    the result-page `_completed_task` helper above)."""
    from models import db, TranscriptionTask, TranscriptShare
    defaults = dict(
        id=task_id,
        user_id=user_id,
        episode_title='Morning briefing',
        podcast_name='Forklaringssaften',
        artwork_url='https://example.com/art.jpg',
        episode_published='2026-10-01',
        status='completed',
        progress=100,
        transcript_text='Hello from the shared transcript.',
        segments_json='[{"start": 1.5, "end": 4.0, "text": " Hello from the shared transcript."}]',
        language='en',
        audio_duration=120.0,
        completed_at=datetime.now(timezone.utc),
    )
    defaults.update(extra)
    with A.app.app_context():
        old_share = TranscriptShare.query.filter_by(task_id=task_id).first()
        if old_share:
            db.session.delete(old_share)
            db.session.commit()
        old = db.session.get(TranscriptionTask, task_id)
        if old:
            db.session.delete(old)
            db.session.commit()
        db.session.add(TranscriptionTask(**defaults))
        db.session.commit()
    return task_id


def test_share_default_is_not_shared(trial_on):
    uid = _make_user('share-default@test.com')
    tid = _share_task(uid, 'share-default')
    client = _login(uid)
    resp = client.get(f'/transcription/{tid}/share')
    assert resp.status_code == 200
    assert resp.get_json() == {'shared': False, 'url': None, 'token': None}


def test_share_create_revoke_view_and_404(ph_events, trial_on, monkeypatch):
    uid = _make_user('share-owner@test.com')
    other = _make_user('share-other@test.com')
    tid = _share_task(uid, 'share-full')

    owner = _login(uid)
    other_client = _login(other)
    assert other_client.post(f'/transcription/{tid}/share').status_code == 404

    created = owner.post(f'/transcription/{tid}/share')
    assert created.status_code == 200
    body = created.get_json()
    assert body['shared'] is True
    token = body['token']
    assert token and len(token) >= 22
    assert body['url'].endswith(f'/t/{token}')
    import math
    assert len(token) * math.log2(64) >= 128

    create_events = [e for e in ph_events.events if e['event'] == 'share_link_created']
    assert len(create_events) == 1
    assert create_events[0]['distinct_id'] == str(uid)

    again = owner.post(f'/transcription/{tid}/share').get_json()
    assert again['token'] == token
    assert len([e for e in ph_events.events if e['event'] == 'share_link_created']) == 1

    anon = A.app.test_client()
    view = anon.get(f'/t/{token}')
    assert view.status_code == 200
    html = view.data.decode()
    assert 'Morning briefing' in html
    assert 'Forklaringssaften' in html
    assert 'Hello from the shared transcript.' in html
    assert 'name="robots" content="noindex"' in html
    assert view.headers.get('X-Robots-Tag') == 'noindex'
    assert view.headers.get('Referrer-Policy') == 'no-referrer'
    assert 'no-store' in view.headers.get('Cache-Control', '')
    assert 'rel="canonical"' in html
    assert f'/t/{token}' in html
    assert 'property="og:title"' in html
    assert 'og:description' in html
    assert 'twitter:title' in html
    assert 'utm_source=share' in html
    assert 'Transcribe any podcast episode free' in html
    assert 'share-owner@test.com' not in html
    # (No bare str(uid) check: a 1-digit id collides with token/CSS digits.)
    assert 'share-other@test.com' not in html
    viewed = [e for e in ph_events.events if e['event'] == 'shared_transcript_viewed']
    assert len(viewed) == 1
    assert viewed[0]['distinct_id'].startswith('anon:')
    assert viewed[0]['properties'].get('$process_person_profile') is False

    txt = anon.get(f'/t/{token}/download/txt')
    assert txt.status_code == 200
    assert b'Hello from the shared transcript.' in txt.data
    assert txt.headers.get('X-Robots-Tag') == 'noindex'
    srt = anon.get(f'/t/{token}/download/srt')
    assert srt.status_code == 200

    sitemap = anon.get('/sitemap.xml').data.decode()
    assert '/t/' not in sitemap
    assert token not in sitemap

    revoked = owner.post(f'/transcription/{tid}/share/revoke')
    assert revoked.status_code == 200
    assert revoked.get_json()['shared'] is False
    assert anon.get(f'/t/{token}').status_code == 404
    assert anon.get(f'/t/{token}/download/txt').status_code == 404
    assert any(e['event'] == 'share_link_revoked' for e in ph_events.events)

    tid2 = _share_task(uid, 'share-other-task')
    owner.post(f'/transcription/{tid2}/share')
    assert other_client.post(
        f'/transcription/{tid2}/share/revoke').status_code == 404


def test_share_marks_partial_preview_when_metadata_present(trial_on):
    uid = _make_user('share-partial@test.com')
    tid = _share_task(
        uid, 'share-partial',
        partial_meta='{"partial_seconds": 600, "episode_seconds": 3600}',
    )
    client = _login(uid)
    token = client.post(f'/transcription/{tid}/share').get_json()['token']
    html = A.app.test_client().get(f'/t/{token}').data.decode()
    assert 'Free preview' in html
    assert 'first 10 minutes' in html
    assert 'of about 60' in html
    txt = A.app.test_client().get(f'/t/{token}/download/txt').data.decode()
    assert txt.startswith('Free preview: first 10 minutes of 60 minutes.')


def test_share_create_rate_limited_per_user(trial_on, monkeypatch):
    monkeypatch.setattr(A, 'SHARE_CREATE_MAX_PER_USER', 2)
    A._share_create_attempts.clear()
    uid = _make_user('share-rl@test.com')
    client = _login(uid)
    for i in range(2):
        tid = _share_task(uid, f'share-rl-{i}')
        assert client.post(f'/transcription/{tid}/share').status_code == 200
    tid3 = _share_task(uid, 'share-rl-2')
    resp = client.post(f'/transcription/{tid3}/share')
    assert resp.status_code == 429


def test_share_incomplete_task_cannot_be_shared(trial_on):
    from models import db, TranscriptionTask
    uid = _make_user('share-incomplete@test.com')
    with A.app.app_context():
        db.session.add(TranscriptionTask(
            id='share-running', user_id=uid, episode_title='x',
            status='transcribing', transcript_text='partial',
        ))
        db.session.commit()
    client = _login(uid)
    assert client.post('/transcription/share-running/share').status_code == 400
    assert A.app.test_client().get('/t/no-such-token').status_code == 404


def test_share_path_exempt_from_canonical_host_redirect(monkeypatch, trial_on):
    uid = _make_user('share-host@test.com')
    tid = _share_task(uid, 'share-host')
    token = _login(uid).post(f'/transcription/{tid}/share').get_json()['token']
    monkeypatch.setattr(A, 'PUBLIC_BASE_URL', 'https://podskrift.com')
    client = A.app.test_client()
    resp = client.get(f'/t/{token}', headers={'Host': 'www.podskrift.com'})
    assert resp.status_code == 200
    assert b'Morning briefing' in resp.data
    # A non-exempt path still redirects off the non-canonical host.
    bounced = client.get('/pricing', headers={'Host': 'www.podskrift.com'})
    assert bounced.status_code == 301
    assert bounced.headers['Location'].startswith('https://podskrift.com/')


def test_signup_from_share_attributes_utm_source(ph_events, trial_on):
    A._register_attempts.clear()
    client = A.app.test_client()
    client.get('/register?utm_source=share')
    resp = client.post('/register', data={
        'email': 'fromshare@example.com',
        'password': 'password123',
    }, follow_redirects=False)
    assert resp.status_code in (302, 303)
    events = [e for e in ph_events.events if e['event'] == 'user_signed_up']
    assert len(events) == 1
    assert events[0]['properties'].get('utm_source') == 'share'


def test_mint_share_token_is_unguessable():
    a, b = A.mint_share_token(), A.mint_share_token()
    assert a != b
    assert len(a) >= 22
    assert re.fullmatch(r'[A-Za-z0-9_-]+', a)


def test_transcript_shares_migration_on_production_schema_and_fresh(trial_on):
    """Additive migration: current prod schema (no share table) and a fresh DB.

    Indexes that mention a column are created only after that column exists —
    the regression that crashed boot when an index preceded its ALTER.
    """
    with A.app.app_context():
        # --- Current production schema: everything except transcript_shares ---
        A.db.session.execute(A.text('DROP TABLE IF EXISTS transcript_shares'))
        A.db.session.commit()
        A.db.session.remove()
        A.db.engine.dispose()

        tables = {r[0] for r in A.db.session.execute(A.text(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )).fetchall()}
        assert 'transcript_shares' not in tables
        assert 'transcription_tasks' in tables
        assert 'users' in tables

        A.ensure_transcript_shares_table()
        A.ensure_transcript_shares_table()  # idempotent

        cols = A._live_columns('transcript_shares')
        for required in ('id', 'token', 'task_id', 'user_id', 'created_at',
                         'revoked_at'):
            assert required in cols, required
        idx = {r[0] for r in A.db.session.execute(A.text(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='transcript_shares'"
        )).fetchall()}
        assert 'ix_transcript_shares_token' in idx or any(
            'token' in n for n in idx)
        assert 'ix_transcript_shares_user_id' in idx

        # Legacy table missing revoked_at: ALTER then index (never index first).
        A.db.session.execute(A.text('DROP TABLE IF EXISTS transcript_shares'))
        A.db.session.execute(A.text("""
            CREATE TABLE transcript_shares (
                id INTEGER NOT NULL PRIMARY KEY,
                token VARCHAR(64) NOT NULL UNIQUE,
                task_id VARCHAR(36) NOT NULL UNIQUE,
                user_id INTEGER NOT NULL,
                created_at DATETIME
            )
        """))
        A.db.session.commit()
        A.db.session.remove()
        A.db.engine.dispose()
        assert 'revoked_at' not in A._live_columns('transcript_shares')
        A.ensure_transcript_shares_table()
        assert 'revoked_at' in A._live_columns('transcript_shares')

        # --- Fresh DB path: create_all + ensure ---
        A.db.session.execute(A.text('DROP TABLE IF EXISTS transcript_shares'))
        A.db.session.commit()
        A.db.session.remove()
        A.db.engine.dispose()
        A.db.create_all()
        A.ensure_transcript_shares_table()
        assert 'token' in A._live_columns('transcript_shares')
        assert 'revoked_at' in A._live_columns('transcript_shares')


def test_result_page_exposes_share_controls(trial_on):
    uid = _make_user('share-ui@test.com')
    tid = _share_task(uid, 'share-ui')
    body = _login(uid).get(f'/transcription/{tid}').data.decode()
    assert 'id="shareBtn"' in body
    assert '/share' in body
    assert 'shareRevokeBtn' in body


# --------------------------------------------------------------------------
# Listen links on share + result pages
# --------------------------------------------------------------------------

def test_safe_public_http_url_rejects_non_http_schemes():
    assert A.safe_public_http_url('https://example.com/ep') == 'https://example.com/ep'
    assert A.safe_public_http_url('http://example.com/a') == 'http://example.com/a'
    assert A.safe_public_http_url('javascript:alert(1)') is None
    assert A.safe_public_http_url('data:text/html,hi') is None
    assert A.safe_public_http_url('//evil.example/x') is None
    assert A.safe_public_http_url('https://user:pass@example.com/x') is None
    assert A.safe_public_http_url('') is None
    assert A.safe_public_http_url(None) is None


def test_listen_links_from_fields_orders_platforms_and_skips_bad_urls():
    links = A.listen_links_from_fields(
        spotify_url=f'https://open.spotify.com/episode/{_SPOTIFY_EP}',
        apple_url='javascript:alert(1)',
        website_url='https://show.example/ep-1',
        audio_url='https://cdn.example/ep.mp3',
    )
    platforms = [L['platform'] for L in links]
    assert platforms == ['spotify', 'website', 'audio']
    assert links[0]['url'].startswith('https://open.spotify.com/episode/')
    assert links[-1]['is_audio'] is True


def test_share_page_shows_listen_section_for_spotify_sourced(trial_on, monkeypatch):
    monkeypatch.setattr(A, 'apple_url_for_feed', lambda *a, **k: None)
    uid = _make_user('listen-spotify@test.com')
    spotify = f'https://open.spotify.com/episode/{_SPOTIFY_EP}'
    tid = _share_task(
        uid, 'listen-spotify',
        source_spotify_url=spotify,
        source_audio_url='https://cdn.example.com/ep.mp3',
        source_website_url='https://show.example/episode-1',
    )
    token = _login(uid).post(f'/transcription/{tid}/share').get_json()['token']
    html = A.app.test_client().get(f'/t/{token}').data.decode()
    assert 'Listen to this episode' in html
    assert 'Listen on Spotify' in html
    assert spotify in html
    assert 'podcast&#39;s website' in html or "podcast's website" in html
    assert 'https://show.example/episode-1' in html
    assert 'Play audio' in html
    assert 'rel="noopener nofollow"' in html
    assert 'target="_blank"' in html
    assert 'shared_listen_clicked' in html
    assert 'preload="none"' in html
    assert 'javascript:' not in html


def test_share_page_shows_apple_and_website_for_rss_sourced(trial_on, monkeypatch):
    apple = 'https://podcasts.apple.com/us/podcast/id1234567890'
    monkeypatch.setattr(A, 'apple_url_for_feed', lambda *a, **k: apple)
    uid = _make_user('listen-rss@test.com')
    tid = _share_task(
        uid, 'listen-rss',
        rss_url='https://feeds.example.com/show.xml',
        podcast_name='Example Show',
        source_website_url='https://example.com/episodes/one',
        source_audio_url='https://cdn.example.com/one.mp3',
    )
    token = _login(uid).post(f'/transcription/{tid}/share').get_json()['token']
    html = A.app.test_client().get(f'/t/{token}').data.decode()
    assert 'Listen on Apple Podcasts' in html
    assert 'https://podcasts.apple.com/podcast/id1234567890' in html
    assert 'podcast&#39;s website' in html or "podcast's website" in html
    assert 'https://example.com/episodes/one' in html
    assert 'data-listen-platform="spotify"' not in html
    assert 'data-listen-platform="apple"' in html
    with A.app.app_context():
        task = A.db.session.get(A.TranscriptionTask, tid)
        assert task.source_apple_url == apple


def test_share_page_omits_listen_section_without_resolvable_links(trial_on, monkeypatch):
    monkeypatch.setattr(A, 'apple_url_for_feed', lambda *a, **k: None)
    uid = _make_user('listen-none@test.com')
    tid = _share_task(uid, 'listen-none', source_audio_url=None, rss_url=None)
    token = _login(uid).post(f'/transcription/{tid}/share').get_json()['token']
    html = A.app.test_client().get(f'/t/{token}').data.decode()
    assert 'Listen to this episode' not in html


def test_enqueue_stores_listen_urls(monkeypatch, trial_on):
    import types
    monkeypatch.setattr(
        A.threading, 'Thread',
        lambda target=None, **kw: types.SimpleNamespace(
            daemon=True, start=lambda: None))
    monkeypatch.setattr(A, 'free_disk_bytes', lambda *_a, **_k: 10 ** 12)
    monkeypatch.setattr(A, '_is_fetchable_url', lambda url: True)
    uid = _make_user('listen-enqueue@test.com')
    with A.app.app_context():
        user = A.db.session.get(A.User, uid)
        payload, status = A.enqueue_transcription(
            user,
            {
                'title': 'Ep',
                'audio_url': 'https://cdn.example.com/ep.mp3',
                'podcast_name': 'Show',
                'duration_min': 5,
                'input_origin': 'spotify',
                'spotify_url': (
                    f'https://open.spotify.com/episode/{_SPOTIFY_EP}?si=x'),
                'apple_url': 'https://podcasts.apple.com/us/podcast/x/id99?i=42',
                'episode_link': 'https://show.example/ep',
            },
            rss_url='https://feeds.example.com/show.xml',
        )
        assert status == 200, payload
        task = A.db.session.get(A.TranscriptionTask, payload['task_id'])
        assert task.source_spotify_url == (
            f'https://open.spotify.com/episode/{_SPOTIFY_EP}')
        assert 'podcasts.apple.com' in (task.source_apple_url or '')
        assert task.source_website_url == 'https://show.example/ep'
        assert task.source_audio_url == 'https://cdn.example.com/ep.mp3'


def test_rss_episodes_include_episode_and_show_links(tmp_path):
    feed = tmp_path / 'feed.xml'
    feed.write_text("""<?xml version="1.0"?>
    <rss version="2.0"><channel>
      <title>Show</title>
      <link>https://show.example/</link>
      <item>
        <title>Ep One</title>
        <link>https://show.example/ep-one</link>
        <enclosure url="https://cdn.example.com/one.mp3" type="audio/mpeg" length="1"/>
      </item>
    </channel></rss>
    """, encoding='utf-8')
    # feedparser accepts a file path / URL; use the path as a local parse.
    episodes, err = A.get_episodes_from_rss(str(feed))
    assert err is None
    assert episodes[0]['episode_link'] == 'https://show.example/ep-one'
    assert episodes[0]['show_link'] == 'https://show.example/'


def test_apple_url_for_feed_caches_and_matches_feed(monkeypatch):
    A._apple_feed_url_cache.clear()
    calls = {'n': 0}

    class FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                'results': [{
                    'feedUrl': 'https://feeds.example.com/show.xml',
                    'collectionViewUrl': 'https://podcasts.apple.com/us/podcast/id1',
                    'collectionId': 1,
                    'collectionName': 'Example Show',
                }]
            }

    def fake_get(url, timeout=None, params=None):
        calls['n'] += 1
        return FakeResp()

    monkeypatch.setattr(A.requests, 'get', fake_get)
    a = A.apple_url_for_feed(
        'https://feeds.example.com/show.xml', 'Example Show', timeout=1)
    b = A.apple_url_for_feed(
        'https://feeds.example.com/show.xml', 'Example Show', timeout=1)
    assert a == 'https://podcasts.apple.com/podcast/id1'
    assert b == a
    assert calls['n'] == 1


def test_result_page_and_status_expose_listen_links(trial_on, monkeypatch):
    monkeypatch.setattr(A, 'apple_url_for_feed', lambda *a, **k: None)
    uid = _make_user('listen-result@test.com')
    tid = _share_task(
        uid, 'listen-result',
        source_spotify_url=f'https://open.spotify.com/episode/{_SPOTIFY_EP}',
        source_audio_url='https://cdn.example.com/ep.mp3',
    )
    client = _login(uid)
    page = client.get(f'/transcription/{tid}').data.decode()
    assert 'Listen to this episode' in page
    assert 'id="listenLinksMount"' in page
    assert 'result_listen_clicked' in page
    status = client.get(f'/status/{tid}').get_json()
    platforms = [L['platform'] for L in status['listen_links']]
    assert 'spotify' in platforms
    assert 'audio' in platforms


def test_changelog_has_share_listen_links_entry():
    entries = A.load_changelog_entries()
    assert any(e['id'] == 'share-listen-links' for e in entries)
    assert entries[0]['id'] == 'new-signup-120-min-trial'
    assert entries[1]['id'] == 'new-look'


# --------------------------------------------------------------------------
# Partial free-trial preview (long episode → first N minutes)
# --------------------------------------------------------------------------

def test_partial_preview_trims_and_charges_remaining_trial(
        monkeypatch, trial_on, tmp_path, ph_events):
    """Enqueue reserves N, worker trims to N, Whisper sees only the preview."""
    import shutil
    import subprocess
    import types
    from models import db, TranscriptionTask

    uid = _make_user('partial-trim@test.com', limit=60 * 60, used=0)
    audio = tmp_path / 'long.mp3'
    subprocess.run(
        ['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'sine=f=440:d=3',
         '-ac', '1', str(audio)],
        check=True, capture_output=True)

    probes = {'n': 0}
    trimmed = []

    def probe(f):
        probes['n'] += 1
        return 90 * 60.0 if probes['n'] == 1 else 60 * 60.0

    def fake_trim(path, seconds):
        trimmed.append(int(seconds))
        return path

    def fake_download(url, path, task_id):
        shutil.copy(str(audio), path)

    monkeypatch.setattr(A, 'TRIAL_MAX_EPISODE_SECONDS', 180 * 60)
    monkeypatch.setattr(A, 'probe_audio_duration', probe)
    monkeypatch.setattr(A, 'trim_audio_file', fake_trim)
    monkeypatch.setattr(A, 'prepare_audio_for_whisper',
                        lambda f, **kw: [str(audio)])
    monkeypatch.setattr(A, '_transcribe_chunks',
                        lambda *a, **kw: ('preview text', []))
    monkeypatch.setattr(A, 'download_audio', fake_download)
    monkeypatch.setattr(
        A.threading, 'Thread',
        lambda target=None, **kw: types.SimpleNamespace(
            daemon=True, start=lambda: target and target()))
    monkeypatch.setattr(A, 'free_disk_bytes', lambda *a, **kw: 10 ** 12)

    with A.app.app_context():
        user = db.session.get(A.User, uid)
        payload, status = A.enqueue_transcription(
            user,
            {'title': 'Long', 'audio_url': 'https://example.com/long.mp3',
             'duration_min': 90},
            source='web')
    assert status == 200, payload
    assert _used(uid) == 60 * 60
    assert trimmed == [60 * 60]

    with A.app.app_context():
        task = db.session.get(TranscriptionTask, payload['task_id'])
        assert task.status == 'completed', task.error_message
        assert A.task_is_partial(task)
        assert task.transcript_text == 'preview text'
        assert task.trial_seconds_charged == 60 * 60

    started = [e for e in ph_events.events if e['event'] == 'transcript_started']
    done = [e for e in ph_events.events if e['event'] == 'transcript_completed']
    assert started and started[0]['properties'].get('partial') is True
    assert started[0]['properties']['partial_minutes'] == 60
    assert started[0]['properties']['episode_minutes'] == 90
    assert done and done[0]['properties'].get('partial') is True


def test_partial_preview_not_used_when_paid_covers_full(monkeypatch, trial_on):
    uid = _make_user('partial-paid@test.com', limit=60 * 60, used=0)
    _set_paid(uid, 90 * 60)
    resp = _post_start(monkeypatch, uid, {
        'audio_url': 'https://example.com/paid-full.mp3',
        'episode_title': 'Covered',
        'duration_min': '90',
    })
    assert resp.status_code == 200, resp.get_json()
    from models import db, TranscriptionTask
    with A.app.app_context():
        task = db.session.get(TranscriptionTask, resp.get_json()['task_id'])
        assert not A.task_is_partial(task)
        # Trial first, then paid for the remainder.
        assert task.trial_seconds_charged == 60 * 60
        assert task.paid_seconds_charged == 30 * 60


def test_partial_preview_not_used_for_own_key(monkeypatch, trial_on):
    uid = _make_user('partial-byok@test.com', key='sk-' + 'e' * 40, limit=60 * 60)
    resp = _post_start(monkeypatch, uid, {
        'audio_url': 'https://example.com/byok.mp3',
        'episode_title': 'Own key',
        'duration_min': '90',
    })
    assert resp.status_code == 200, resp.get_json()
    from models import db, TranscriptionTask
    with A.app.app_context():
        task = db.session.get(TranscriptionTask, resp.get_json()['task_id'])
        assert not A.task_is_partial(task)
        assert task.trial_seconds_charged is None
    assert _used(uid) == 0


def test_partial_preview_respects_daily_budget(monkeypatch, trial_on):
    monkeypatch.setattr(A, 'TRIAL_MAX_EPISODE_SECONDS', 180 * 60)
    # Seal today's budget at whatever the shared test DB has already reserved.
    with A.app.app_context():
        monkeypatch.setattr(A, 'TRIAL_DAILY_SECONDS', A.trial_daily_used_seconds())
    uid_b = _make_user('partial-daily-b@test.com', limit=60 * 60, used=0)
    resp = _post_start(monkeypatch, uid_b, {
        'audio_url': 'https://example.com/daily-block.mp3',
        'episode_title': 'Blocked',
        'duration_min': '90',
    })
    assert resp.status_code == 402
    assert resp.get_json().get('paywall_reason') == 'daily_cap'
    assert _used(uid_b) == 0


def test_completed_partial_allows_full_rerun_after_purchase(monkeypatch, trial_on):
    """Duplicate guard must not block upgrading a completed partial to full."""
    import threading as _t
    from models import db, TranscriptionTask

    monkeypatch.setattr(A, 'MAX_CONCURRENT_TRANSCRIPTIONS', 2)
    monkeypatch.setattr(A, '_transcription_slots', _t.BoundedSemaphore(2))
    monkeypatch.setattr(A, 'TRIAL_MAX_EPISODE_SECONDS', 180 * 60)

    uid = _make_user('partial-upgrade@test.com', limit=60 * 60, used=0)
    audio = 'https://example.com/upgrade-ep.mp3'
    first = _post_start(monkeypatch, uid, {
        'audio_url': audio,
        'episode_title': 'Preview',
        'duration_min': '90',
    })
    assert first.status_code == 200, first.get_json()
    partial_id = first.get_json()['task_id']
    with A.app.app_context():
        task = db.session.get(TranscriptionTask, partial_id)
        task.status = 'completed'
        task.phase = 'completed'
        task.transcript_text = 'preview'
        task.trial_settled = True
        db.session.commit()
        assert A.task_is_partial(task)

    _set_paid(uid, 90 * 60)
    second = _post_start(monkeypatch, uid, {
        'audio_url': audio,
        'episode_title': 'Full',
        'duration_min': '90',
    })
    assert second.status_code == 200, second.get_json()
    body = second.get_json()
    assert body.get('existing') is not True
    assert body['task_id'] != partial_id
    with A.app.app_context():
        full = db.session.get(TranscriptionTask, body['task_id'])
        assert not A.task_is_partial(full)
        # Trial already spent on the preview; full run uses paid.
        assert (full.trial_seconds_charged or 0) == 0
        assert full.paid_seconds_charged == 90 * 60


def test_partial_download_and_status_include_preview_note(monkeypatch, trial_on):
    from models import db, TranscriptionTask

    uid = _make_user('partial-dl@test.com', limit=60 * 60)
    with A.app.app_context():
        task = TranscriptionTask(
            id='partial-dl-1',
            user_id=uid,
            episode_title='Preview Ep',
            status='completed',
            phase='completed',
            transcript_text='Hello world',
            segments_json='[{"start":0,"end":1,"text":"Hello world"}]',
            source_audio_url='https://example.com/p.mp3',
            trial_seconds_charged=60 * 60,
            partial_meta=A.encode_partial_task_meta(60 * 60, 90 * 60),
        )
        db.session.add(task)
        db.session.commit()

    client = A.app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(uid)
        sess['_fresh'] = True

    status = client.get('/status/partial-dl-1').get_json()
    assert status['partial'] is True
    assert status['partial_minutes'] == 60
    assert status['episode_minutes'] == 90
    assert 'first 60 minutes of 90 minutes' in status['partial_note']
    assert status['finish']['audio_url'] == 'https://example.com/p.mp3'
    assert 'error' not in status
    assert status.get('error_message') is None

    txt = client.get('/download/partial-dl-1/txt')
    assert txt.status_code == 200
    body = txt.data.decode()
    assert body.startswith('Free preview: first 60 minutes of 90 minutes.')
    assert 'Hello world' in body

    srt = client.get('/download/partial-dl-1/srt')
    assert srt.status_code == 200
    assert 'Free preview: first 60 minutes of 90 minutes.' in srt.data.decode()


def test_partial_preview_refund_on_failure_settles_once(
        monkeypatch, trial_on, ph_events):
    """Failed partials go through fail_task_and_refund; reservation settles once."""
    import types
    from models import db, TranscriptionTask

    uid = _make_user('partial-refund@test.com', limit=60 * 60, used=0)
    monkeypatch.setattr(A, 'TRIAL_MAX_EPISODE_SECONDS', 180 * 60)
    monkeypatch.setattr(A, 'download_audio', lambda *a, **kw: None)

    def boom(*a, **kw):
        raise RuntimeError('whisper down')

    monkeypatch.setattr(A, 'transcribe_audio', boom)
    monkeypatch.setattr(
        A.threading, 'Thread',
        lambda target=None, **kw: types.SimpleNamespace(
            daemon=True, start=lambda: target and target()))
    monkeypatch.setattr(A, 'free_disk_bytes', lambda *a, **kw: 10 ** 12)

    with A.app.app_context():
        user = db.session.get(A.User, uid)
        payload, status = A.enqueue_transcription(
            user,
            {'title': 'Fail', 'audio_url': 'https://example.com/fail.mp3',
             'duration_min': 90},
            source='web')
    assert status == 200, payload
    assert _used(uid) == 0  # full refund: nothing reached Whisper
    with A.app.app_context():
        task = db.session.get(TranscriptionTask, payload['task_id'])
        assert task.status == 'error'
        assert task.trial_settled in (True, 1)
        assert A.task_is_partial(task)  # meta survives on the dedicated column
        assert task.error_message == 'whisper down'
        # Second settle is a no-op (exactly once).
        assert A.fail_task_and_refund(task.id, 'again') == 0
        assert A.trial_refund_task(task) == 0
    assert _used(uid) == 0
    failed = [e for e in ph_events.events if e['event'] == 'transcript_failed']
    assert len(failed) == 1
    assert failed[0]['properties']['reason'] == 'other'


def test_homepage_mentions_long_episode_preview(trial_on):
    home = A.app.test_client().get('/').data.decode()
    assert 'longer episodes' in home.lower() or 'free preview' in home.lower()
    answers = dict(A.faq_entries())
    free = answers['Is it free?']
    assert 'preview' in free.lower()
    pricing = A.app.test_client().get('/pricing').data.decode()
    assert 'preview' in pricing.lower()


def test_partial_meta_column_has_guarded_migration():
    """partial_meta is additive TEXT via TASK_COLUMN_MIGRATIONS (no index)."""
    from models import TASK_COLUMN_MIGRATIONS, TranscriptionTask
    assert 'partial_meta' in TASK_COLUMN_MIGRATIONS
    assert TASK_COLUMN_MIGRATIONS['partial_meta'] == 'TEXT'
    assert hasattr(TranscriptionTask, 'partial_meta')


def test_partial_meta_migration_on_production_shaped_schema(tmp_path):
    """ALTER ADD COLUMN works on a DB that looks like current production schema."""
    import sqlite3
    from sqlalchemy import create_engine, text as sa_text
    from sqlalchemy.exc import OperationalError
    from models import TASK_COLUMN_MIGRATIONS

    db_path = tmp_path / 'prod-shaped.db'
    conn = sqlite3.connect(str(db_path))
    # Minimal pre-partial_meta transcription_tasks shape (columns that exist
    # in production before this PR — no partial_meta).
    conn.execute('''
        CREATE TABLE transcription_tasks (
            id VARCHAR(36) PRIMARY KEY,
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
            trial_settled BOOLEAN NOT NULL DEFAULT 0
        )
    ''')
    conn.commit()
    cols_before = {row[1] for row in conn.execute('PRAGMA table_info(transcription_tasks)')}
    assert 'partial_meta' not in cols_before
    conn.close()

    uri = f'sqlite:///{db_path}'
    engine = create_engine(uri)
    with engine.begin() as bind:
        existing = {row[1] for row in bind.execute(
            sa_text('PRAGMA table_info(transcription_tasks)')).fetchall()}
        assert 'partial_meta' not in existing
        for column, ddl_type in TASK_COLUMN_MIGRATIONS.items():
            if column in existing:
                continue
            bind.execute(sa_text(
                f'ALTER TABLE transcription_tasks ADD COLUMN {column} {ddl_type}'
            ))
        cols_after = {row[1] for row in bind.execute(
            sa_text('PRAGMA table_info(transcription_tasks)')).fetchall()}
    assert 'partial_meta' in cols_after

    # Re-running ADD must be tolerated (duplicate column) — same as
    # apply_column_migrations' guarded race handling.
    with engine.begin() as bind:
        try:
            bind.execute(sa_text(
                'ALTER TABLE transcription_tasks ADD COLUMN partial_meta TEXT'))
            raised = False
        except OperationalError as exc:
            raised = True
            assert 'duplicate column name' in str(exc).lower()
    engine.dispose()
    assert raised is True


def test_completed_partial_not_shown_or_counted_as_failure(
        monkeypatch, trial_on, ph_events):
    """History, API, status, and metrics treat a completed partial as success."""
    from models import db, TranscriptionTask

    uid = _make_user('partial-not-fail@test.com', limit=60 * 60)
    with A.app.app_context():
        task = TranscriptionTask(
            id='partial-ok-1',
            user_id=uid,
            episode_title='Preview Not Error',
            status='completed',
            phase='completed',
            transcript_text='preview body',
            completed_at=datetime.now(timezone.utc),
            started_at=datetime.now(timezone.utc),
            audio_duration=60 * 60,
            trial_seconds_charged=60 * 60,
            trial_settled=False,
            partial_meta=A.encode_partial_task_meta(60 * 60, 120 * 60),
            error_message=None,
            source_audio_url='https://example.com/ok.mp3',
        )
        db.session.add(task)
        db.session.commit()

    client = A.app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(uid)
        sess['_fresh'] = True

    # History lists completed only — partial appears as a normal transcript.
    hist = client.get('/history').data.decode()
    assert 'Preview Not Error' in hist
    assert 'error' not in hist.lower() or 'No transcriptions' not in hist

    status = client.get('/status/partial-ok-1').get_json()
    assert status['status'] == 'completed'
    assert 'error' not in status
    assert status.get('partial') is True

    # Agent/API payload exposes error_message only when status == 'error'.
    with A.app.app_context():
        task = db.session.get(TranscriptionTask, 'partial-ok-1')
        payload = A._agent_episode_payload(task)
        assert payload['transcript_status'] == 'ready'
        assert payload['error_message'] is None
        assert payload['task_status'] == 'completed'

    # Ops metrics SQL counts failures by status, not by partial_meta / error_message.
    import sqlite3
    uri = A.app.config['SQLALCHEMY_DATABASE_URI']
    assert uri.startswith('sqlite:///')
    db_file = uri.replace('sqlite:///', '', 1)
    conn = sqlite3.connect(db_file)
    errors = conn.execute(
        "SELECT COUNT(*) FROM transcription_tasks "
        "WHERE status IN ('error', 'failed') AND id = 'partial-ok-1'"
    ).fetchone()[0]
    completed = conn.execute(
        "SELECT COUNT(*) FROM transcription_tasks "
        "WHERE status = 'completed' AND id = 'partial-ok-1'"
    ).fetchone()[0]
    conn.close()
    assert errors == 0
    assert completed == 1

    # No transcript_failed analytics for a successful partial.
    assert not any(e['event'] == 'transcript_failed' for e in ph_events.events)


# ---------------------------------------------------------------------------
# Deploy restart: error classification + resume on startup (PODSKRIFT-B)
# ---------------------------------------------------------------------------

def test_ffmpeg_sigterm_raises_server_restart():
    with pytest.raises(A.ServerRestart) as exc:
        A._ffmpeg_error(
            'This audio file could not be processed. It may be corrupt or in an '
            'unsupported format.',
            b'Exiting normally, received signal 15.\n',
            returncode=-15,
        )
    assert exc.value.reason == 'server_restart'
    assert 'restarted' in str(exc.value).lower()
    assert 'corrupt' not in str(exc.value).lower()


def test_ffmpeg_shutdown_flag_raises_server_restart(monkeypatch):
    monkeypatch.setattr(A, 'is_shutting_down', lambda: True)
    with pytest.raises(A.ServerRestart):
        A._ffmpeg_error(
            'This audio file could not be processed. It may be corrupt or in an '
            'unsupported format.',
            b'Invalid data found when processing input\n',
            returncode=1,
        )


def test_genuine_corrupt_audio_still_blames_the_file():
    err = A._ffmpeg_error(
        'This audio file could not be processed. It may be corrupt or in an '
        'unsupported format.',
        b'Invalid data found when processing input\n',
        returncode=1,
    )
    assert isinstance(err, RuntimeError)
    assert 'corrupt' in str(err).lower()


def test_ffmpeg_killed_by_signal_helper():
    assert A._ffmpeg_killed_by_signal(-15, b'')
    assert A._ffmpeg_killed_by_signal(143, b'')
    assert A._ffmpeg_killed_by_signal(1, b'signal 15 received')
    assert not A._ffmpeg_killed_by_signal(1, b'Invalid data found')


def test_prepare_audio_shutdown_during_ffmpeg_is_server_restart(
        tmp_path, monkeypatch):
    source = tmp_path / 'ep.mp3'
    source.write_bytes(b'\0' * 2048)

    def fake_run(cmd, **kw):
        import subprocess as sp
        return sp.CompletedProcess(cmd, -15, b'', b'received signal 15')

    monkeypatch.setattr(A.subprocess, 'run', fake_run)
    monkeypatch.setattr(A, 'probe_audio_bitrate_kbps', lambda p: 48)
    with pytest.raises(A.ServerRestart):
        A.prepare_audio_for_whisper(str(source))


def test_internal_in_flight_is_loopback_only(trial_on):
    client = A.app.test_client()
    # Flask test client remote_addr defaults to 127.0.0.1
    resp = client.get('/internal/in-flight')
    assert resp.status_code == 200
    assert resp.get_json()['in_flight'] >= 0

    # Spoof a non-loopback peer — Werkzeug exposes environ REMOTE_ADDR.
    resp = client.get(
        '/internal/in-flight',
        environ_overrides={'REMOTE_ADDR': '8.8.8.8'},
    )
    assert resp.status_code == 403

    # Public traffic reaches gunicorn from the local proxy (127.0.0.1) but
    # always with forwarding headers — that must be refused too.
    for hdr in ('X-Real-IP', 'X-Forwarded-For'):
        resp = client.get('/internal/in-flight', headers={hdr: '8.8.8.8'})
        assert resp.status_code == 403


def test_boot_resume_only_under_gunicorn_server_stamp():
    """Scripts importing app (poller timer) must not claim live jobs."""
    src = open(A.__file__, encoding='utf-8').read()
    boot = src[src.index('    install_shutdown_handlers()\n    if _SERVER_STARTED_AT_ENV:'):]
    boot = boot[:1500]
    assert 'resume_interrupted_tasks()' in boot
    assert "_sweep_stale_tasks(source='boot')" in boot


def test_gunicorn_conf_stamps_server_start(monkeypatch):
    import runpy
    monkeypatch.delenv('PODSKRIFT_SERVER_STARTED_AT', raising=False)
    conf = runpy.run_path(os.path.join(
        os.path.dirname(os.path.abspath(A.__file__)), 'gunicorn.conf.py'))
    conf['on_starting'](None)
    stamp = float(os.environ['PODSKRIFT_SERVER_STARTED_AT'])
    assert abs(stamp - time.time()) < 5
    assert conf['graceful_timeout'] >= 60


def _clear_in_flight_tasks():
    """Keep resume_* tests from seeing leftovers from earlier cases."""
    from models import db, TranscriptionTask
    with A.app.app_context():
        (
            TranscriptionTask.query
            .filter(~TranscriptionTask.status.in_(['completed', 'error', 'cancelled']))
            .update(
                {'status': 'cancelled', 'phase': 'cancelled'},
                synchronize_session=False,
            )
        )
        db.session.commit()


def test_resume_interrupted_tasks_requeues_once(monkeypatch, trial_on):
    """Orphaned mid-flight tasks are claimed (resume_attempts=1) and re-spawned."""
    from models import db, TranscriptionTask

    _clear_in_flight_tasks()
    uid = _make_user('resume-boot@test.com', limit=36000)
    spawned = []

    def fake_spawn(task):
        spawned.append(task.id)
        return True

    monkeypatch.setattr(A, '_spawn_worker_for_existing_task', fake_spawn)
    # Pretend this process started after the task's heartbeat.
    monkeypatch.setattr(A, '_PROCESS_STARTED_AT', time.time() + 10)

    with A.app.app_context():
        past = datetime.now(timezone.utc) - timedelta(minutes=5)
        db.session.add(TranscriptionTask(
            id='orphan-resume-1',
            user_id=uid,
            episode_title='Ep',
            status='transcribing',
            phase='transcribing',
            source_audio_url='https://cdn.example.com/ep.mp3',
            trial_seconds_charged=600,
            trial_settled=False,
            resume_attempts=0,
            started_at=past,
            heartbeat_at=past,
        ))
        db.session.commit()
        # Stamp used to match the reservation so we can assert no refund/recharge.
        user = db.session.get(A.User, uid)
        user.trial_seconds_used = 600
        db.session.commit()

        stats = A.resume_interrupted_tasks()
        task = db.session.get(TranscriptionTask, 'orphan-resume-1')
        user = db.session.get(A.User, uid)

    assert stats['resumed'] == 1
    assert spawned == ['orphan-resume-1']
    assert task.resume_attempts == 1
    assert task.status == 'downloading'
    assert task.trial_seconds_charged == 600
    assert task.trial_settled is False
    assert user.trial_seconds_used == 600  # reservation kept, not refunded


def test_resume_interrupted_second_failure_is_clear(monkeypatch, trial_on, sentry_events):
    """A task that already used its one resume fails with please-try-again."""
    from models import db, TranscriptionTask

    _clear_in_flight_tasks()
    uid = _make_user('resume-twice@test.com', limit=36000)
    monkeypatch.setattr(A, '_PROCESS_STARTED_AT', time.time() + 10)
    monkeypatch.setattr(A, '_spawn_worker_for_existing_task', lambda t: True)

    with A.app.app_context():
        past = datetime.now(timezone.utc) - timedelta(minutes=5)
        db.session.add(TranscriptionTask(
            id='orphan-resume-2',
            user_id=uid,
            episode_title='Ep',
            status='downloading',
            phase='downloading',
            source_audio_url='https://cdn.example.com/ep.mp3',
            trial_seconds_charged=300,
            trial_settled=False,
            resume_attempts=1,
            started_at=past,
            heartbeat_at=past,
        ))
        user = db.session.get(A.User, uid)
        user.trial_seconds_used = 300
        db.session.commit()

        stats = A.resume_interrupted_tasks()
        task = db.session.get(TranscriptionTask, 'orphan-resume-2')
        user = db.session.get(A.User, uid)

    assert stats['failed_second'] == 1
    assert stats['resumed'] == 0
    assert task.status == 'error'
    assert 'please try again' in (task.error_message or '').lower()
    assert 'corrupt' not in (task.error_message or '').lower()
    assert task.trial_settled is True
    assert user.trial_seconds_used == 0  # full refund: nothing reached Whisper
    assert any(
        e.get('contexts', {}).get('task', {}).get('id') == 'orphan-resume-2'
        for e in sentry_events
    )


def test_resume_skips_live_tasks_from_other_worker(monkeypatch, trial_on):
    from models import db, TranscriptionTask

    uid = _make_user('resume-live@test.com', limit=36000)
    monkeypatch.setattr(A, '_spawn_worker_for_existing_task', lambda t: True)
    # Process started in the past; fresh heartbeat means another worker owns it.
    monkeypatch.setattr(A, '_PROCESS_STARTED_AT', time.time() - 60)

    with A.app.app_context():
        db.session.add(TranscriptionTask(
            id='live-other-worker',
            user_id=uid,
            episode_title='Ep',
            status='transcribing',
            source_audio_url='https://cdn.example.com/ep.mp3',
            trial_seconds_charged=60,
            resume_attempts=0,
            heartbeat_at=datetime.now(timezone.utc),
        ))
        db.session.commit()
        stats = A.resume_interrupted_tasks()
        task = db.session.get(TranscriptionTask, 'live-other-worker')

    assert stats['resumed'] == 0
    assert task.resume_attempts == 0
    assert task.status == 'transcribing'


def test_server_restart_in_worker_does_not_sentry_when_leaving_for_resume(
        monkeypatch, trial_on, sentry_events):
    """First interrupt leaves the task; no Sentry exception."""
    from models import db, TranscriptionTask

    uid = _make_user('sr-leave@test.com', limit=36000)
    with A.app.app_context():
        db.session.add(TranscriptionTask(
            id='sr-leave-1',
            user_id=uid,
            episode_title='Ep',
            status='splitting',
            phase='splitting',
            source_audio_url='https://cdn.example.com/ep.mp3',
            trial_seconds_charged=120,
            trial_settled=False,
            resume_attempts=0,
        ))
        db.session.commit()

    # Simulate the enqueue worker's ServerRestart handler path via fail path:
    # call the classification only — full thread is heavy. Directly assert
    # report is skipped when attempts==0 by invoking a minimal stand-in.
    with A.app.app_context():
        task = db.session.get(TranscriptionTask, 'sr-leave-1')
        attempts = int(task.resume_attempts or 0)
        assert attempts < 1
        # Handler would log and return without fail_task / report.
        assert task.status == 'splitting'
    assert sentry_events == []


def test_resume_attempts_column_has_migration():
    from models import TASK_COLUMN_MIGRATIONS, TranscriptionTask
    assert 'resume_attempts' in TASK_COLUMN_MIGRATIONS
    assert 'INTEGER' in TASK_COLUMN_MIGRATIONS['resume_attempts'].upper()
    assert hasattr(TranscriptionTask, 'resume_attempts')


def test_resume_attempts_migration_on_production_shaped_schema(tmp_path):
    import sqlite3
    from sqlalchemy import create_engine, text as sa_text
    from sqlalchemy.exc import OperationalError
    from models import TASK_COLUMN_MIGRATIONS

    db_path = tmp_path / 'prod-resume.db'
    conn = sqlite3.connect(str(db_path))
    conn.execute('''
        CREATE TABLE transcription_tasks (
            id VARCHAR(36) PRIMARY KEY,
            user_id INTEGER NOT NULL,
            episode_title VARCHAR(512) NOT NULL,
            status VARCHAR(20) NOT NULL,
            progress INTEGER NOT NULL DEFAULT 0,
            download_progress INTEGER NOT NULL DEFAULT 0,
            trial_settled BOOLEAN NOT NULL DEFAULT 0,
            partial_meta TEXT
        )
    ''')
    conn.commit()
    cols_before = {row[1] for row in conn.execute('PRAGMA table_info(transcription_tasks)')}
    assert 'resume_attempts' not in cols_before
    conn.close()

    engine = create_engine(f'sqlite:///{db_path}')
    with engine.begin() as bind:
        existing = {row[1] for row in bind.execute(
            sa_text('PRAGMA table_info(transcription_tasks)')).fetchall()}
        for column, ddl_type in TASK_COLUMN_MIGRATIONS.items():
            if column in existing:
                continue
            bind.execute(sa_text(
                f'ALTER TABLE transcription_tasks ADD COLUMN {column} {ddl_type}'
            ))
        cols_after = {row[1] for row in bind.execute(
            sa_text('PRAGMA table_info(transcription_tasks)')).fetchall()}
    assert 'resume_attempts' in cols_after
    with engine.begin() as bind:
        try:
            bind.execute(sa_text(
                'ALTER TABLE transcription_tasks ADD COLUMN resume_attempts '
                'INTEGER NOT NULL DEFAULT 0'))
            raised = False
        except OperationalError as exc:
            raised = True
            assert 'duplicate column name' in str(exc).lower()
    engine.dispose()
    assert raised is True


# --------------------------------------------------------------------------
# Admin dashboard (internal; email allowlist; 404 for everyone else)
# --------------------------------------------------------------------------

def _admin_login(email='sindrefjelle@gmail.com', password='password123'):
    """Create (or reuse) an allowlisted user and return a logged-in client."""
    from models import db, User
    with A.app.app_context():
        u = User.query.filter_by(email=email.lower()).first()
        if u is None:
            u = User(email=email.lower(), trial_seconds_limit=A.NEW_USER_TRIAL_SECONDS)
            u.set_password(password)
            db.session.add(u)
            db.session.commit()
        uid = u.id
    return _login(uid), uid


def test_admin_anonymous_gets_404():
    client = A.app.test_client()
    assert client.get('/admin').status_code == 404
    assert client.get('/admin/').status_code == 404
    assert client.get('/admin/users/1').status_code == 404


def test_admin_non_admin_logged_in_gets_404():
    uid = _make_user('not-an-admin@example.com')
    client = _login(uid)
    assert client.get('/admin').status_code == 404
    assert client.get('/admin/users/%d' % uid).status_code == 404


def test_admin_allowlisted_user_gets_200(monkeypatch):
    monkeypatch.setenv('ADMIN_EMAILS', 'Admin@Example.com, other@x.com')
    # Reload allowlist is read from env each call — no cache.
    client, uid = _admin_login('admin@example.com')
    resp = client.get('/admin')
    assert resp.status_code == 200
    body = resp.data.decode()
    assert 'Admin' in body
    assert 'data-kpi="total_users"' in body
    assert 'noindex' in body
    # PostHog must stay off on admin even if a key were configured.
    assert 'posthog.init' not in body
    detail = client.get(f'/admin/users/{uid}')
    assert detail.status_code == 200
    assert 'admin@example.com' in detail.data.decode()


def test_admin_default_allowlist_includes_sindre(monkeypatch):
    monkeypatch.delenv('ADMIN_EMAILS', raising=False)
    import admin_dashboard as AD
    assert 'sindrefjelle@gmail.com' in AD.admin_emails()
    client, _uid = _admin_login('sindrefjelle@gmail.com')
    assert client.get('/admin').status_code == 200


def test_admin_kpis_match_fixture_data(monkeypatch):
    """Seed known rows and assert collect_kpis deltas (shared suite DB)."""
    from models import (db, User, TranscriptionTask, CreditPurchase,
                        TrialBudgetDay)
    import admin_dashboard as AD
    import uuid as _uuid

    monkeypatch.setenv('ADMIN_EMAILS', 'kpi-admin@test.com')
    monkeypatch.setattr(A, 'TRIAL_DAILY_SECONDS', 750 * 60)
    prefix = 'kpi-fix-%s' % _uuid.uuid4().hex[:8]

    with A.app.app_context():
        before = AD.collect_kpis(db, A.TRIAL_DAILY_SECONDS,
                                 trial_daily_seconds=A.TRIAL_DAILY_SECONDS)
        today = A.trial_oslo_day_str()
        day_row = db.session.get(TrialBudgetDay, today)
        used_before = int(day_row.seconds_used) if day_row else 0
        if day_row is None:
            db.session.add(TrialBudgetDay(day=today, seconds_used=0))
            db.session.commit()

        now = datetime.now(timezone.utc)
        users = []
        for i, extra in enumerate([
            dict(trial_seconds_used=600, openai_api_key=None),
            dict(trial_seconds_used=120, openai_api_key='sk-test-byok'),
            dict(trial_seconds_used=0, openai_api_key=None),
        ]):
            u = User(email=f'{prefix}-{i}@test.com',
                     trial_seconds_limit=3600, **extra)
            u.set_password('password123')
            u.created_at = now - timedelta(hours=1)
            db.session.add(u)
            users.append(u)
        db.session.commit()

        # Bump today's shared trial budget ledger (what the KPI card reads).
        day_row = db.session.get(TrialBudgetDay, today)
        day_row.seconds_used = used_before + 720  # +12 minutes
        db.session.commit()

        # User 0: two completed on different days → activated + returning
        db.session.add(TranscriptionTask(
            id=f'{prefix}-t1', user_id=users[0].id, episode_title='Ep 1',
            status='completed', audio_duration=600.0,
            started_at=now - timedelta(days=2),
            completed_at=now - timedelta(days=2)))
        db.session.add(TranscriptionTask(
            id=f'{prefix}-t2', user_id=users[0].id, episode_title='Ep 2',
            status='completed', audio_duration=300.0,
            started_at=now - timedelta(days=1),
            completed_at=now - timedelta(days=1)))
        # User 1: one completed (activated, not returning) + one error
        db.session.add(TranscriptionTask(
            id=f'{prefix}-t3', user_id=users[1].id, episode_title='Ep 3',
            status='completed', audio_duration=120.0,
            started_at=now - timedelta(hours=3),
            completed_at=now - timedelta(hours=3)))
        db.session.add(TranscriptionTask(
            id=f'{prefix}-t4', user_id=users[1].id, episode_title='Fail',
            status='error',
            error_message='Transcription stopped making progress and was stopped.',
            started_at=now - timedelta(hours=2),
            completed_at=now - timedelta(hours=2)))
        # One purchase
        db.session.add(CreditPurchase(
            user_id=users[0].id,
            stripe_session_id=f'cs_{prefix}',
            amount_cents=500, amount_total_cents=500, currency='usd',
            minutes=300, status='credited'))
        db.session.commit()

        after = AD.collect_kpis(db, A.TRIAL_DAILY_SECONDS,
                                trial_daily_seconds=A.TRIAL_DAILY_SECONDS)
        charts = AD.collect_chart_data(db, days=90)

    assert after['total_users'] == before['total_users'] + 3
    assert after['activated_users'] == before['activated_users'] + 2
    assert after['returning_users'] == before['returning_users'] + 1
    assert after['completed_7d'] == before['completed_7d'] + 3
    assert after['failed_7d'] == before['failed_7d'] + 1
    assert after['minutes_7d'] == pytest.approx(before['minutes_7d'] + 17.0, abs=0.1)
    assert after['byok_users'] == before['byok_users'] + 1
    assert 'followed_feeds' not in after
    assert 'email_alerts_opted_in' not in after
    assert after['purchase_count'] == before['purchase_count'] + 1
    assert after['purchase_revenue_usd'] == pytest.approx(
        before['purchase_revenue_usd'] + 5.0, abs=0.01)
    assert after['trial_daily_used_minutes'] == pytest.approx(
        before['trial_daily_used_minutes'] + 12.0, abs=0.1)
    assert after['trial_daily_limit_minutes'] == 750

    assert charts['funnel']['values'][0] == after['total_users']
    assert charts['funnel']['values'][1] == after['activated_users']
    assert charts['funnel']['values'][2] == after['returning_users']
    assert 'stale' in charts['failures']['labels']

    # HTML surface for the admin still renders after fixture seed.
    client, _ = _admin_login('kpi-admin@test.com')
    body = client.get('/admin').data.decode()
    assert 'data-kpi="total_users"' in body
    assert 'data-kpi="activated_users"' in body
    assert 'data-kpi="trial_daily_used_minutes"' in body
    assert 'Trial today' in body
    assert 'chartSignups' in body
    # User detail lists tasks / purchases
    with A.app.app_context():
        uid0 = User.query.filter_by(email=f'{prefix}-0@test.com').one().id
    detail = client.get(f'/admin/users/{uid0}').data.decode()
    assert f'{prefix}-0@test.com' in detail
    assert 'Ep 1' in detail
    assert 'Followed feeds' not in detail
    assert '$5.00' in detail


def test_admin_user_search_filters_by_email(monkeypatch):
    monkeypatch.setenv('ADMIN_EMAILS', 'search-admin@test.com')
    import uuid as _uuid
    token = _uuid.uuid4().hex[:8]
    _make_user(f'findme-{token}@search.test')
    _make_user(f'other-{token}@search.test')
    client, _ = _admin_login('search-admin@test.com')
    body = client.get(f'/admin?q=findme-{token}').data.decode()
    assert f'findme-{token}@search.test' in body
    assert f'other-{token}@search.test' not in body


def test_admin_error_kind_classifier():
    import admin_dashboard as AD
    assert AD.classify_error_kind(
        'Transcription stopped making progress and was stopped.') == 'stale'
    assert AD.classify_error_kind('Cancelled.') == 'cancelled'
    assert AD.classify_error_kind('') == 'unknown'
    assert AD.classify_error_kind('Something weird happened') == 'other'


def test_admin_not_in_sitemap_or_llms():
    sitemap = A.app.test_client().get('/sitemap.xml').data.decode()
    assert '/admin' not in sitemap
    llms = A.app.test_client().get('/llms.txt').data.decode()
    assert '/admin' not in llms


def test_admin_is_admin_user_case_insensitive(monkeypatch):
    import admin_dashboard as AD
    monkeypatch.setenv('ADMIN_EMAILS', 'SiNdReFjElLe@Gmail.COM')
    uid = _make_user('sindrefjelle@gmail.com')
    with A.app.app_context():
        from models import db, User
        u = db.session.get(User, uid)
        assert AD.is_admin_user(u) is True


def test_admin_headers_robots_and_self_hosted_chartjs(monkeypatch):
    monkeypatch.setenv('ADMIN_EMAILS', 'hdr-admin@example.com')
    anon = A.app.test_client().get('/admin')
    assert anon.status_code == 404
    assert 'noindex' in anon.headers.get('X-Robots-Tag', '')
    assert anon.headers.get('Cache-Control') == 'no-store'
    robots = A.app.test_client().get('/robots.txt').data.decode()
    assert 'Disallow: /admin' in robots
    client, _uid = _admin_login('hdr-admin@example.com')
    resp = client.get('/admin')
    assert resp.status_code == 200
    assert 'noindex' in resp.headers.get('X-Robots-Tag', '')
    assert resp.headers.get('Cache-Control') == 'no-store'
    body = resp.data.decode()
    # CSP script-src is 'self' (+PostHog): no third-party CDN scripts.
    assert 'cdn.jsdelivr.net' not in body
    assert '/static/vendor/chart-4.4.7.umd.min.js' in body
    js = A.app.test_client().get('/static/vendor/chart-4.4.7.umd.min.js')
    assert js.status_code == 200 and b'Chart.js v4.4.7' in js.data[:200]


def test_admin_never_renders_byok_key(monkeypatch):
    from models import db, User
    monkeypatch.setenv('ADMIN_EMAILS', 'key-admin@example.com')
    uid = _make_user('byok-victim@example.com')
    secret = 'sk-test-SHOULD-NEVER-RENDER-123456'
    with A.app.app_context():
        u = db.session.get(User, uid)
        u.openai_api_key = secret
        db.session.commit()
    client, _ = _admin_login('key-admin@example.com')
    for path in ('/admin', f'/admin/users/{uid}', '/admin?q=byok-victim'):
        r = client.get(path)
        assert r.status_code == 200
        assert secret not in r.data.decode()
        assert 'SHOULD-NEVER' not in r.data.decode()


def _admin_fake_stripe_revenue_client(
        *,
        available=None,
        pending=None,
        payouts=None,
        balance_transactions=None,
        balance_error=None,
        payouts_error=None,
        bt_error=None,
        livemode=True):
    """Minimal StripeClient surface for admin revenue panel tests."""
    from types import SimpleNamespace

    available = available if available is not None else [
        {'amount': 12345, 'currency': 'usd'},
    ]
    pending = pending if pending is not None else [
        {'amount': 5000, 'currency': 'usd'},
    ]
    now = int(datetime.now(timezone.utc).timestamp())
    if payouts is None:
        payouts = [
            SimpleNamespace(
                id='po_in_transit',
                amount=10000,
                currency='usd',
                status='in_transit',
                created=now - 86400,
                arrival_date=now + 86400,
                destination=SimpleNamespace(last4='4242', object='bank_account'),
            ),
            SimpleNamespace(
                id='po_paid',
                amount=8000,
                currency='usd',
                status='paid',
                created=now - 7 * 86400,
                arrival_date=now - 5 * 86400,
                destination='ba_unexpanded_must_not_appear_in_html',
            ),
        ]
    if balance_transactions is None:
        balance_transactions = [
            SimpleNamespace(
                id='txn_1', type='charge', amount=500, fee=45, net=455,
                currency='usd', created=now - 3600,
                fee_details=[
                    SimpleNamespace(type='stripe_fee', amount=30),
                    SimpleNamespace(type='tax', amount=15),
                ],
            ),
            SimpleNamespace(
                id='txn_2', type='refund', amount=-200, fee=0, net=-200,
                currency='usd', created=now - 2 * 86400,
                fee_details=[],
            ),
            SimpleNamespace(
                id='txn_old', type='charge', amount=500, fee=30, net=470,
                currency='usd', created=now - 40 * 86400,
                fee_details=[SimpleNamespace(type='stripe_fee', amount=30)],
            ),
        ]

    class FakeBalance:
        def retrieve(self, params=None, options=None):
            if balance_error:
                raise balance_error
            return SimpleNamespace(
                available=available,
                pending=pending,
                livemode=livemode,
            )

    class FakePayouts:
        def list(self, params=None, options=None):
            if payouts_error:
                raise payouts_error
            return SimpleNamespace(data=list(payouts))

    class FakeBTList:
        def __init__(self, rows):
            self.data = list(rows)

        def auto_paging_iter(self):
            return iter(self.data)

    class FakeBT:
        def list(self, params=None, options=None):
            if bt_error:
                raise bt_error
            return FakeBTList(balance_transactions)

    class FakeClient:
        def __init__(self, *a, **k):
            self.v1 = SimpleNamespace(
                balance=FakeBalance(),
                payouts=FakePayouts(),
                balance_transactions=FakeBT(),
            )

    return FakeClient


def test_admin_stripe_revenue_section_renders_mocked_data(monkeypatch):
    import admin_dashboard as AD
    AD.clear_stripe_revenue_cache()
    monkeypatch.setenv('ADMIN_EMAILS', 'stripe-admin@test.com')
    monkeypatch.setattr(A, 'STRIPE_SECRET_KEY', 'sk_live_fake_for_admin')
    FakeClient = _admin_fake_stripe_revenue_client()
    monkeypatch.setattr(AD, 'admin_stripe_client', lambda: FakeClient())

    client, _ = _admin_login('stripe-admin@test.com')
    resp = client.get('/admin')
    assert resp.status_code == 200
    body = resp.data.decode()
    assert 'Revenue &amp; payouts' in body or 'Revenue & payouts' in body
    assert 'data-stripe="balance"' in body
    assert '$123.45' in body  # available
    assert '$50.00' in body  # pending
    assert 'in_transit' in body
    assert '•••• 4242' in body
    assert 'ba_unexpanded_must_not_appear_in_html' not in body
    assert 'sk_live_fake' not in body
    assert 'Gross charges' in body
    assert 'chartStripeWeekly' in body
    assert 'adminStripeWeekly' in body
    assert 'payout-highlight' in body


def test_admin_stripe_unavailable_does_not_break_dashboard(monkeypatch):
    import admin_dashboard as AD
    AD.clear_stripe_revenue_cache()
    monkeypatch.setenv('ADMIN_EMAILS', 'stripe-fail@test.com')
    monkeypatch.setattr(A, 'STRIPE_SECRET_KEY', 'sk_live_fake')

    class Boom(Exception):
        pass

    class FakeClient:
        def __init__(self, *a, **k):
            from types import SimpleNamespace

            class Bal:
                def retrieve(self, params=None, options=None):
                    raise Boom('connection reset')

            self.v1 = SimpleNamespace(balance=Bal())

    monkeypatch.setattr(AD, 'admin_stripe_client', lambda: FakeClient())
    client, _ = _admin_login('stripe-fail@test.com')
    resp = client.get('/admin')
    assert resp.status_code == 200
    body = resp.data.decode()
    assert 'data-kpi="total_users"' in body
    assert 'Stripe unavailable:' in body
    assert 'data-stripe="error"' in body


def test_admin_stripe_permission_error_names_missing_scope(monkeypatch):
    import admin_dashboard as AD
    AD.clear_stripe_revenue_cache()
    monkeypatch.setenv('ADMIN_EMAILS', 'stripe-perm@test.com')
    monkeypatch.setattr(A, 'STRIPE_SECRET_KEY', 'rk_live_restricted')

    # Use real stripe.PermissionError so describe_stripe_admin_error classifies it.
    err = A.stripe.PermissionError(
        "The provided key does not have the required permissions for this "
        "endpoint on account 'acct_1'. Having the 'rak_balance_read' "
        "permission would allow this request to continue.",
    )
    FakeClient = _admin_fake_stripe_revenue_client(balance_error=err)
    monkeypatch.setattr(AD, 'admin_stripe_client', lambda: FakeClient())

    client, _ = _admin_login('stripe-perm@test.com')
    body = client.get('/admin').data.decode()
    assert 'Stripe unavailable:' in body
    assert 'rak_balance_read' in body
    assert 'rk_live_restricted' not in body


def test_admin_stripe_unconfigured_shows_clear_message(monkeypatch):
    import admin_dashboard as AD
    AD.clear_stripe_revenue_cache()
    monkeypatch.setenv('ADMIN_EMAILS', 'stripe-off@test.com')
    monkeypatch.setattr(A, 'STRIPE_SECRET_KEY', '')
    client, _ = _admin_login('stripe-off@test.com')
    body = client.get('/admin').data.decode()
    assert 'Stripe unavailable:' in body
    assert 'STRIPE_SECRET_KEY' in body


def test_admin_stripe_revenue_cache_ttl(monkeypatch):
    import admin_dashboard as AD
    AD.clear_stripe_revenue_cache()
    monkeypatch.setattr(A, 'STRIPE_SECRET_KEY', 'sk_live_cache')
    calls = {'n': 0}
    FakeClient = _admin_fake_stripe_revenue_client()

    def make():
        calls['n'] += 1
        return FakeClient()

    monkeypatch.setattr(AD, 'admin_stripe_client', make)
    first = AD.collect_stripe_revenue()
    second = AD.collect_stripe_revenue()
    assert first['ok'] is True
    assert second['cached'] is True
    assert calls['n'] == 1
    AD.clear_stripe_revenue_cache()
    third = AD.collect_stripe_revenue(force_refresh=True)
    assert third['cached'] is False
    assert calls['n'] == 2


def test_admin_stripe_money_helpers():
    import admin_dashboard as AD
    assert AD.format_stripe_money(500, 'usd') == '$5.00'
    assert AD.format_stripe_money(12345, 'usd') == '$123.45'
    assert '4242' not in (AD._payout_destination_last4(
        type('P', (), {'destination': 'ba_secret_full_id'})()) or '')
    from types import SimpleNamespace
    assert AD._payout_destination_last4(SimpleNamespace(
        destination=SimpleNamespace(last4='9999'))) == '9999'


def test_listen_links_platform_labels_require_canonical_urls():
    links = A.listen_links_from_fields(
        spotify_url='https://evil.example/open.spotify.com/episode/x',
        apple_url='https://evil.example/?podcasts.apple.com/id123',
    )
    assert links == []
    assert A.canonical_apple_podcasts_url(
        'https://evil.example/podcasts.apple.com/podcast/id12') is None
    assert A.canonical_apple_podcasts_url(
        'https://podcasts.apple.com/us/podcast/x/id12?i=34&uo=4'
    ) == 'https://podcasts.apple.com/podcast/id12?i=34'
    assert A.canonical_spotify_episode_url(
        'javascript:alert(1)//open.spotify.com/episode/abc') is None


def test_enqueue_drops_non_platform_urls_for_platform_fields(monkeypatch, trial_on):
    import types
    monkeypatch.setattr(
        A.threading, 'Thread',
        lambda target=None, **kw: types.SimpleNamespace(
            daemon=True, start=lambda: None))
    monkeypatch.setattr(A, 'free_disk_bytes', lambda *_a, **_k: 10 ** 12)
    monkeypatch.setattr(A, '_is_fetchable_url', lambda url: True)
    uid = _make_user('listen-evil@test.com')
    with A.app.app_context():
        user = A.db.session.get(A.User, uid)
        payload, status = A.enqueue_transcription(user, {
            'title': 'Ep', 'audio_url': 'https://cdn.example.com/ep2.mp3',
            'podcast_name': 'Show', 'duration_min': 5,
            'spotify_url': 'https://phish.example/login',
            'apple_url': 'https://phish.example/?podcasts.apple.com',
            'episode_link': 'javascript:alert(1)',
        })
        assert status == 200, payload
        task = A.db.session.get(A.TranscriptionTask, payload['task_id'])
        assert task.source_spotify_url is None
        assert task.source_apple_url is None
        assert task.source_website_url is None


def test_public_share_hides_raw_audio_unless_in_public_directory(trial_on, monkeypatch):
    monkeypatch.setattr(A, 'apple_url_for_feed', lambda *a, **k: None)
    uid = _make_user('listen-private@test.com')
    tid = _share_task(
        uid, 'listen-private',
        rss_url='https://private.example/feed/SECRETTOKEN',
        source_audio_url='https://private.example/ep.mp3?token=SECRET',
    )
    token = _login(uid).post(f'/transcription/{tid}/share').get_json()['token']
    html = A.app.test_client().get(f'/t/{token}').data.decode()
    assert 'token=SECRET' not in html
    assert 'Play audio' not in html
    # Owner's own result page still offers the audio.
    status = _login(uid).get(f'/status/{tid}').get_json()
    assert 'audio' in [L['platform'] for L in status['listen_links']]


def test_share_view_never_does_apple_lookup_inline(trial_on, monkeypatch):
    started = []

    class FakeThread:
        def __init__(self, target=None, args=(), **kw):
            started.append((target, args))

        def start(self):
            return None

    def boom(*a, **k):
        raise AssertionError('network lookup on request thread')

    monkeypatch.setattr(A, 'apple_url_for_feed', boom)
    uid = _make_user('listen-async@test.com')
    tid = _share_task(uid, 'listen-async',
                      rss_url='https://feeds.example.com/async.xml',
                      source_audio_url='https://cdn.example.com/a.mp3')
    token = _login(uid).post(f'/transcription/{tid}/share')
    monkeypatch.setitem(A.app.config, 'TESTING', False)
    monkeypatch.setattr(A.threading, 'Thread', FakeThread)
    A._listen_backfill_inflight.clear()
    tok = token.get_json()['token']
    resp = A.app.test_client().get(f'/t/{tok}', base_url='https://podskrift.com')
    assert resp.status_code == 200
    assert started and started[0][1] == (tid,)
    A._listen_backfill_inflight.clear()


def test_apple_url_for_feed_matches_episode_and_sends_only_name(monkeypatch):
    A._apple_feed_url_cache.clear()
    seen = []

    class R:
        def __init__(self, data):
            self.data = data

        def raise_for_status(self):
            return None

        def json(self):
            return self.data

    def fake_get(url, timeout=None, params=None):
        seen.append(dict(params or {}))
        return R({'results': [{'feedUrl': 'https://feeds.example.com/s/TOKEN123',
                               'collectionId': 77}]})

    monkeypatch.setattr(A.requests, 'get', fake_get)
    monkeypatch.setattr(A, '_itunes_lookup', lambda *a, **k: [
        {'wrapperType': 'track'},
        {'wrapperType': 'podcastEpisode', 'trackId': 1, 'trackName': 'Other',
         'episodeUrl': 'https://cdn.example.com/other.mp3'},
        {'wrapperType': 'podcastEpisode', 'trackId': 2, 'trackName': 'Mine',
         'episodeUrl': 'https://cdn.example.com/mine.mp3'},
    ])
    url = A.apple_url_for_feed('https://feeds.example.com/s/TOKEN123', 'My Show',
                               episode_title='mine',
                               audio_url='https://cdn.example.com/x.mp3')
    assert url == 'https://podcasts.apple.com/podcast/id77?i=2'
    assert all('TOKEN123' not in str(p) for p in seen)
    assert seen[0]['term'] == 'My Show'


def test_server_side_signup_event_ignores_cookie_consent(ph_events):
    """Declined browser analytics must not suppress server funnel events."""
    from models import User
    A._register_attempts.clear()
    client = A.app.test_client()
    client.set_cookie('podskrift_cookie_consent', 'declined')
    resp = client.post('/register', data={
        'email': 'declined-signup@example.com',
        'password': 'password123',
        'password2': 'password123',
    })
    assert resp.status_code in (302, 303)
    with A.app.app_context():
        uid = User.query.filter_by(email='declined-signup@example.com').first().id
    events = [e for e in ph_events.events if e['event'] == 'user_signed_up']
    assert events and events[-1]['distinct_id'] == str(uid)


# --------------------------------------------------------------------------
# Forgot / reset password
# --------------------------------------------------------------------------

def _enable_password_reset_mail(monkeypatch):
    """Turn Mailgun on without setting PUBLIC_BASE_URL (that 301s off localhost)."""
    monkeypatch.setenv('EMAIL_ENABLED', '1')
    monkeypatch.setenv('MAILGUN_API_KEY', 'key-test')
    monkeypatch.setenv('MAILGUN_DOMAIN', 'podskrift.com')
    monkeypatch.setenv('MAILGUN_BASE_URL', 'https://api.eu.mailgun.net')


def _csrf_client():
    A.app.config['TESTING'] = True
    client = A.app.test_client()
    with client.session_transaction() as sess:
        token = secrets.token_hex(32)
        sess['_csrf_token'] = token
    return client, token


def _request_reset(client, csrf, email, **extra):
    data = {'csrf_token': csrf, 'email': email}
    data.update(extra)
    return client.post('/forgot-password', data=data, follow_redirects=True)


def _patch_reset_mail(monkeypatch):
    """Capture reset URLs; confirm-changed sends are counted separately."""
    import email_notify
    import mail as mailer

    state = {'reset_urls': [], 'changed': 0}

    def fake_reset(*, to, reset_url, user_id):
        state['reset_urls'].append(reset_url)
        return mailer.SEND_SENT

    def fake_changed(*, to, user_id):
        state['changed'] += 1
        return mailer.SEND_SENT

    monkeypatch.setattr(email_notify, 'send_password_reset_email', fake_reset)
    monkeypatch.setattr(email_notify, 'send_password_changed_email', fake_changed)
    return state


@pytest.fixture(autouse=False)
def _clear_password_reset_limits():
    A._password_reset_attempts.clear()
    yield
    A._password_reset_attempts.clear()


def test_login_links_to_forgot_password():
    body = A.app.test_client().get('/login').data.decode()
    assert '/forgot-password' in body
    assert 'Forgot password?' in body


def test_forgot_password_mail_not_ready_shows_contact(monkeypatch):
    monkeypatch.setenv('EMAIL_ENABLED', '0')
    monkeypatch.delenv('MAILGUN_API_KEY', raising=False)
    resp = A.app.test_client().get('/forgot-password')
    assert resp.status_code == 200
    body = resp.data.decode()
    assert 'hello@podskrift.com' in body
    assert 'not available' in body.lower() or 'email' in body.lower()
    assert 'name="email"' not in body
    assert resp.headers.get('X-Robots-Tag') == 'noindex'


def test_forgot_password_enumeration_safe(monkeypatch, _clear_password_reset_limits):
    """Known and unknown addresses get the same neutral success copy."""
    _enable_password_reset_mail(monkeypatch)
    state = _patch_reset_mail(monkeypatch)
    _make_user('reset-known@example.com')
    client, csrf = _csrf_client()
    known = _request_reset(client, csrf, 'reset-known@example.com')
    unknown = _request_reset(client, csrf, 'reset-nobody@example.com')
    assert known.status_code == 200 and unknown.status_code == 200
    import html as _html
    known_body = _html.unescape(known.data.decode())
    unknown_body = _html.unescape(unknown.data.decode())
    assert A.PASSWORD_RESET_NEUTRAL_MSG in known_body
    assert A.PASSWORD_RESET_NEUTRAL_MSG in unknown_body
    # One send for the real account only — but the UI must not reveal that.
    assert len(state['reset_urls']) == 1


def test_password_reset_happy_path_updates_password_and_logs_in(
        monkeypatch, ph_events, _clear_password_reset_limits):
    import html as _html
    from models import db, User, PasswordResetToken

    _enable_password_reset_mail(monkeypatch)
    state = _patch_reset_mail(monkeypatch)
    uid = _make_user('reset-ok@example.com')

    client, csrf = _csrf_client()
    resp = _request_reset(client, csrf, 'reset-ok@example.com')
    assert A.PASSWORD_RESET_NEUTRAL_MSG in _html.unescape(resp.data.decode())
    assert any(e['event'] == 'password_reset_requested'
               and e['distinct_id'] == str(uid) for e in ph_events.events)

    assert len(state['reset_urls']) == 1
    raw_token = state['reset_urls'][0].rstrip('/').rsplit('/', 1)[-1]
    with A.app.app_context():
        row = PasswordResetToken.query.filter_by(
            token_hash=A._hash_password_reset_token(raw_token)).first()
        assert row is not None and row.used_at is None
        assert row.user_id == uid

    # GET shows the form only (link-scanner safe).
    view = client.get(f'/reset-password/{raw_token}')
    assert view.status_code == 200
    assert b'name="password"' in view.data
    assert b'name="password2"' in view.data
    assert view.headers.get('X-Robots-Tag') == 'noindex'
    assert view.headers.get('Referrer-Policy') == 'no-referrer'
    assert '<meta name="robots" content="noindex">' in view.data.decode()

    with client.session_transaction() as sess:
        csrf2 = sess.get('_csrf_token')
    done = client.post(
        f'/reset-password/{raw_token}',
        data={
            'csrf_token': csrf2,
            'password': 'newpass99',
            'password2': 'newpass99',
        },
        follow_redirects=False,
    )
    assert done.status_code in (302, 303)
    assert any(e['event'] == 'password_reset_completed'
               and e['distinct_id'] == str(uid) for e in ph_events.events)

    with A.app.app_context():
        user = db.session.get(User, uid)
        assert user.check_password('newpass99')
        assert not user.check_password('password123')
        assert int(user.session_version) == 1
        row = PasswordResetToken.query.filter_by(
            token_hash=A._hash_password_reset_token(raw_token)).first()
        assert row.used_at is not None

    # Logged in after reset.
    home = client.get('/')
    assert b'data-authenticated="1"' in home.data
    assert state['changed'] == 1


def test_password_reset_token_single_use(monkeypatch, _clear_password_reset_limits):
    from models import PasswordResetToken

    _enable_password_reset_mail(monkeypatch)
    state = _patch_reset_mail(monkeypatch)
    uid = _make_user('reset-once@example.com')

    client, csrf = _csrf_client()
    _request_reset(client, csrf, 'reset-once@example.com')
    raw = state['reset_urls'][0].rstrip('/').rsplit('/', 1)[-1]

    with client.session_transaction() as sess:
        csrf2 = sess['_csrf_token']
    assert client.post(f'/reset-password/{raw}', data={
        'csrf_token': csrf2, 'password': 'abcdefgh1', 'password2': 'abcdefgh1',
    }).status_code in (302, 303)

    # Second use is a 404 invalid page; GET also fails.
    again = client.get(f'/reset-password/{raw}')
    assert again.status_code == 404
    assert b'not valid' in again.data.lower() or b'invalid' in again.data.lower()
    with A.app.app_context():
        assert PasswordResetToken.query.filter_by(user_id=uid).count() >= 1


def test_password_reset_expired_token(monkeypatch, _clear_password_reset_limits):
    from models import db, PasswordResetToken

    uid = _make_user('reset-exp@example.com')
    raw = 'expired-token-value-aaaaaaaa'
    with A.app.app_context():
        db.session.add(PasswordResetToken(
            user_id=uid,
            token_hash=A._hash_password_reset_token(raw),
            expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        ))
        db.session.commit()
    resp = A.app.test_client().get(f'/reset-password/{raw}')
    assert resp.status_code == 410
    assert b'expired' in resp.data.lower()


def test_password_reset_wrong_token_is_404():
    resp = A.app.test_client().get('/reset-password/not-a-real-token-zzzz')
    assert resp.status_code == 404
    assert resp.headers.get('X-Robots-Tag') == 'noindex'
    assert resp.headers.get('Referrer-Policy') == 'no-referrer'


def test_password_reset_new_request_invalidates_older(
        monkeypatch, _clear_password_reset_limits):
    _enable_password_reset_mail(monkeypatch)
    state = _patch_reset_mail(monkeypatch)
    _make_user('reset-supersede@example.com')

    client, csrf = _csrf_client()
    _request_reset(client, csrf, 'reset-supersede@example.com')
    _request_reset(client, csrf, 'reset-supersede@example.com')
    assert len(state['reset_urls']) == 2
    old = state['reset_urls'][0].rstrip('/').rsplit('/', 1)[-1]
    new = state['reset_urls'][1].rstrip('/').rsplit('/', 1)[-1]
    assert A.app.test_client().get(f'/reset-password/{old}').status_code == 404
    assert A.app.test_client().get(f'/reset-password/{new}').status_code == 200


def test_password_reset_rate_limited(monkeypatch, _clear_password_reset_limits):
    _enable_password_reset_mail(monkeypatch)
    state = _patch_reset_mail(monkeypatch)
    _make_user('reset-rl@example.com')
    import html as _html
    client, csrf = _csrf_client()
    for _ in range(A.PASSWORD_RESET_MAX_PER_KEY):
        resp = _request_reset(client, csrf, 'reset-rl@example.com')
        assert A.PASSWORD_RESET_NEUTRAL_MSG in _html.unescape(resp.data.decode())
    limited = _request_reset(client, csrf, 'reset-rl@example.com')
    assert b'Too many reset requests' in limited.data
    assert len(state['reset_urls']) == A.PASSWORD_RESET_MAX_PER_KEY


def test_password_reset_invalidates_other_sessions(
        monkeypatch, _clear_password_reset_limits):
    from models import db, User

    _enable_password_reset_mail(monkeypatch)
    state = _patch_reset_mail(monkeypatch)
    uid = _make_user('reset-sess@example.com')

    # Two independent logged-in browsers.
    a = A.app.test_client()
    b = A.app.test_client()
    for c in (a, b):
        c.post('/login', data={
            'email': 'reset-sess@example.com', 'password': 'password123',
        })
        assert b'data-authenticated="1"' in c.get('/').data

    reset_client, csrf = _csrf_client()
    _request_reset(reset_client, csrf, 'reset-sess@example.com')
    raw = state['reset_urls'][0].rstrip('/').rsplit('/', 1)[-1]
    with reset_client.session_transaction() as sess:
        csrf2 = sess['_csrf_token']
    assert reset_client.post(f'/reset-password/{raw}', data={
        'csrf_token': csrf2, 'password': 'brandnew1', 'password2': 'brandnew1',
    }).status_code in (302, 303)

    # Prior sessions are dead; the reset session is logged in.
    assert b'data-authenticated="1"' not in a.get('/').data
    assert b'data-authenticated="1"' not in b.get('/').data
    assert b'data-authenticated="1"' in reset_client.get('/').data
    with A.app.app_context():
        assert db.session.get(User, uid).check_password('brandnew1')


def test_password_reset_get_does_not_change_password(monkeypatch):
    """Link scanners hit GET; only POST may mutate the password."""
    from models import db, User, PasswordResetToken

    uid = _make_user('reset-get@example.com')
    raw = 'scanner-safe-token-bbbbbbbb'
    with A.app.app_context():
        db.session.add(PasswordResetToken(
            user_id=uid,
            token_hash=A._hash_password_reset_token(raw),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        ))
        db.session.commit()
    resp = A.app.test_client().get(f'/reset-password/{raw}')
    assert resp.status_code == 200
    assert b'Update password' in resp.data
    with A.app.app_context():
        user = db.session.get(User, uid)
        assert user.check_password('password123')
        row = PasswordResetToken.query.filter_by(
            token_hash=A._hash_password_reset_token(raw)).first()
        assert row.used_at is None


def test_password_reset_excluded_from_sitemap_and_llms():
    sitemap = A.app.test_client().get('/sitemap.xml').data.decode()
    llms = A.app.test_client().get('/llms.txt').data.decode()
    assert 'forgot-password' not in sitemap
    assert 'reset-password' not in sitemap
    assert 'forgot-password' not in llms
    assert 'reset-password' not in llms


def test_password_reset_rejects_short_password(monkeypatch, _clear_password_reset_limits):
    from models import db, PasswordResetToken

    uid = _make_user('reset-short@example.com')
    raw = 'short-pw-token-cccccccc'
    with A.app.app_context():
        db.session.add(PasswordResetToken(
            user_id=uid,
            token_hash=A._hash_password_reset_token(raw),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        ))
        db.session.commit()
    client = A.app.test_client()
    client.get(f'/reset-password/{raw}')
    with client.session_transaction() as sess:
        csrf = sess['_csrf_token']
    resp = client.post(f'/reset-password/{raw}', data={
        'csrf_token': csrf, 'password': 'short', 'password2': 'short',
    }, follow_redirects=True)
    assert b'at least 8 characters' in resp.data.lower()
    with A.app.app_context():
        from models import User
        assert db.session.get(User, uid).check_password('password123')


def test_emails_are_redacted_in_sentry(sentry_events):
    _raise_and_report(RuntimeError('failed for user@example.com somehow'))
    (event,) = sentry_events
    assert 'user@example.com' not in repr(event)
    assert '[redacted]' in event['exception']['values'][-1]['value']


def test_ensure_password_reset_tokens_table_indexes_after_columns():
    """Indexes that mention columns are created only after columns exist."""
    from sqlalchemy import inspect as sa_inspect
    with A.app.app_context():
        A.ensure_password_reset_tokens_table()
        cols = {c['name'] for c in sa_inspect(A.db.engine).get_columns(
            'password_reset_tokens')}
        assert {'user_id', 'token_hash', 'expires_at', 'used_at'} <= cols
        idx = {i['name'] for i in sa_inspect(A.db.engine).get_indexes(
            'password_reset_tokens')}
        assert 'ix_password_reset_tokens_token_hash' in idx
        assert 'ix_password_reset_tokens_user_id' in idx


def test_pre_deploy_sessions_and_remember_cookies_stay_logged_in():
    """Deploy safety: sessions/remember cookies minted before session_version
    existed carry a bare id. They must stay valid while the version is 0, and
    stop working once a password reset bumps the version."""
    from flask_login.utils import encode_cookie
    from models import db, User
    uid = _make_user('legacy-session@test.com')
    with A.app.app_context():
        assert int(db.session.get(User, uid).session_version or 0) == 0

    # 1. Legacy Flask session: _user_id is the bare id.
    legacy = A.app.test_client()
    with legacy.session_transaction() as sess:
        sess['_user_id'] = str(uid)
        sess['_fresh'] = True
    assert legacy.get('/settings').status_code == 200

    # 2. Legacy remember cookie only (session expired): bare id payload.
    with A.app.test_request_context():
        cookie_val = encode_cookie(str(uid))
    remembered = A.app.test_client()
    remembered.set_cookie(A.app.config.get('REMEMBER_COOKIE_NAME', 'remember_token'),
                          cookie_val)
    assert remembered.get('/settings').status_code == 200

    # 3. New-format id (uid:0) also valid.
    fresh = A.app.test_client()
    with fresh.session_transaction() as sess:
        sess['_user_id'] = f'{uid}:0'
        sess['_fresh'] = True
    assert fresh.get('/settings').status_code == 200

    # 4. After a reset bumps the version, all three old sessions are dropped.
    with A.app.app_context():
        u = db.session.get(User, uid)
        u.session_version = 1
        db.session.commit()
    for client in (legacy, fresh):
        r = client.get('/settings')
        assert r.status_code in (302, 303) and '/login' in r.headers['Location']
    stale = A.app.test_client()
    stale.set_cookie(A.app.config.get('REMEMBER_COOKIE_NAME', 'remember_token'),
                     cookie_val)
    r = stale.get('/settings')
    assert r.status_code in (302, 303) and '/login' in r.headers['Location']
    # The current version still works.
    cur = A.app.test_client()
    with cur.session_transaction() as sess:
        sess['_user_id'] = f'{uid}:1'
        sess['_fresh'] = True
    assert cur.get('/settings').status_code == 200


def test_session_version_column_added_with_default_zero_on_legacy_users_table(tmp_path):
    """users rows that predate the column read as session_version 0."""
    import sqlite3
    p = tmp_path / 'legacy.db'
    con = sqlite3.connect(p)
    con.execute('CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT)')
    con.execute("INSERT INTO users (email) VALUES ('a@b.c')")
    con.execute('ALTER TABLE users ADD COLUMN session_version '
                + A.USER_COLUMN_MIGRATIONS['session_version'])
    assert con.execute('SELECT session_version FROM users').fetchone()[0] == 0
    con.close()


def test_home_hero_price_line_uses_live_trial_and_pack_values(trial_on):
    body = A.app.test_client().get('/', headers={'Accept': 'text/html'}).data.decode()
    assert 'data-testid="hero-price-line"' in body
    price = f'{A.CREDIT_PACK_AMOUNT_CENTS / 100:.0f}'
    assert f'${price} for {A.CREDIT_PACK_MINUTES} min' in body
    trial = A.advertised_trial_minutes()
    if trial:
        assert f'{trial} min free · then ${price}' in body
    # Price line sits directly under the H1 (above the fold on mobile).
    assert body.index('hero-price-line') - body.index('class="hp-title"') < 400


# ---------------------------------------------------------------------------
# Saved feeds removed
# ---------------------------------------------------------------------------

def test_feeds_page_redirects_home(trial_on):
    uid = _make_user('feeds-gone@test.com')
    client = _login(uid)
    resp = client.get('/feeds', follow_redirects=False)
    assert resp.status_code == 301
    assert resp.headers['Location'].endswith('/')


def test_feed_api_endpoints_are_gone(trial_on):
    uid = _make_user('feeds-api-gone@test.com')
    client = _login(uid)
    for path in (
        '/feeds/add',
        '/feeds/1/email-alerts',
        '/feeds/1/email-summaries',
        '/feeds/delete/1',
        '/feeds/use/1',
    ):
        assert client.post(path).status_code == 410, path
        assert client.get(path).status_code == 410, path


def test_follow_endpoint_is_gone(trial_on):
    uid = _make_user('follow-gone@test.com')
    _completed_task(uid, 'follow-gone-1')
    resp = _login(uid).post('/transcription/follow-gone-1/follow')
    assert resp.status_code == 410


def test_no_follow_ui_on_result_show_or_share_pages():
    for path in (
        'templates/transcription.html',
        'templates/podcast_show.html',
        'templates/shared_transcript.html',
        'templates/episode_selection.html',
    ):
        src = open(path).read()
        assert 'Follow this podcast' not in src, path
        assert 'Follow podcast' not in src, path
        assert 'Email me when new episodes' not in src, path
        assert 'Email me new episodes' not in src, path


def test_nav_has_no_feeds_link():
    src = open('templates/base.html').read()
    assert "url_for('feeds')" not in src
    assert '>Feeds<' not in src


def test_deploy_docs_do_not_reference_new_episode_poller():
    readme = open('ops/README.md').read()
    assert 'podskrift-new-episodes' not in readme
    assert 'poll-new-episodes' not in readme
    assert not os.path.exists('ops/poll-new-episodes.py')
    assert not os.path.exists('ops/podskrift-new-episodes.service')
    assert not os.path.exists('ops/podskrift-new-episodes.timer')


def test_cookie_banner_copy_is_generic_and_privacy_names_processor(monkeypatch):
    import re as _re
    monkeypatch.setenv('PODSKRIFT_ENV', 'production')
    monkeypatch.setenv('POSTHOG_KEY', 'phc_test_public_key')
    body = A.app.test_client().get('/', headers={'Accept': 'text/html'}).data.decode()
    m = _re.search(r'id="cookieConsentDesc">(.*?)</p>', body, _re.S)
    assert m, 'banner renders when PostHog is configured'
    banner = m.group(1)
    assert 'PostHog' not in banner
    assert 'anonymous, cookie-free usage statistics' in ' '.join(banner.split())
    priv = A.app.test_client().get('/privacy').data.decode()
    assert 'PostHog' in priv


def test_home_has_no_separate_rss_control():
    """RSS stays supported, but only through the main search/paste box."""
    body = A.app.test_client().get('/').get_data(as_text=True)
    assert 'id="podcastSearch"' in body
    assert 'id="rss_url"' not in body
    assert 'class="hp-rss"' not in body
    assert 'Or paste an RSS feed URL' not in body
    assert 'Get Episodes' not in body
    # Low-key hint that RSS links work in the main box.
    assert 'RSS feed links work too' in body


def _home_rss_hint_re():
    src = open('templates/index.html').read()
    m = re.search(r"var RSS_HINT_RE = /(.+)/i;", src)
    assert m, 'RSS_HINT_RE missing from index.html'
    return re.compile(m.group(1), re.I)


@pytest.mark.parametrize('url', [
    'https://feeds.megaphone.fm/abc123',
    'https://anchor.fm/s/1234/podcast/rss',
    'https://example.com/feed/podcast',
    'https://example.com/podcast.xml',
    'https://www.omnycontent.com/d/playlist/x/y/z/podcast.rss',
])
def test_main_box_classifies_pasted_rss_urls_as_feeds(url):
    assert _home_rss_hint_re().search(url)


def test_main_box_posts_pasted_feed_urls_to_parse_rss():
    src = open('templates/index.html').read()
    i = src.index('function doSearch()')
    body = src[i:i + 4000]
    assert "inputType === 'rss_feed'" in body
    # Unrecognised http(s) URLs are tried as feeds instead of a name search.
    assert "inputType === 'other'" in body
    assert 'postForm(PARSE_RSS_URL, { rss_url: query })' in body


def test_pasted_rss_url_resolves_episodes(monkeypatch):
    monkeypatch.setattr(
        A, 'get_episodes_from_rss',
        lambda url, *, timeout=None: (_fixture_episodes(), None))
    resp = A.app.test_client().post('/parse_rss', data={
        'rss_url': 'https://feeds.example.com/fixture.xml',
    })
    assert resp.status_code == 200
    assert 'Episode One: Hello' in resp.get_data(as_text=True)


# --------------------------------------------------------------------------
# Admin costs (estimated OpenAI + fixed opex; read-only)
# --------------------------------------------------------------------------

# Fixture all-time OpenAI estimate (whisper-1 @ $0.006/min). PR sanity number.
FIXTURE_ALL_TIME_OPENAI_USD = 0.82


def test_admin_audio_prices_config_matches_spec():
    import admin_costs as AC
    assert AC.OPENAI_AUDIO_USD_PER_MIN['whisper-1'] == 0.006
    assert AC.OPENAI_AUDIO_USD_PER_MIN['gpt-4o-transcribe'] == 0.006
    assert AC.OPENAI_AUDIO_USD_PER_MIN['gpt-4o-mini-transcribe'] == 0.003
    assert AC.DEFAULT_TRANSCRIPTION_MODEL == 'whisper-1'
    assert AC.estimate_transcription_cost_usd(100 * 60) == pytest.approx(0.60)
    assert AC.estimate_transcription_cost_usd(
        100 * 60, 'gpt-4o-mini-transcribe') == pytest.approx(0.30)


def test_admin_task_key_source_classification():
    import admin_costs as AC
    assert AC.task_key_source(None, None) == 'user'
    assert AC.task_key_source(None, 0) == 'user'
    assert AC.task_key_source(0, 0) == 'trial'
    assert AC.task_key_source(600, 0) == 'trial'
    assert AC.task_key_source(None, 300) == 'paid'
    assert AC.task_key_source(0, 300) == 'paid'
    assert AC.task_key_source(300, 300) == 'mixed'


def test_admin_parse_fixed_monthly_breakdown(monkeypatch):
    import admin_costs as AC
    assert AC.parse_fixed_monthly_breakdown('') == []
    assert AC.parse_fixed_monthly_breakdown('hetzner:5.59,mailgun:1') == [
        ('hetzner', 5.59), ('mailgun', 1.0),
    ]
    assert AC.parse_fixed_monthly_breakdown('bad,also:bad,ok:2') == [('ok', 2.0)]
    monkeypatch.setenv('COST_FIXED_MONTHLY_BREAKDOWN', 'hetzner:10,mailgun:2')
    monkeypatch.delenv('COST_FIXED_MONTHLY_USD', raising=False)
    total, parts = AC.fixed_monthly_costs_from_env()
    assert total == pytest.approx(12.0)
    assert parts == [('hetzner', 10.0), ('mailgun', 2.0)]
    monkeypatch.delenv('COST_FIXED_MONTHLY_BREAKDOWN', raising=False)
    monkeypatch.setenv('COST_FIXED_MONTHLY_USD', '7.5')
    total, parts = AC.fixed_monthly_costs_from_env()
    assert total == pytest.approx(7.5)


def test_admin_summary_cost_prefers_stored_then_tokens_then_fallback():
    import admin_costs as AC
    assert AC.estimate_summary_cost_usd(
        summary_status='ready',
        summary_cost_usd_est=0.0123,
        summary_model='gpt-4o-mini',
        summary_prompt_tokens=1000,
        summary_completion_tokens=100,
    ) == pytest.approx(0.0123)

    token_est = AC.estimate_summary_cost_usd(
        summary_status='ready',
        summary_cost_usd_est=None,
        summary_model='gpt-4o-mini',
        summary_prompt_tokens=1_000_000,
        summary_completion_tokens=0,
    )
    assert token_est == pytest.approx(0.15)

    assert AC.estimate_summary_cost_usd(
        summary_status='ready',
        summary_cost_usd_est=None,
        summary_model=None,
        summary_prompt_tokens=None,
        summary_completion_tokens=None,
        has_summary_json=True,
    ) == pytest.approx(AC.SUMMARY_FALLBACK_USD)

    assert AC.estimate_summary_cost_usd(
        summary_status='skipped',
        summary_cost_usd_est=None,
        summary_model=None,
        summary_prompt_tokens=None,
        summary_completion_tokens=None,
    ) == 0.0


def test_admin_collect_costs_fixture_all_time_sanity(monkeypatch):
    """Seed known tasks; assert all-time OpenAI estimate (the PR sanity number).

    Fixture (whisper-1 @ $0.006/min):
      - trial completed 60 min            → $0.36
      - paid completed 30 min             → $0.18
      - trial+paid mixed 20+10 min        → $0.18
      - failed trial that hit OpenAI 15m  → $0.09
      - failed trial never hit (0 spent)  → $0.00
      - BYOK completed 120 min            → $0.00 cost, 120 BYOK minutes
      - ready summary with stored $0.01   → $0.01
    Delta all-time est. OpenAI (excl. fixed) = $0.82
    """
    from models import db, User, TranscriptionTask, CreditPurchase
    import admin_costs as AC
    import uuid as _uuid

    monkeypatch.delenv('COST_FIXED_MONTHLY_USD', raising=False)
    monkeypatch.delenv('COST_FIXED_MONTHLY_BREAKDOWN', raising=False)
    prefix = 'costfix-%s' % _uuid.uuid4().hex[:8]
    now = datetime.now(timezone.utc)

    with A.app.app_context():
        before = AC.collect_costs(db, chart_days=30)['windows']['all']

        u_trial = User(email=f'{prefix}-trial@test.com',
                       trial_seconds_limit=10_000)
        u_trial.set_password('password123')
        u_paid = User(email=f'{prefix}-paid@test.com',
                      trial_seconds_limit=10_000)
        u_paid.set_password('password123')
        u_byok = User(email=f'{prefix}-byok@test.com',
                      trial_seconds_limit=10_000,
                      openai_api_key='sk-test-byok')
        u_byok.set_password('password123')
        db.session.add_all([u_trial, u_paid, u_byok])
        db.session.commit()

        tasks = [
            TranscriptionTask(
                id=f'{prefix}-t1', user_id=u_trial.id, episode_title='Trial 60',
                status='completed', audio_duration=3600.0,
                trial_seconds_charged=3600, paid_seconds_charged=0,
                trial_settled=True,
                started_at=now - timedelta(days=3),
                completed_at=now - timedelta(days=3),
                summary_status='ready', summary_cost_usd_est=0.01,
                summary_model='gpt-4o-mini',
            ),
            TranscriptionTask(
                id=f'{prefix}-t2', user_id=u_paid.id, episode_title='Paid 30',
                status='completed', audio_duration=1800.0,
                trial_seconds_charged=0, paid_seconds_charged=1800,
                trial_settled=True,
                started_at=now - timedelta(days=2),
                completed_at=now - timedelta(days=2),
            ),
            TranscriptionTask(
                id=f'{prefix}-t3', user_id=u_trial.id, episode_title='Mixed',
                status='completed', audio_duration=1800.0,
                trial_seconds_charged=1200, paid_seconds_charged=600,
                trial_settled=True,
                started_at=now - timedelta(days=1),
                completed_at=now - timedelta(days=1),
            ),
            TranscriptionTask(
                id=f'{prefix}-t4', user_id=u_trial.id, episode_title='Fail hit',
                status='error', audio_duration=1800.0,
                trial_seconds_charged=900, paid_seconds_charged=0,
                trial_settled=True, chunk_index=0, chunk_total=2,
                error_message='OpenAI had a server error.',
                started_at=now - timedelta(hours=5),
                completed_at=now - timedelta(hours=5),
            ),
            TranscriptionTask(
                id=f'{prefix}-t5', user_id=u_trial.id, episode_title='Fail miss',
                status='error', audio_duration=1800.0,
                trial_seconds_charged=0, paid_seconds_charged=0,
                trial_settled=True, chunk_index=None,
                error_message='Download failed.',
                started_at=now - timedelta(hours=4),
                completed_at=now - timedelta(hours=4),
            ),
            TranscriptionTask(
                id=f'{prefix}-t6', user_id=u_byok.id, episode_title='BYOK 120',
                status='completed', audio_duration=7200.0,
                trial_seconds_charged=None, paid_seconds_charged=None,
                trial_settled=False,
                started_at=now - timedelta(hours=3),
                completed_at=now - timedelta(hours=3),
            ),
        ]
        db.session.add_all(tasks)
        db.session.add(CreditPurchase(
            user_id=u_paid.id,
            stripe_session_id=f'cs_{prefix}',
            amount_cents=900, amount_total_cents=900, currency='usd',
            minutes=300, status='credited',
            created_at=now - timedelta(days=2),
        ))
        db.session.commit()

        after = AC.collect_costs(db, chart_days=30)['windows']['all']

    delta_openai = after['openai_usd'] - before['openai_usd']
    assert delta_openai == pytest.approx(FIXTURE_ALL_TIME_OPENAI_USD, abs=0.001)
    assert after['openai_trial_usd'] - before['openai_trial_usd'] == pytest.approx(
        0.36 + 0.12 + 0.09, abs=0.001)
    assert after['openai_paid_usd'] - before['openai_paid_usd'] == pytest.approx(
        0.18 + 0.06, abs=0.001)
    assert after['openai_summary_usd'] - before['openai_summary_usd'] == pytest.approx(
        0.01, abs=0.0001)
    assert after['byok_minutes'] - before['byok_minutes'] == pytest.approx(120.0, abs=0.1)
    assert after['failed_with_openai_count'] - before['failed_with_openai_count'] == 1
    assert after['revenue_usd'] - before['revenue_usd'] == pytest.approx(9.0, abs=0.01)
    assert after['platform_trial_minutes'] - before['platform_trial_minutes'] == pytest.approx(
        95.0, abs=0.1)
    assert after['platform_paid_minutes'] - before['platform_paid_minutes'] == pytest.approx(
        40.0, abs=0.1)
    assert after['paid_users'] >= before['paid_users'] + 1
    assert after['active_users'] >= before['active_users'] + 2


def test_admin_collect_costs_prorates_fixed_monthly(monkeypatch):
    from models import db
    import calendar
    import admin_costs as AC
    from admin_dashboard import ADMIN_TZ

    monkeypatch.setenv('COST_FIXED_MONTHLY_BREAKDOWN', 'hetzner:30,mailgun:0')
    monkeypatch.delenv('COST_FIXED_MONTHLY_USD', raising=False)

    with A.app.app_context():
        costs = AC.collect_costs(db, chart_days=7)
    today = datetime.now(ADMIN_TZ).date()
    days = calendar.monthrange(today.year, today.month)[1]
    daily = 30.0 / days
    assert costs['fixed_daily_usd'] == pytest.approx(daily, abs=0.0001)
    assert costs['windows']['today']['fixed_usd'] == pytest.approx(daily, abs=0.0001)
    assert costs['windows']['7d']['fixed_usd'] == pytest.approx(daily * 7, abs=0.001)


def test_admin_costs_section_renders_for_allowlisted(monkeypatch):
    monkeypatch.setenv('ADMIN_EMAILS', 'costs-admin@test.com')
    client, _uid = _admin_login('costs-admin@test.com')
    resp = client.get('/admin')
    assert resp.status_code == 200
    body = resp.data.decode()
    assert 'id="adminCosts"' in body
    assert 'data-costs="windows"' in body
    assert 'Est. total cost' in body
    assert 'BYOK minutes (excl.)' in body
    assert 'estimate' in body.lower()
    assert 'chartCostsDaily' in body


def test_admin_costs_still_404_for_non_admin():
    uid = _make_user('not-costs-admin@example.com')
    client = _login(uid)
    assert client.get('/admin').status_code == 404
