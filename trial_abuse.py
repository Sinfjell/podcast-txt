"""Trial-abuse checks run once, at signup, before the free trial is stamped.

Pure helpers live here so they are testable without the Flask app. The app
decides what to do with the verdict (see ``register()`` in app.py): a denied
signup still gets an account, just with ``trial_seconds_limit = 0``.

Signals (first match wins, order is the ``reason`` priority):

* ``disposable_email``  -- domain on the bundled disposable list
* ``duplicate_email``   -- normalized address already has an account
                           (gmail: dots and +tags ignored; others: +tag ignored)
* ``similar_email``     -- same local part once trailing digits are stripped,
                           signed up within ``SIMILAR_WINDOW`` (name1, name2, ...)
* ``same_device``       -- device cookie already got a trial within ``REUSE_WINDOW``
* ``same_ip``           -- hashed signup IP already got a trial within ``REUSE_WINDOW``

IPs are never stored raw: ``hash_ip`` is an HMAC keyed with the app secret.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

GMAIL_DOMAINS = frozenset({'gmail.com', 'googlemail.com'})
SIMILAR_WINDOW = timedelta(hours=24)
REUSE_WINDOW = timedelta(days=7)
DEVICE_COOKIE = 'ps_did'
_DEVICE_RE = re.compile(r'^[a-f0-9]{32}$')

_HERE = os.path.dirname(os.path.abspath(__file__))
DISPOSABLE_LIST_PATH = os.path.join(_HERE, 'disposable_email_domains.txt')


def load_disposable_domains(path: str = DISPOSABLE_LIST_PATH) -> frozenset:
    """Domains from the bundled list; empty set if the file is missing."""
    try:
        with open(path, encoding='utf-8') as fh:
            return frozenset(
                line.strip().lower() for line in fh
                if line.strip() and not line.lstrip().startswith('#'))
    except OSError:
        return frozenset()


def normalize_email(email: str) -> str:
    """Canonical mailbox: lowercased; +tag dropped; gmail dots dropped."""
    email = (email or '').strip().lower()
    if '@' not in email:
        return email
    local, domain = email.rsplit('@', 1)
    local = local.split('+', 1)[0]
    if domain in GMAIL_DOMAINS:
        local = local.replace('.', '')
        domain = 'gmail.com'
    return f'{local}@{domain}'


def email_base(normalized: str) -> str:
    """Normalized address with trailing digits stripped from the local part."""
    if '@' not in normalized:
        return normalized
    local, domain = normalized.rsplit('@', 1)
    return f'{local.rstrip("0123456789")}@{domain}'


def is_disposable(email: str, domains: Iterable[str]) -> bool:
    domain = (email or '').rsplit('@', 1)[-1].strip().lower()
    if not domain:
        return False
    if domain in domains:
        return True
    # Subdomains of a listed domain (x.mailinator.com) count too.
    parts = domain.split('.')
    return any('.'.join(parts[i:]) in domains for i in range(1, len(parts) - 1))


def hash_ip(ip: Optional[str], secret: str) -> Optional[str]:
    if not ip or ip == 'unknown':
        return None
    return hmac.new(secret.encode('utf-8'), ip.encode('utf-8'),
                    hashlib.sha256).hexdigest()


def valid_device_id(value: Optional[str]) -> Optional[str]:
    """Accept only ids we minted (32 lowercase hex); anything else is ignored."""
    if value and _DEVICE_RE.match(value):
        return value
    return None


def _aware(ts):
    if ts is None:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def denial_reason(*, email: str, ip_hash: Optional[str],
                  device_id: Optional[str], existing, disposable_domains,
                  now: Optional[datetime] = None) -> Optional[str]:
    """Return a reason string to deny the trial, or None to grant it.

    ``existing`` yields rows with ``email``, ``email_normalized``,
    ``created_at``, ``signup_ip_hash``, ``signup_device_id``,
    ``trial_seconds_limit`` and ``trial_denied_reason`` (other accounts only).
    """
    now = now or datetime.now(timezone.utc)
    if is_disposable(email, disposable_domains):
        return 'disposable_email'
    norm = normalize_email(email)
    base = email_base(norm)
    similar_since = now - SIMILAR_WINDOW
    reuse_since = now - REUSE_WINDOW
    reasons = set()
    for row in existing:
        other = row.email_normalized or normalize_email(row.email)
        created = _aware(row.created_at)
        if other == norm:
            reasons.add('duplicate_email')
            continue
        recent = created is not None and created >= similar_since
        if (recent and len(base.split('@', 1)[0]) >= 3
                and email_base(other) == base):
            reasons.add('similar_email')
        got_trial = (row.trial_denied_reason is None
                     and (row.trial_seconds_limit is None
                          or row.trial_seconds_limit > 0))
        if not got_trial or created is None or created < reuse_since:
            continue
        if device_id and row.signup_device_id == device_id:
            reasons.add('same_device')
        if ip_hash and row.signup_ip_hash == ip_hash:
            reasons.add('same_ip')
    for reason in ('duplicate_email', 'similar_email', 'same_device', 'same_ip'):
        if reason in reasons:
            return reason
    return None
