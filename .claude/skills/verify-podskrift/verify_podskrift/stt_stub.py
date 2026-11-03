"""In-process STT stub for verify (default). Never calls a real provider.

Installed by the verify server launcher via monkeypatch before any request.
Does not modify ``stt.py`` shadow comparison code or prod config.
"""

from __future__ import annotations

import os
import types
from typing import Any


STUB_TEXT = (
    'This is a Podskrift verify stub transcript. '
    'It proves job lifecycle, chunking, and status without a real STT call.'
)

STUB_FAIL_MARKER = 'verify-fail'


def should_fail(chunk_file: str | None = None, task_id: str | None = None) -> bool:
    mode = (os.environ.get('PODSKRIFT_VERIFY_STT') or 'stub').strip().lower()
    if mode in ('fail', 'stub-fail'):
        return True
    name = (chunk_file or '').lower()
    if STUB_FAIL_MARKER in name or '/fail.mp3' in name:
        return True
    if task_id:
        try:
            from models import TranscriptionTask, db
            task = db.session.get(TranscriptionTask, task_id)
            for attr in ('source_audio_url', 'rss_url'):
                url = (getattr(task, attr, None) or '').lower()
                if STUB_FAIL_MARKER in url or '/fail.mp3' in url:
                    return True
        except Exception:  # noqa: BLE001 - stub must not crash the worker
            pass
    return False


def stub_whisper_transcribe_chunk(
        openai_client, chunk_file, language, *,
        task_id=None, chunk_label=None,
        attempts=None, model=None, chunk_seconds=None,
        job=None):
    """Drop-in replacement for ``app._whisper_transcribe_chunk``."""
    if should_fail(chunk_file, task_id=task_id):
        raise RuntimeError(
            'verify stub STT failure path '
            '(PODSKRIFT_VERIFY_STT=fail or fail.mp3 / verify-fail URL)')

    # Prefer measured chunk length when the app already probed it.
    dur = float(chunk_seconds or 0)
    if dur <= 0:
        try:
            import app as A
            dur = float(A.probe_audio_duration(chunk_file) or 0.0)
        except Exception:  # noqa: BLE001 - stub must never raise on probe
            dur = 1.0
    if dur <= 0:
        dur = 1.0

    text = STUB_TEXT
    if language:
        text = f'{STUB_TEXT} (language={language})'
    return types.SimpleNamespace(
        text=text,
        segments=[types.SimpleNamespace(start=0.0, end=dur, text=text)],
        language=language or 'en',
    )


def install_stubs(app_module: Any) -> dict[str, str]:
    """Monkeypatch the running app module. Returns what was installed."""
    mode = (os.environ.get('PODSKRIFT_VERIFY_STT') or 'stub').strip().lower()
    installed = {'stt_mode': mode}
    if mode == 'full':
        installed['whisper'] = 'real'
        return installed

    app_module._whisper_transcribe_chunk = stub_whisper_transcribe_chunk
    installed['whisper'] = 'stub'

    # Loopback fixture audio is served by our stub HTTP; allow it through the
    # SSRF guard only while PODSKRIFT_VERIFY=1.
    real_fetchable = app_module._is_fetchable_url

    def verify_fetchable(raw):
        try:
            from urllib.parse import urlparse
            parsed = urlparse(raw)
            host = (parsed.hostname or '').lower()
            stub_port = os.environ.get('PODSKRIFT_VERIFY_STUB_PORT', '')
            if host in ('127.0.0.1', 'localhost') and stub_port and (
                    f':{stub_port}' in (raw or '')):
                return parsed.scheme in ('http', 'https')
        except Exception:  # noqa: BLE001
            pass
        return real_fetchable(raw)

    app_module._is_fetchable_url = verify_fetchable
    installed['fetchable'] = 'loopback-stub-allowed'
    return installed
