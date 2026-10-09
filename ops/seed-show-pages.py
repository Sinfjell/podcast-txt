#!/usr/bin/env python3
"""Fetch Apple top-podcast charts and write data/show_pages.json.

Run manually when refreshing the curated set (not on every deploy):

    python3 ops/seed-show-pages.py

Reads Apple's public top-podcasts JSON for a few storefronts, looks up each
show's RSS feed via the iTunes Lookup API, and writes a committed JSON file
the app serves from. Live pages never call Apple at request time.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import unicodedata
from pathlib import Path
from urllib.parse import urlparse

import requests

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / 'data' / 'show_pages.json'

STOREFRONTS = ('us', 'gb', 'de', 'nl', 'no', 'es', 'br', 'it')
CHART_LIMIT = 100
TARGET_SHOWS = 150
USER_AGENT = 'Mozilla/5.0 (compatible; PodskriftSeed/1.0; +https://podskrift.com)'
SESSION = requests.Session()
SESSION.headers.update({'User-Agent': USER_AGENT, 'Accept': 'application/json'})


def slugify(name: str) -> str:
    """ASCII kebab slug from a show name."""
    text = unicodedata.normalize('NFKD', name or '')
    text = text.encode('ascii', 'ignore').decode('ascii')
    text = text.lower()
    text = re.sub(r'[^a-z0-9]+', '-', text).strip('-')
    return text or 'podcast'


def _label(node, default=''):
    if node is None:
        return default
    if isinstance(node, dict):
        return (node.get('label') or default).strip()
    return str(node).strip() or default


def fetch_chart(country: str, limit: int = CHART_LIMIT) -> list[dict]:
    url = f'https://itunes.apple.com/{country}/rss/toppodcasts/limit={limit}/json'
    resp = SESSION.get(url, timeout=30)
    resp.raise_for_status()
    feed = resp.json().get('feed') or {}
    entries = feed.get('entry') or []
    if isinstance(entries, dict):
        entries = [entries]
    out = []
    for entry in entries:
        itunes_id = (entry.get('id') or {}).get('attributes', {}).get('im:id')
        if not itunes_id:
            continue
        images = entry.get('im:image') or []
        artwork = ''
        if images:
            # Prefer the largest listed thumbnail.
            artwork = _label(images[-1])
        out.append({
            'itunes_id': str(itunes_id),
            'name': _label(entry.get('im:name')),
            'author': _label(entry.get('im:artist')),
            'summary': _label(entry.get('summary')),
            'artwork': artwork,
            'storefront': country,
        })
    return out


def lookup_podcast(itunes_id: str) -> dict | None:
    resp = SESSION.get(
        'https://itunes.apple.com/lookup',
        params={'id': itunes_id},
        timeout=20,
    )
    resp.raise_for_status()
    results = resp.json().get('results') or []
    for item in results:
        if item.get('feedUrl'):
            return item
    return results[0] if results else None


def _is_http_url(raw: str) -> bool:
    try:
        parsed = urlparse(raw)
    except ValueError:
        return False
    return parsed.scheme in ('http', 'https') and bool(parsed.hostname)


def collect_candidates(storefronts, per_chart: int) -> list[dict]:
    """Union chart entries across storefronts, first-seen wins for metadata."""
    by_id: dict[str, dict] = {}
    order: list[str] = []
    for country in storefronts:
        print(f'  chart {country}…', file=sys.stderr)
        try:
            entries = fetch_chart(country, per_chart)
        except Exception as exc:  # noqa: BLE001 - keep other storefronts going
            print(f'  ! {country}: {exc}', file=sys.stderr)
            continue
        for entry in entries:
            iid = entry['itunes_id']
            if iid in by_id:
                continue
            by_id[iid] = entry
            order.append(iid)
        time.sleep(0.35)
    return [by_id[i] for i in order]


def build_shows(candidates: list[dict], target: int) -> list[dict]:
    shows = []
    used_slugs: set[str] = set()
    for cand in candidates:
        if len(shows) >= target:
            break
        itunes_id = cand['itunes_id']
        try:
            item = lookup_podcast(itunes_id)
        except Exception as exc:  # noqa: BLE001
            print(f'  ! lookup {itunes_id}: {exc}', file=sys.stderr)
            time.sleep(0.5)
            continue
        time.sleep(0.2)
        if not item:
            continue
        feed_url = (item.get('feedUrl') or '').strip()
        if not feed_url or not _is_http_url(feed_url):
            continue
        name = (item.get('collectionName') or cand.get('name') or '').strip()
        if not name:
            continue
        author = (item.get('artistName') or cand.get('author') or '').strip()
        description = (item.get('description') or cand.get('summary') or '').strip()
        # Keep descriptions short for the landing page; strip HTML crud lightly.
        description = re.sub(r'<[^>]+>', ' ', description)
        description = re.sub(r'\s+', ' ', description).strip()
        if len(description) > 480:
            description = description[:477].rstrip() + '…'
        artwork = (
            item.get('artworkUrl600')
            or item.get('artworkUrl100')
            or cand.get('artwork')
            or ''
        )
        language = (item.get('country') or cand.get('storefront') or '').lower()
        # collection-level language is often missing; storefront is a weak hint.
        lang = (item.get('language') or '').strip().lower()[:8] or language[:2]

        base = slugify(name)
        slug = base
        if slug in used_slugs:
            slug = f'{base}-{itunes_id}'
        # Extremely defensive: still collide somehow.
        n = 2
        while slug in used_slugs:
            slug = f'{base}-{itunes_id}-{n}'
            n += 1
        used_slugs.add(slug)

        shows.append({
            'slug': slug,
            'name': name,
            'author': author,
            'itunes_id': itunes_id,
            'feed_url': feed_url,
            'artwork': artwork,
            'language': lang,
            'description': description,
            'source': 'apple_top',
        })
        print(f'  + {slug} ({len(shows)}/{target})', file=sys.stderr)
    return shows


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '-o', '--output', type=Path, default=DEFAULT_OUT,
        help=f'output path (default: {DEFAULT_OUT})',
    )
    parser.add_argument('--target', type=int, default=TARGET_SHOWS)
    parser.add_argument(
        '--storefronts', nargs='+', default=list(STOREFRONTS),
    )
    parser.add_argument('--per-chart', type=int, default=CHART_LIMIT)
    args = parser.parse_args(argv)

    print('Collecting chart candidates…', file=sys.stderr)
    candidates = collect_candidates(args.storefronts, args.per_chart)
    print(f'{len(candidates)} unique itunes ids; looking up feeds…', file=sys.stderr)
    shows = build_shows(candidates, args.target)
    if len(shows) < 50:
        print(f'error: only got {len(shows)} shows; aborting', file=sys.stderr)
        return 1

    payload = {
        'version': 1,
        'generated_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'storefronts': list(args.storefronts),
        'shows': shows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8',
    )
    print(f'Wrote {len(shows)} shows → {args.output}', file=sys.stderr)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
