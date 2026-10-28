"""Per-day unit economics for the Notion metrics row: variable costs only.

Python 3 stdlib only (runs next to notion-daily-metrics.py on the host).
Read-only against SQLite; Stripe is read-only (GET balance_transactions).
No fixed/server costs here -- this answers "did yesterday's usage make or lose
money on the margin".

Definitions (USD, Europe/Oslo calendar days, same day bucketing as the rest of
the metrics row):

* Gross revenue  = price paid incl. VAT of counted purchases (status
  ``credited``, internal/test buyers excluded -- same rule as admin_purchases).
* Net revenue    = what lands with us: Stripe balance-transaction ``net``
  (after Stripe fees and VAT withheld by Managed Payments) minus the separate
  daily "Managed Payments Transaction Fee", prorated to counted purchases.
  When Stripe cannot be reached: gross - VAT - modelled fee (estimate).
* STT cost       = ``transcription_tasks.stt_cost_usd`` (what we paid, incl.
  any Scribe shadow spend) for platform-funded jobs. Jobs before per-job cost
  tracking (no ``stt_provider``) are estimated at whisper-1 list price on the
  charged minutes. BYOK jobs (nothing charged to trial/paid) cost us nothing.
* LLM cost       = ``summary_cost_usd_est`` (token-based estimate).
* Contribution margin = Net revenue - (STT + LLM).
* Cumulative CM  = sum of daily CM from FIRST_REVENUE_DAY through the day.

Refunded/disputed purchases are not counted (they never enter gross/net).
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

OSLO = ZoneInfo('Europe/Oslo')
UTC = timezone.utc

#: First real customer purchase; cumulative contribution margin starts here.
FIRST_REVENUE_DAY = date(2026, 10, 9)
COUNTED_STATUS = 'credited'

#: Mirrors lifecycle_emails.DEFAULT_EXCLUDED_USER_IDS (kept in sync by a test;
#: lifecycle_emails needs app deps the metrics host python may not have).
DEFAULT_EXCLUDED_USER_IDS = frozenset({
    1, 31, 40, 90, 126, 130, 131, 140, 205, 212, 221, 222,
    207, 209, 210, 213, 214, 215,
})
EXCLUDED_EMAIL_DOMAINS = frozenset({
    'podskrift.com', 'test.podskrift.com', 'nettsmed.no', 'nettsmed.dev',
    'example.com', 'example.org', 'example.net',
})
EXCLUDED_EMAIL_TLDS = frozenset({'test', 'invalid', 'example', 'localhost', 'local'})
_TEST_LOCAL_RE = re.compile(r'^(smoke|e2e|test|abuseshot|ux-shots)[-+._0-9]', re.I)

#: whisper-1 list price, for jobs that predate per-job cost tracking.
LEGACY_STT_USD_PER_MIN = 0.006
#: Fee model when Stripe is unreachable (mirrors pricing_volume).
MODEL_FEE_FIXED_USD = 0.30
MODEL_FEE_PCT = 0.064

STRIPE_API = 'https://api.stripe.com/v1'
MP_FEE_RE = re.compile(r'Managed Payments Transaction Fee \((\d{4}-\d{2}-\d{2})\)')

PROVIDER_BUCKETS = ('together', 'openai', 'mixed')


def excluded_user_ids() -> frozenset:
    extra = {int(p) for p in (os.getenv('LIFECYCLE_EXCLUDE_USER_IDS') or '').split(',')
             if p.strip().isdigit()}
    return DEFAULT_EXCLUDED_USER_IDS | frozenset(extra)


def is_test_buyer(user_id, email) -> bool:
    try:
        if user_id is not None and int(user_id) in excluded_user_ids():
            return True
    except (TypeError, ValueError):
        pass
    email = (email or '').strip()
    if '@' not in email:
        return False
    local, domain = email.rsplit('@', 1)
    domain = domain.lower()
    tld = domain.rsplit('.', 1)[-1]
    if domain in EXCLUDED_EMAIL_DOMAINS or tld in EXCLUDED_EMAIL_TLDS:
        return True
    if any(domain.endswith('.' + d) for d in EXCLUDED_EMAIL_DOMAINS):
        return True
    if _TEST_LOCAL_RE.match(local) or local.lower().startswith('sindrefjelle'):
        return True
    return False


def oslo_day(raw) -> date | None:
    """Oslo calendar day of a naive-UTC DB timestamp."""
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(str(raw).replace('Z', ''))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.astimezone(OSLO).date()


def _utc_naive(d: date) -> str:
    """Start of Oslo day *d* as naive UTC string (DB format)."""
    start = datetime.combine(d, time.min, tzinfo=OSLO).astimezone(UTC)
    return start.replace(tzinfo=None).isoformat(sep=' ')


def provider_bucket(label) -> str:
    p = (label or '').strip().lower()
    if p == 'together':
        return 'together'
    if not p or p.startswith('whisper') or p.startswith('gpt') or p == 'openai':
        return 'openai'
    return 'mixed'


# --------------------------------------------------------------------------- DB

def counted_purchases(conn, end_day: date) -> list[dict]:
    """Counted purchases created before the end of Oslo day *end_day*."""
    hi = _utc_naive(end_day + timedelta(days=1))
    rows = conn.execute(
        '''
        SELECT cp.id, cp.user_id, u.email, cp.created_at,
               COALESCE(cp.amount_total_cents, cp.amount_cents) AS gross_cents,
               COALESCE(cp.amount_tax_cents, 0) AS tax_cents,
               cp.stripe_payment_intent_id AS pi
          FROM credit_purchases cp
          LEFT JOIN users u ON u.id = cp.user_id
         WHERE cp.status = ? AND cp.created_at < ?
         ORDER BY cp.created_at, cp.id
        ''',
        (COUNTED_STATUS, hi),
    ).fetchall()
    out = []
    for r in rows:
        r = dict(r)
        if is_test_buyer(r['user_id'], r['email']):
            continue
        r['day'] = oslo_day(r['created_at'])
        if r['day'] is None:
            continue
        out.append(r)
    return out


def variable_costs_by_day(conn, start: date, end: date) -> dict[date, dict]:
    """{oslo_day: {stt_<bucket>, stt_est, stt, llm}} for platform-funded jobs."""
    lo, hi = _utc_naive(start), _utc_naive(end + timedelta(days=1))
    out: dict[date, dict] = {}

    def slot(d):
        return out.setdefault(d, {**{f'stt_{b}': 0.0 for b in PROVIDER_BUCKETS},
                                  'stt_est': 0.0, 'stt': 0.0, 'llm': 0.0})

    for r in conn.execute(
        '''
        SELECT COALESCE(completed_at, started_at) AS ev, stt_provider, stt_cost_usd,
               COALESCE(trial_seconds_charged, 0) + COALESCE(paid_seconds_charged, 0)
                   AS charged_s
          FROM transcription_tasks
         WHERE status IN ('completed', 'error', 'cancelled')
           AND COALESCE(completed_at, started_at) >= ?
           AND COALESCE(completed_at, started_at) < ?
        ''',
        (lo, hi),
    ):
        if int(r['charged_s'] or 0) <= 0:
            continue  # BYOK / nothing on our key
        d = oslo_day(r['ev'])
        if d is None:
            continue
        s = slot(d)
        if r['stt_cost_usd'] is not None:
            cost = float(r['stt_cost_usd'])
        elif r['stt_provider'] is None:
            cost = int(r['charged_s']) / 60.0 * LEGACY_STT_USD_PER_MIN
            s['stt_est'] += cost
        else:
            cost = 0.0
        s[f'stt_{provider_bucket(r["stt_provider"])}'] += cost
        s['stt'] += cost

    for r in conn.execute(
        '''
        SELECT COALESCE(completed_at, started_at) AS ev, summary_cost_usd_est
          FROM transcription_tasks
         WHERE summary_cost_usd_est IS NOT NULL
           AND COALESCE(completed_at, started_at) >= ?
           AND COALESCE(completed_at, started_at) < ?
        ''',
        (lo, hi),
    ):
        d = oslo_day(r['ev'])
        if d is not None:
            slot(d)['llm'] += float(r['summary_cost_usd_est'] or 0.0)
    return out


# ----------------------------------------------------------------------- Stripe

def stripe_key(app_dir: Path) -> str:
    """STRIPE_SECRET_KEY from env, else that single line of the app .env."""
    key = os.environ.get('STRIPE_SECRET_KEY', '').strip()
    if key:
        return key
    try:
        for line in (app_dir / '.env').read_text().splitlines():
            if line.startswith('STRIPE_SECRET_KEY='):
                return line.split('=', 1)[1].strip().strip('"\'')
    except OSError:
        pass
    return ''


def fetch_balance_transactions(key: str, since: date, *, opener=None) -> list[dict]:
    """All balance transactions created since *since* (charge source expanded)."""
    opener = opener or urllib.request.urlopen
    since_ts = int(datetime.combine(since - timedelta(days=1), time.min,
                                    tzinfo=UTC).timestamp())
    out, after = [], None
    while True:
        params = [('limit', '100'), ('created[gte]', str(since_ts)),
                  ('expand[]', 'data.source')]
        if after:
            params.append(('starting_after', after))
        req = urllib.request.Request(
            f'{STRIPE_API}/balance_transactions?{urllib.parse.urlencode(params)}',
            headers={'Authorization': f'Bearer {key}'},
        )
        with opener(req, timeout=30) as resp:
            page = json.loads(resp.read().decode('utf-8'))
        data = page.get('data') or []
        out.extend(data)
        if not page.get('has_more') or not data:
            return out
        after = data[-1]['id']


def stripe_net_by_day(purchases: list[dict], bts: list[dict]) -> tuple[dict, set]:
    """({oslo_day: net_usd}, pis_found) from Stripe for counted purchases.

    Charges settle in the account currency; each charge's USD factor is the DB
    gross (USD cents) over the balance-transaction amount. Daily Managed
    Payments fees are converted at the mean factor and prorated to counted
    purchases by that day's share of charged gross.
    """
    counted = {p['pi']: p for p in purchases if p.get('pi')}
    charge_by_pi, all_charge_amount_by_utc_day, factors = {}, {}, []
    for b in bts:
        src = b.get('source') if isinstance(b.get('source'), dict) else {}
        if b.get('type') in ('charge', 'payment') and src.get('payment_intent'):
            utc_d = datetime.fromtimestamp(int(b['created']), tz=UTC).date()
            all_charge_amount_by_utc_day[utc_d] = (
                all_charge_amount_by_utc_day.get(utc_d, 0) + int(b['amount']))
            p = counted.get(src['payment_intent'])
            if p and int(b['amount']) > 0:
                charge_by_pi[p['pi']] = b
                factors.append(int(p['gross_cents']) / int(b['amount']))
    net: dict[date, float] = {}
    counted_amount_by_utc_day: dict[date, int] = {}
    for pi, b in charge_by_pi.items():
        p = counted[pi]
        f = int(p['gross_cents']) / int(b['amount'])
        net[p['day']] = net.get(p['day'], 0.0) + int(b['net']) * f / 100.0
        utc_d = datetime.fromtimestamp(int(b['created']), tz=UTC).date()
        counted_amount_by_utc_day[utc_d] = (
            counted_amount_by_utc_day.get(utc_d, 0) + int(b['amount']))
    mean_f = sum(factors) / len(factors) if factors else None
    for b in bts:
        if b.get('type') != 'stripe_fee' or mean_f is None:
            continue
        m = MP_FEE_RE.search(b.get('description') or '')
        if not m:
            continue
        fee_day = date.fromisoformat(m.group(1))
        total = all_charge_amount_by_utc_day.get(fee_day, 0)
        mine = counted_amount_by_utc_day.get(fee_day, 0)
        if total <= 0 or mine <= 0:
            continue
        share = mine / total
        net[fee_day] = net.get(fee_day, 0.0) + int(b['amount']) * mean_f / 100.0 * share
    return net, set(charge_by_pi)


def model_net_usd(gross_cents: int, tax_cents: int) -> float:
    gross = int(gross_cents or 0) / 100.0
    tax = min(int(tax_cents or 0) / 100.0, gross)
    return gross - tax - (MODEL_FEE_FIXED_USD + gross * MODEL_FEE_PCT)


# ---------------------------------------------------------------------- collect

def collect_unit_economics(conn, day: date, *, bts: list[dict] | None,
                           start: date = FIRST_REVENUE_DAY) -> dict:
    """Notion-bound unit-economics fields for *day* plus cumulative CM.

    *bts* = Stripe balance transactions, or None when Stripe is unavailable
    (net revenue is then modelled and the source says ``estimate``).
    """
    purchases = counted_purchases(conn, day)
    first_day_by_user: dict = {}
    for p in purchases:
        first_day_by_user.setdefault(p['user_id'], p['day'])

    stripe_net, found = ({}, set()) if bts is None else stripe_net_by_day(purchases, bts)
    net_by_day: dict[date, float] = dict(stripe_net)
    estimated_days: set[date] = set()
    for p in purchases:
        if p.get('pi') in found:
            continue
        net_by_day[p['day']] = net_by_day.get(p['day'], 0.0) + model_net_usd(
            p['gross_cents'], p['tax_cents'])
        estimated_days.add(p['day'])

    costs = variable_costs_by_day(conn, min(start, day), day)
    zero = {**{f'stt_{b}': 0.0 for b in PROVIDER_BUCKETS},
            'stt_est': 0.0, 'stt': 0.0, 'llm': 0.0}

    cumulative = 0.0
    d = start
    while d <= day:
        c = costs.get(d, zero)
        cumulative += net_by_day.get(d, 0.0) - c['stt'] - c['llm']
        d += timedelta(days=1)

    todays = [p for p in purchases if p['day'] == day]
    c = costs.get(day, zero)
    net = net_by_day.get(day, 0.0)
    var_cost = c['stt'] + c['llm']
    if not todays:
        source = 'n/a'
    elif day in estimated_days:
        source = 'estimate' if not (set(p['pi'] for p in todays) & found) else 'mixed'
    else:
        source = 'stripe'
    r2 = lambda x: round(x, 2)  # noqa: E731
    r4 = lambda x: round(x, 4)  # noqa: E731
    return {
        'Gross revenue (USD)': r2(sum(int(p['gross_cents']) for p in todays) / 100.0),
        'Net revenue (USD)': r2(net),
        'Net revenue source': source,
        'Purchases 1d': len(todays),
        'Buyers 1d': len({p['user_id'] for p in todays}),
        'New buyers 1d': len({p['user_id'] for p in todays
                              if first_day_by_user.get(p['user_id']) == day}),
        'STT cost Together (USD)': r4(c['stt_together']),
        'STT cost OpenAI (USD)': r4(c['stt_openai']),
        'STT cost mixed/Scribe (USD)': r4(c['stt_mixed']),
        'STT cost estimated (USD)': r4(c['stt_est']),
        'STT cost (USD)': r4(c['stt']),
        'LLM cost est. (USD)': r4(c['llm']),
        'Variable cost (USD)': r4(var_cost),
        'Contribution margin (USD)': r2(net - var_cost),
        'Cumulative CM (USD)': r2(cumulative),
    }


NOTION_NUMBER_KEYS = (
    'Gross revenue (USD)', 'Net revenue (USD)', 'Purchases 1d', 'Buyers 1d',
    'New buyers 1d', 'STT cost Together (USD)', 'STT cost OpenAI (USD)',
    'STT cost mixed/Scribe (USD)', 'STT cost estimated (USD)', 'STT cost (USD)',
    'LLM cost est. (USD)', 'Variable cost (USD)', 'Contribution margin (USD)',
    'Cumulative CM (USD)',
)
NOTION_SELECT_KEYS = ('Net revenue source',)
USD_KEYS = tuple(k for k in NOTION_NUMBER_KEYS if k.endswith('(USD)'))


def load_stripe_bts(app_dir: Path, since: date = FIRST_REVENUE_DAY) -> tuple[list | None, str | None]:
    """(balance transactions, None) or (None, reason). Never raises."""
    if os.environ.get('UNIT_ECONOMICS_STRIPE', '1').strip() in ('0', 'false', 'no'):
        return None, 'Stripe-oppslag avslått (UNIT_ECONOMICS_STRIPE=0)'
    key = stripe_key(app_dir)
    if not key:
        return None, 'STRIPE_SECRET_KEY mangler'
    try:
        return fetch_balance_transactions(key, since), None
    except urllib.error.HTTPError as exc:
        return None, f'Stripe HTTP {exc.code}'
    except Exception as exc:  # noqa: BLE001 - reporting must not block the row
        return None, f'Stripe: {type(exc).__name__}'
