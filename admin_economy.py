"""Economy overview for the admin Economy page: revenue, income, cost, result.

Read-only reporting over SQLite, plus Stripe and the OpenAI Costs API when they
answer. Nothing here touches reservation, refund or trial logic.

Definitions (USD, UTC days, so Stripe and OpenAI buckets line up):

* revenue  = what customers paid, minus VAT, minus refunded money
* fees     = Stripe's fee (from the balance transaction when Stripe is
             reachable, otherwise the model in ``pricing_volume``)
* income   = revenue - fees, the money that actually lands with us
* cost     = OpenAI cost (actual from the Costs API when an admin key exists,
             otherwise list price x settled minutes) + prorated fixed costs
* result   = income - cost

Cost is split into trial and paid minutes by that day's share of minutes, so
the page can show how much of the spend is free trial.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import text

PAID_STATUSES = ('credited', 'refunded', 'disputed')
MAX_DAYS = 180
PAYERS_LIMIT = 200


def _utc_day(value):
    """UTC date (iso string) of a DB datetime value, or None."""
    from admin_dashboard import _as_utc, _parse_db_datetime
    dt = _parse_db_datetime(value)
    if dt is None:
        return None
    return _as_utc(dt).date().isoformat()


def _days(start, end):
    out, d = [], start
    while d <= end:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _zeros(n):
    return [0.0] * n


def resolve_window(db, *, today=None):
    """(start, end) UTC dates: from the first signup, at most MAX_DAYS back."""
    today = today or datetime.now(timezone.utc).date()
    first = db.session.execute(text('SELECT MIN(created_at) FROM users')).scalar()
    first_day = _utc_day(first)
    start = date.fromisoformat(first_day) if first_day else today
    start = max(start, today - timedelta(days=MAX_DAYS - 1))
    return min(start, today), today


def _model_fee_usd(gross_usd):
    import pricing_volume as PV
    return (PV.STRIPE_FEE_FIXED_CENTS / 100.0) + gross_usd * PV.STRIPE_FEE_PCT


def collect_purchases(db, start, end, stripe_by_pi):
    """One dict per purchase in [start, end], money in USD."""
    from admin_margin import plan_key
    lo = datetime(start.year, start.month, start.day)
    hi = datetime(end.year, end.month, end.day) + timedelta(days=1)
    first_ids = {
        r[0]: r[1] for r in db.session.execute(text(f"""
            SELECT user_id, MIN(id) FROM credit_purchases
             WHERE status IN {PAID_STATUSES} AND user_id IS NOT NULL
             GROUP BY user_id
        """))
    }
    rows = db.session.execute(text(f"""
        SELECT id, user_id, created_at, minutes,
               COALESCE(amount_total_cents, amount_cents) AS gross,
               COALESCE(amount_tax_cents, 0) AS tax,
               COALESCE(amount_refunded_cents, 0) AS refunded,
               customer_country, price_source, pricing_version,
               stripe_payment_intent_id
          FROM credit_purchases
         WHERE status IN {PAID_STATUSES}
           AND created_at >= :lo AND created_at < :hi
         ORDER BY created_at, id
    """), {'lo': lo, 'hi': hi}).fetchall()
    out = []
    for (pid, uid, created, minutes, gross, tax, refunded, country, src,
         version, pi) in rows:
        day = _utc_day(created)
        if day is None:
            continue
        gross_usd = int(gross or 0) / 100.0
        vat_usd = min(int(tax or 0) / 100.0, gross_usd)
        ex_vat = gross_usd - vat_usd
        refund_share = min(1.0, int(refunded or 0) / (gross or 1)) if gross else 0.0
        stripe = stripe_by_pi.get(pi or '')
        if stripe:
            fee_usd, fee_src = stripe['fees'], 'stripe'
        else:
            fee_usd, fee_src = _model_fee_usd(gross_usd), 'model'
        out.append({
            'd': day,
            'minutes': int(minutes or 0),
            'gross': round(gross_usd, 4),
            'vat': round(vat_usd, 4),
            'refunds': round(ex_vat * refund_share, 4),
            'revenue': round(ex_vat * (1 - refund_share), 4),
            'fee': round(fee_usd, 4),
            'fee_src': fee_src,
            'country': (country or '').upper() or None,
            'plan': plan_key(minutes, src, version),
            'new': first_ids.get(uid) == pid,
            'src': src or None,
        })
    return out


def minutes_by_day(db, start, end):
    """{day: (trial_min, paid_min)} charged on our key, settled tasks."""
    lo = datetime(start.year, start.month, start.day)
    hi = datetime(end.year, end.month, end.day) + timedelta(days=1)
    out = {}
    for ev, trial, paid in db.session.execute(text("""
        SELECT COALESCE(completed_at, started_at),
               COALESCE(trial_seconds_charged, 0), COALESCE(paid_seconds_charged, 0)
          FROM transcription_tasks
         WHERE status IN ('completed', 'error', 'cancelled')
           AND COALESCE(completed_at, started_at) >= :lo
           AND COALESCE(completed_at, started_at) < :hi
    """), {'lo': lo, 'hi': hi}):
        day = _utc_day(ev)
        if day is None:
            continue
        t, p = out.get(day, (0.0, 0.0))
        out[day] = (t + float(trial) / 60.0, p + float(paid) / 60.0)
    return out


def build_series(days, purchases, minutes, api_cost_by_day, fixed_monthly):
    """Per-day money arrays aligned with *days*."""
    from admin_costs import fixed_daily_usd
    n = len(days)
    idx = {d: i for i, d in enumerate(days)}
    s = {k: _zeros(n) for k in (
        'gross', 'vat', 'refunds', 'revenue', 'fees', 'income', 'purchases',
        'trial_min', 'paid_min', 'cost_trial', 'cost_paid', 'cost_fixed',
        'cost', 'result')}
    for p in purchases:
        i = idx.get(p['d'])
        if i is None:
            continue
        s['gross'][i] += p['gross']
        s['vat'][i] += p['vat']
        s['refunds'][i] += p['refunds']
        s['revenue'][i] += p['revenue']
        s['fees'][i] += p['fee']
        s['purchases'][i] += 1
    for d, (t, p) in minutes.items():
        i = idx.get(d)
        if i is not None:
            s['trial_min'][i], s['paid_min'][i] = t, p
    for i, d in enumerate(days):
        api = float(api_cost_by_day.get(d, 0.0))
        t, p = s['trial_min'][i], s['paid_min'][i]
        share_t = t / (t + p) if (t + p) > 0 else 0.0
        s['cost_trial'][i] = api * share_t
        s['cost_paid'][i] = api - api * share_t
        s['cost_fixed'][i] = fixed_daily_usd(
            fixed_monthly, on_date=date.fromisoformat(d))
        s['income'][i] = s['revenue'][i] - s['fees'][i]
        s['cost'][i] = api + s['cost_fixed'][i]
        s['result'][i] = s['income'][i] - s['cost'][i]
    return {k: [round(v, 4) for v in vals] for k, vals in s.items()}


def paying_user_ids(db):
    return {r[0] for r in db.session.execute(text(f"""
        SELECT DISTINCT user_id FROM credit_purchases
         WHERE status IN {PAID_STATUSES} AND user_id IS NOT NULL
    """))}


def collect_activity(db, days, payer_ids, purchases_first_days):
    """Signups, transcripts and minutes per day, all users vs payers."""
    n = len(days)
    idx = {d: i for i, d in enumerate(days)}
    lo = datetime.fromisoformat(days[0])
    hi = datetime.fromisoformat(days[-1]) + timedelta(days=1)
    a = {k: _zeros(n) for k in (
        'signups', 'signups_paying', 'transcripts', 'transcripts_paying',
        'minutes', 'minutes_paying', 'new_payers')}
    for uid, created in db.session.execute(text(
            'SELECT id, created_at FROM users WHERE created_at >= :lo AND created_at < :hi'),
            {'lo': lo, 'hi': hi}):
        i = idx.get(_utc_day(created))
        if i is None:
            continue
        a['signups'][i] += 1
        if uid in payer_ids:
            a['signups_paying'][i] += 1
    for ev, uid, dur in db.session.execute(text("""
        SELECT COALESCE(completed_at, started_at), user_id, COALESCE(audio_duration, 0)
          FROM transcription_tasks
         WHERE status = 'completed'
           AND COALESCE(completed_at, started_at) >= :lo
           AND COALESCE(completed_at, started_at) < :hi
    """), {'lo': lo, 'hi': hi}):
        i = idx.get(_utc_day(ev))
        if i is None:
            continue
        a['transcripts'][i] += 1
        a['minutes'][i] += float(dur or 0) / 60.0
        if uid in payer_ids:
            a['transcripts_paying'][i] += 1
            a['minutes_paying'][i] += float(dur or 0) / 60.0
    for d in purchases_first_days:
        i = idx.get(d)
        if i is not None:
            a['new_payers'][i] += 1
    return {k: [round(v, 2) for v in vals] for k, vals in a.items()}


def first_purchase_days(db):
    """Day of each paying user's first purchase (all time)."""
    out = []
    for (created,) in db.session.execute(text(f"""
        SELECT MIN(created_at) FROM credit_purchases
         WHERE status IN {PAID_STATUSES} AND user_id IS NOT NULL
         GROUP BY user_id
    """)):
        d = _utc_day(created)
        if d:
            out.append(d)
    return out


def list_payers(db, limit=PAYERS_LIMIT):
    """Users who have bought minutes, latest buyer first."""
    rows = db.session.execute(text(f"""
        SELECT u.id, u.email, u.created_at,
               COUNT(cp.id), MIN(cp.created_at), MAX(cp.created_at),
               SUM(COALESCE(cp.amount_total_cents, cp.amount_cents)
                   - COALESCE(cp.amount_tax_cents, 0)
                   - COALESCE(cp.amount_refunded_cents, 0)),
               SUM(cp.minutes), COALESCE(u.paid_seconds_balance, 0),
               (SELECT COUNT(*) FROM transcription_tasks t
                 WHERE t.user_id = u.id AND t.status = 'completed')
          FROM credit_purchases cp
          JOIN users u ON u.id = cp.user_id
         WHERE cp.status IN {PAID_STATUSES}
         GROUP BY u.id
         ORDER BY MAX(cp.created_at) DESC
         LIMIT :limit
    """), {'limit': int(limit)}).fetchall()
    from admin_dashboard import format_oslo
    out = []
    for (uid, email, created, n, first, last, cents, minutes, bal, tx) in rows:
        bought = float(minutes or 0)
        left = max(0.0, float(bal or 0) / 60.0)
        out.append({
            'id': uid, 'email': email,
            'signed_up': format_oslo(created, '%Y-%m-%d'),
            'first_purchase': format_oslo(first, '%Y-%m-%d'),
            'last_purchase': format_oslo(last, '%Y-%m-%d'),
            'purchases': int(n or 0),
            'paid_usd': round(float(cents or 0) / 100.0, 2),
            'minutes_bought': int(bought),
            'minutes_left': round(left, 1),
            'used_pct': round(100 * min(1.0, max(0.0, 1 - left / bought)), 0) if bought else 0,
            'transcripts': int(tx or 0),
        })
    return out


def collect_economy(db, stripe_revenue, *, start=None, end=None, today=None,
                    actual_fetcher=None):
    """Everything the Economy page needs, for [start, end] UTC days."""
    from admin_actual_costs import estimated_daily_by_utc, fetch_actual_costs
    from admin_costs import fixed_monthly_costs_from_env
    from admin_margin import (cost_per_minute, stripe_usd_by_payment_intent)

    today = today or datetime.now(timezone.utc).date()
    if start is None or end is None:
        start, end = resolve_window(db, today=today)
    days = _days(start, end)

    stripe_ok = bool((stripe_revenue or {}).get('ok'))
    bts = (stripe_revenue or {}).get('bts_norm') or [] if stripe_ok else []
    by_pi = stripe_usd_by_payment_intent(bts)
    purchases = collect_purchases(db, start, end, by_pi)

    actual = (actual_fetcher or fetch_actual_costs)(start, end)
    cost_ok = bool(actual.get('ok'))
    if cost_ok:
        api_by_day = {k: sum(v.values()) for k, v in (actual.get('days') or {}).items()}
    else:
        api_by_day = {k: sum(v.values())
                      for k, v in estimated_daily_by_utc(db, start, end).items()}

    minutes = minutes_by_day(db, start, end)
    fixed_monthly, _fixed_items = fixed_monthly_costs_from_env()
    series = build_series(days, purchases, minutes, api_by_day, fixed_monthly)

    trial_min = sum(series['trial_min'])
    paid_min = sum(series['paid_min'])
    cpm, cpm_source = cost_per_minute(sum(api_by_day.values()), trial_min,
                                      paid_min, cost_ok)
    balance_min = (db.session.execute(text(
        'SELECT COALESCE(SUM(paid_seconds_balance), 0) FROM users '
        'WHERE paid_seconds_balance > 0')).scalar() or 0) / 60.0

    payer_ids = paying_user_ids(db)
    activity = collect_activity(db, days, payer_ids, first_purchase_days(db))
    n_stripe = sum(1 for p in purchases if p['fee_src'] == 'stripe')
    return {
        'start': start.isoformat(),
        'end': end.isoformat(),
        'today': today.isoformat(),
        'days': days,
        'series': series,
        'purchases': purchases,
        'activity': activity,
        'payers': list_payers(db),
        'paying_users': len(payer_ids),
        'unused_paid_minutes': round(balance_min, 1),
        'unused_paid_cost_usd': round(balance_min * cpm, 2),
        'cost_per_min_usd': round(cpm, 5),
        'sources': {
            'revenue': 'database',
            'fees': ('stripe' if purchases and n_stripe == len(purchases)
                     else 'mixed' if n_stripe else 'model'),
            'fees_stripe_count': n_stripe,
            'purchase_count': len(purchases),
            'cost': 'actual' if cost_ok else 'estimate',
            'cost_error': actual.get('error'),
            'cost_per_min_source': cpm_source,
            'fixed': 'configured' if fixed_monthly else 'none',
            'fixed_monthly_usd': round(float(fixed_monthly), 2),
            'stripe_ok': stripe_ok,
        },
    }
