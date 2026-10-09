"""Mailgun transactional mail — EU region, feature-flagged, never raises into callers.

Off unless EMAIL_ENABLED is truthy and MAILGUN_API_KEY is set.
Domain defaults to podskrift.com (root sending domain, EU). Misconfiguration or
Mailgun failures are logged; 5xx retries with backoff; each failure *kind* is
reported to Sentry once per process (not once per recipient).

Mailgun click/open tracking is forced off — we use our own utm params instead
(email.podskrift.com tracking CNAME may exist but is unused by default).
"""

from __future__ import annotations

import logging
import os
import time
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

# Kinds already reported this process lifetime — avoid inbox-scale Sentry spam.
_sentry_kinds_reported: set[str] = set()


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
) -> bool:
    """Send one message via Mailgun. Never raises. Returns True on 2xx.

    When email is disabled or misconfigured: log and return False.
    Retries with exponential backoff on 5xx / transport errors only.
    """
    if not to or not subject:
        logger.warning('mail.send skipped (%s): missing to/subject', kind)
        return False
    if not email_enabled():
        logger.info('mail.send no-op (%s): EMAIL_ENABLED is off', kind)
        return False
    if not mail_configured():
        logger.warning(
            'mail.send no-op (%s): MAILGUN_API_KEY not set',
            kind,
        )
        _report_once('misconfigured', message='Mailgun not configured while EMAIL_ENABLED')
        return False

    domain = mailgun_domain()
    url = f'{mailgun_base_url()}/v3/{domain}/messages'
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
    ]
    if html:
        fields.append(('html', html))
    reply = mail_reply_to()
    if reply:
        fields.append(('h:Reply-To', reply))
    for key, value in (headers or {}).items():
        if not key or value is None:
            continue
        header_key = key if key.lower().startswith('h:') else f'h:{key}'
        fields.append((header_key, str(value)))
    for tag in (tags or []):
        if tag:
            fields.append(('o:tag', str(tag)))

    auth = ('api', mailgun_api_key())
    last_exc: BaseException | None = None
    for attempt in range(1, max(1, max_attempts) + 1):
        try:
            resp = requests.post(url, auth=auth, data=fields, timeout=20)
            if 200 <= resp.status_code < 300:
                return True
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
                return False
            # 4xx — do not retry (bad address, auth, etc.)
            logger.error(
                'mailgun rejected %s with %s: %s',
                kind, resp.status_code, (resp.text or '')[:200],
            )
            _report_once(
                f'http_{resp.status_code}',
                message=f'Mailgun {resp.status_code} for kind={kind}',
            )
            return False
        except requests.RequestException as exc:
            last_exc = exc
            logger.warning(
                'mailgun transport error (%s) attempt %s/%s: %s',
                kind, attempt, max_attempts, type(exc).__name__,
            )
            if attempt < max_attempts:
                time.sleep(min(8.0, 0.5 * (2 ** (attempt - 1))))
                continue
            _report_once('transport', last_exc)
            return False
    if last_exc is not None:
        _report_once('transport', last_exc)
    return False
