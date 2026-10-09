"""Estimated OpenAI + fixed operating costs for the admin dashboard.

Read-only aggregates over SQLite. Does not touch billing/trial reservation
logic. Every dollar figure is an estimate from published list prices and
stored duration/token fields — label it as such in the UI.
"""

from __future__ import annotations

import calendar
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

# ---------------------------------------------------------------------------
# OpenAI list prices used for admin estimates only (not billing).
# Sources (check periodically; OpenAI may change rates):
#   Audio transcription — https://openai.com/api/pricing/
#     whisper-1:                $0.006 / minute
#     gpt-4o-transcribe:        $0.006 / minute
#     gpt-4o-mini-transcribe:   $0.003 / minute
#   Chat summaries (gpt-4o-mini) — same page, $ / 1M tokens:
#     input $0.15 / output $0.60  (via summary.estimate_cost_usd)
# ---------------------------------------------------------------------------
OPENAI_AUDIO_USD_PER_MIN = {
    'whisper-1': 0.006,                 # https://openai.com/api/pricing/
    'gpt-4o-transcribe': 0.006,         # https://openai.com/api/pricing/
    'gpt-4o-mini-transcribe': 0.003,    # https://openai.com/api/pricing/
}

# Production always sends whisper-1 today (app._whisper_transcribe_chunk);
# tasks do not store the model name, so estimates default here.
DEFAULT_TRANSCRIPTION_MODEL = 'whisper-1'

# When a ready summary has no stored cost and no token counts.
SUMMARY_FALLBACK_USD = 0.002  # ~rough gpt-4o-mini TL;DR for a typical episode

# Env knobs for fixed monthly opex (hosting, email, …). Empty = $0.
# COST_FIXED_MONTHLY_BREAKDOWN='hetzner:5.59,mailgun:1.00'
# COST_FIXED_MONTHLY_USD='6.59'  (used when breakdown is empty)
COST_FIXED_MONTHLY_USD_ENV = 'COST_FIXED_MONTHLY_USD'
COST_FIXED_MONTHLY_BREAKDOWN_ENV = 'COST_FIXED_MONTHLY_BREAKDOWN'


def audio_usd_per_minute(model=None):
    """USD per audio minute for *model* (falls back to whisper-1)."""
    key = (model or DEFAULT_TRANSCRIPTION_MODEL).strip() or DEFAULT_TRANSCRIPTION_MODEL
    if key in OPENAI_AUDIO_USD_PER_MIN:
        return OPENAI_AUDIO_USD_PER_MIN[key]
    return OPENAI_AUDIO_USD_PER_MIN[DEFAULT_TRANSCRIPTION_MODEL]


def parse_fixed_monthly_breakdown(raw=None):
    """Parse 'name:amount,name:amount' into an ordered list of (name, usd).

    Invalid tokens are skipped. Amounts must be finite non-negative numbers.
    """
    if raw is None:
        raw = os.getenv(COST_FIXED_MONTHLY_BREAKDOWN_ENV, '')
    raw = (raw or '').strip()
    if not raw:
        return []
    out = []
    for part in raw.split(','):
        part = part.strip()
        if not part:
            continue
        if ':' not in part:
            continue
        name, amount_s = part.rsplit(':', 1)
        name = name.strip()
        amount_s = amount_s.strip()
        if not name:
            continue
        try:
            amount = float(amount_s)
        except (TypeError, ValueError):
            continue
        if amount < 0 or amount != amount:  # NaN check
            continue
        out.append((name, amount))
    return out


def fixed_monthly_costs_from_env():
    """Return (monthly_total_usd, breakdown_list[(name, usd)]).

    Prefer the breakdown sum when present; otherwise COST_FIXED_MONTHLY_USD.
    """
    breakdown = parse_fixed_monthly_breakdown()
    if breakdown:
        total = sum(a for _, a in breakdown)
        return total, breakdown
    raw = (os.getenv(COST_FIXED_MONTHLY_USD_ENV) or '').strip()
    if not raw:
        return 0.0, []
    try:
        total = float(raw)
    except (TypeError, ValueError):
        return 0.0, []
    if total < 0 or total != total:
        return 0.0, []
    return total, [('fixed', total)] if total else []


def fixed_daily_usd(monthly_total, *, on_date=None):
    """Prorate a monthly fixed cost to one calendar day (Europe/Oslo month length)."""
    from admin_dashboard import ADMIN_TZ

    if not monthly_total:
        return 0.0
    if on_date is None:
        on_date = datetime.now(ADMIN_TZ).date()
    days_in_month = calendar.monthrange(on_date.year, on_date.month)[1]
    return float(monthly_total) / float(days_in_month)


def task_key_source(trial_seconds_charged, paid_seconds_charged):
    """Classify a task as trial / paid / user (BYOK) for cost attribution.

    Matches metering invariants: ``trial_seconds_charged is NULL`` means the
    job ran on the user's key and was never reserved against our allowance.
    A settled platform job that spent nothing has 0 (not NULL).
    Hybrid jobs (both trial and paid seconds) are reported as ``'mixed'``.
    """
    trial = trial_seconds_charged
    paid = int(paid_seconds_charged or 0)
    if trial is None and paid <= 0:
        return 'user'
    trial_n = int(trial or 0)
    if trial_n > 0 and paid > 0:
        return 'mixed'
    if paid > 0:
        return 'paid'
    return 'trial'


def estimate_transcription_cost_usd(seconds, model=None):
    """Audio-minute estimate for *seconds* of audio on *model*."""
    minutes = max(0.0, float(seconds or 0) / 60.0)
    return minutes * audio_usd_per_minute(model)


def estimate_summary_cost_usd(
    *,
    summary_status,
    summary_cost_usd_est,
    summary_model,
    summary_prompt_tokens,
    summary_completion_tokens,
    has_summary_json=False,
):
    """Estimate summary spend for one task, or 0 if no summary ran on our key.

    Preference: stored ``summary_cost_usd_est`` → token-based gpt-4o-mini
    rate → per-summary fallback when status is ready (or JSON is present).
    """
    if summary_cost_usd_est is not None and float(summary_cost_usd_est) > 0:
        return float(summary_cost_usd_est)

    status = (summary_status or '').strip().lower()
    tokens_present = (
        summary_prompt_tokens is not None or summary_completion_tokens is not None
    )
    looks_summarized = status == 'ready' or bool(has_summary_json)
    if not looks_summarized and not tokens_present:
        return 0.0

    if tokens_present:
        import summary as summary_mod
        model = (summary_model or summary_mod.DEFAULT_SUMMARY_MODEL)
        return float(summary_mod.estimate_cost_usd(
            model,
            int(summary_prompt_tokens or 0),
            int(summary_completion_tokens or 0),
        ))

    if looks_summarized:
        return float(SUMMARY_FALLBACK_USD)
    return 0.0


def _empty_window():
    return {
        'openai_trial_usd': 0.0,
        'openai_paid_usd': 0.0,
        'openai_summary_usd': 0.0,
        'openai_usd': 0.0,
        'fixed_usd': 0.0,
        'total_usd': 0.0,
        'revenue_usd': 0.0,
        'gross_margin_usd': 0.0,
        'gross_margin_pct': None,
        'platform_trial_minutes': 0.0,
        'platform_paid_minutes': 0.0,
        'byok_minutes': 0.0,
        'failed_with_openai_count': 0,
        'active_users': 0,
        'paid_users': 0,
        'cost_per_active_user_usd': None,
        'cost_per_paid_user_usd': None,
        'task_count_platform': 0,
        'task_count_byok': 0,
    }


def _round_money(n):
    return round(float(n or 0), 4)


def _finalize_window(w):
    w['openai_usd'] = _round_money(
        w['openai_trial_usd'] + w['openai_paid_usd'] + w['openai_summary_usd'])
    w['total_usd'] = _round_money(w['openai_usd'] + w['fixed_usd'])
    w['openai_trial_usd'] = _round_money(w['openai_trial_usd'])
    w['openai_paid_usd'] = _round_money(w['openai_paid_usd'])
    w['openai_summary_usd'] = _round_money(w['openai_summary_usd'])
    w['fixed_usd'] = _round_money(w['fixed_usd'])
    w['revenue_usd'] = _round_money(w['revenue_usd'])
    w['gross_margin_usd'] = _round_money(w['revenue_usd'] - w['total_usd'])
    if w['revenue_usd'] > 0:
        w['gross_margin_pct'] = round(
            100.0 * w['gross_margin_usd'] / w['revenue_usd'], 1)
    else:
        w['gross_margin_pct'] = None
    w['platform_trial_minutes'] = round(w['platform_trial_minutes'], 2)
    w['platform_paid_minutes'] = round(w['platform_paid_minutes'], 2)
    w['byok_minutes'] = round(w['byok_minutes'], 2)
    if w['active_users'] > 0:
        w['cost_per_active_user_usd'] = _round_money(
            w['total_usd'] / w['active_users'])
    else:
        w['cost_per_active_user_usd'] = None
    if w['paid_users'] > 0:
        w['cost_per_paid_user_usd'] = _round_money(
            w['total_usd'] / w['paid_users'])
    else:
        w['cost_per_paid_user_usd'] = None
    return w


def collect_costs(db, *, chart_days=30, model=None):
    """Build cost estimates for today / 7d / 30d / all-time + daily chart.

    Platform OpenAI cost uses settled ``trial_seconds_charged`` /
    ``paid_seconds_charged`` (post-refund = audio that hit Whisper). BYOK
    jobs (NULL trial charge, no paid charge) are excluded from $ cost but
    their ``audio_duration`` minutes are reported separately.
    """
    from admin_dashboard import (
        ADMIN_TZ, _naive_utc, _oslo_day_start_utc, _parse_db_datetime,
        format_oslo, to_oslo,
    )

    transcription_model = model or DEFAULT_TRANSCRIPTION_MODEL
    monthly_fixed, fixed_breakdown = fixed_monthly_costs_from_env()
    daily_fixed = fixed_daily_usd(monthly_fixed)

    now_oslo = datetime.now(ADMIN_TZ)
    today_start = _naive_utc(_oslo_day_start_utc(0))
    d7 = _naive_utc(_oslo_day_start_utc(6))  # last 7 Oslo days incl. today
    d30 = _naive_utc(_oslo_day_start_utc(29))

    # Terminal tasks only — in-flight reservations are not spend yet.
    rows = list(db.session.execute(text("""
        SELECT id, user_id, status,
               COALESCE(completed_at, started_at) AS event_at,
               started_at,
               audio_duration,
               trial_seconds_charged,
               paid_seconds_charged,
               chunk_index,
               summary_status,
               summary_model,
               summary_prompt_tokens,
               summary_completion_tokens,
               summary_cost_usd_est,
               CASE WHEN summary_json IS NOT NULL AND summary_json != ''
                    THEN 1 ELSE 0 END AS has_summary_json
          FROM transcription_tasks
         WHERE status IN ('completed', 'error', 'cancelled')
           AND (summary_source_task_id IS NULL OR summary_source_task_id = '')
    """)))

    purchases = list(db.session.execute(text("""
        SELECT user_id, created_at,
               COALESCE(amount_total_cents, amount_cents, 0) AS cents
          FROM credit_purchases
         WHERE status = 'credited'
    """)))

    # Earliest activity for all-time fixed-cost span.
    first_ts = db.session.execute(text("""
        SELECT MIN(ts) FROM (
            SELECT MIN(created_at) AS ts FROM users
            UNION ALL
            SELECT MIN(started_at) FROM transcription_tasks
            UNION ALL
            SELECT MIN(created_at) FROM credit_purchases
        )
    """)).scalar()

    windows = {
        'today': _empty_window(),
        '7d': _empty_window(),
        '30d': _empty_window(),
        'all': _empty_window(),
    }
    # Per-window distinct user sets.
    active_sets = {k: set() for k in windows}
    paid_sets = {k: set() for k in windows}

    chart_labels = []
    for i in range(chart_days - 1, -1, -1):
        chart_labels.append((now_oslo - timedelta(days=i)).date().isoformat())
    daily_openai = {lab: 0.0 for lab in chart_labels}
    daily_fixed_series = {lab: daily_fixed for lab in chart_labels}

    def _in_window(event_at, key):
        if event_at is None:
            return key == 'all'
        if key == 'all':
            return True
        naive = _naive_utc(_parse_db_datetime(event_at))
        if naive is None:
            return False
        if key == 'today':
            return naive >= today_start
        if key == '7d':
            return naive >= d7
        if key == '30d':
            return naive >= d30
        return False

    for r in rows:
        (tid, uid, status, event_at, _started, audio_duration,
         trial_charged, paid_charged, chunk_index,
         summary_status, summary_model, prompt_tok, completion_tok,
         summary_cost, has_summary_json) = r

        source = task_key_source(trial_charged, paid_charged)
        trial_secs = int(trial_charged or 0) if trial_charged is not None else 0
        paid_secs = int(paid_charged or 0)
        platform = source != 'user'

        if platform:
            trial_cost = estimate_transcription_cost_usd(
                trial_secs, transcription_model)
            paid_cost = estimate_transcription_cost_usd(
                paid_secs, transcription_model)
            # Failed/cancelled that still hit OpenAI (spent seconds > 0, or
            # chunk_index set meaning at least one chunk was uploaded).
            hit_openai = (trial_secs + paid_secs) > 0 or (
                status in ('error', 'cancelled') and chunk_index is not None)
            summary_cost_n = estimate_summary_cost_usd(
                summary_status=summary_status,
                summary_cost_usd_est=summary_cost,
                summary_model=summary_model,
                summary_prompt_tokens=prompt_tok,
                summary_completion_tokens=completion_tok,
                has_summary_json=bool(has_summary_json),
            )
        else:
            trial_cost = paid_cost = 0.0
            summary_cost_n = 0.0
            hit_openai = False
            # BYOK minutes from measured audio duration.
            byok_secs = float(audio_duration or 0)

        openai_task = trial_cost + paid_cost + summary_cost_n

        local = to_oslo(event_at)
        day_key = local.date().isoformat() if local else None
        if day_key and day_key in daily_openai and platform:
            daily_openai[day_key] += openai_task

        for key in windows:
            if not _in_window(event_at, key):
                continue
            w = windows[key]
            if platform:
                w['openai_trial_usd'] += trial_cost
                w['openai_paid_usd'] += paid_cost
                w['openai_summary_usd'] += summary_cost_n
                w['platform_trial_minutes'] += trial_secs / 60.0
                w['platform_paid_minutes'] += paid_secs / 60.0
                w['task_count_platform'] += 1
                if status in ('error', 'cancelled') and hit_openai:
                    w['failed_with_openai_count'] += 1
                if uid is not None and (
                        status == 'completed' or (trial_secs + paid_secs) > 0):
                    active_sets[key].add(uid)
            else:
                w['byok_minutes'] += byok_secs / 60.0
                w['task_count_byok'] += 1
                if uid is not None and status == 'completed':
                    active_sets[key].add(uid)

    for user_id, created_at, cents in purchases:
        rev = int(cents or 0) / 100.0
        for key in windows:
            if not _in_window(created_at, key):
                continue
            windows[key]['revenue_usd'] += rev
            if user_id is not None:
                paid_sets[key].add(user_id)

    # Fixed costs: today = 1 day; Nd = N days; all-time = days from first
    # activity (Oslo) through today inclusive.
    def _fixed_for(key):
        if monthly_fixed <= 0:
            return 0.0
        if key == 'today':
            return daily_fixed
        if key == '7d':
            return daily_fixed * 7
        if key == '30d':
            return daily_fixed * 30
        # all-time
        first = None
        if first_ts is not None:
            first = to_oslo(first_ts)
        if first is None:
            return 0.0
        days = (now_oslo.date() - first.date()).days + 1
        return daily_fixed * max(0, days)

    for key in windows:
        windows[key]['fixed_usd'] = _fixed_for(key)
        windows[key]['active_users'] = len(active_sets[key])
        windows[key]['paid_users'] = len(paid_sets[key])
        _finalize_window(windows[key])

    chart = {
        'labels': chart_labels,
        'openai_usd': [round(daily_openai[lab], 4) for lab in chart_labels],
        'fixed_usd': [round(daily_fixed_series[lab], 4) for lab in chart_labels],
        'total_usd': [
            round(daily_openai[lab] + daily_fixed_series[lab], 4)
            for lab in chart_labels
        ],
    }

    return {
        'estimate': True,
        'transcription_model': transcription_model,
        'audio_prices_usd_per_min': dict(OPENAI_AUDIO_USD_PER_MIN),
        'summary_fallback_usd': SUMMARY_FALLBACK_USD,
        'fixed_monthly_usd': round(monthly_fixed, 4),
        'fixed_daily_usd': round(daily_fixed, 4),
        'fixed_breakdown': [
            {'name': n, 'usd': round(a, 4)} for n, a in fixed_breakdown
        ],
        'windows': windows,
        'chart': chart,
        'as_of_oslo': format_oslo(datetime.now(timezone.utc)),
        'note': (
            'Estimates from stored audio seconds × OpenAI list prices '
            f'({transcription_model} '
            f'${audio_usd_per_minute(transcription_model):.3f}/min). '
            'BYOK jobs excluded from $ cost. Failed jobs counted when '
            'charged seconds (post-refund) show Whisper was hit.'
        ),
    }
