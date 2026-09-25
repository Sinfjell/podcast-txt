"""Product analytics via PostHog — funnel events; session replay is client-side.

Off unless POSTHOG_KEY is set. Soft-imports the SDK so a deploy that skips
`pip install` degrades rather than dies, matching observability.py.

Never put email addresses, OpenAI API keys, passwords, or transcript text in
event properties. distinct_id is the internal user id as a string.
"""

import logging
import os

try:
    from posthog import Posthog
except ImportError:  # pragma: no cover - exercised only on a stale venv
    Posthog = None

DEFAULT_HOST = 'https://eu.i.posthog.com'

# None = not initialised; False = disabled; Posthog instance = on.
_client = None


def posthog_key():
    return os.getenv('POSTHOG_KEY', '').strip()


def posthog_host():
    return (os.getenv('POSTHOG_HOST', '') or DEFAULT_HOST).strip().rstrip('/')


def init_posthog(**overrides):
    """Start the SDK if POSTHOG_KEY is set. Returns whether it is on.

    `overrides` exists for the test suite (swap in a fake client / force a key).
    """
    global _client
    key = overrides.pop('project_api_key', None) or posthog_key()
    if not key:
        _client = False
        return False
    if Posthog is None:
        logging.getLogger(__name__).error(
            'POSTHOG_KEY is set but posthog is not installed; analytics are off. '
            'Run pip install -r requirements.txt.')
        _client = False
        return False
    host = overrides.pop('host', None) or posthog_host()
    options = dict(
        project_api_key=key,
        host=host,
        # Privacy: do not sync person properties we never set; events carry
        # distinct_id only.
        sync_mode=False,
    )
    options.update(overrides)
    _client = Posthog(**options)
    return True


def get_client():
    """Return the live client, initialising lazily, or None when disabled."""
    global _client
    if _client is None:
        init_posthog()
    return None if _client is False else _client


def capture(event, distinct_id, properties=None):
    """Fire a named event. Never raises. No-op when disabled.

    Callers must not pass email, keys, passwords, or transcript text.
    """
    client = get_client()
    if client is None or not distinct_id:
        return
    try:
        client.capture(
            event,
            distinct_id=str(distinct_id),
            properties=dict(properties or {}),
        )
    except Exception:  # noqa: BLE001 - analytics must never break a request
        logging.getLogger(__name__).exception('posthog capture failed for %s', event)


def openai_fail_reason(exc=None, *, looks_like_key=True):
    """Coarse reason for openai_key_validation_failed / transcript_failed.

    Values: invalid_key | no_billing | network | other. Never includes key material.
    """
    if not looks_like_key:
        return 'invalid_key'
    if exc is None:
        return 'other'
    status = getattr(exc, 'status_code', None)
    if status == 401 or status == 403:
        return 'invalid_key'
    if status == 429:
        return 'no_billing'
    # Import locally so analytics stays importable without the OpenAI SDK.
    try:
        from openai import APIConnectionError, APITimeoutError
    except ImportError:  # pragma: no cover
        APIConnectionError = APITimeoutError = ()
    if isinstance(exc, (APIConnectionError, APITimeoutError)):
        return 'network'
    if status is not None and 500 <= status < 600:
        return 'network'
    return 'other'
