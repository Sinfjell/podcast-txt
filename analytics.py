"""Product analytics via PostHog — funnel events; session replay is client-side.

On only in production when POSTHOG_KEY is set (see runtime_env.is_production).
Soft-imports the SDK so a deploy that skips `pip install` degrades rather
than dies, matching observability.py.

Never put email addresses, OpenAI API keys, passwords, or transcript text in
event properties. distinct_id is the internal user id as a string.

Consent (browser): the client SDK starts with persistence 'memory' and
opt_out_capturing_by_default until the visitor Accepts
(podskrift_cookie_consent). Session recording and identify() run only after
Accept. Decline keeps capturing off. See static/cookie-consent.js and
templates/base.html.

Server-side capture() never reads browser cookies. It keys events on the
authenticated internal user id. Optional $session_id is only attached when
checkout stamps ph_sid — and app.py ignores ph_sid unless the consent cookie
is 'accepted'. Stripe webhook metadata therefore cannot invent a cookie id
without a prior consented checkout.
"""

import logging
import os

from runtime_env import is_production, release_sha, resolve_environment

try:
    from posthog import Posthog
except ImportError:  # pragma: no cover - exercised only on a stale venv
    Posthog = None

DEFAULT_HOST = 'https://eu.i.posthog.com'

# None = not initialised; False = disabled; Posthog instance = on.
_client = None


def posthog_key():
    """Public project key for the browser snippet, or '' when analytics are off.

    Empty outside production so templates never load the client SDK against
    the live project from CI, pytest or sandboxes — even if POSTHOG_KEY is set.
    """
    if not is_production():
        return ''
    return os.getenv('POSTHOG_KEY', '').strip()


def posthog_host():
    return (os.getenv('POSTHOG_HOST', '') or DEFAULT_HOST).strip().rstrip('/')


def init_posthog(**overrides):
    """Start the SDK if production and POSTHOG_KEY is set. Returns whether on.

    `overrides` exists for the test suite (swap in a fake client / force a key).
    A forced ``project_api_key`` still requires production — tests inject a
    fake via ``_client`` instead of talking to the network.
    """
    global _client
    if not is_production():
        _client = False
        return False
    key = overrides.pop('project_api_key', None) or os.getenv('POSTHOG_KEY', '').strip()
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
        # sync_mode=False: queue events and flush asynchronously (default SDK
        # behaviour). True would flush each capture() synchronously before
        # returning — slower request paths, not a person-property privacy flag.
        sync_mode=False,
    )
    options.update(overrides)
    _client = Posthog(**options)
    return True


def get_client():
    """Return the live client, initialising lazily, or None when disabled."""
    global _client
    if not is_production():
        # Refuse a leftover real SDK instance; test doubles on `_client` still work.
        if _client is False or _client is None:
            _client = False
            return None
        if Posthog is not None and isinstance(_client, Posthog):
            _client = False
            return None
        return _client
    if _client is None:
        init_posthog()
    return None if _client is False else _client


def capture(event, distinct_id, properties=None, uuid=None):
    """Fire a named event. Never raises. No-op when disabled.

    Callers must not pass email, keys, passwords, or transcript text.
    Optional `uuid` is passed through for idempotent dedupe when the SDK
    supports it. Production events carry ``environment`` and ``release``.
    """
    if not distinct_id:
        return
    try:
        client = get_client()
        if client is None:
            return
        props = dict(properties or {})
        props['app'] = 'podskrift'
        if is_production():
            props.setdefault('environment', resolve_environment())
            release = release_sha()
            if release:
                props.setdefault('release', release)
        kwargs = {
            'distinct_id': str(distinct_id),
            'properties': props,
        }
        if uuid is not None:
            kwargs['uuid'] = str(uuid)
        client.capture(event, **kwargs)
    except Exception:  # noqa: BLE001 - analytics must never break a request
        logging.getLogger(__name__).exception('posthog capture failed for %s', event)


def openai_error_code(exc):
    """Best-effort OpenAI error `code` (e.g. insufficient_quota). Never the message."""
    if exc is None:
        return None
    code = getattr(exc, 'code', None)
    if isinstance(code, str) and code:
        return code
    body = getattr(exc, 'body', None)
    if isinstance(body, dict):
        nested = body.get('error')
        if isinstance(nested, dict):
            nested_code = nested.get('code')
            if isinstance(nested_code, str) and nested_code:
                return nested_code
        top = body.get('code')
        if isinstance(top, str) and top:
            return top
    return None


def openai_fail_reason(exc=None, *, looks_like_key=True, key_source=None):
    """Coarse reason for openai_key_validation_failed / transcript_failed.

    Values: invalid_key | no_billing | own_key_invalid | own_key_no_credit |
    rate_limit | network | other.
    Never includes key material.

    When `key_source` is ``'user'`` (BYOK), auth/billing failures are remapped
    to ``own_key_*`` so product analytics can tell a user's empty OpenAI
    account apart from our platform key being out of credit.
    """
    if not looks_like_key:
        return 'invalid_key'
    if exc is None:
        return 'other'
    status = getattr(exc, 'status_code', None)
    if status == 401 or status == 403:
        reason = 'invalid_key'
    elif status == 429:
        # models.list can 429 either way; Whisper usually sends a code.
        code = openai_error_code(exc)
        if code == 'rate_limit_exceeded':
            reason = 'rate_limit'
        else:
            # insufficient_quota, or a bare 429 with no code (common on empty billing).
            reason = 'no_billing'
    else:
        reason = None
    if reason is None:
        # Import locally so analytics stays importable without the OpenAI SDK.
        try:
            from openai import APIConnectionError, APITimeoutError
        except ImportError:  # pragma: no cover
            APIConnectionError = APITimeoutError = ()
        if isinstance(exc, (APIConnectionError, APITimeoutError)):
            reason = 'network'
        elif status is not None and 500 <= status < 600:
            reason = 'network'
        else:
            reason = 'other'
    if key_source == 'user':
        if reason == 'no_billing':
            return 'own_key_no_credit'
        if reason == 'invalid_key':
            return 'own_key_invalid'
    return reason
