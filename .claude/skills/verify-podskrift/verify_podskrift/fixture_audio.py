"""Generate short fixture MP3s with ffmpeg (no PII)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from .paths import FIXTURES


def ensure_short_mp3(*, seconds: float = 3.0, name: str = 'short-3s.mp3') -> Path:
    """Return path to a mono 16 kHz sine MP3, creating it if missing."""
    FIXTURES.mkdir(parents=True, exist_ok=True)
    path = FIXTURES / name
    if path.is_file() and path.stat().st_size > 0:
        return path
    subprocess.run(
        [
            'ffmpeg', '-v', 'error', '-y',
            '-f', 'lavfi', '-i', f'sine=frequency=440:duration={seconds}',
            '-ac', '1', '-ar', '16000', '-b:a', '32k',
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


def ensure_fail_mp3(*, seconds: float = 2.0) -> Path:
    """Audio whose filename triggers the stub failure path."""
    return ensure_short_mp3(seconds=seconds, name='verify-fail-2s.mp3')
