"""Hard guards for ``transcribe --stt full`` (real provider call).

Refuses unless:
- audio duration is measured and under 30 s (unknown duration = refuse)
- at most one full call has been recorded in this verify worktree state
- (CI / missing process-env key are enforced earlier in ``build_verify_environ``)
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .env import GuardError
from .paths import FIXTURES
from .state import load_state, update_state

FULL_STT_MAX_SECONDS = 30.0
FULL_STT_CALLS_KEY = 'full_stt_calls'


def probe_duration_seconds(path: Path | str) -> float | None:
    """Return measured duration in seconds, or None if it cannot be determined.

    Prefers ffprobe; falls back to mutagen when available.
    """
    audio = Path(path)
    if not audio.is_file():
        return None

    try:
        out = subprocess.run(
            [
                'ffprobe', '-v', 'error',
                '-show_entries', 'format=duration',
                '-of', 'default=noprint_wrappers=1:nokey=1',
                str(audio),
            ],
            capture_output=True, text=True, timeout=60, check=True,
        )
        duration = float(out.stdout.strip())
        if duration > 0:
            return duration
    except (subprocess.SubprocessError, ValueError, OSError):
        pass

    try:
        from mutagen import File as MutagenFile  # type: ignore
    except ImportError:
        return None
    try:
        meta = MutagenFile(str(audio))
        length = getattr(meta, 'info', None)
        duration = float(getattr(length, 'length', 0) or 0)
        if duration > 0:
            return duration
    except Exception:  # noqa: BLE001 — any parse failure → unknown
        return None
    return None


def _fixture_for_stub_url(audio_url: str) -> Path | None:
    """Map default fixture HTTP paths to on-disk fixtures (no download)."""
    lower = audio_url.split('?', 1)[0].lower()
    if lower.endswith('/fail.mp3') or lower.endswith('/verify-fail-2s.mp3'):
        path = FIXTURES / 'verify-fail-2s.mp3'
        return path if path.is_file() else None
    if lower.endswith('/audio.mp3') or lower.endswith('/short.mp3'):
        path = FIXTURES / 'short-3s.mp3'
        return path if path.is_file() else None
    return None


def materialize_audio(audio_url: str, *, dest_dir: Path | None = None) -> Path:
    """Resolve *audio_url* (file path or http(s) URL) to a local path for probing."""
    raw = (audio_url or '').strip()
    if not raw:
        raise GuardError('full STT: audio URL is empty — refuse.')

    as_path = Path(raw)
    if as_path.is_file():
        return as_path.resolve()

    fixture = _fixture_for_stub_url(raw)
    if fixture is not None:
        return fixture.resolve()

    if not (raw.startswith('http://') or raw.startswith('https://')):
        raise GuardError(
            f'full STT: cannot locate audio at {raw!r} — refuse.')

    parent = dest_dir or Path(tempfile.mkdtemp(prefix='podskrift-verify-audio-'))
    parent.mkdir(parents=True, exist_ok=True)
    dest = parent / 'input.audio'
    try:
        with urllib.request.urlopen(raw, timeout=30) as resp:
            dest.write_bytes(resp.read())
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise GuardError(
            f'full STT: failed to fetch audio for duration check ({exc}) — refuse.'
        ) from exc
    if not dest.is_file() or dest.stat().st_size <= 0:
        raise GuardError('full STT: downloaded audio is empty — refuse.')
    return dest


def full_stt_calls_recorded(state: dict[str, Any] | None = None) -> int:
    st = state if state is not None else load_state()
    try:
        return int(st.get(FULL_STT_CALLS_KEY) or 0)
    except (TypeError, ValueError):
        return 0


def record_full_stt_call(*, root: Path | None = None) -> int:
    """Persist that one real provider call is reserved for this verify run."""
    st = load_state(root)
    n = full_stt_calls_recorded(st) + 1
    update_state({FULL_STT_CALLS_KEY: n}, root)
    return n


def assert_full_stt_transcribe_allowed(
        audio_url: str,
        *,
        state: dict[str, Any] | None = None,
        dest_dir: Path | None = None,
) -> float:
    """Hard-stop guard before a real Whisper call.

    Returns measured duration (seconds). Raises ``GuardError`` when refused.
    """
    st = state if state is not None else load_state()
    if full_stt_calls_recorded(st) >= 1:
        raise GuardError(
            'full STT: already used the one allowed provider call for this '
            'verify run — refuse a second.')

    if os.environ.get('CI') or os.environ.get('GITHUB_ACTIONS'):
        raise GuardError('full STT is refused in CI.')

    path = materialize_audio(audio_url, dest_dir=dest_dir)
    duration = probe_duration_seconds(path)
    if duration is None:
        raise GuardError(
            'full STT: could not determine audio duration — refuse '
            '(need measurable input under 30 s).')
    if duration >= FULL_STT_MAX_SECONDS:
        raise GuardError(
            f'full STT: audio is {duration:.1f}s; must be under '
            f'{FULL_STT_MAX_SECONDS:.0f}s — refuse.')
    return duration
