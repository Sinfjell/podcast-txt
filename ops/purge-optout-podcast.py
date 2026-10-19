#!/usr/bin/env python3
"""Delete every stored trace of podcasts in podcast_optout.OPTOUTS.

Dry run by default: prints row counts per table and the affected user ids.
``--apply`` deletes inside one SQLite transaction. Take a backup first
(``bash ops/backup-db.sh``). User accounts are never touched.

    sudo -u podskrift .venv/bin/python ops/purge-optout-podcast.py [--apply]

Prints counts and numeric user ids only -- no emails, no transcript text.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import podcast_optout as P  # noqa: E402
from show_pages import is_opted_out_slug  # noqa: E402

TASK_URL_COLS = ('rss_url', 'source_audio_url', 'source_apple_url',
                 'source_spotify_url', 'source_website_url')


def _cols(conn, table):
    return {r[1] for r in conn.execute(f'PRAGMA table_info({table})')}


def plan(conn, cache_dir: Path):
    out = {'tasks': [], 'shares': [], 'summary_jobs': [], 'saved_feeds': [],
           'feed_cache_files': [], 'user_ids': set()}
    tcols = _cols(conn, 'transcription_tasks')
    url_cols = [c for c in TASK_URL_COLS if c in tcols]
    sel = ', '.join(['id', 'user_id', 'podcast_name'] + url_cols)
    for row in conn.execute(f'SELECT {sel} FROM transcription_tasks'):
        task_id, user_id, name, *urls = row
        if P.match_fields(urls=[u for u in urls if u], names=[name]):
            out['tasks'].append(task_id)
            if user_id is not None:
                out['user_ids'].add(int(user_id))
    ids = set(out['tasks'])
    if 'summary_source_task_id' in tcols and ids:
        # Summaries built from a blocked transcript are derived data too.
        for (tid, uid, src) in conn.execute(
                'SELECT id, user_id, summary_source_task_id FROM transcription_tasks'
                ' WHERE summary_source_task_id IS NOT NULL'):
            if src in ids and tid not in ids:
                out['tasks'].append(tid)
                ids.add(tid)
                if uid is not None:
                    out['user_ids'].add(int(uid))
    for (sid, tid) in conn.execute('SELECT id, task_id FROM transcript_shares'):
        if tid in ids:
            out['shares'].append(sid)
    if _cols(conn, 'summary_email_jobs'):
        for (jid, tid, rss, audio, name) in conn.execute(
                'SELECT id, task_id, rss_url, audio_url, podcast_name'
                ' FROM summary_email_jobs'):
            if tid in ids or P.match_fields(urls=[rss, audio], names=[name]):
                out['summary_jobs'].append(jid)
    for (fid, uid, name, rss) in conn.execute(
            'SELECT id, user_id, name, rss_url FROM saved_feeds'):
        if P.match_fields(urls=[rss], names=[name]):
            out['saved_feeds'].append(fid)
            out['user_ids'].add(int(uid))
    if cache_dir.is_dir():
        for path in sorted(cache_dir.glob('*.json')):
            try:
                data = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, json.JSONDecodeError):
                data = {}
            if not isinstance(data, dict):
                data = {}
            urls = [data.get('feed_url')] + [
                ep.get('audio_url') for ep in data.get('episodes') or []
                if isinstance(ep, dict)]
            hit = P.match_fields(urls=[u for u in urls if u])
            if hit or is_opted_out_slug(path.stem):
                out['feed_cache_files'].append(path)
    return out


def _delete(conn, table, ids):
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        conn.execute(f'DELETE FROM {table} WHERE id IN ({",".join("?" * len(chunk))})',
                     chunk)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default=str(ROOT / 'data' / 'podcast.db'))
    ap.add_argument('--cache-dir', default=str(ROOT / 'data' / 'show_feed_cache'))
    ap.add_argument('--apply', action='store_true')
    args = ap.parse_args(argv)

    conn = sqlite3.connect(args.db, timeout=30)
    try:
        p = plan(conn, Path(args.cache_dir))
        counts = {
            'transcription_tasks': len(p['tasks']),
            'transcript_shares': len(p['shares']),
            'summary_email_jobs': len(p['summary_jobs']),
            'saved_feeds': len(p['saved_feeds']),
            'show_feed_cache_files': len(p['feed_cache_files']),
        }
        print(json.dumps({'mode': 'apply' if args.apply else 'dry-run',
                          'counts': counts,
                          'user_ids': sorted(p['user_ids'])}))
        if not args.apply:
            return 0
        with conn:  # one transaction
            _delete(conn, 'transcript_shares', p['shares'])
            if p['summary_jobs']:
                _delete(conn, 'summary_email_jobs', p['summary_jobs'])
            _delete(conn, 'saved_feeds', p['saved_feeds'])
            _delete(conn, 'transcription_tasks', p['tasks'])
        for path in p['feed_cache_files']:
            os.unlink(path)
        after = plan(conn, Path(args.cache_dir))
        print(json.dumps({'remaining': {
            'transcription_tasks': len(after['tasks']),
            'transcript_shares': len(after['shares']),
            'summary_email_jobs': len(after['summary_jobs']),
            'saved_feeds': len(after['saved_feeds']),
            'show_feed_cache_files': len(after['feed_cache_files']),
        }}))
        return 0
    finally:
        conn.close()


if __name__ == '__main__':
    sys.exit(main())
