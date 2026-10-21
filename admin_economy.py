"""Economy overview for the admin Economy page: revenue, income, cost, result.

Read-only reporting over SQLite, plus Stripe and the OpenAI Costs API when they
answer. Nothing here touches reservation, refund or trial logic.

Definitions (USD, UTC days, so Stripe and OpenAI buckets line up) — purchase
counting follows ``admin_purchases`` (credited, not refunded/disputed, exclude
internal/test users):

* revenue  = price paid including VAT (counted purchases only)
* ex_vat   = revenue minus VAT (shown alongside revenue)
* fees     = Stripe's fee (from the balance transaction when Stripe is
             reachable, otherwise the model in ``pricing_volume``)
* income   = ex_vat - fees, the money that actually lands with us
* refunds  = refunded / disputed amounts (reported separately; not in
             purchase count or revenue)
* cost     = OpenAI cost (actual from the Costs API when an admin key exists,
             otherwise list price x settled minutes) + prorated fixed costs
* result   = income - cost

Cost is split into trial and paid minutes by that day's share of minutes, so
the page can show how much of the spend is free trial.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import text

from admin_purchases import (
    COUNTED_STATUS,
    counted_purchases,
    fetch_purchase_rows,
    is_internal_or_test_user,
    refund_dispute_purchases,
)

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


def _first_counted_purchase_ids(db):
    """user_id → id of that user's first counted (credited, non-internal) buy."""
    rows = fetch_purchase_rows(db, statuses=(COUNTED_STATUS,))
    first = {}
    for r in counted_purchases(rows):
        uid = r.get('user_id')
        if uid is None or uid in first:
            continue
        first[uid] = r['id']
    return first


def collect_purchases(db, start, end, stripe_by_pi):
    """Counted purchases in [start, end] plus separate refund/dispute rows."""
    from admin_margin import plan_key
    first_ids = _first_counted_purchase_ids(db)
    rows = fetch_purchase_rows(db, start=start, end=end)
    purchases = []
    for r in counted_purchases(rows):
        day = _utc_day(r.get('created_at'))
        if day is None:
            continue
        gross_usd = int(r.get('gross_cents') or 0) / 100.0
        vat_usd = min(int(r.get('tax_cents') or 0) / 100.0, gross_usd)
        ex_vat = gross_usd - vat_usd
        pi = r.get('pi') or ''
        stripe = stripe_by_pi.get(pi)
        if stripe:
            fee_usd, fee_src = stripe['fees'], 'stripe'
        else:
            fee_usd, fee_src = _model_fee_usd(gross_usd), 'model'
        purchases.append({
            'd': day,
            'minutes': int(r.get('minutes') or 0),
            'gross': round(gross_usd, 4),
            'vat': round(vat_usd, 4),
            'ex_vat': round(ex_vat, 4),
            # Canonical revenue: price paid incl. VAT.
            'revenue': round(gross_usd, 4),
            'fee': round(fee_usd, 4),
            'fee_src': fee_src,
            'country': (r.get('customer_country') or '').upper() or None,
            'plan': plan_key(r.get('minutes'), r.get('price_source'),
                            r.get('pricing_version')),
            'new': first_ids.get(r.get('user_id')) == r.get('id'),
            'src': r.get('price_source') or None,
        })
    refunds = []
    for r in refund_dispute_purchases(rows):
        day = _utc_day(r.get('created_at'))
        if day is None:
            continue
        gross_usd = int(r.get('gross_cents') or 0) / 100.0
        refunded = int(r.get('refunded_cents') or 0) / 100.0
        amount = refunded if refunded > 0 else gross_usd
        refunds.append({
            'd': day,
            'amount': round(amount, 4),
            'status': r.get('status') or '',
        })
    return purchases, refunds


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


def build_series(days, purchases, refunds, minutes, api_cost_by_day,
                 fixed_monthly):
    """Per-day money arrays aligned with *days*."""
    from admin_costs import fixed_daily_usd
    n = len(days)
    idx = {d: i for i, d in enumerate(days)}
    s = {k: _zeros(n) for k in (
        'gross', 'vat', 'ex_vat', 'refunds', 'revenue', 'fees', 'income',
        'purchases', 'trial_min', 'paid_min', 'cost_trial', 'cost_paid',
        'cost_fixed', 'cost', 'result')}
    for p in purchases:
        i = idx.get(p['d'])
        if i is None:
            continue
        s['gross'][i] += p['gross']
        s['vat'][i] += p['vat']
        s['ex_vat'][i] += p['ex_vat']
        s['revenue'][i] += p['revenue']
        s['fees'][i] += p['fee']
        s['purchases'][i] += 1
    for r in refunds:
        i = idx.get(r['d'])
        if i is not None:
            s['refunds'][i] += r['amount']
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
        # Income is what lands with us: ex-VAT minus Stripe fees.
        s['income'][i] = s['ex_vat'][i] - s['fees'][i]
        s['cost'][i] = api + s['cost_fixed'][i]
        s['result'][i] = s['income'][i] - s['cost'][i]
    return {k: [round(v, 4) for v in vals] for k, vals in s.items()}


def paying_user_ids(db):
    """Users with at least one counted purchase (external buyers only)."""
    rows = fetch_purchase_rows(db, statuses=(COUNTED_STATUS,))
    return {r['user_id'] for r in counted_purchases(rows) if r.get('user_id')}


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
    """Day of each paying user's first counted purchase (all time)."""
    first = _first_counted_purchase_ids(db)
    if not first:
        return []
    id_list = ','.join(str(int(i)) for i in first.values())
    out = []
    for (created,) in db.session.execute(text(
            f'SELECT created_at FROM credit_purchases WHERE id IN ({id_list})'
    )):
        d = _utc_day(created)
        if d:
            out.append(d)
    return out


def list_payers(db, limit=PAYERS_LIMIT):
    """Users who have a counted purchase, latest buyer first."""
    rows = db.session.execute(text("""
        SELECT u.id, u.email, u.created_at,
               COUNT(cp.id), MIN(cp.created_at), MAX(cp.created_at),
               SUM(COALESCE(cp.amount_total_cents, cp.amount_cents)),
               SUM(COALESCE(cp.amount_tax_cents, 0)),
               SUM(cp.minutes), COALESCE(u.paid_seconds_balance, 0),
               (SELECT COUNT(*) FROM transcription_tasks t
                 WHERE t.user_id = u.id AND t.status = 'completed')
          FROM credit_purchases cp
          JOIN users u ON u.id = cp.user_id
         WHERE cp.status = :status
         GROUP BY u.id
         ORDER BY MAX(cp.created_at) DESC
         LIMIT :limit
    """), {'status': COUNTED_STATUS, 'limit': int(limit) * 3}).fetchall()
    from admin_dashboard import format_oslo
    out = []
    for (uid, email, created, n, first, last, gross, tax, minutes, bal,
         tx) in rows:
        if is_internal_or_test_user(uid, email):
            continue
        bought = float(minutes or 0)
        left = max(0.0, float(bal or 0) / 60.0)
        gross_usd = float(gross or 0) / 100.0
        tax_usd = min(float(tax or 0) / 100.0, gross_usd)
        out.append({
            'id': uid, 'email': email,
            'signed_up': format_oslo(created, '%Y-%m-%d'),
            'first_purchase': format_oslo(first, '%Y-%m-%d'),
            'last_purchase': format_oslo(last, '%Y-%m-%d'),
            'purchases': int(n or 0),
            # Paid = revenue incl. VAT; ex_vat kept for the table subtitle.
            'paid_usd': round(gross_usd, 2),
            'ex_vat_usd': round(gross_usd - tax_usd, 2),
            'minutes_bought': int(bought),
            'minutes_left': round(left, 1),
            'used_pct': round(100 * min(1.0, max(0.0, 1 - left / bought)), 0) if bought else 0,
            'transcripts': int(tx or 0),
        })
        if len(out) >= int(limit):
            break
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
    purchases, refunds = collect_purchases(db, start, end, by_pi)

    actual = (actual_fetcher or fetch_actual_costs)(start, end)
    cost_ok = bool(actual.get('ok'))
    if cost_ok:
        api_by_day = {k: sum(v.values()) for k, v in (actual.get('days') or {}).items()}
    else:
        api_by_day = {k: sum(v.values())
                      for k, v in estimated_daily_by_utc(db, start, end).items()}

    minutes = minutes_by_day(db, start, end)
    fixed_monthly, _fixed_items = fixed_monthly_costs_from_env()
    series = build_series(days, purchases, refunds, minutes, api_by_day,
                          fixed_monthly)

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
    refund_usd = round(sum(r['amount'] for r in refunds), 2)
    return {
        'start': start.isoformat(),
        'end': end.isoformat(),
        'today': today.isoformat(),
        'days': days,
        'series': series,
        'purchases': purchases,
        'refunds_disputes': refunds,
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
            'refund_dispute_count': len(refunds),
            'refunds_disputes_usd': refund_usd,
            'cost': 'actual' if cost_ok else 'estimate',
            'cost_error': actual.get('error'),
            'cost_per_min_source': cpm_source,
            'fixed': 'configured' if fixed_monthly else 'none',
            'fixed_monthly_usd': round(float(fixed_monthly), 2),
            'stripe_ok': stripe_ok,
        },
    }
