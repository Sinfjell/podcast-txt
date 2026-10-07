#!/usr/bin/env python3
"""Suggest changelog.json entries from recently merged PR titles.

Cheap, local, opt-in — not wired into CI. Intended for agents and humans
before opening a PR that ships a user-facing feature.

Usage:
  python3 ops/suggest-changelog.py
  python3 ops/suggest-changelog.py --since 2026-09-01
  python3 ops/suggest-changelog.py --limit 20

Requires `gh` (GitHub CLI) authenticated for the repo. Exits 0 even when
there is nothing to suggest; prints JSON candidates to stdout.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHANGELOG_PATH = ROOT / 'changelog.json'

# Titles that are almost never user-facing changelog material.
SKIP_RE = re.compile(
    r'(?i)\b('
    r'fix|hotfix|chore|docs?|ops|ci|refactor|bump|dependenc|'
    r'test|typo|lint|analytics|posthog|sentry|deploy|backup|'
    r'pragma|alter table|schema|hot.?fix'
    r')\b'
)

FEAT_HINT = re.compile(r'(?i)^(feat|feature)[:(\[]|user-facing|what.?s new')


def _run_gh(args: list[str]) -> list[dict]:
    cmd = ['gh', 'pr', 'list', '--state', 'merged', '--limit', str(args.limit),
           '--json', 'number,title,mergedAt']
    try:
        out = subprocess.check_output(cmd, cwd=ROOT, text=True, stderr=subprocess.PIPE)
    except FileNotFoundError:
        print('gh not found; install GitHub CLI to use this helper', file=sys.stderr)
        sys.exit(2)
    except subprocess.CalledProcessError as exc:
        print(exc.stderr or str(exc), file=sys.stderr)
        sys.exit(exc.returncode or 1)
    return json.loads(out)


def _slug(title: str, number: int) -> str:
    base = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')
    base = re.sub(r'^(feat|fix|docs|chore)(-|$)', '', base).strip('-')
    if not base:
        base = f'pr-{number}'
    return f'{base[:48]}-pr{number}'


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--since', default=None,
                        help='Only consider PRs merged on/after YYYY-MM-DD')
    parser.add_argument('--limit', type=int, default=30,
                        help='How many recently merged PRs to scan (default 30)')
    args = parser.parse_args()

    existing_ids: set[str] = set()
    existing_titles: set[str] = set()
    if CHANGELOG_PATH.is_file():
        data = json.loads(CHANGELOG_PATH.read_text(encoding='utf-8'))
        for entry in data.get('entries', []):
            existing_ids.add(entry.get('id', ''))
            existing_titles.add((entry.get('title') or '').lower())

    prs = _run_gh(args)
    suggestions = []
    for pr in prs:
        merged = (pr.get('mergedAt') or '')[:10]
        if args.since and merged and merged < args.since:
            continue
        title = (pr.get('title') or '').strip()
        if not title:
            continue
        if SKIP_RE.search(title) and not FEAT_HINT.search(title):
            continue
        # Prefer explicit feat/ titles; keep other non-skip titles as soft hints.
        soft = not FEAT_HINT.search(title)
        entry_id = _slug(title, pr['number'])
        if entry_id in existing_ids:
            continue
        # Rough title collision — already curated under another id.
        short = re.sub(r'[^a-z0-9]+', ' ', title.lower()).strip()
        if any(short and short in t for t in existing_titles):
            continue
        suggestions.append({
            'id': entry_id,
            'date': merged or 'YYYY-MM-DD',
            'title': title.split(':', 1)[-1].strip() if ':' in title else title,
            'summary': 'TODO: one or two sentences in plain user language.',
            'pr': pr['number'],
            'soft': soft,
        })

    print(json.dumps({'suggestions': suggestions}, indent=2, ensure_ascii=False))
    if suggestions:
        print(
            f'\n# {len(suggestions)} candidate(s). '
            'Curate into changelog.json (skip soft=true unless user-facing).',
            file=sys.stderr,
        )
    else:
        print('# No new candidates.', file=sys.stderr)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
