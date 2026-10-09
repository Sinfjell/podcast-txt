"""Mailgun transactional mail — EU region, feature-flagged, never raises into callers.

Off unless EMAIL_ENABLED is truthy and MAILGUN_API_KEY is set.
Domain defaults to podskrift.com (root sending domain, EU). Misconfiguration or
Mailgun failures are logged; clear 5xx responses retry with backoff and a
stable Message-Id; ambiguous transport errors (timeouts) are not retried so a
late accept cannot double-send. Each failure *kind* is reported to Sentry once
per process (not once per recipient). 4xx is logged at warning without PII so
the logging integration does not create a Sentry event per bad address.

Mailgun click/open tracking is forced off — we use our own utm params instead
(email.podskrift.com tracking CNAME may exist but is unused by default).
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
import uuid
from typing import Iterable, Mapping, Optional

import requests

try:
    import sentry_sdk
except ImportError:  # pragma: no cover
    sentry_sdk = None

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = 'https://api.eu.mailgun.net'
DEFAULT_DOMAIN = 'podskrift.com'
DEFAULT_FROM = 'Podskrift <hello@podskrift.com>'
DEFAULT_TIMEOUT_SEC = 10

#: send_email outcomes. 'ambiguous' = transport timeout / connection drop —
#: the request may already have been accepted, so callers must not release
#: an idempotency claim (or retry with a new Message-Id).
SEND_SENT = 'sent'
SEND_FAILED = 'failed'
SEND_AMBIGUOUS = 'ambiguous'

# Kinds already reported this process lifetime — avoid inbox-scale Sentry spam.
_sentry_kinds_reported: set[str] = set()


def stable_message_id(kind: str, idempotency_key: str = '') -> str:
    """RFC 5322 Message-Id reused across retries of the same logical send."""
    if idempotency_key:
        digest = hashlib.sha256(
            idempotency_key.encode('utf-8', errors='replace')
        ).hexdigest()[:24]
        local = f'{kind}.{digest}'
    else:
        local = f'{kind}.{uuid.uuid4().hex}'
    # Keep chars conservative for header safety.
    safe = ''.join(c if c.isalnum() or c in '.-_' else '-' for c in local)[:72]
    return f'<{safe}@podskrift.com>'


def _truthy(raw: Optional[str]) -> bool:
    if raw is None:
        return False
    return raw.strip().lower() in ('1', 'true', 'yes', 'on')


def email_enabled() -> bool:
    return _truthy(os.getenv('EMAIL_ENABLED', '0'))


def mailgun_api_key() -> str:
    """Domain sending API key from Mailgun."""
    return (os.getenv('MAILGUN_API_KEY') or '').strip()


def mailgun_domain() -> str:
    return (os.getenv('MAILGUN_DOMAIN') or '').strip() or DEFAULT_DOMAIN


def mailgun_base_url() -> str:
    return (os.getenv('MAILGUN_BASE_URL') or '').strip().rstrip('/') or DEFAULT_BASE_URL


def mail_from() -> str:
    return (os.getenv('MAIL_FROM') or '').strip() or DEFAULT_FROM


def mail_reply_to() -> str:
    """Optional. Replies to hello@ are forwarded by a Mailgun route — leave unset."""
    return (os.getenv('MAIL_REPLY_TO') or '').strip()


def mail_configured() -> bool:
    # Domain always resolves (default podskrift.com); key is the gate.
    return bool(mailgun_api_key())


def mail_ready() -> bool:
    """True when sending is turned on and credentials look present."""
    return email_enabled() and mail_configured()


def _report_once(kind: str, exc: BaseException | None = None, *, message: str = ''):
    """Sentry once per failure kind for the life of the process."""
    if kind in _sentry_kinds_reported:
        return
    _sentry_kinds_reported.add(kind)
    try:
        if sentry_sdk is None:
            return
        if exc is not None:
            sentry_sdk.capture_exception(
                exc,
                fingerprint=['mailgun', kind],
                tags={'mail.kind': kind},
            )
        else:
            sentry_sdk.capture_message(
                message or f'Mailgun failure: {kind}',
                level='error',
                fingerprint=['mailgun', kind],
                tags={'mail.kind': kind},
            )
    except Exception:  # noqa: BLE001 — reporting must never break callers
        pass


def reset_sentry_kinds_for_tests():
    """Test helper: allow the suite to assert one-per-kind reporting again."""
    _sentry_kinds_reported.clear()


def send_email(
    *,
    to: str,
    subject: str,
    text: str,
    html: Optional[str] = None,
    headers: Optional[Mapping[str, str]] = None,
    tags: Optional[Iterable[str]] = None,
    kind: str = 'generic',
    max_attempts: int = 3,
    timeout: float = DEFAULT_TIMEOUT_SEC,
    message_id: Optional[str] = None,
    idempotency_key: str = '',
) -> str:
    """Send one message via Mailgun. Never raises.

    Returns SEND_SENT, SEND_FAILED, or SEND_AMBIGUOUS (timeout / connection
    error — treat as possibly delivered; do not release idempotency claims).

    When email is disabled or misconfigured: log and return SEND_FAILED.
    Retries with exponential backoff on clear 5xx responses only.
    Transport timeouts / connection errors are not retried. A stable
    Message-Id is set for every attempt of this call.
    """
    if not to or not subject:
        logger.warning('mail.send skipped (%s): missing to/subject', kind)
        return SEND_FAILED
    if not email_enabled():
        logger.info('mail.send no-op (%s): EMAIL_ENABLED is off', kind)
        return SEND_FAILED
    if not mail_configured():
        logger.warning(
            'mail.send no-op (%s): MAILGUN_API_KEY not set',
            kind,
        )
        _report_once('misconfigured', message='Mailgun not configured while EMAIL_ENABLED')
        return SEND_FAILED

    domain = mailgun_domain()
    url = f'{mailgun_base_url()}/v3/{domain}/messages'
    # Resolve Message-Id once so 5xx retries are the same logical message.
    hdrs = dict(headers or {})
    msg_id = (
        message_id
        or hdrs.get('Message-Id')
        or hdrs.get('Message-ID')
        or hdrs.get('h:Message-Id')
        or hdrs.get('h:Message-ID')
        or stable_message_id(kind, idempotency_key)
    )
    for drop in ('Message-Id', 'Message-ID', 'h:Message-Id', 'h:Message-ID'):
        hdrs.pop(drop, None)

    # List of pairs so repeated o:tag fields encode correctly.
    fields: list[tuple[str, str]] = [
        ('from', mail_from()),
        ('to', to),
        ('subject', subject),
        ('text', text or ''),
        # Prefer our utm_* links over Mailgun rewrite tracking.
        ('o:tracking', 'no'),
        ('o:tracking-clicks', 'no'),
        ('o:tracking-opens', 'no'),
        ('h:Message-Id', msg_id),
    ]
    if html:
        fields.append(('html', html))
    reply = mail_reply_to()
    if reply:
        fields.append(('h:Reply-To', reply))
    for key, value in hdrs.items():
        if not key or value is None:
            continue
        header_key = key if key.lower().startswith('h:') else f'h:{key}'
        fields.append((header_key, str(value)))
    for tag in (tags or []):
        if tag:
            fields.append(('o:tag', str(tag)))

    auth = ('api', mailgun_api_key())
    post_timeout = max(1.0, float(timeout))
    for attempt in range(1, max(1, max_attempts) + 1):
        try:
            resp = requests.post(
                url, auth=auth, data=fields, timeout=post_timeout)
            if 200 <= resp.status_code < 300:
                return SEND_SENT
            if 500 <= resp.status_code < 600:
                logger.warning(
                    'mailgun %s %s attempt %s/%s',
                    kind, resp.status_code, attempt, max_attempts,
                )
                if attempt < max_attempts:
                    time.sleep(min(8.0, 0.5 * (2 ** (attempt - 1))))
                    continue
                _report_once(
                    f'http_{resp.status_code}',
                    message=f'Mailgun {resp.status_code} for kind={kind}',
                )
                return SEND_FAILED
            # 4xx — do not retry (bad address, auth, etc.). Warning level so
            # Sentry's logging integration (ERROR+) does not create an event
            # per recipient; _report_once still records the kind once.
            logger.warning(
                'mailgun rejected %s with HTTP %s',
                kind, resp.status_code,
            )
            _report_once(
                f'http_{resp.status_code}',
                message=f'Mailgun {resp.status_code} for kind={kind}',
            )
            return SEND_FAILED
        except requests.RequestException as exc:
            # Ambiguous: the request may have reached Mailgun after we timed
            # out. Do not retry — Message-Id alone is not a guaranteed dedupe.
            logger.warning(
                'mailgun transport error (%s): %s (not retrying)',
                kind, type(exc).__name__,
            )
            _report_once('transport', exc)
            return SEND_AMBIGUOUS
    return SEND_FAILED
