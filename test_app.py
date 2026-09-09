"""Regression tests for the transcription pipeline.

Focused on the defects this suite was written to catch: a progress bar that
reported numbers it did not have, a download bar stuck at 0%, and an
unvalidated server-side fetch of a client-supplied URL.

Run: pytest test_app.py
"""

import os
import tempfile
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
