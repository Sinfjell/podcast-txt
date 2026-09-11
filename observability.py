"""Error reporting to Sentry -- light coverage, not APM.

Off unless SENTRY_DSN is set, and it is only set on the production server.
What gets reported:

- uncaught exceptions in requests (the Flask integration),
- ERROR log records (the default logging integration),
- a transcription task that fails (report_task_failure),
- a task whose worker went quiet and was failed by a sweep (report_stale_task).

The scrubbing here is the point of this module. OpenAI echoes the submitted key
back in a 401, users have pasted passwords into that field, and private podcast
feeds carry their access token in the audio URL. None of that may leave the box,
so request bodies, local variables and PII are never collected, and every string
in an event passes through the redactor before it is sent.
"""

import logging
import os
import re

# The documented deploy is `git pull && systemctl restart`, with no pip step.
# A hard import would turn that deploy into an outage the first time, so a
# missing SDK only disables reporting -- loudly, if a DSN says it should be on.
try:
    import sentry_sdk
    from sentry_sdk.integrations.flask import FlaskIntegration
except ImportError:  # pragma: no cover - exercised only on a stale venv
    sentry_sdk = None

REDACTED = '[redacted]'
REDACTED_OPENAI = '[OpenAI error message redacted: it can echo the submitted key]'

# The key shapes OpenAI issues (sk-..., sk-proj-...), plus the phrase its 401
# uses to echo whatever was submitted -- which is how a password leaks.
_SECRET_PATTERNS = (
    re.compile(r'sk-[A-Za-z0-9_\-*]{8,}'),
    re.compile(r'(Incorrect API key provided:\s*)\S+'),
)
# Private feeds (Supercast, Patreon, Memberful) authorise by query string.
_URL_QUERY = re.compile(r'(https?://[^\s?#\'"]+)\?[^\s#\'"]*')


def _redact_text(value):
    for pattern in _SECRET_PATTERNS:
        value = pattern.sub(lambda m: (m.group(1) if m.groups() else '') + REDACTED, value)
    return _URL_QUERY.sub(lambda m: f'{m.group(1)}?{REDACTED}', value)


def _redact(node):
    """Walk an event and redact every string in it, keys excepted."""
    if isinstance(node, str):
        return _redact_text(node)
    if isinstance(node, dict):
        return {k: _redact(v) for k, v in node.items()}
    if isinstance(node, (list, tuple)):
        return [_redact(v) for v in node]
    return node


def _is_openai_module(module):
    return (module or '').split('.')[0] == 'openai'


def scrub_event(event, hint):
    """before_send: drop OpenAI's own message outright, then redact the rest.

    The pattern redactor alone is not enough for OpenAI errors: a password
    pasted into the key field matches no key pattern, and the 401 wording may
    change. The exception type, module and stack still identify the failure.
    """
    for value in (event.get('exception') or {}).get('values') or []:
        if _is_openai_module(value.get('module')):
            value['value'] = REDACTED_OPENAI
    return _redact(event)


def scrub_breadcrumb(crumb, hint):
    return _redact(crumb)


def init_sentry(**overrides):
    """Start the SDK if SENTRY_DSN is set. Returns whether it is on.

    `overrides` exists for the test suite, which swaps in a capturing transport.
    """
    dsn = overrides.pop('dsn', None) or os.getenv('SENTRY_DSN', '').strip()
    if not dsn:
        return False
    if sentry_sdk is None:
        logging.getLogger(__name__).error(
            'SENTRY_DSN is set but sentry-sdk is not installed; errors are not '
            'being reported. Run pip install -r requirements.txt.')
        return False
    options = dict(
        dsn=dsn,
        environment=os.getenv('SENTRY_ENVIRONMENT', 'production'),
        release=os.getenv('SENTRY_RELEASE') or None,
        integrations=[FlaskIntegration()],
        # Errors only. Performance tracing is out of scope and costs quota.
        traces_sample_rate=0.0,
        # No cookies, headers, user IPs or form bodies: the settings form
        # carries the OpenAI key and the login form a password.
        send_default_pii=False,
        max_request_body_size='never',
        # Stack-frame locals would include `key`, `api_key` and the client.
        include_local_variables=False,
        before_send=scrub_event,
        before_breadcrumb=scrub_breadcrumb,
    )
    options.update(overrides)
    sentry_sdk.init(**options)
    return True


def report_task_failure(exc, task_id, key_source):
    """Report a transcription task that ended in error. Never raises.

    OpenAI failures are grouped by status and key source, so a wave of 429s on
    the trial key is one issue that alerts once -- and is told apart from a
    BYOK user's rejected key, which is theirs to fix, not ours.
    """
    try:
        tags = {'task.key_source': key_source or 'unknown'}
        fingerprint = None
        if _is_openai_module(type(exc).__module__):
            status = getattr(exc, 'status_code', None)
            tags['openai.status'] = str(status or type(exc).__name__)
            fingerprint = ['openai-error', tags['task.key_source'], tags['openai.status']]
        sentry_sdk.capture_exception(
            exc, tags=tags, fingerprint=fingerprint,
            contexts={'task': {'id': task_id}})
    except Exception:  # noqa: BLE001 - reporting must never break the refund path
        pass


def report_stale_task(task_id, last_status, quiet_seconds, source):
    """Report a task the stale sweep failed: its worker died or hung. Never raises."""
    try:
        sentry_sdk.capture_message(
            'Transcription task stopped making progress',
            level='warning',
            fingerprint=['stale-task'],
            tags={'task.last_status': str(last_status), 'sweep.source': source},
            contexts={'task': {'id': task_id, 'quiet_seconds': int(quiet_seconds)}})
    except Exception:  # noqa: BLE001 - reporting must never break the sweep
        pass
