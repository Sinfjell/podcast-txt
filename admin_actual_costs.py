"""Actual OpenAI spend for the admin Costs tab (OpenAI Costs API).

Reads ``GET /v1/organization/costs`` with an organization Admin key
(``OPENAI_ADMIN_KEY``, ``sk-admin-...``). The normal project key cannot read
this endpoint. Buckets are whole UTC days, and OpenAI usually lags a few
hours, so "today" is partial and labelled that way in the UI.

Read-only. Never raises into the page: every failure becomes a payload with
``ok=False`` and a short error. The key is never logged or rendered.
"""

from __future__ import annotations

import os
import threading
import time
from datetime import date, datetime, timedelta, timezone

import requests
from sqlalchemy import text

OPENAI_COSTS_URL = 'https://api.openai.com/v1/organization/costs'
ADMIN_KEY_ENV = 'OPENAI_ADMIN_KEY'
# Optional: limit actual spend to the Podskrift project when the org has others.
PROJECT_ID_ENV = 'OPENAI_COST_PROJECT_ID'
CACHE_TTL_SEC = 15 * 60
MAX_RANGE_DAYS = 180
RANGE_PRESETS = ('today', '7d', '30d', '90d', 'custom')
COST_TYPES = ('all', 'transcription', 'summary')

_cache = {}
_lock = threading.Lock()


def admin_key_configured():
    key = (os.getenv(ADMIN_KEY_ENV) or '').strip()
    return key.startswith('sk-admin-')


def classify_line_item(line_item):
    """Map an OpenAI cost line item to transcription / summary / other."""
    s = (line_item or '').lower()
    if 'whisper' in s or 'transcri' in s or 'audio' in s:
        return 'transcription'
    if 'gpt' in s or 'o1' in s or 'o3' in s or 'o4' in s or 'chat' in s:
        return 'summary'
    return 'other'


def resolve_range(preset=None, start=None, end=None, *, today=None):
    """Return (preset, start_date, end_date) as inclusive UTC dates.

    Bad or missing input falls back to 30d. Custom ranges are clamped to
    ``MAX_RANGE_DAYS`` and never run past today.
    """
    today = today or datetime.now(timezone.utc).date()
    preset = (preset or '30d').strip().lower()
    if preset not in RANGE_PRESETS:
        preset = '30d'
    if preset == 'custom':
        try:
            s = date.fromisoformat((start or '').strip())
            e = date.fromisoformat((end or '').strip())
        except ValueError:
            return resolve_range('30d', today=today)
        if s > e:
            s, e = e, s
        if e > today:
            e = today
        if s > e:
            s = e
        if (e - s).days + 1 > MAX_RANGE_DAYS:
            s = e - timedelta(days=MAX_RANGE_DAYS - 1)
        return preset, s, e
    days = {'today': 1, '7d': 7, '30d': 30, '90d': 90}[preset]
    return preset, today - timedelta(days=days - 1), today


def _day_ts(d):
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())


def fetch_actual_costs_uncached(start, end, *, session=None):
    """Daily actual USD by cost type for inclusive UTC dates [start, end]."""
    key = (os.getenv(ADMIN_KEY_ENV) or '').strip()
    if not key.startswith('sk-admin-'):
        return {'ok': False, 'missing_key': True,
                'error': 'No OpenAI Admin key configured (OPENAI_ADMIN_KEY).'}
    http = session or requests
    params = {
        'start_time': _day_ts(start),
        'end_time': _day_ts(end + timedelta(days=1)),
        'bucket_width': '1d',
        'group_by': 'line_item',
        'limit': MAX_RANGE_DAYS,
    }
    project = (os.getenv(PROJECT_ID_ENV) or '').strip()
    if project:
        params['project_ids'] = project
    days = {}
    line_items = {}
    page = None
    for _ in range(20):  # pagination guard
        if page:
            params['page'] = page
        try:
            resp = http.get(OPENAI_COSTS_URL, params=params, timeout=15,
                            headers={'Authorization': f'Bearer {key}'})
        except requests.RequestException as exc:
            return {'ok': False, 'error': 'OpenAI Costs API unreachable ('
                    + type(exc).__name__ + ')'}
        if resp.status_code != 200:
            hint = ''
            if resp.status_code in (401, 403):
                hint = ' Check that OPENAI_ADMIN_KEY is a valid Admin key.'
            return {'ok': False,
                    'error': f'OpenAI Costs API returned {resp.status_code}.{hint}'}
        body = resp.json() or {}
        for bucket in body.get('data') or []:
            d = datetime.fromtimestamp(int(bucket.get('start_time') or 0),
                                       tz=timezone.utc).date().isoformat()
            slot = days.setdefault(d, {'transcription': 0.0, 'summary': 0.0,
                                       'other': 0.0})
            for res in bucket.get('results') or []:
                amount = res.get('amount') or {}
                try:
                    value = float(amount.get('value') or 0)
                except (TypeError, ValueError):
                    continue
                item = res.get('line_item') or 'unknown'
                slot[classify_line_item(item)] += value
                line_items[item] = line_items.get(item, 0.0) + value
        if not body.get('has_more') or not body.get('next_page'):
            break
        page = body.get('next_page')
    return {
        'ok': True,
        'days': days,
        'line_items': sorted(
            ({'name': k, 'usd': round(v, 4)} for k, v in line_items.items()),
            key=lambda x: -x['usd']),
        'project_scoped': bool(project),
    }


def fetch_actual_costs(start, end, *, force_refresh=False, session=None):
    """Cached wrapper (15 min per range) so the page cannot hammer OpenAI."""
    ck = (start.isoformat(), end.isoformat())
    now = time.monotonic()
    with _lock:
        hit = _cache.get(ck)
        if hit and not force_refresh and now < hit[0]:
            out = dict(hit[1])
            out['cached'] = True
            return out
    payload = fetch_actual_costs_uncached(start, end, session=session)
    payload['fetched_at'] = datetime.now(timezone.utc)
    if not payload.get('missing_key'):
        with _lock:
            _cache[ck] = (time.monotonic() + CACHE_TTL_SEC, payload)
    out = dict(payload)
    out['cached'] = False
    return out


def clear_cache():
    with _lock:
        _cache.clear()


def estimated_daily_by_utc(db, start, end):
    """Estimated platform OpenAI USD per UTC day, split transcription/summary.

    Same rules as ``admin_costs.collect_costs`` (BYOK excluded, settled
    charged seconds), bucketed by UTC day so it lines up with OpenAI's buckets.
    """
    from admin_costs import (
        estimate_summary_cost_usd, estimate_transcription_cost_usd,
        task_key_source,
    )
    from admin_dashboard import _parse_db_datetime, _as_utc

    lo = datetime(start.year, start.month, start.day)
    hi = datetime(end.year, end.month, end.day) + timedelta(days=1)
    rows = db.session.execute(text("""
        SELECT COALESCE(completed_at, started_at) AS event_at,
               trial_seconds_charged, paid_seconds_charged,
               summary_status, summary_model, summary_prompt_tokens,
               summary_completion_tokens, summary_cost_usd_est,
               CASE WHEN summary_json IS NOT NULL AND summary_json != ''
                    THEN 1 ELSE 0 END
          FROM transcription_tasks
         WHERE status IN ('completed', 'error', 'cancelled')
           AND (summary_source_task_id IS NULL OR summary_source_task_id = '')
    """))
    days = {}
    for (event_at, trial, paid, s_status, s_model, p_tok, c_tok, s_cost,
         has_json) in rows:
        if task_key_source(trial, paid) == 'user':
            continue
        dt = _parse_db_datetime(event_at)
        if dt is None:
            continue
        dt = _as_utc(dt).replace(tzinfo=None)
        if not (lo <= dt < hi):
            continue
        slot = days.setdefault(dt.date().isoformat(),
                               {'transcription': 0.0, 'summary': 0.0})
        slot['transcription'] += estimate_transcription_cost_usd(
            int(trial or 0) + int(paid or 0))
        slot['summary'] += estimate_summary_cost_usd(
            summary_status=s_status, summary_cost_usd_est=s_cost,
            summary_model=s_model, summary_prompt_tokens=p_tok,
            summary_completion_tokens=c_tok, has_summary_json=bool(has_json))
    return days


def _pick(slot, cost_type):
    if not slot:
        return 0.0
    if cost_type == 'all':
        return sum(slot.values())
    return slot.get(cost_type, 0.0)


def collect_actual_vs_estimated(db, preset=None, start=None, end=None,
                                cost_type=None, *, today=None,
                                fetcher=None):
    """Payload for the 'Actual vs estimated' panel."""
    preset, s, e = resolve_range(preset, start, end, today=today)
    cost_type = (cost_type or 'all').strip().lower()
    if cost_type not in COST_TYPES:
        cost_type = 'all'
    fetcher = fetcher or fetch_actual_costs
    actual = fetcher(s, e)
    est = estimated_daily_by_utc(db, s, e)
    today_utc = today or datetime.now(timezone.utc).date()

    labels, a_series, e_series, rows = [], [], [], []
    d = s
    while d <= e:
        k = d.isoformat()
        a = _pick((actual.get('days') or {}).get(k), cost_type) if actual.get('ok') else None
        ev = _pick(est.get(k), cost_type)
        labels.append(k)
        a_series.append(round(a, 4) if a is not None else None)
        e_series.append(round(ev, 4))
        rows.append({'day': k, 'actual_usd': a, 'estimated_usd': ev,
                     'partial': d == today_utc})
        d += timedelta(days=1)

    total_est = sum(e_series)
    total_act = sum(x for x in a_series if x is not None) if actual.get('ok') else None
    today_row = next((r for r in rows if r['partial']), None)
    fetched = actual.get('fetched_at')
    return {
        'preset': preset,
        'start': s.isoformat(),
        'end': e.isoformat(),
        'cost_type': cost_type,
        'presets': RANGE_PRESETS,
        'cost_types': COST_TYPES,
        'ok': bool(actual.get('ok')),
        'missing_key': bool(actual.get('missing_key')),
        'error': actual.get('error'),
        'cached': bool(actual.get('cached')),
        'fetched_at': fetched,
        'project_scoped': bool(actual.get('project_scoped')),
        'line_items': actual.get('line_items') or [],
        'total_actual_usd': round(total_act, 4) if total_act is not None else None,
        'total_estimated_usd': round(total_est, 4),
        'diff_usd': (round(total_act - total_est, 4)
                     if total_act is not None else None),
        'today': today_row,
        'rows': list(reversed(rows)),
        'chart': {'labels': labels, 'actual_usd': a_series,
                  'estimated_usd': e_series},
    }
