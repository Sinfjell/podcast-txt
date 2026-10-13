"""Daily margin and contribution margin (dekningsbidrag) per plan, in USD.

Read-only money reporting for the admin Costs tab.

* Income is Stripe net per balance transaction, converted to USD with that
  BT's own exchange_rate (same rules as admin_dashboard.summarize_bts_usd).
* Cost is OpenAI's actual cost (Costs API). Cost per minute = actual cost in
  the period / minutes actually transcribed on our key in the same period.
* Days are UTC days, because OpenAI buckets cost by UTC day; Stripe income is
  bucketed by UTC day too so both sides line up.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import text

#: Whisper list price, shown next to the measured cost per minute.
WHISPER_LIST_USD_PER_MIN = 0.006
#: Above this multiple of list price the measured $/min is not trusted.
MAX_PLAUSIBLE_FACTOR = 5
PRESET_MINUTES = (60, 300, 600, 1200)
PLAN_ORDER = ['vol_60', 'vol_300', 'vol_600', 'vol_1200', 'vol_custom',
              'episode', 'legacy']
PLAN_LABELS = {
    'vol_60': '60 min ($1.50)', 'vol_300': '300 min ($5)',
    'vol_600': '600 min ($6)', 'vol_1200': '1200 min ($11)',
    'vol_custom': 'Other slider amounts', 'episode': 'Single episode',
    'legacy': 'Legacy 300-min pack',
}


def plan_key(minutes, price_source, pricing_version):
    """Classify one purchase into a plan bucket."""
    if not pricing_version:
        return 'legacy'
    if (price_source or '') == 'episode':
        return 'episode'
    if int(minutes or 0) in PRESET_MINUTES:
        return f'vol_{int(minutes)}'
    return 'vol_custom'


def _bt_factor(bt, rated):
    from admin_dashboard import _bt_usd_factor
    f = _bt_usd_factor(bt['currency'], bt['exchange_rate'], bt['source_currency'])
    if f is None:
        same = [r for r in rated if r[2] == (bt['currency'] or '').lower()]
        if same:
            f = min(same, key=lambda r: abs(r[0] - int(bt['created'] or 0)))[1]
    return f


def _rated(bts):
    from admin_dashboard import _bt_usd_factor
    out = []
    for b in bts:
        cur = (b['currency'] or '').lower()
        f = _bt_usd_factor(cur, b['exchange_rate'], b['source_currency'])
        if cur != 'usd' and f:
            out.append((int(b['created'] or 0), f, cur))
    return out


_MONEY_TYPES = {'charge', 'payment', 'refund', 'payment_refund', 'stripe_fee',
                'tax', 'tax_fee', 'adjustment'}


def stripe_net_usd_by_utc_day(bts):
    """{utc_date_iso: net_usd} over income-relevant balance transactions."""
    rated = _rated(bts)
    out = {}
    for b in bts:
        if (b['type'] or '') not in _MONEY_TYPES:
            continue
        f = _bt_factor(b, rated)
        if f is None:
            continue
        d = datetime.fromtimestamp(int(b['created'] or 0), tz=timezone.utc).date().isoformat()
        out[d] = out.get(d, 0.0) + int(b['net'] or 0) * f / 100.0
    return out


def stripe_usd_by_payment_intent(bts):
    """{payment_intent: {gross, fees, tax, refunds, net}} in USD."""
    rated = _rated(bts)
    out = {}
    for b in bts:
        pi = b.get('payment_intent')
        if not pi:
            continue
        f = _bt_factor(b, rated)
        if f is None:
            continue
        r = out.setdefault(pi, {'gross': 0.0, 'fees': 0.0, 'tax': 0.0,
                                'refunds': 0.0, 'net': 0.0})
        btype = b['type'] or ''
        amount = int(b['amount'] or 0)
        for fd_type, fd_amt in b.get('fee_details') or []:
            if fd_type in ('withheld_tax', 'withheld_tax_refund', 'tax'):
                r['tax'] += fd_amt * f / 100.0
            else:
                r['fees'] += fd_amt * f / 100.0
        if not b.get('fee_details'):
            r['fees'] += int(b['fee'] or 0) * f / 100.0
        if btype in ('charge', 'payment') and amount > 0:
            r['gross'] += amount * f / 100.0
        if btype in ('refund', 'payment_refund'):
            r['refunds'] += abs(amount) * f / 100.0
        r['net'] += int(b['net'] or 0) * f / 100.0
    return out


def daily_margin(start, end, income_by_day, cost_by_day, *, today=None,
                 cost_ok=True):
    """Rows per UTC day: income, actual cost, margin; today flagged partial."""
    today = today or datetime.now(timezone.utc).date()
    rows = []
    d = start
    while d <= end:
        k = d.isoformat()
        inc = round(income_by_day.get(k, 0.0), 4)
        cost = round(cost_by_day.get(k, 0.0), 4) if cost_ok else None
        rows.append({'day': k, 'income_usd': inc, 'cost_usd': cost,
                     'margin_usd': round(inc - cost, 4) if cost is not None else None,
                     'partial': d == today})
        d += timedelta(days=1)
    tot_inc = round(sum(r['income_usd'] for r in rows), 2)
    tot_cost = round(sum(r['cost_usd'] for r in rows), 2) if cost_ok else None
    return {
        'rows': rows,
        'total_income_usd': tot_inc,
        'total_cost_usd': tot_cost,
        'total_margin_usd': round(tot_inc - tot_cost, 2) if cost_ok else None,
        'chart': {'labels': [r['day'] for r in rows],
                  'income_usd': [r['income_usd'] for r in rows],
                  'cost_usd': [r['cost_usd'] for r in rows],
                  'margin_usd': [r['margin_usd'] for r in rows]},
    }


def _utc_naive_bounds(start, end):
    lo = datetime(start.year, start.month, start.day)
    return lo, datetime(end.year, end.month, end.day) + timedelta(days=1)


def transcribed_minutes(db, start, end):
    """(trial_min, paid_min) charged on our key, tasks finished in [start, end]."""
    lo, hi = _utc_naive_bounds(start, end)
    row = db.session.execute(text("""
        SELECT COALESCE(SUM(trial_seconds_charged), 0),
               COALESCE(SUM(paid_seconds_charged), 0)
          FROM transcription_tasks
         WHERE status IN ('completed', 'error', 'cancelled')
           AND COALESCE(completed_at, started_at) >= :lo
           AND COALESCE(completed_at, started_at) < :hi
    """), {'lo': lo, 'hi': hi}).fetchone()
    return float(row[0] or 0) / 60.0, float(row[1] or 0) / 60.0


def purchase_rows(db, start, end):
    lo, hi = _utc_naive_bounds(start, end)
    return [dict(r._mapping) for r in db.session.execute(text("""
        SELECT id, user_id, minutes, COALESCE(amount_total_cents, amount_cents) AS cents,
               status, price_source, pricing_version, stripe_payment_intent_id AS pi,
               COALESCE(seconds_clawed_back, 0) AS clawed
          FROM credit_purchases
         WHERE status IN ('credited', 'refunded', 'disputed')
           AND created_at >= :lo AND created_at < :hi
    """), {'lo': lo, 'hi': hi})]


def user_used_fraction(db):
    """{user_id: share of all purchased paid minutes already used (0..1)}."""
    rows = db.session.execute(text("""
        SELECT u.id, COALESCE(u.paid_seconds_balance, 0),
               (SELECT COALESCE(SUM(cp.minutes * 60 - COALESCE(cp.seconds_clawed_back, 0)), 0)
                  FROM credit_purchases cp
                 WHERE cp.user_id = u.id AND cp.status IN ('credited', 'refunded', 'disputed'))
          FROM users u
    """))
    out = {}
    for uid, bal, bought in rows:
        bought = float(bought or 0)
        if bought <= 0:
            continue
        out[uid] = min(1.0, max(0.0, 1.0 - float(bal or 0) / bought))
    return out


def contribution_by_plan(purchases, stripe_by_pi, cost_per_min, used_frac,
                         trial_minutes=0.0):
    """Contribution margin per plan, 'all minutes used' and 'used so far'."""
    plans = {}
    for p in purchases:
        k = plan_key(p['minutes'], p['price_source'], p['pricing_version'])
        g = plans.setdefault(k, {'plan': k, 'label': PLAN_LABELS[k], 'count': 0,
                                 'minutes_sold': 0.0, 'minutes_used': 0.0,
                                 'gross_usd': 0.0, 'fees_usd': 0.0, 'tax_usd': 0.0,
                                 'refunds_usd': 0.0, 'net_usd': 0.0,
                                 'stripe_matched': 0})
        g['count'] += 1
        mins = max(0.0, float(p['minutes'] or 0) - float(p['clawed'] or 0) / 60.0)
        g['minutes_sold'] += mins
        g['minutes_used'] += mins * float(used_frac.get(p['user_id'], 0.0))
        s = stripe_by_pi.get(p['pi'] or '')
        if s:
            g['stripe_matched'] += 1
            g['gross_usd'] += s['gross']
            g['fees_usd'] += s['fees']
            g['tax_usd'] += s['tax']
            g['refunds_usd'] += s['refunds']
            g['net_usd'] += s['net']
        else:  # no Stripe row: list price, fees unknown (flagged by stripe_matched)
            g['gross_usd'] += int(p['cents'] or 0) / 100.0
            g['net_usd'] += int(p['cents'] or 0) / 100.0
    out = []
    for k in PLAN_ORDER:
        g = plans.get(k)
        if not g:
            continue
        g['cost_all_used_usd'] = g['minutes_sold'] * cost_per_min
        g['cost_used_so_far_usd'] = g['minutes_used'] * cost_per_min
        g['cost_all_used_list_usd'] = g['minutes_sold'] * WHISPER_LIST_USD_PER_MIN
        g['db_all_used_usd'] = g['net_usd'] - g['cost_all_used_usd']
        g['db_used_so_far_usd'] = g['net_usd'] - g['cost_used_so_far_usd']
        g['db_per_purchase_all_used_usd'] = g['db_all_used_usd'] / g['count']
        g['db_per_purchase_used_so_far_usd'] = g['db_used_so_far_usd'] / g['count']
        gross = g['gross_usd']
        g['db_pct_all_used'] = round(100 * g['db_all_used_usd'] / gross, 1) if gross else None
        g['db_pct_used_so_far'] = round(100 * g['db_used_so_far_usd'] / gross, 1) if gross else None
        for key in list(g):
            if key.endswith('_usd') or key.startswith('minutes'):
                g[key] = round(g[key], 4)
        out.append(g)
    trial = {'plan': 'trial', 'label': 'Free trial (cost only)',
             'minutes_used': round(trial_minutes, 2),
             'cost_usd': round(trial_minutes * cost_per_min, 4),
             'cost_list_usd': round(trial_minutes * WHISPER_LIST_USD_PER_MIN, 4)}
    totals = {k: round(sum(g[k] for g in out), 4) for k in (
        'count', 'gross_usd', 'fees_usd', 'tax_usd', 'net_usd',
        'cost_all_used_usd', 'cost_used_so_far_usd', 'db_all_used_usd',
        'db_used_so_far_usd')}
    totals['db_after_trial_usd'] = round(totals['db_used_so_far_usd'] - trial['cost_usd'], 4)
    return {'plans': out, 'trial': trial, 'totals': totals}


def theoretical_table(cost_per_min, fee_pct_observed=None):
    """Per vol_v1 price point, even with 0 sales: price, fees, cost, DB."""
    import pricing_volume as PV
    rows = []
    for minutes, cents in PV.VOLUME_ANCHORS:
        price = cents / 100.0
        model_fee = (PV.STRIPE_FEE_FIXED_CENTS + cents * PV.STRIPE_FEE_PCT) / 100.0
        fee = price * fee_pct_observed if fee_pct_observed is not None else model_fee
        cost = minutes * cost_per_min
        db_ = price - fee - cost
        rows.append({'minutes': minutes, 'price_usd': price,
                     'fee_tax_usd': round(fee, 4), 'model_fee_usd': round(model_fee, 4),
                     'cost_usd': round(cost, 4),
                     'cost_list_usd': round(minutes * WHISPER_LIST_USD_PER_MIN, 4),
                     'db_usd': round(db_, 4),
                     'db_pct': round(100 * db_ / price, 1) if price else None})
    return rows


def cost_per_minute(actual_cost_usd, trial_min, paid_min, cost_ok=True):
    """Actual $ per transcribed minute (trial + paid on our key), else list."""
    total = float(trial_min or 0) + float(paid_min or 0)
    if cost_ok and total > 0:
        cpm = actual_cost_usd / total
        # Org-wide cost over very few minutes (staging, BYOK-heavy periods)
        # gives a meaningless rate; fall back to list and say so.
        if cpm > MAX_PLAUSIBLE_FACTOR * WHISPER_LIST_USD_PER_MIN:
            return WHISPER_LIST_USD_PER_MIN, 'list_implausible'
        return cpm, 'actual'
    return WHISPER_LIST_USD_PER_MIN, 'list'


def collect_margin(db, stripe_revenue, start, end, *, actual_fetcher=None,
                   today=None):
    """Everything the margin + DB panel needs for [start, end] (UTC days)."""
    from admin_actual_costs import fetch_actual_costs
    today = today or datetime.now(timezone.utc).date()
    bts = (stripe_revenue or {}).get('bts_norm') or [] if (stripe_revenue or {}).get('ok') else []
    actual = (actual_fetcher or fetch_actual_costs)(start, end)
    cost_ok = bool(actual.get('ok'))
    cost_by_day = {k: sum(v.values()) for k, v in (actual.get('days') or {}).items()}
    daily = daily_margin(start, end, stripe_net_usd_by_utc_day(bts), cost_by_day,
                         today=today, cost_ok=cost_ok)
    trial_min, paid_min = transcribed_minutes(db, start, end)
    total_min = trial_min + paid_min
    cpm, cpm_source = cost_per_minute(sum(cost_by_day.values()), trial_min,
                                      paid_min, cost_ok)
    by_pi = stripe_usd_by_payment_intent(bts)
    contrib = contribution_by_plan(purchase_rows(db, start, end), by_pi, cpm,
                                   user_used_fraction(db), trial_minutes=trial_min)
    gross = contrib['totals']['gross_usd']
    fee_pct = ((contrib['totals']['fees_usd'] + contrib['totals']['tax_usd']) / gross
               if gross else None)
    return {
        'stripe_ok': bool((stripe_revenue or {}).get('ok')),
        'cost_ok': cost_ok,
        'cost_error': actual.get('error'),
        'daily': daily,
        'cost_per_min_usd': round(cpm, 5),
        'cost_per_min_source': cpm_source,
        'whisper_list_usd_per_min': WHISPER_LIST_USD_PER_MIN,
        'transcribed_minutes': round(total_min, 1),
        'trial_minutes': round(trial_min, 1),
        'paid_minutes': round(paid_min, 1),
        'contribution': contrib,
        'observed_fee_tax_pct': round(100 * fee_pct, 1) if fee_pct is not None else None,
        'theoretical': theoretical_table(cpm, fee_pct),
        'theoretical_model': theoretical_table(cpm, None),
    }
