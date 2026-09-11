#!/usr/bin/env python3
"""Upsert one day's Podskrift growth metrics into a Notion database.

Python 3 stdlib only (sqlite3, urllib). Read-only against the SQLite database.
Timezone for the metric day is Europe/Oslo; default day is yesterday.

Run via ops/notion-daily-metrics.sh on the host, or:

    DRY_RUN=1 python3 ops/notion-daily-metrics.py
    python3 ops/notion-daily-metrics.py --day 2026-09-10

Requires ops/.env.metrics (or $APP_DIR/.env.metrics) with NOTION_TOKEN.
Do not set the Entity relation — the integration is database-scoped only.
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

OSLO = ZoneInfo('Europe/Oslo')
UTC = timezone.utc

DEFAULT_APP_DIR = '/var/www/vhosts/podskrift.nettsmed.dev/app'
DEFAULT_DATABASE_ID = '0d846d9fc2a4441ba5d542eb4e7609da'
NOTION_VERSION = '2022-06-28'
# Matches app.py TRIAL_MINUTES default when users.trial_seconds_limit is NULL.
DEFAULT_TRIAL_MINUTES = 60


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


def collect_metrics(conn: sqlite3.Connection, day: date,
                    trial_default_seconds: int) -> dict:
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

    trial_minutes_used = float(_scalar(
        conn,
        'SELECT COALESCE(SUM(trial_seconds_used), 0) / 60.0 FROM users',
    ))
    # Round to one decimal for a stable Notion number.
    trial_minutes_used = round(trial_minutes_used, 1)

    trial_exhausted = _scalar(
        conn,
        '''
        SELECT COUNT(*) FROM users
         WHERE COALESCE(trial_seconds_used, 0)
               >= COALESCE(trial_seconds_limit, ?)
        ''',
        (trial_default_seconds,),
    )
    saved_feeds = _scalar(
        conn,
        'SELECT COUNT(*) FROM saved_feeds WHERE created_at < ?',
        (end,),
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
        'Trial exhausted': int(trial_exhausted),
        'Saved feeds': int(saved_feeds),
    }


def notion_properties(metrics: dict, source: str, synced_at: datetime) -> dict:
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
        'Saved feeds',
    ):
        props[key] = {'number': metrics[key]}
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


def upsert_notion(token: str, database_id: str, metrics: dict, source: str) -> str:
    synced_at = datetime.now(UTC)
    props = notion_properties(metrics, source, synced_at)
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

    day = parse_day(args.day)
    with open_db(db_path) as conn:
        metrics = collect_metrics(conn, day, trial_default_seconds)

    print(json.dumps(metrics, indent=2, sort_keys=True))
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

    action = upsert_notion(token, database_id, metrics, source)
    print(f'# notion: {action} for {metrics["day"]}', file=sys.stderr)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
