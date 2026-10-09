#!/usr/bin/env python3
"""Upsert one day's Podskrift growth metrics into a Notion database.

Python 3 stdlib only (sqlite3, urllib). Read-only against the SQLite database.
Timezone for the metric day is Europe/Oslo; default day is yesterday.

Each run also queries PostHog (EU) for a short product-health check, and flags
a stale/missing SQLite backup under data/backups/ (older than 26 h). When
something looks abnormal — or the check cannot run — warning lines are written
to the Notion rich-text property Notes (Norwegian, under 2000 chars). When
everything is fine, Notes is omitted so manual notes stay untouched. A missing
PostHog key, timeout, or API error never blocks the metrics upsert.

Run via ops/notion-daily-metrics.sh on the host, or:

    DRY_RUN=1 python3 ops/notion-daily-metrics.py
    python3 ops/notion-daily-metrics.py --day 2026-09-10

Requires ops/.env.metrics (or $APP_DIR/.env.metrics) with NOTION_TOKEN.
Optional: POSTHOG_PERSONAL_API_KEY (query:read), POSTHOG_PROJECT_ID,
POSTHOG_HOST. Do not set the Entity relation — the integration is database-scoped
only.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# Sibling module (ops/ is not a package).
sys.path.insert(0, str(Path(__file__).resolve().parent))
import posthog_daily_health as phh  # noqa: E402

OSLO = ZoneInfo('Europe/Oslo')
UTC = timezone.utc

DEFAULT_APP_DIR = '/var/www/vhosts/podskrift.nettsmed.dev/app'
DEFAULT_DATABASE_ID = '0d846d9fc2a4441ba5d542eb4e7609da'
NOTION_VERSION = '2022-06-28'
# Matches app.py TRIAL_MINUTES default when users.trial_seconds_limit is NULL.
DEFAULT_TRIAL_MINUTES = 180
# Matches app.py TRIAL_DAILY_MINUTES — shared free-trial budget per Oslo day.
DEFAULT_TRIAL_DAILY_MINUTES = 750
# Optional lifetime safety (app default: unset = off). Kept for ops that still
# set TRIAL_GLOBAL_MINUTES.
DEFAULT_TRIAL_GLOBAL_MINUTES = 0
# Warn (Notes + Sentry) when that Oslo day's trial usage crosses these ratios.
TRIAL_DAILY_WARN_THRESHOLDS = (0.70, 0.90)
# Back-compat alias used by older tests / callers.
TRIAL_GLOBAL_WARN_THRESHOLDS = TRIAL_DAILY_WARN_THRESHOLDS
# Nightly backup should land by 23:45 UTC; flag if the newest .gz is older than
# this (covers a missed night plus a little slack before the weekday metrics run).
BACKUP_STALE_AFTER_HOURS = 26

try:
    import sentry_sdk
except ImportError:  # pragma: no cover - metrics host may omit the SDK
    sentry_sdk = None


def _script_dir() -> Path:
    return Path(__file__).resolve().parent


def load_env_metrics() -> Path | None:
    """Load KEY=VALUE pairs from .env.metrics. Existing env wins."""
    app_dir = Path(os.environ.get('APP_DIR', DEFAULT_APP_DIR))
    candidates = [
        _script_dir() / '.env.metrics',
        app_dir / '.env.metrics',
        app_dir / 'ops' / '.env.metrics',
    ]
    chosen = next((p for p in candidates if p.is_file()), None)
    if chosen is None:
        return None
    for raw in chosen.read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
    return chosen


def parse_day(value: str | None) -> date:
    if value:
        return date.fromisoformat(value)
    return datetime.now(OSLO).date() - timedelta(days=1)


def day_bounds_utc(day: date) -> tuple[str, str]:
    """Inclusive Oslo calendar day as half-open [start, end) UTC strings.

    SQLite stores naive UTC timestamps from SQLAlchemy (no updated_at on tasks).
    """
    start = datetime(day.year, day.month, day.day, tzinfo=OSLO).astimezone(UTC)
    end = start + timedelta(days=1)
    fmt = '%Y-%m-%d %H:%M:%S'
    return start.strftime(fmt), end.strftime(fmt)


def open_db(path: Path) -> sqlite3.Connection:
    if not path.is_file() or path.stat().st_size == 0:
        raise SystemExit(f'No database at {path}')
    # mode=ro refuses CREATE; still need the file to exist (checked above).
    conn = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _scalar(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int | float:
    row = conn.execute(sql, params).fetchone()
    value = row[0] if row else 0
    return 0 if value is None else value


def trial_global_usage_ratio(used_seconds: int | float, cap_seconds: int | float) -> float | None:
    """used/cap, or None when the cap is disabled (0)."""
    cap = int(cap_seconds or 0)
    if cap <= 0:
        return None
    return max(0.0, float(used_seconds or 0) / cap)


def trial_daily_cap_warning_lines(
    used_seconds: int | float,
    cap_seconds: int | float,
) -> list[str]:
    """Norwegian Notes lines when usage crosses 70% / 90% of the daily budget."""
    ratio = trial_global_usage_ratio(used_seconds, cap_seconds)
    if ratio is None:
        return []
    used_min = float(used_seconds or 0) / 60.0
    cap_min = float(cap_seconds) / 60.0
    lines: list[str] = []
    for threshold in TRIAL_DAILY_WARN_THRESHOLDS:
        if ratio >= threshold:
            lines.append(
                f'Daglig trial-budsjett: {used_min:.0f}/{cap_min:.0f} min brukt '
                f'({ratio:.0%} av TRIAL_DAILY_MINUTES, varsel ved '
                f'{int(threshold * 100)}%+). Gratis-minutter kan snart stoppe '
                f'for alle kontoer til midnatt (Oslo).'
            )
    return lines


def trial_global_cap_warning_lines(
    used_seconds: int | float,
    cap_seconds: int | float,
) -> list[str]:
    """Back-compat alias — warnings now refer to the daily budget."""
    return trial_daily_cap_warning_lines(used_seconds, cap_seconds)


def newest_backup_mtime(
    backup_dir: Path,
    *,
    pattern: str = 'podcast-*.db.gz',
) -> float | None:
    """Epoch mtime of the newest matching backup, or None if none exist."""
    if not backup_dir.is_dir():
        return None
    newest: float | None = None
    for path in backup_dir.glob(pattern):
        if not path.is_file():
            continue
        mtime = path.stat().st_mtime
        if newest is None or mtime > newest:
            newest = mtime
    return newest


def backup_staleness_warning_lines(
    backup_dir: Path,
    *,
    max_age_hours: float = BACKUP_STALE_AFTER_HOURS,
    now: datetime | None = None,
) -> list[str]:
    """Norwegian Notes lines when the newest backup is missing or too old."""
    now_utc = now if now is not None else datetime.now(UTC)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=UTC)
    else:
        now_utc = now_utc.astimezone(UTC)

    mtime = newest_backup_mtime(backup_dir)
    if mtime is None:
        return [
            f'Ingen DB-backup funnet i {backup_dir} '
            f'(forventet podcast-*.db.gz). Sjekk podskrift-backup.timer.'
        ]

    age_sec = max(0.0, now_utc.timestamp() - mtime)
    age_hours = age_sec / 3600.0
    if age_hours <= max_age_hours:
        return []

    newest_iso = datetime.fromtimestamp(mtime, tz=UTC).strftime(
        '%Y-%m-%d %H:%M UTC'
    )
    return [
        f'DB-backup eldre enn {max_age_hours:g} timer '
        f'(nyeste: {newest_iso}, {age_hours:.0f} t gammel). '
        f'Sjekk podskrift-backup.timer / podskrift-backup.service.'
    ]


def emit_trial_global_sentry_warnings(
    used_seconds: int | float,
    cap_seconds: int | float,
    *,
    metric_day: date,
) -> list[float]:
    """Fire one Sentry warning per crossed threshold (fingerprint = day+pct).

    Returns the thresholds that fired. Never raises. No-op without SENTRY_DSN.
    """
    ratio = trial_global_usage_ratio(used_seconds, cap_seconds)
    if ratio is None or sentry_sdk is None:
        return []
    dsn = os.environ.get('SENTRY_DSN', '').strip()
    if not dsn:
        return []
    try:
        # Soft init so a bare cron host with only the DSN set still reports.
        # Tests may have already initialised a capturing transport.
        client = None
        try:
            client = sentry_sdk.get_client()
        except Exception:  # noqa: BLE001
            client = None
        if client is None or not getattr(client, 'dsn', None):
            sentry_sdk.init(
                dsn=dsn,
                environment=os.environ.get('SENTRY_ENVIRONMENT', 'production'),
                traces_sample_rate=0.0,
                send_default_pii=False,
            )
    except Exception:  # noqa: BLE001
        return []
    used_min = float(used_seconds or 0) / 60.0
    cap_min = float(cap_seconds) / 60.0
    fired: list[float] = []
    for threshold in TRIAL_GLOBAL_WARN_THRESHOLDS:
        if ratio < threshold:
            continue
        pct = int(threshold * 100)
        try:
            sentry_sdk.capture_message(
                f'Daily trial budget at {ratio:.0%} of cap '
                f'({used_min:.0f}/{cap_min:.0f} min, Oslo day)',
                level='warning',
                fingerprint=[
                    'trial-daily-cap',
                    str(pct),
                    metric_day.isoformat(),
                ],
            )
            fired.append(threshold)
        except Exception:  # noqa: BLE001 — metrics must never die on Sentry
            pass
    return fired


def collect_metrics(conn: sqlite3.Connection, day: date,
                    trial_default_seconds: int,
                    trial_global_seconds: int | None = None,
                    trial_daily_seconds: int | None = None) -> dict:
    start, end = day_bounds_utc(day)

    users_total = _scalar(
        conn,
        'SELECT COUNT(*) FROM users WHERE created_at < ?',
        (end,),
    )
    signups_1d = _scalar(
        conn,
        'SELECT COUNT(*) FROM users WHERE created_at >= ? AND created_at < ?',
        (start, end),
    )
    # No updated_at on transcription_tasks — activity from started/completed/heartbeat.
    active_users_1d = _scalar(
        conn,
        '''
        SELECT COUNT(DISTINCT user_id) FROM transcription_tasks
         WHERE (started_at >= ? AND started_at < ?)
            OR (completed_at >= ? AND completed_at < ?)
            OR (heartbeat_at >= ? AND heartbeat_at < ?)
        ''',
        (start, end, start, end, start, end),
    )
    completions_1d = _scalar(
        conn,
        '''
        SELECT COUNT(*) FROM transcription_tasks
         WHERE status = 'completed'
           AND completed_at >= ? AND completed_at < ?
        ''',
        (start, end),
    )
    # Prefer completed_at when present; otherwise started_at (errors often omit completed_at).
    # Include 'failed' for forward-compat even though the app currently writes 'error'.
    errors_1d = _scalar(
        conn,
        '''
        SELECT COUNT(*) FROM transcription_tasks
         WHERE status IN ('error', 'failed')
           AND COALESCE(completed_at, started_at) >= ?
           AND COALESCE(completed_at, started_at) < ?
        ''',
        (start, end),
    )
    never_started = _scalar(
        conn,
        '''
        SELECT COUNT(*) FROM users u
         WHERE u.created_at < ?
           AND NOT EXISTS (
                 SELECT 1 FROM transcription_tasks t
                  WHERE t.user_id = u.id AND t.started_at < ?
               )
        ''',
        (end, end),
    )
    ever_completed = _scalar(
        conn,
        '''
        SELECT COUNT(*) FROM users u
         WHERE EXISTS (
                 SELECT 1 FROM transcription_tasks t
                  WHERE t.user_id = u.id
                    AND t.status = 'completed'
                    AND t.completed_at IS NOT NULL
                    AND t.completed_at < ?
               )
        ''',
        (end,),
    )

    # Distinct Oslo calendar days with activity, as of end of metric day.
    returned_rows = conn.execute(
        '''
        SELECT user_id, started_at FROM transcription_tasks
         WHERE started_at IS NOT NULL AND started_at < ?
        ''',
        (end,),
    ).fetchall()
    days_by_user: dict[int, set[date]] = {}
    for row in returned_rows:
        raw = row['started_at']
        if not raw:
            continue
        try:
            # Stored naive UTC from the app.
            ts = datetime.fromisoformat(str(raw).replace('Z', ''))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            oslo_day = ts.astimezone(OSLO).date()
        except ValueError:
            continue
        days_by_user.setdefault(int(row['user_id']), set()).add(oslo_day)
    returned_2plus = sum(1 for days in days_by_user.values() if len(days) >= 2)

    trial_seconds_used_total = int(_scalar(
        conn,
        'SELECT COALESCE(SUM(trial_seconds_used), 0) FROM users',
    ))
    trial_minutes_used = round(trial_seconds_used_total / 60.0, 1)

    day_key = day.isoformat()
    # Prefer the atomic daily ledger; fall back to summing task charges for
    # that Oslo day when the table is absent on an old DB snapshot.
    try:
        trial_daily_used_seconds = int(_scalar(
            conn,
            'SELECT COALESCE(seconds_used, 0) FROM trial_budget_days WHERE day = ?',
            (day_key,),
        ))
    except sqlite3.OperationalError:
        trial_daily_used_seconds = int(_scalar(
            conn,
            '''
            SELECT COALESCE(SUM(COALESCE(trial_seconds_charged, 0)), 0)
              FROM transcription_tasks
             WHERE trial_seconds_charged IS NOT NULL
               AND started_at >= ? AND started_at < ?
            ''',
            (start, end),
        ))
    if trial_daily_seconds is None:
        if trial_global_seconds is not None and int(trial_global_seconds) > 0:
            # Older callers passed the daily figure via trial_global_seconds.
            trial_daily_seconds = int(trial_global_seconds)
        else:
            trial_daily_seconds = DEFAULT_TRIAL_DAILY_MINUTES * 60
    trial_daily_used_minutes = round(trial_daily_used_seconds / 60.0, 1)
    trial_daily_cap_minutes = round(int(trial_daily_seconds) / 60.0, 1)

    trial_exhausted = _scalar(
        conn,
        '''
        SELECT COUNT(*) FROM users
         WHERE COALESCE(trial_seconds_used, 0)
               >= COALESCE(trial_seconds_limit, ?)
        ''',
        (trial_default_seconds,),
    )
    return {
        'day': day.isoformat(),
        'Users total': int(users_total),
        'Signups 1d': int(signups_1d),
        'Active users 1d': int(active_users_1d),
        'Completions 1d': int(completions_1d),
        'Errors 1d': int(errors_1d),
        'Never started': int(never_started),
        'Ever completed': int(ever_completed),
        'Returned 2+ days': int(returned_2plus),
        'Trial minutes used': trial_minutes_used,
        # Shared free-trial budget for this Oslo calendar day.
        # Printed in the cron JSON; not synced as Notion columns (no schema change).
        'Trial daily used minutes': trial_daily_used_minutes,
        'Trial daily cap minutes': trial_daily_cap_minutes,
        # Back-compat keys for older dashboards / tests.
        'Trial global used minutes': trial_daily_used_minutes,
        'Trial global cap minutes': trial_daily_cap_minutes,
        'Trial exhausted': int(trial_exhausted),
        # Internal seconds for threshold checks (not Notion-bound).
        '_trial_daily_used_seconds': trial_daily_used_seconds,
        '_trial_daily_cap_seconds': int(trial_daily_seconds),
        '_trial_global_used_seconds': trial_daily_used_seconds,
        '_trial_global_cap_seconds': int(trial_daily_seconds),
    }


def notion_properties(
    metrics: dict,
    source: str,
    synced_at: datetime,
    notes_content: str | None = None,
) -> dict:
    day = metrics['day']
    props = {
        'Name': {'title': [{'type': 'text', 'text': {'content': day}}]},
        'Date': {'date': {'start': day}},
        'Source': {'select': {'name': source}},
        'Synced at': {
            'date': {
                'start': synced_at.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%S.000Z'),
            }
        },
    }
    for key in (
        'Users total',
        'Signups 1d',
        'Active users 1d',
        'Completions 1d',
        'Errors 1d',
        'Never started',
        'Ever completed',
        'Returned 2+ days',
        'Trial minutes used',
        'Trial exhausted',
    ):
        props[key] = {'number': metrics[key]}
    # Only set Notes when the health check produced warnings — omit otherwise
    # so a manual note on the row is left untouched.
    if notes_content:
        props['Notes'] = phh.notes_rich_text_property(notes_content)
    return props


def notion_request(method: str, url: str, token: str, body: dict | None = None) -> dict:
    data = None if body is None else json.dumps(body).encode('utf-8')
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            'Authorization': f'Bearer {token}',
            'Notion-Version': NOTION_VERSION,
            'Content-Type': 'application/json',
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode('utf-8', errors='replace')
        raise SystemExit(f'Notion {method} {url} failed: HTTP {exc.code}: {detail}') from exc


def find_page_id(token: str, database_id: str, day: str) -> str | None:
    url = f'https://api.notion.com/v1/databases/{database_id}/query'
    body = {
        'filter': {
            'property': 'Date',
            'date': {'equals': day},
        },
        'page_size': 1,
    }
    result = notion_request('POST', url, token, body)
    results = result.get('results') or []
    if not results:
        return None
    return results[0]['id']


def upsert_notion(
    token: str,
    database_id: str,
    metrics: dict,
    source: str,
    notes_content: str | None = None,
) -> str:
    synced_at = datetime.now(UTC)
    props = notion_properties(metrics, source, synced_at, notes_content=notes_content)
    page_id = find_page_id(token, database_id, metrics['day'])
    if page_id:
        notion_request(
            'PATCH',
            f'https://api.notion.com/v1/pages/{page_id}',
            token,
            {'properties': props},
        )
        return f'updated {page_id}'
    notion_request(
        'POST',
        'https://api.notion.com/v1/pages',
        token,
        {
            'parent': {'database_id': database_id},
            'properties': props,
            # Entity relation intentionally omitted (integration is DB-scoped).
        },
    )
    return 'created'


def posthog_query(host: str, project_id: str, api_key: str, hogql: str) -> list:
    """Run one HogQL query via the PostHog query API. Raises on HTTP/parse errors."""
    url = f'{host.rstrip("/")}/api/projects/{project_id}/query/'
    body = {'query': {'kind': 'HogQLQuery', 'query': hogql}}
    data = json.dumps(body).encode('utf-8')
    req = urllib.request.Request(
        url,
        data=data,
        method='POST',
        headers={
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
        },
    )
    with urllib.request.urlopen(req, timeout=phh.POSTHOG_QUERY_TIMEOUT_SEC) as resp:
        payload = json.loads(resp.read().decode('utf-8'))
    # HogQL query endpoint returns {"results": [[...], ...], "columns": [...]}
    rows = payload.get('results')
    if rows is None:
        rows = payload.get('result') or []
    return rows


def _health_error_reason(exc: BaseException) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return f'PostHog HTTP {exc.code}'
    if isinstance(exc, urllib.error.URLError):
        reason = getattr(exc, 'reason', exc)
        return f'PostHog nettverksfeil ({reason})'
    if isinstance(exc, TimeoutError):
        return 'PostHog timeout'
    msg = str(exc).strip() or exc.__class__.__name__
    # Keep Notes short; never echo secrets.
    if len(msg) > 120:
        msg = msg[:117] + '...'
    return msg


def fetch_health_snapshot(metric_day: date, *, api_key: str, host: str,
                          project_id: str) -> phh.HealthSnapshot:
    window_days = phh.health_window_days(metric_day)
    window_start, window_end, baseline_start, baseline_end = (
        phh.window_and_baseline_bounds(metric_day, window_days=window_days)
    )
    queries = phh.build_health_queries(
        window_start, window_end, baseline_start, baseline_end,
    )
    pt_rows = posthog_query(host, project_id, api_key, queries['pt_cta_utm'])
    if not pt_rows:
        pt_row: list | dict = [0, 0, 0, 0]
    else:
        pt_row = pt_rows[0]
    cta_rows = posthog_query(host, project_id, api_key, queries['cta_breakdown'])
    event_rows = posthog_query(host, project_id, api_key, queries['podskrift_events'])
    fail_rows = posthog_query(host, project_id, api_key, queries['failed_reasons'])
    return phh.snapshot_from_query_results(
        window_days=window_days,
        baseline_days=phh.HEALTH_BASELINE_DAYS,
        pt_cta_utm_row=pt_row,
        cta_breakdown_rows=cta_rows or [],
        podskrift_event_rows=event_rows or [],
        failed_reason_rows=fail_rows or [],
    )


def run_health_check(metric_day: date) -> list[str]:
    """Return unprefixed warning lines. Never raises — fail soft into one line."""
    try:
        api_key = os.environ.get('POSTHOG_PERSONAL_API_KEY', '').strip()
        if not api_key:
            return [
                'PostHog-helsesjekk kjørte ikke: POSTHOG_PERSONAL_API_KEY mangler',
            ]
        host = (
            os.environ.get('POSTHOG_HOST', '').strip().rstrip('/')
            or phh.DEFAULT_POSTHOG_HOST
        )
        project_id = (
            os.environ.get('POSTHOG_PROJECT_ID', '').strip()
            or phh.DEFAULT_POSTHOG_PROJECT_ID
        )
        snap = fetch_health_snapshot(
            metric_day, api_key=api_key, host=host, project_id=project_id,
        )
        return phh.evaluate_health(snap)
    except Exception as exc:  # noqa: BLE001 — health must never break metrics
        return [f'PostHog-helsesjekk kjørte ikke: {_health_error_reason(exc)}']


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--day', help='Metric day YYYY-MM-DD in Europe/Oslo (default: yesterday)')
    parser.add_argument('--db', help='Path to podcast.db (overrides PODSKRIFT_DB / APP_DIR)')
    args = parser.parse_args(argv)

    env_path = load_env_metrics()
    app_dir = Path(os.environ.get('APP_DIR', DEFAULT_APP_DIR))
    db_path = Path(
        args.db
        or os.environ.get('PODSKRIFT_DB')
        or (app_dir / 'data' / 'podcast.db')
    )
    dry_run = os.environ.get('DRY_RUN', '').strip() in ('1', 'true', 'True', 'yes')
    source = os.environ.get('METRICS_SOURCE', 'cron').strip() or 'cron'
    database_id = (
        os.environ.get('NOTION_METRICS_DATABASE_ID', DEFAULT_DATABASE_ID).strip()
        or DEFAULT_DATABASE_ID
    )
    trial_minutes = int(os.environ.get('TRIAL_MINUTES', DEFAULT_TRIAL_MINUTES))
    trial_default_seconds = trial_minutes * 60

    trial_daily_minutes = int(
        os.environ.get('TRIAL_DAILY_MINUTES', DEFAULT_TRIAL_DAILY_MINUTES)
    )
    trial_daily_seconds = max(0, trial_daily_minutes) * 60

    day = parse_day(args.day)
    with open_db(db_path) as conn:
        metrics = collect_metrics(
            conn, day, trial_default_seconds,
            trial_daily_seconds=trial_daily_seconds,
        )

    # Health check after metrics collect; failures become a single Notes line.
    health_lines = run_health_check(day)
    # Daily free-trial budget — silent stop risk for every free-trial user.
    used_s = metrics.pop(
        '_trial_daily_used_seconds',
        metrics.pop('_trial_global_used_seconds', 0),
    )
    cap_s = metrics.pop(
        '_trial_daily_cap_seconds',
        metrics.pop('_trial_global_cap_seconds', trial_daily_seconds),
    )
    metrics.pop('_trial_global_used_seconds', None)
    metrics.pop('_trial_global_cap_seconds', None)
    health_lines.extend(trial_daily_cap_warning_lines(used_s, cap_s))
    emit_trial_global_sentry_warnings(used_s, cap_s, metric_day=day)
    # Nightly SQLite backup — catches a stopped timer even if OnFailure never fired.
    backup_dir = Path(
        os.environ.get('BACKUP_DIR')
        or (app_dir / 'data' / 'backups')
    )
    health_lines.extend(backup_staleness_warning_lines(backup_dir))
    notes_content = phh.format_notes_content(health_lines)

    # Public metrics JSON (no internal underscore keys).
    print(json.dumps(metrics, indent=2, sort_keys=True))
    if notes_content:
        print(notes_content)
    else:
        print('# health: ok (no Notes)', file=sys.stderr)
    if env_path:
        print(f'# env: {env_path}', file=sys.stderr)
    print(f'# db: {db_path}', file=sys.stderr)
    print(f'# source: {source}', file=sys.stderr)

    if dry_run:
        print('# DRY_RUN=1 — skipped Notion upsert', file=sys.stderr)
        return 0

    token = os.environ.get('NOTION_TOKEN', '').strip()
    if not token:
        raise SystemExit(
            'NOTION_TOKEN is not set. Copy ops/env.metrics.example to '
            '.env.metrics (mode 600) and fill in the token.'
        )

    action = upsert_notion(
        token, database_id, metrics, source, notes_content=notes_content,
    )
    print(f'# notion: {action} for {metrics["day"]}', file=sys.stderr)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
