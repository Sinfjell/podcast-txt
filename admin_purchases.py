"""Canonical purchase accounting for admin revenue views.

A purchase counts only when:

* ``status`` is ``credited`` (completed; not refunded / partially refunded /
  disputed), and
* the buyer is not an internal or test user (same exclusion list and email
  rules as lifecycle mail).

Revenue is the price paid **including VAT**. Ex-VAT is shown alongside where
revenue is displayed. Refunds and disputes are reported separately and never
inflate the purchase count or revenue total.

Nothing here touches Stripe Checkout, pricing, or money movement — read-only
aggregates over ``credit_purchases``.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import text

#: Completed, keep-the-money statuses that count as a purchase.
COUNTED_STATUS = 'credited'
#: Money-out statuses reported separately (not in purchase count / revenue).
REFUND_DISPUTE_STATUSES = ('refunded', 'partially_refunded', 'disputed')
#: Historical paid statuses still used when listing refund/dispute rows.
PAID_STATUSES = (COUNTED_STATUS,) + REFUND_DISPUTE_STATUSES


def is_internal_or_test_user(user_id, email=None):
    """True for our own / smoke / excluded accounts — never customer revenue."""
    from lifecycle_emails import email_problem, excluded_user_ids
    try:
        uid = int(user_id) if user_id is not None else None
    except (TypeError, ValueError):
        uid = None
    if uid is not None and uid in excluded_user_ids():
        return True
    return email_problem(email) == 'test_user'


def _utc_bounds(start, end):
    """Inclusive date window → naive UTC [lo, hi)."""
    lo = datetime(start.year, start.month, start.day)
    hi = datetime(end.year, end.month, end.day) + timedelta(days=1)
    return lo, hi


def _row_money(gross_cents, tax_cents, refunded_cents=0):
    """USD floats from stored cents. Revenue = gross incl. VAT."""
    gross = int(gross_cents or 0) / 100.0
    tax = min(int(tax_cents or 0) / 100.0, gross)
    refunded = min(int(refunded_cents or 0) / 100.0, gross)
    return {
        'gross_usd': round(gross, 4),
        'vat_usd': round(tax, 4),
        'ex_vat_usd': round(gross - tax, 4),
        'refunded_usd': round(refunded, 4),
        # Canonical revenue for a counted purchase: price paid incl. VAT.
        'revenue_usd': round(gross, 4),
    }


def fetch_purchase_rows(db, *, start=None, end=None, statuses=None):
    """Raw purchase rows joined to buyer email, optionally windowed."""
    statuses = tuple(statuses or PAID_STATUSES)
    placeholders = ', '.join(f"'{s}'" for s in statuses)
    sql = f"""
        SELECT cp.id, cp.user_id, u.email, cp.created_at, cp.minutes, cp.status,
               COALESCE(cp.amount_total_cents, cp.amount_cents) AS gross_cents,
               COALESCE(cp.amount_tax_cents, 0) AS tax_cents,
               COALESCE(cp.amount_refunded_cents, 0) AS refunded_cents,
               cp.customer_country, cp.price_source, cp.pricing_version,
               cp.stripe_payment_intent_id AS pi,
               cp.stripe_session_id,
               COALESCE(cp.seconds_clawed_back, 0) AS clawed
          FROM credit_purchases cp
          LEFT JOIN users u ON u.id = cp.user_id
         WHERE cp.status IN ({placeholders})
    """
    params = {}
    if start is not None and end is not None:
        lo, hi = _utc_bounds(start, end)
        sql += ' AND cp.created_at >= :lo AND cp.created_at < :hi'
        params = {'lo': lo, 'hi': hi}
    sql += ' ORDER BY cp.created_at, cp.id'
    return [dict(r._mapping) for r in db.session.execute(text(sql), params)]


def counted_purchases(rows):
    """Filter to purchases that count toward count / revenue."""
    out = []
    for r in rows:
        if (r.get('status') or '') != COUNTED_STATUS:
            continue
        if is_internal_or_test_user(r.get('user_id'), r.get('email')):
            continue
        out.append(r)
    return out


def refund_dispute_purchases(rows):
    """Refunded / disputed rows for external (non-internal) buyers only."""
    out = []
    for r in rows:
        if (r.get('status') or '') not in REFUND_DISPUTE_STATUSES:
            continue
        if is_internal_or_test_user(r.get('user_id'), r.get('email')):
            continue
        out.append(r)
    return out


def summarize_purchase_money(rows):
    """Aggregate counted purchases + separate refund/dispute totals.

    Returns a dict safe to pass into templates / JSON (no live amounts from
    production — callers supply their own DB rows / fixtures).
    """
    counted = counted_purchases(rows)
    refunded = refund_dispute_purchases(rows)
    revenue = 0.0
    ex_vat = 0.0
    vat = 0.0
    for r in counted:
        m = _row_money(r.get('gross_cents'), r.get('tax_cents'))
        revenue += m['revenue_usd']
        ex_vat += m['ex_vat_usd']
        vat += m['vat_usd']
    refund_gross = 0.0
    refund_count = 0
    dispute_count = 0
    for r in refunded:
        m = _row_money(r.get('gross_cents'), r.get('tax_cents'),
                       r.get('refunded_cents'))
        # Prefer the amount actually refunded; fall back to full gross.
        refund_gross += m['refunded_usd'] or m['gross_usd']
        if (r.get('status') or '') == 'disputed':
            dispute_count += 1
        else:
            refund_count += 1
    return {
        'purchase_count': len(counted),
        'revenue_usd': round(revenue, 2),
        'ex_vat_usd': round(ex_vat, 2),
        'vat_usd': round(vat, 2),
        'refund_count': refund_count,
        'dispute_count': dispute_count,
        'refunds_disputes_usd': round(refund_gross, 2),
        'counted': counted,
        'refunds_disputes': refunded,
    }


def purchase_totals_all_time(db):
    """Dashboard KPI: all-time counted purchases (incl. VAT revenue)."""
    return summarize_purchase_money(fetch_purchase_rows(db))
