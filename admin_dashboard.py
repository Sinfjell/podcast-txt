"""Internal admin dashboard — read-only metrics for operators.

Access is email allowlist (ADMIN_EMAILS) + logged-in session. Non-admins and
anonymous visitors get 404 on every /admin route (not 403). No mutating
actions in v1; all routes are GET.
"""

from __future__ import annotations

import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import Blueprint, abort, current_app, render_template, request
from flask_login import current_user
from sqlalchemy import text
from zoneinfo import ZoneInfo

admin_bp = Blueprint('admin', __name__, url_prefix='/admin')

ADMIN_TZ = ZoneInfo('Europe/Oslo')
_DEFAULT_ADMIN_EMAILS = 'sindrefjelle@gmail.com'

# Page sizes for dashboard tables.
USERS_PAGE_SIZE = 25
TASKS_PAGE_SIZE = 25

# Stripe revenue panel (server-side only; never break /admin on failure).
STRIPE_ADMIN_TIMEOUT_SEC = 8
STRIPE_ADMIN_CACHE_TTL_SEC = 300
STRIPE_ADMIN_BT_MAX = 5000
STRIPE_ADMIN_PAYOUT_LIMIT = 20
STRIPE_ADMIN_WEEKLY_WEEKS = 16
# Balance-transaction types that count toward revenue (exclude payouts).
_REVENUE_BT_TYPES = frozenset({
    'charge', 'payment', 'payment_refund', 'refund', 'adjustment',
    'stripe_fee', 'tax', 'tax_fee', 'application_fee', 'application_fee_refund',
    'reserve_transaction', 'reserved_funds', 'fee',
})
_CHARGE_BT_TYPES = frozenset({'charge', 'payment'})
_REFUND_BT_TYPES = frozenset({'refund', 'payment_refund'})
_PERMISSION_RE = re.compile(
    r"(?:Having the |requires the |missing (?:the )?)['\"]?([a-z0-9_.]+)['\"]?"
    r"(?: permission)?",
    re.IGNORECASE,
)

_stripe_revenue_cache = {'expires_at': 0.0, 'payload': None}
_stripe_revenue_lock = threading.Lock()


def admin_emails():
    """Case-insensitive allowlist from ADMIN_EMAILS (comma-separated)."""
    raw = os.getenv('ADMIN_EMAILS', _DEFAULT_ADMIN_EMAILS)
    return {e.strip().lower() for e in raw.split(',') if e.strip()}


def is_admin_user(user=None):
    """True when *user* is authenticated and their email is in ADMIN_EMAILS."""
    u = user if user is not None else current_user
    if not getattr(u, 'is_authenticated', False):
        return False
    email = (getattr(u, 'email', None) or '').strip().lower()
    return bool(email) and email in admin_emails()


def admin_required(view):
    """Decorator: non-admins (incl. anonymous) get a plain 404."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not is_admin_user():
            abort(404)
        return view(*args, **kwargs)
    return wrapped


def _parse_db_datetime(value):
    """Coerce SQLite/SQLAlchemy datetime values to aware UTC datetime.

    Raw ``text()`` queries often return ISO strings from SQLite; ORM rows may
    return naive datetimes. Both are treated as UTC.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        # SQLite may use a space separator; fromisoformat accepts 'T' or space
        # on 3.11+, but normalize for older parsers and trailing 'Z'.
        if s.endswith('Z'):
            s = s[:-1] + '+00:00'
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            for fmt in ('%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S',
                        '%Y-%m-%d'):
                try:
                    dt = datetime.strptime(s, fmt)
                    break
                except ValueError:
                    continue
            else:
                return None
    else:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _as_utc(dt):
    """Treat naive DB datetimes as UTC; leave aware ones alone."""
    return _parse_db_datetime(dt)


def to_oslo(dt):
    """Convert a UTC (or naive-UTC) datetime to Europe/Oslo."""
    utc = _as_utc(dt)
    if utc is None:
        return None
    return utc.astimezone(ADMIN_TZ)


def format_oslo(dt, fmt='%Y-%m-%d %H:%M'):
    local = to_oslo(dt)
    return local.strftime(fmt) if local else '—'


def _oslo_day_start_utc(days_ago=0):
    """UTC instant of midnight Europe/Oslo *days_ago* days before today (Oslo)."""
    now_oslo = datetime.now(ADMIN_TZ)
    start = (now_oslo - timedelta(days=days_ago)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc)


def _naive_utc(dt):
    """Strip tz for SQLite comparisons against naive stored columns."""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _bucket_daily(timestamps, days):
    """Count events per Oslo calendar day for the last *days* days (incl. today).

    Returns (labels, counts) oldest → newest. labels are 'YYYY-MM-DD' in Oslo.
    """
    now_oslo = datetime.now(ADMIN_TZ)
    labels = []
    for i in range(days - 1, -1, -1):
        d = (now_oslo - timedelta(days=i)).date()
        labels.append(d.isoformat())
    counts = {lab: 0 for lab in labels}
    for ts in timestamps:
        local = to_oslo(ts)
        if local is None:
            continue
        key = local.date().isoformat()
        if key in counts:
            counts[key] += 1
    return labels, [counts[lab] for lab in labels]


def _bucket_daily_sum(pairs, days):
    """Like _bucket_daily but sums a numeric value per (timestamp, value)."""
    now_oslo = datetime.now(ADMIN_TZ)
    labels = []
    for i in range(days - 1, -1, -1):
        d = (now_oslo - timedelta(days=i)).date()
        labels.append(d.isoformat())
    totals = {lab: 0.0 for lab in labels}
    for ts, value in pairs:
        local = to_oslo(ts)
        if local is None:
            continue
        key = local.date().isoformat()
        if key in totals:
            totals[key] += float(value or 0)
    return labels, [round(totals[lab], 1) for lab in labels]


def classify_error_kind(message):
    """Derive a coarse error_kind from a stored error_message (no DB column)."""
    msg = (message or '').strip().lower()
    if not msg:
        return 'unknown'
    if 'stopped making progress' in msg:
        return 'stale'
    if msg.startswith('cancelled') or msg == 'cancelled.':
        return 'cancelled'
    if 'no credit' in msg or 'quota' in msg or 'billing' in msg:
        return 'openai_quota'
    if 'rate-limit' in msg or 'rate limit' in msg or 'rate_limit' in msg:
        return 'openai_rate_limit'
    if 'rejected this key' in msg or 'not an openai' in msg or 'api key' in msg:
        return 'openai_auth'
    if 'did not respond' in msg or 'could not reach openai' in msg:
        return 'openai_network'
    if 'openai had a server error' in msg or 'server error' in msg:
        return 'openai_server'
    if 'download' in msg or 'fetch' in msg or 'http' in msg:
        return 'download'
    if 'ffmpeg' in msg or 'split' in msg or 'audio' in msg and 'prepare' in msg:
        return 'ffmpeg'
    if 'disk' in msg or 'space' in msg or 'capacity' in msg:
        return 'capacity'
    return 'other'


def ensure_admin_indexes(db):
    """Additive indexes that speed admin aggregates. Safe IF NOT EXISTS.

    Created only after the target columns are known to exist (boot order:
    create_all → column migrations → this).
    """
    from sqlalchemy.exc import OperationalError

    statements = [
        ('users', 'created_at',
         'CREATE INDEX IF NOT EXISTS ix_users_created_at ON users (created_at)'),
        ('transcription_tasks', 'user_id',
         'CREATE INDEX IF NOT EXISTS ix_transcription_tasks_user_id '
         'ON transcription_tasks (user_id)'),
        ('transcription_tasks', 'started_at',
         'CREATE INDEX IF NOT EXISTS ix_transcription_tasks_started_at '
         'ON transcription_tasks (started_at)'),
        ('transcription_tasks', 'status',
         'CREATE INDEX IF NOT EXISTS ix_transcription_tasks_status '
         'ON transcription_tasks (status)'),
        ('saved_feeds', 'user_id',
         'CREATE INDEX IF NOT EXISTS ix_saved_feeds_user_id '
         'ON saved_feeds (user_id)'),
    ]
    for table, column, ddl in statements:
        try:
            cols = {
                row[1]
                for row in db.session.execute(text(f'PRAGMA table_info({table})'))
            }
        except OperationalError:
            db.session.rollback()
            continue
        if column not in cols:
            continue
        try:
            db.session.execute(text(ddl))
            db.session.commit()
        except OperationalError:
            db.session.rollback()
            current_app.logger.exception('Could not create admin index on %s.%s',
                                         table, column)


def collect_kpis(db, trial_global_seconds):
    """Single-pass KPI dict for the dashboard header cards."""
    now_utc = datetime.now(timezone.utc)
    today_start = _naive_utc(_oslo_day_start_utc(0))
    d7 = _naive_utc(now_utc - timedelta(days=7))
    d30 = _naive_utc(now_utc - timedelta(days=30))

    total_users = db.session.execute(text(
        'SELECT COUNT(*) FROM users'
    )).scalar() or 0

    signups_today = db.session.execute(text(
        'SELECT COUNT(*) FROM users WHERE created_at >= :since'
    ), {'since': today_start}).scalar() or 0

    signups_7d = db.session.execute(text(
        'SELECT COUNT(*) FROM users WHERE created_at >= :since'
    ), {'since': d7}).scalar() or 0

    signups_30d = db.session.execute(text(
        'SELECT COUNT(*) FROM users WHERE created_at >= :since'
    ), {'since': d30}).scalar() or 0

    activated = db.session.execute(text("""
        SELECT COUNT(DISTINCT user_id) FROM transcription_tasks
         WHERE status = 'completed'
    """)).scalar() or 0

    # Returning: ≥2 completed transcripts on different UTC calendar days.
    # (Oslo day boundaries would need per-row conversion; day-count is enough
    # for the KPI and stays one aggregate query.)
    returning = db.session.execute(text("""
        SELECT COUNT(*) FROM (
            SELECT user_id
              FROM transcription_tasks
             WHERE status = 'completed'
             GROUP BY user_id
            HAVING COUNT(DISTINCT date(COALESCE(completed_at, started_at))) >= 2
        )
    """)).scalar() or 0

    completed_7d = db.session.execute(text("""
        SELECT COUNT(*) FROM transcription_tasks
         WHERE status = 'completed'
           AND COALESCE(completed_at, started_at) >= :since
    """), {'since': d7}).scalar() or 0

    failed_7d = db.session.execute(text("""
        SELECT COUNT(*) FROM transcription_tasks
         WHERE status = 'error'
           AND COALESCE(completed_at, started_at) >= :since
    """), {'since': d7}).scalar() or 0

    minutes_7d = db.session.execute(text("""
        SELECT COALESCE(SUM(audio_duration), 0) / 60.0
          FROM transcription_tasks
         WHERE status = 'completed'
           AND COALESCE(completed_at, started_at) >= :since
    """), {'since': d7}).scalar() or 0.0

    trial_used_seconds = db.session.execute(text(
        'SELECT COALESCE(SUM(trial_seconds_used), 0) FROM users'
    )).scalar() or 0

    purchases = db.session.execute(text("""
        SELECT COUNT(*),
               COALESCE(SUM(COALESCE(amount_total_cents, amount_cents)), 0)
          FROM credit_purchases
         WHERE status = 'credited'
    """)).fetchone()
    purchase_count = int(purchases[0] or 0) if purchases else 0
    purchase_revenue_cents = int(purchases[1] or 0) if purchases else 0

    byok_users = db.session.execute(text("""
        SELECT COUNT(*) FROM users
         WHERE openai_api_key IS NOT NULL AND openai_api_key != ''
    """)).scalar() or 0

    followed_feeds = db.session.execute(text(
        'SELECT COUNT(*) FROM saved_feeds'
    )).scalar() or 0

    email_alerts = db.session.execute(text("""
        SELECT COUNT(*) FROM saved_feeds sf
         JOIN users u ON u.id = sf.user_id
         WHERE sf.email_new_episodes = 1
           AND u.email_unsubscribed_at IS NULL
    """)).scalar() or 0

    return {
        'total_users': int(total_users),
        'signups_today': int(signups_today),
        'signups_7d': int(signups_7d),
        'signups_30d': int(signups_30d),
        'activated_users': int(activated),
        'returning_users': int(returning),
        'completed_7d': int(completed_7d),
        'failed_7d': int(failed_7d),
        'minutes_7d': round(float(minutes_7d), 1),
        'trial_used_minutes': round(int(trial_used_seconds) / 60.0, 1),
        'trial_global_minutes': int(trial_global_seconds) // 60,
        'purchase_count': purchase_count,
        'purchase_revenue_usd': round(purchase_revenue_cents / 100.0, 2),
        'byok_users': int(byok_users),
        'followed_feeds': int(followed_feeds),
        'email_alerts_opted_in': int(email_alerts),
    }


def collect_chart_data(db, days=90):
    """Daily series + funnel + failure breakdown for the last *days* days."""
    since = _naive_utc(_oslo_day_start_utc(days - 1))

    signup_ts = [
        row[0] for row in db.session.execute(text(
            'SELECT created_at FROM users WHERE created_at >= :since'
        ), {'since': since})
    ]
    completed_rows = list(db.session.execute(text("""
        SELECT COALESCE(completed_at, started_at), user_id,
               COALESCE(audio_duration, 0)
          FROM transcription_tasks
         WHERE status = 'completed'
           AND COALESCE(completed_at, started_at) >= :since
    """), {'since': since}))

    completed_ts = [r[0] for r in completed_rows]
    minutes_pairs = [(r[0], (r[2] or 0) / 60.0) for r in completed_rows]

    # Active users: distinct user_ids with a completed transcript that Oslo day.
    labels_90, _ = _bucket_daily(completed_ts, days)
    active_by_day = {lab: set() for lab in labels_90}
    for ts, uid, _dur in completed_rows:
        local = to_oslo(ts)
        if local is None:
            continue
        key = local.date().isoformat()
        if key in active_by_day:
            active_by_day[key].add(uid)

    def series_for(n):
        lab, signups = _bucket_daily(signup_ts, n)
        _, completed = _bucket_daily(completed_ts, n)
        _, minutes = _bucket_daily_sum(minutes_pairs, n)
        active = [len(active_by_day.get(d, ())) for d in lab]
        return {
            'labels': lab,
            'signups': signups,
            'completed': completed,
            'active_users': active,
            'minutes': minutes,
        }

    # Funnel (all-time): signed up → ≥1 completed → ≥2 on different days.
    signed_up = db.session.execute(text('SELECT COUNT(*) FROM users')).scalar() or 0
    first_tx = db.session.execute(text("""
        SELECT COUNT(DISTINCT user_id) FROM transcription_tasks
         WHERE status = 'completed'
    """)).scalar() or 0
    second_tx = db.session.execute(text("""
        SELECT COUNT(*) FROM (
            SELECT user_id
              FROM transcription_tasks
             WHERE status = 'completed'
             GROUP BY user_id
            HAVING COUNT(DISTINCT date(COALESCE(completed_at, started_at))) >= 2
        )
    """)).scalar() or 0

    fail_msgs = [
        row[0] for row in db.session.execute(text("""
            SELECT error_message FROM transcription_tasks
             WHERE status = 'error'
               AND COALESCE(completed_at, started_at) >= :since
        """), {'since': since})
    ]
    fail_counts = {}
    for msg in fail_msgs:
        kind = classify_error_kind(msg)
        fail_counts[kind] = fail_counts.get(kind, 0) + 1
    fail_sorted = sorted(fail_counts.items(), key=lambda kv: (-kv[1], kv[0]))

    return {
        'range_30': series_for(30),
        'range_90': series_for(90) if days >= 90 else series_for(days),
        'funnel': {
            'labels': ['Signed up', 'First transcript', 'Second transcript'],
            'values': [int(signed_up), int(first_tx), int(second_tx)],
        },
        'failures': {
            'labels': [k for k, _ in fail_sorted],
            'values': [v for _, v in fail_sorted],
        },
    }


def list_users_page(db, page=1, q='', per_page=USERS_PAGE_SIZE):
    """Paginated latest signups with aggregates for the users table."""
    page = max(1, int(page or 1))
    q = (q or '').strip().lower()
    offset = (page - 1) * per_page

    where = ''
    params = {'limit': per_page, 'offset': offset}
    if q:
        where = 'WHERE lower(u.email) LIKE :q'
        params['q'] = f'%{q}%'

    total = db.session.execute(text(
        f'SELECT COUNT(*) FROM users u {where}'
    ), params).scalar() or 0

    rows = db.session.execute(text(f"""
        SELECT u.id, u.email, u.created_at,
               u.trial_seconds_used, u.trial_seconds_limit,
               u.openai_api_key,
               (SELECT COUNT(*) FROM transcription_tasks t
                 WHERE t.user_id = u.id AND t.status = 'completed') AS tx_count,
               (SELECT MAX(COALESCE(t.completed_at, t.started_at))
                  FROM transcription_tasks t WHERE t.user_id = u.id) AS last_active,
               (SELECT COUNT(*) FROM credit_purchases cp
                 WHERE cp.user_id = u.id AND cp.status = 'credited') AS purchase_count
          FROM users u
          {where}
         ORDER BY u.created_at DESC, u.id DESC
         LIMIT :limit OFFSET :offset
    """), params).fetchall()

    users = []
    for r in rows:
        limit_secs = r[4]
        users.append({
            'id': r[0],
            'email': r[1],
            'created_at': r[2],
            'created_at_oslo': format_oslo(r[2]),
            'trial_used_min': round((r[3] or 0) / 60.0, 1),
            'trial_limit_min': (
                None if limit_secs is None else round(limit_secs / 60.0, 1)
            ),
            'byok': bool(r[5]),
            'tx_count': int(r[6] or 0),
            'last_active_oslo': format_oslo(r[7]),
            'purchase_count': int(r[8] or 0),
            'signup_source': None,  # UTM not persisted on User today
        })

    pages = max(1, (int(total) + per_page - 1) // per_page)
    return {
        'users': users,
        'total': int(total),
        'page': page,
        'pages': pages,
        'per_page': per_page,
        'q': q,
    }


def list_tasks_page(db, page=1, per_page=TASKS_PAGE_SIZE):
    """Latest transcription tasks (any status)."""
    page = max(1, int(page or 1))
    offset = (page - 1) * per_page

    total = db.session.execute(text(
        'SELECT COUNT(*) FROM transcription_tasks'
    )).scalar() or 0

    rows = db.session.execute(text("""
        SELECT t.id, t.user_id, u.email, t.episode_title, t.status,
               t.audio_duration, t.error_message, t.started_at, t.completed_at
          FROM transcription_tasks t
          LEFT JOIN users u ON u.id = t.user_id
         ORDER BY t.started_at DESC, t.id DESC
         LIMIT :limit OFFSET :offset
    """), {'limit': per_page, 'offset': offset}).fetchall()

    tasks = []
    for r in rows:
        dur = r[5]
        tasks.append({
            'id': r[0],
            'user_id': r[1],
            'email': r[2] or '—',
            'title': r[3] or '—',
            'status': r[4],
            'duration_min': round(dur / 60.0, 1) if dur else None,
            'error_kind': classify_error_kind(r[6]) if r[4] == 'error' else None,
            'error_message': r[6],
            'created_oslo': format_oslo(r[7]),
        })

    pages = max(1, (int(total) + per_page - 1) // per_page)
    return {
        'tasks': tasks,
        'total': int(total),
        'page': page,
        'pages': pages,
        'per_page': per_page,
    }


def user_detail(db, user_id, trial_default_seconds):
    """Per-user detail payload, or None if missing."""
    row = db.session.execute(text("""
        SELECT id, email, created_at, openai_api_key,
               trial_seconds_used, trial_seconds_limit, paid_seconds_balance,
               email_transcript_ready, email_unsubscribed_at
          FROM users WHERE id = :uid
    """), {'uid': user_id}).fetchone()
    if not row:
        return None

    limit_secs = row[5]
    if limit_secs is None:
        limit_secs = trial_default_seconds

    tasks = db.session.execute(text("""
        SELECT id, episode_title, status, audio_duration, error_message,
               started_at, completed_at, podcast_name
          FROM transcription_tasks
         WHERE user_id = :uid
         ORDER BY started_at DESC
         LIMIT 100
    """), {'uid': user_id}).fetchall()

    feeds = db.session.execute(text("""
        SELECT id, name, rss_url, created_at, email_new_episodes
          FROM saved_feeds
         WHERE user_id = :uid
         ORDER BY created_at DESC
    """), {'uid': user_id}).fetchall()

    purchases = db.session.execute(text("""
        SELECT id, minutes, amount_cents, amount_total_cents, currency,
               status, created_at, stripe_session_id
          FROM credit_purchases
         WHERE user_id = :uid
         ORDER BY created_at DESC
    """), {'uid': user_id}).fetchall()

    return {
        'id': row[0],
        'email': row[1],
        'created_at_oslo': format_oslo(row[2]),
        'byok': bool(row[3]),
        'trial_used_min': round((row[4] or 0) / 60.0, 1),
        'trial_limit_min': round(limit_secs / 60.0, 1),
        'paid_min': round((row[6] or 0) / 60.0, 1),
        'email_transcript_ready': bool(row[7]),
        'email_unsubscribed': row[8] is not None,
        'tasks': [{
            'id': t[0],
            'title': t[1],
            'status': t[2],
            'duration_min': round(t[3] / 60.0, 1) if t[3] else None,
            'error_kind': classify_error_kind(t[4]) if t[2] == 'error' else None,
            'created_oslo': format_oslo(t[5]),
            'podcast_name': t[7],
        } for t in tasks],
        'feeds': [{
            'id': f[0],
            'name': f[1],
            'rss_url': f[2],
            'created_oslo': format_oslo(f[3]),
            'email_alerts': bool(f[4]),
        } for f in feeds],
        'purchases': [{
            'id': p[0],
            'minutes': p[1],
            'amount_usd': round((p[3] if p[3] is not None else p[2] or 0) / 100.0, 2),
            'currency': p[4],
            'status': p[5],
            'created_oslo': format_oslo(p[6]),
            'session_id': p[7],
        } for p in purchases],
    }


# ---------------------------------------------------------------------------
# Stripe revenue & payouts (live mode via STRIPE_SECRET_KEY; cached ~5 min)
# ---------------------------------------------------------------------------

def _stripe_obj_get(obj, key, default=None):
    """Read a field from a Stripe SDK object or plain dict."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _stripe_to_dict(obj):
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, 'to_dict'):
        try:
            return obj.to_dict()
        except Exception:  # noqa: BLE001
            pass
    return {}


def format_stripe_money(amount_cents, currency):
    """Format Stripe's integer minor units for display (USD/EUR/… ÷ 100)."""
    if amount_cents is None:
        return '—'
    cur = (currency or '').lower() or 'usd'
    # Stripe zero-decimal currencies (subset we might see).
    zero_decimal = {
        'bif', 'clp', 'djf', 'gnf', 'jpy', 'kmf', 'krw', 'mga', 'pyg', 'rwf',
        'ugx', 'vnd', 'vuv', 'xaf', 'xof', 'xpf',
    }
    try:
        n = int(amount_cents)
    except (TypeError, ValueError):
        return '—'
    sign = '-' if n < 0 else ''
    n = abs(n)
    if cur in zero_decimal:
        major = float(n)
        decimals = 0
    else:
        major = n / 100.0
        decimals = 2
    symbol = {'usd': '$', 'eur': '€', 'gbp': '£', 'nok': 'kr '}.get(cur, cur.upper() + ' ')
    if decimals == 0:
        body = f'{major:,.0f}'
    else:
        body = f'{major:,.{decimals}f}'
    if symbol.endswith(' '):
        return f'{sign}{symbol}{body}'
    return f'{sign}{symbol}{body}'


def _format_oslo_unix(ts):
    if ts is None:
        return '—'
    try:
        dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return '—'
    return format_oslo(dt, '%Y-%m-%d %H:%M')


def _format_oslo_unix_date(ts):
    if ts is None:
        return '—'
    try:
        dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return '—'
    return format_oslo(dt, '%Y-%m-%d')


def describe_stripe_admin_error(exc):
    """Human-readable Stripe failure for the admin panel (never includes secrets)."""
    if exc is None:
        return 'unknown error'
    name = type(exc).__name__
    msg = str(exc) or ''
    # Strip anything that looks like a secret key fragment.
    msg = re.sub(r'sk[_-](?:live|test)?[_-]?\w+', '[redacted]', msg)
    msg = re.sub(r'rk[_-](?:live|test)?[_-]?\w+', '[redacted]', msg)

    import app as app_module
    stripe_mod = getattr(app_module, 'stripe', None)
    perm_cls = getattr(stripe_mod, 'PermissionError', None) if stripe_mod else None
    auth_cls = getattr(stripe_mod, 'AuthenticationError', None) if stripe_mod else None
    conn_cls = getattr(stripe_mod, 'APIConnectionError', None) if stripe_mod else None

    if perm_cls and isinstance(exc, perm_cls):
        perms = _PERMISSION_RE.findall(msg)
        # Prefer Stripe's rak_* / permission-looking tokens.
        rak = [p for p in perms if 'rak_' in p or p.endswith('_read')
               or p.endswith('_write') or 'permission' in p.lower()]
        if not rak:
            rak = [p for p in perms if p.startswith('rak_') or '_' in p]
        if rak:
            # Deduplicate, keep order.
            seen = []
            for p in rak:
                if p not in seen:
                    seen.append(p)
            return (
                'API key lacks permission: ' + ', '.join(seen)
                + '. Grant balance/payouts/balance_transactions read on the '
                'restricted key (or use a secret key with those scopes).'
            )
        return (
            'API key lacks permission to read balance/payouts/'
            'balance_transactions. ' + (msg[:200] if msg else '')
        ).strip()
    if auth_cls and isinstance(exc, auth_cls):
        return 'Stripe authentication failed (invalid or revoked API key).'
    if conn_cls and isinstance(exc, conn_cls):
        return 'Stripe network error or timeout.'
    if name == 'PermissionError' or 'permission' in msg.lower():
        perms = _PERMISSION_RE.findall(msg)
        if perms:
            return 'API key lacks permission: ' + ', '.join(dict.fromkeys(perms))
    short = msg.strip().split('\n')[0][:240] if msg else name
    return f'{name}: {short}' if short and short != name else name


def admin_stripe_client():
    """Short-timeout StripeClient using app.STRIPE_SECRET_KEY. None if unset."""
    import app as app_module
    key = (getattr(app_module, 'STRIPE_SECRET_KEY', None) or '').strip()
    stripe_mod = getattr(app_module, 'stripe', None)
    if not key or stripe_mod is None:
        return None
    try:
        from stripe._http_client import RequestsClient
    except Exception:  # noqa: BLE001
        RequestsClient = None
    version = getattr(app_module, 'STRIPE_API_VERSION', None) or None
    try:
        kwargs = {'max_network_retries': 0}
        if version:
            kwargs['stripe_version'] = version
        if RequestsClient is not None:
            kwargs['http_client'] = RequestsClient(timeout=STRIPE_ADMIN_TIMEOUT_SEC)
        return stripe_mod.StripeClient(key, **kwargs)
    except TypeError:
        # Fake StripeClient in tests may not accept http_client / retries.
        try:
            if version:
                return stripe_mod.StripeClient(key, stripe_version=version)
            return stripe_mod.StripeClient(key)
        except Exception:  # noqa: BLE001
            return None


def clear_stripe_revenue_cache():
    """Test helper: drop the in-process Stripe revenue cache."""
    with _stripe_revenue_lock:
        _stripe_revenue_cache['expires_at'] = 0.0
        _stripe_revenue_cache['payload'] = None


def _empty_money_bucket():
    return {
        'gross_charges': 0,
        'stripe_fees': 0,
        'refunds': 0,
        'tax': 0,
        'other_fee_details': 0,
        'net': 0,
    }


def _accumulate_bt(bucket, bt):
    """Fold one balance_transaction into a money bucket (minor units)."""
    btype = (_stripe_obj_get(bt, 'type') or '').strip()
    amount = int(_stripe_obj_get(bt, 'amount') or 0)
    fee = int(_stripe_obj_get(bt, 'fee') or 0)
    net = int(_stripe_obj_get(bt, 'net') or 0)

    if btype in _CHARGE_BT_TYPES and amount > 0:
        bucket['gross_charges'] += amount
    if btype in _REFUND_BT_TYPES:
        # Refunds are negative amounts; store as positive outflow for display.
        bucket['refunds'] += abs(amount)

    fee_details = _stripe_obj_get(bt, 'fee_details') or []
    if fee_details:
        for fd in fee_details:
            fd_type = (_stripe_obj_get(fd, 'type') or '').strip()
            fd_amt = int(_stripe_obj_get(fd, 'amount') or 0)
            if fd_type == 'stripe_fee':
                bucket['stripe_fees'] += fd_amt
            elif fd_type == 'tax':
                bucket['tax'] += fd_amt
            elif fd_type:
                bucket['other_fee_details'] += fd_amt
    elif fee and btype in _CHARGE_BT_TYPES:
        # No breakdown — attribute the fee line as Stripe reports it on the BT.
        bucket['stripe_fees'] += fee

    if btype == 'stripe_fee':
        # Standalone fee rows: amount is typically negative.
        bucket['stripe_fees'] += abs(amount)
    if btype in ('tax', 'tax_fee'):
        bucket['tax'] += abs(amount)

    if btype in _REVENUE_BT_TYPES or btype in _CHARGE_BT_TYPES or btype in _REFUND_BT_TYPES:
        bucket['net'] += net


def _payout_destination_last4(payout):
    dest = _stripe_obj_get(payout, 'destination')
    if dest is None:
        return None
    if isinstance(dest, str):
        # Unexpanded id — never echo full bank account ids into HTML.
        return None
    last4 = _stripe_obj_get(dest, 'last4')
    if last4:
        return str(last4)
    # Nested external account dict
    d = _stripe_to_dict(dest)
    last4 = d.get('last4')
    return str(last4) if last4 else None


def _oslo_week_start(d):
    """Monday date for the Oslo calendar week containing *d* (date or datetime)."""
    if isinstance(d, datetime):
        d = d.astimezone(ADMIN_TZ).date()
    return d - timedelta(days=d.weekday())


def fetch_stripe_revenue_uncached():
    """Hit Stripe for balance, payouts, and balance_transactions. Never raises."""
    client = admin_stripe_client()
    if client is None:
        return {
            'ok': False,
            'error': 'Stripe unavailable: STRIPE_SECRET_KEY not configured '
                     '(or stripe package missing).',
            'cached': False,
            'fetched_at_oslo': format_oslo(datetime.now(timezone.utc)),
        }

    try:
        balance = client.v1.balance.retrieve(
            options={'max_network_retries': 0},
        )
    except TypeError:
        try:
            balance = client.v1.balance.retrieve()
        except Exception as exc:  # noqa: BLE001
            return {
                'ok': False,
                'error': 'Stripe unavailable: ' + describe_stripe_admin_error(exc),
                'cached': False,
                'fetched_at_oslo': format_oslo(datetime.now(timezone.utc)),
            }
    except Exception as exc:  # noqa: BLE001
        return {
            'ok': False,
            'error': 'Stripe unavailable: ' + describe_stripe_admin_error(exc),
            'cached': False,
            'fetched_at_oslo': format_oslo(datetime.now(timezone.utc)),
        }

    try:
        try:
            payout_list = client.v1.payouts.list(
                params={
                    'limit': STRIPE_ADMIN_PAYOUT_LIMIT,
                    'expand': ['data.destination'],
                },
                options={'max_network_retries': 0},
            )
        except TypeError:
            payout_list = client.v1.payouts.list(
                params={
                    'limit': STRIPE_ADMIN_PAYOUT_LIMIT,
                    'expand': ['data.destination'],
                },
            )
    except Exception as exc:  # noqa: BLE001
        return {
            'ok': False,
            'error': 'Stripe unavailable: ' + describe_stripe_admin_error(exc),
            'cached': False,
            'fetched_at_oslo': format_oslo(datetime.now(timezone.utc)),
            'partial': 'balance',
        }

    # Balance transactions — paginate once; bucket into 7d / 30d / all.
    now = datetime.now(timezone.utc)
    cutoff_7d = int((now - timedelta(days=7)).timestamp())
    cutoff_30d = int((now - timedelta(days=30)).timestamp())
    week_labels = []
    now_oslo = datetime.now(ADMIN_TZ)
    this_monday = _oslo_week_start(now_oslo.date())
    for i in range(STRIPE_ADMIN_WEEKLY_WEEKS - 1, -1, -1):
        week_labels.append((this_monday - timedelta(weeks=i)).isoformat())

    summaries = {
        '7d': {},
        '30d': {},
        'all': {},
    }
    weekly_by_currency = {}  # currency -> {week_label: net}
    bt_count = 0
    bt_truncated = False

    try:
        try:
            bt_page = client.v1.balance_transactions.list(
                params={'limit': 100},
                options={'max_network_retries': 0},
            )
        except TypeError:
            bt_page = client.v1.balance_transactions.list(params={'limit': 100})

        def _iter_bts(page):
            if hasattr(page, 'auto_paging_iter'):
                yield from page.auto_paging_iter()
                return
            data = _stripe_obj_get(page, 'data') or []
            for item in data:
                yield item

        for bt in _iter_bts(bt_page):
            bt_count += 1
            if bt_count > STRIPE_ADMIN_BT_MAX:
                bt_truncated = True
                break
            currency = (_stripe_obj_get(bt, 'currency') or 'usd').lower()
            created = int(_stripe_obj_get(bt, 'created') or 0)
            btype = (_stripe_obj_get(bt, 'type') or '').strip()
            net = int(_stripe_obj_get(bt, 'net') or 0)

            for window, cutoff in (('all', 0), ('30d', cutoff_30d), ('7d', cutoff_7d)):
                if window != 'all' and created < cutoff:
                    continue
                bucket = summaries[window].setdefault(currency, _empty_money_bucket())
                _accumulate_bt(bucket, bt)

            if btype in _REVENUE_BT_TYPES or btype in _CHARGE_BT_TYPES or btype in _REFUND_BT_TYPES:
                try:
                    local = datetime.fromtimestamp(created, tz=timezone.utc).astimezone(ADMIN_TZ)
                    wlabel = _oslo_week_start(local.date()).isoformat()
                except (TypeError, ValueError, OSError):
                    wlabel = None
                if wlabel and wlabel in week_labels:
                    weekly_by_currency.setdefault(currency, {})
                    weekly_by_currency[currency][wlabel] = (
                        weekly_by_currency[currency].get(wlabel, 0) + net
                    )
    except Exception as exc:  # noqa: BLE001
        return {
            'ok': False,
            'error': 'Stripe unavailable: ' + describe_stripe_admin_error(exc),
            'cached': False,
            'fetched_at_oslo': format_oslo(datetime.now(timezone.utc)),
            'partial': 'balance_payouts',
        }

    # Prefer the currency with the most gross (usually usd for Podskrift).
    primary = 'usd'
    all_map = summaries['all']
    if all_map:
        primary = max(all_map.keys(), key=lambda c: all_map[c]['gross_charges'])
    elif _stripe_obj_get(balance, 'available'):
        avail = _stripe_obj_get(balance, 'available') or []
        if avail:
            primary = (_stripe_obj_get(avail[0], 'currency') or 'usd').lower()

    def _money_rows(window):
        rows = []
        for cur, b in sorted(summaries[window].items()):
            rows.append({
                'currency': cur,
                'gross_charges': b['gross_charges'],
                'gross_charges_fmt': format_stripe_money(b['gross_charges'], cur),
                'stripe_fees': b['stripe_fees'],
                'stripe_fees_fmt': format_stripe_money(b['stripe_fees'], cur),
                'refunds': b['refunds'],
                'refunds_fmt': format_stripe_money(b['refunds'], cur),
                'tax': b['tax'],
                'tax_fmt': format_stripe_money(b['tax'], cur),
                'other_fee_details': b['other_fee_details'],
                'other_fee_details_fmt': format_stripe_money(
                    b['other_fee_details'], cur),
                'net': b['net'],
                'net_fmt': format_stripe_money(b['net'], cur),
                'has_tax': b['tax'] != 0,
                'has_other_fees': b['other_fee_details'] != 0,
            })
        return rows

    available = []
    for entry in (_stripe_obj_get(balance, 'available') or []):
        cur = (_stripe_obj_get(entry, 'currency') or '').lower()
        amt = int(_stripe_obj_get(entry, 'amount') or 0)
        available.append({
            'currency': cur,
            'amount': amt,
            'amount_fmt': format_stripe_money(amt, cur),
        })
    pending = []
    for entry in (_stripe_obj_get(balance, 'pending') or []):
        cur = (_stripe_obj_get(entry, 'currency') or '').lower()
        amt = int(_stripe_obj_get(entry, 'amount') or 0)
        pending.append({
            'currency': cur,
            'amount': amt,
            'amount_fmt': format_stripe_money(amt, cur),
        })

    payouts = []
    for p in (_stripe_obj_get(payout_list, 'data') or []):
        status = (_stripe_obj_get(p, 'status') or '').strip()
        cur = (_stripe_obj_get(p, 'currency') or '').lower()
        amt = int(_stripe_obj_get(p, 'amount') or 0)
        highlight = status in ('pending', 'in_transit')
        payouts.append({
            'id': _stripe_obj_get(p, 'id') or '',
            'amount': amt,
            'amount_fmt': format_stripe_money(amt, cur),
            'currency': cur,
            'status': status or '—',
            'created_oslo': _format_oslo_unix(_stripe_obj_get(p, 'created')),
            'arrival_oslo': _format_oslo_unix_date(_stripe_obj_get(p, 'arrival_date')),
            'destination_last4': _payout_destination_last4(p),
            'highlight': highlight,
        })

    weekly_map = weekly_by_currency.get(primary, {})
    weekly = {
        'currency': primary,
        'labels': week_labels,
        'values_cents': [int(weekly_map.get(lab, 0)) for lab in week_labels],
        'values': [
            round(int(weekly_map.get(lab, 0)) / 100.0, 2) for lab in week_labels
        ],
    }

    livemode = bool(_stripe_obj_get(balance, 'livemode'))
    return {
        'ok': True,
        'error': None,
        'cached': False,
        'livemode': livemode,
        'fetched_at_oslo': format_oslo(datetime.now(timezone.utc)),
        'balance': {'available': available, 'pending': pending},
        'payouts': payouts,
        'summaries': {
            '7d': _money_rows('7d'),
            '30d': _money_rows('30d'),
            'all': _money_rows('all'),
        },
        'weekly_net': weekly,
        'bt_count': bt_count if not bt_truncated else STRIPE_ADMIN_BT_MAX,
        'bt_truncated': bt_truncated,
        'primary_currency': primary,
    }


def collect_stripe_revenue(force_refresh=False):
    """Cached Stripe revenue payload for the admin dashboard."""
    now = time.monotonic()
    with _stripe_revenue_lock:
        cached = _stripe_revenue_cache.get('payload')
        expires = float(_stripe_revenue_cache.get('expires_at') or 0)
        if not force_refresh and cached is not None and now < expires:
            out = dict(cached)
            out['cached'] = True
            return out

    payload = fetch_stripe_revenue_uncached()
    # Cache successes and soft failures alike so a bad key cannot hammer Stripe.
    with _stripe_revenue_lock:
        _stripe_revenue_cache['payload'] = payload
        _stripe_revenue_cache['expires_at'] = time.monotonic() + STRIPE_ADMIN_CACHE_TTL_SEC
    out = dict(payload)
    out['cached'] = False
    return out


@admin_bp.before_request
def _admin_gate():
    """Every /admin/* path 404s unless the session user is allowlisted."""
    if not is_admin_user():
        abort(404)


@admin_bp.after_request
def _admin_response_headers(resp):
    """Admin pages (and their 404s) are never indexed, cached or referred."""
    resp.headers['X-Robots-Tag'] = 'noindex, nofollow'
    resp.headers['Cache-Control'] = 'no-store'
    resp.headers['Referrer-Policy'] = 'no-referrer'
    return resp


@admin_bp.context_processor
def _admin_template_globals():
    return {
        'is_admin_page': True,
        'admin_tz_name': 'Europe/Oslo',
    }


@admin_bp.route('/')
@admin_bp.route('')
def dashboard():
    from models import db
    import app as app_module
    # Live module constants so tests can monkeypatch TRIAL_GLOBAL_SECONDS.
    trial_global = getattr(app_module, 'TRIAL_GLOBAL_SECONDS', 6000 * 60)
    trial_default = getattr(app_module, 'TRIAL_DEFAULT_SECONDS', 180 * 60)

    kpis = collect_kpis(db, trial_global)
    charts = collect_chart_data(db, days=90)
    users = list_users_page(
        db,
        page=request.args.get('users_page', 1),
        q=request.args.get('q', ''),
    )
    tasks = list_tasks_page(
        db,
        page=request.args.get('tasks_page', 1),
    )
    # Stripe is best-effort: failures become an error box, never a 500.
    try:
        stripe_revenue = collect_stripe_revenue()
    except Exception as exc:  # noqa: BLE001
        current_app.logger.exception('admin Stripe revenue panel failed')
        stripe_revenue = {
            'ok': False,
            'error': 'Stripe unavailable: unexpected error ('
                     + type(exc).__name__ + ')',
            'cached': False,
            'fetched_at_oslo': format_oslo(datetime.now(timezone.utc)),
        }
    return render_template(
        'admin/dashboard.html',
        kpis=kpis,
        charts=charts,
        users_table=users,
        tasks_table=tasks,
        trial_default_minutes=trial_default // 60,
        stripe_revenue=stripe_revenue,
    )


@admin_bp.route('/users/<int:user_id>')
def user_page(user_id):
    from models import db
    import app as app_module
    trial_default = getattr(app_module, 'TRIAL_DEFAULT_SECONDS', 180 * 60)
    detail = user_detail(db, user_id, trial_default)
    if detail is None:
        abort(404)
    return render_template('admin/user_detail.html', user=detail)
