"""Server-side first-touch / last-touch acquisition attribution.

Stored in the Flask session (strictly-necessary signed cookie used for auth
and signup) — not in analytics cookies — so capture does not depend on
``podskrift_cookie_consent``. PostHog ``ph_sid`` / ``ph_did`` remain
consent-gated elsewhere.

First-touch is write-once; last-touch updates whenever a landing request
carries tracked query params. Values are length-capped and stripped of
control characters before storage.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

#: Query keys we capture when present (never invent).
UTM_KEYS = (
    'utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content',
)
CLICK_ID_KEYS = (
    'gclid', 'gbraid', 'wbraid', 'fbclid', 'msclkid',
)
TRACKED_QUERY_KEYS = UTM_KEYS + CLICK_ID_KEYS

#: Caps — keep SQLite / PostHog payloads small; click ids can be longer than utm.
MAX_UTM_LEN = 64
MAX_CLICK_ID_LEN = 256
MAX_PATH_LEN = 128
MAX_HOST_LEN = 253
MAX_JSON_LEN = 4096

_CTRL_RE = re.compile(r'[\x00-\x1f\x7f]')


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sanitize_attr_value(value, *, max_len: int) -> str | None:
    """Strip controls/whitespace and truncate; None when empty after clean."""
    if value is None:
        return None
    text = _CTRL_RE.sub('', str(value)).strip()
    if not text:
        return None
    if len(text) > max_len:
        text = text[:max_len]
    return text


def referrer_host(referrer: str | None, *, own_hosts: frozenset[str] | None = None) -> str | None:
    """External Referer hostname only (never a full URL with path/query)."""
    if not referrer:
        return None
    try:
        host = (urlparse(referrer).hostname or '').lower().rstrip('.')
    except ValueError:
        return None
    if not host:
        return None
    own = own_hosts or frozenset()
    if host in own or host.endswith('.podskrift.com') or host == 'podskrift.com':
        return None
    if host in ('localhost', '127.0.0.1', '::1'):
        return None
    return sanitize_attr_value(host, max_len=MAX_HOST_LEN)


def extract_query_attrs(args) -> dict:
    """Tracked utm_* / click-id values from a query MultiDict / mapping."""
    out = {}
    get = args.get if hasattr(args, 'get') else lambda k, d=None: (args or {}).get(k, d)
    for key in UTM_KEYS:
        val = sanitize_attr_value(get(key), max_len=MAX_UTM_LEN)
        if val:
            out[key] = val
    for key in CLICK_ID_KEYS:
        val = sanitize_attr_value(get(key), max_len=MAX_CLICK_ID_LEN)
        if val:
            out[key] = val
    return out


def build_touch(
    *,
    path: str | None,
    referrer: str | None,
    args,
    own_hosts: frozenset[str] | None = None,
    touched_at: str | None = None,
    include_referrer_without_params: bool = False,
) -> dict:
    """Build one touch dict from a landing request. Empty if nothing to store.

    When ``include_referrer_without_params`` is False (last-touch), we only
    emit a touch if tracked query params are present. First-touch always
    records landing_path (+ optional external referrer / params).
    """
    params = extract_query_attrs(args)
    landing = sanitize_attr_value(path or '/', max_len=MAX_PATH_LEN) or '/'
    host = referrer_host(referrer, own_hosts=own_hosts)
    if not params and not include_referrer_without_params:
        return {}
    touch = {
        'landing_path': landing,
        'touched_at': touched_at or utc_now_iso(),
    }
    touch.update(params)
    if host:
        touch['referrer_host'] = host
    return touch


def merge_first_touch(existing: dict | None, incoming: dict | None) -> dict:
    """Write-once: keep ``existing`` when already set; else take ``incoming``."""
    if existing:
        return dict(existing)
    if incoming:
        return dict(incoming)
    return {}


def merge_last_touch(existing: dict | None, incoming: dict | None) -> dict:
    """Replace with ``incoming`` when it carries tracked params; else keep."""
    if incoming and any(k in incoming for k in TRACKED_QUERY_KEYS):
        return dict(incoming)
    return dict(existing or {})


def dumps_touch(touch: dict | None) -> str | None:
    """Serialize for a users.* TEXT column; None when empty."""
    if not touch:
        return None
    raw = json.dumps(touch, separators=(',', ':'), sort_keys=True)
    if len(raw) > MAX_JSON_LEN:
        # Drop optional keys until it fits; keep landing + timestamp + source.
        slim = {
            k: touch[k] for k in (
                'landing_path', 'touched_at', 'utm_source', 'utm_medium',
                'utm_campaign', 'gclid', 'referrer_host',
            ) if k in touch
        }
        raw = json.dumps(slim, separators=(',', ':'), sort_keys=True)[:MAX_JSON_LEN]
    return raw


def loads_touch(raw: str | None) -> dict:
    """Parse a stored JSON touch; empty dict on missing/corrupt."""
    if not raw or not isinstance(raw, str):
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def event_props_from_first_touch(touch: dict | None) -> dict:
    """Properties for signup / purchase events (first-touch only)."""
    if not touch:
        return {}
    props = {}
    for key in TRACKED_QUERY_KEYS:
        val = touch.get(key)
        if val:
            props[key] = val
    landing = touch.get('landing_path')
    if landing:
        props['landing_path'] = landing
        # Legacy signup key kept for existing dashboards / tests.
        props.setdefault('first_landing', landing)
    host = touch.get('referrer_host')
    if host:
        props['referrer_host'] = host
    at = touch.get('touched_at')
    if at:
        props['first_touch_at'] = at
    return props


def set_once_from_first_touch(touch: dict | None) -> dict:
    """PostHog ``$set_once`` person props: ``initial_*`` plus legacy keys."""
    if not touch:
        return {}
    set_once = {}
    for key in TRACKED_QUERY_KEYS:
        val = touch.get(key)
        if val:
            set_once[f'initial_{key}'] = val
            # Legacy keys (pre-attribution-PR signup events).
            if key in UTM_KEYS:
                set_once[key] = val
    landing = touch.get('landing_path')
    if landing:
        set_once['initial_landing_path'] = landing
        set_once['first_landing'] = landing
    host = touch.get('referrer_host')
    if host:
        set_once['initial_referrer_host'] = host
    at = touch.get('touched_at')
    if at:
        set_once['initial_first_touch_at'] = at
    return set_once
