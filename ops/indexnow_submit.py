#!/usr/bin/env python3
"""Submit sitemap URLs to IndexNow (Bing / ChatGPT search / Yandex / …).

Reads the URL list from the live sitemap (default
https://podskrift.com/sitemap.xml), then POSTs them to
https://api.indexnow.org/indexnow with host, key and keyLocation.

The ownership key comes from ``INDEXNOW_KEY`` (same value served at
``https://podskrift.com/<key>.txt``). Nothing is invented at runtime.

Usage:
  INDEXNOW_KEY=… python3 ops/indexnow_submit.py --dry-run
  INDEXNOW_KEY=… python3 ops/indexnow_submit.py
  INDEXNOW_KEY=… python3 ops/indexnow_submit.py \\
      --sitemap http://127.0.0.1:5002/sitemap.xml

``--dry-run`` prints the JSON payload and exits without sending.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

# Allow ``python3 ops/indexnow_submit.py`` without installing a package.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

INDEXNOW_ENDPOINT = 'https://api.indexnow.org/indexnow'
DEFAULT_HOST = 'podskrift.com'
DEFAULT_SITEMAP = f'https://{DEFAULT_HOST}/sitemap.xml'
SITEMAP_NS = {'sm': 'http://www.sitemaps.org/schemas/sitemap/0.9'}


def parse_sitemap_locs(xml_bytes: bytes) -> list[str]:
    """Return ``<loc>`` values in document order from a sitemap document."""
    root = ET.fromstring(xml_bytes)
    locs = []
    for loc in root.findall('sm:url/sm:loc', SITEMAP_NS):
        text = (loc.text or '').strip()
        if text:
            locs.append(text)
    if locs:
        return locs
    # Tolerant fallback if a feed omits the default xmlns.
    for loc in root.iter('{http://www.sitemaps.org/schemas/sitemap/0.9}loc'):
        text = (loc.text or '').strip()
        if text:
            locs.append(text)
    if locs:
        return locs
    for loc in root.iter('loc'):
        text = (loc.text or '').strip()
        if text:
            locs.append(text)
    return locs


def fetch_sitemap(url: str, timeout: float = 30.0) -> bytes:
    """GET *url* and return the response body."""
    req = urllib.request.Request(
        url,
        headers={'User-Agent': 'Podskrift-IndexNow/1.0 (+https://podskrift.com)'},
        method='GET',
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def build_payload(*, host: str, key: str, key_location: str,
                  url_list: list[str]) -> dict:
    """IndexNow bulk JSON body."""
    return {
        'host': host,
        'key': key,
        'keyLocation': key_location,
        'urlList': list(url_list),
    }


def submit_payload(payload: dict, *, endpoint: str = INDEXNOW_ENDPOINT,
                   timeout: float = 60.0) -> tuple[int, str]:
    """POST *payload* to IndexNow. Returns ``(status_code, response_body)``."""
    body = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(
        endpoint,
        data=body,
        headers={
            'Content-Type': 'application/json; charset=utf-8',
            'User-Agent': 'Podskrift-IndexNow/1.0 (+https://podskrift.com)',
        },
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.getcode(), resp.read().decode('utf-8', errors='replace')
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode('utf-8', errors='replace') if exc.fp else ''
        return exc.code, detail


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--sitemap', default=DEFAULT_SITEMAP,
        help=f'Sitemap URL to read (default {DEFAULT_SITEMAP})')
    parser.add_argument(
        '--host', default=DEFAULT_HOST,
        help=f'IndexNow host field (default {DEFAULT_HOST})')
    parser.add_argument(
        '--key', default=None,
        help='IndexNow key (default: INDEXNOW_KEY env)')
    parser.add_argument(
        '--key-location', default=None,
        help='Public key file URL (default https://<host>/<key>.txt)')
    parser.add_argument(
        '--endpoint', default=INDEXNOW_ENDPOINT,
        help=f'IndexNow POST URL (default {INDEXNOW_ENDPOINT})')
    parser.add_argument(
        '--dry-run', action='store_true',
        help='Print the JSON payload and do not send')
    args = parser.parse_args(argv)

    key = (args.key if args.key is not None else os.environ.get('INDEXNOW_KEY', '')).strip()
    if not key:
        print('INDEXNOW_KEY is unset (and --key not given); nothing to submit.',
              file=sys.stderr)
        return 2

    key_location = (args.key_location
                    or f'https://{args.host}/{key}.txt')

    try:
        xml_bytes = fetch_sitemap(args.sitemap)
    except Exception as exc:  # noqa: BLE001
        print(f'Failed to fetch sitemap {args.sitemap}: {exc}', file=sys.stderr)
        return 1

    urls = parse_sitemap_locs(xml_bytes)
    if not urls:
        print('Sitemap contained no <loc> entries.', file=sys.stderr)
        return 1

    payload = build_payload(
        host=args.host, key=key, key_location=key_location, url_list=urls)

    if args.dry_run:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    status, detail = submit_payload(payload, endpoint=args.endpoint)
    print(f'IndexNow HTTP {status} ({len(urls)} URLs)')
    if detail:
        print(detail)
    # 200 / 202 are success; IndexNow may return 202 on first verify.
    if status in (200, 202):
        return 0
    return 1


if __name__ == '__main__':
    sys.exit(main())
