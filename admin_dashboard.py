"""Internal admin dashboard — read-only metrics for operators.

Access is email allowlist (ADMIN_EMAILS) + logged-in session. Non-admins and
anonymous visitors get 404 on every /admin route (not 403). No mutating
actions in v1; all routes are GET.
"""

from __future__ import annotations

import os
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
    return render_template(
        'admin/dashboard.html',
        kpis=kpis,
        charts=charts,
        users_table=users,
        tasks_table=tasks,
        trial_default_minutes=trial_default // 60,
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
