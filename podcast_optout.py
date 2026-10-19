"""Podcasts whose creators asked not to be transcribed on Podskrift.

One config list, checked at every entry point: feed parsing, the single
enqueue path (web, MCP, API), directory search and link resolvers, transcript
retrieval, show pages and the sitemap. Matching is deliberately specific to
each show -- feed URL variants, the show's own hosts, Apple/Spotify ids, the
exact show title and the exact enclosure files -- because audio hosts such as
mcdn.podbean.com serve many unrelated podcasts.

To add a show: append an entry to OPTOUTS, add its enclosure keys to
podcast_optout_audio.json (see ``audio_key``), and deploy.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import unicodedata
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

OPTOUT_MESSAGE = (
    'The creator of this podcast has asked not to be transcribed on Podskrift.'
)
OPTOUT_REASON = 'creator_opt_out'

AUDIO_LIST_PATH = Path(__file__).resolve().parent / 'podcast_optout_audio.json'
REFRESH_INTERVAL_SEC = 6 * 3600

OPTOUTS = (
    {
        'key': 'divineenergy',
        'names': (
            'DNA Remembrance with Rheanna Aviy',
            # Older title still shown by Spotify for the same show.
            'Navigating Spirituality and Life with Divine Energy',
        ),
        'feed_urls': (
            'https://feed.podbean.com/divineenergy/feed.xml',
        ),
        # Any URL on these hosts belongs to the show (episode pages, GUIDs,
        # the podbean site feed, the creator's own site).
        'hosts': ('divineenergy.podbean.com', 'dnaremembrance.me'),
        # host + path prefix: feed.podbean.com/divineenergy/<anything>
        'path_prefixes': ('feed.podbean.com/divineenergy',),
        'apple_ids': ('1669936175',),
        'spotify_show_ids': ('4SJFFfLMOPPsIe1FP0xarz',),
        'audio_hosts': ('mcdn.podbean.com',),
    },
)


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

def normalize_name(value) -> str:
    text = unicodedata.normalize('NFKC', str(value or '')).casefold()
    text = re.sub(r'[^\w]+', ' ', text)
    return ' '.join(text.split())


def _host_path(url) -> tuple[str, str]:
    text = str(url or '').strip()
    if not text:
        return '', ''
    if '://' not in text:
        text = 'https://' + text.lstrip('/')
    try:
        parsed = urlparse(text)
    except ValueError:
        return '', ''
    host = (parsed.hostname or '').lower().rstrip('.')
    if host.startswith('www.'):
        host = host[4:]
    path = re.sub(r'/+', '/', parsed.path or '').rstrip('/').lower()
    return host, path


def normalize_url(url) -> str:
    """Scheme-, www-, case-, query- and trailing-slash-insensitive key."""
    host, path = _host_path(url)
    return f'{host}{path}' if host else ''


def audio_key(url) -> str:
    """Last two path segments of an enclosure: ``<id>/<file>`` (lowercase)."""
    _, path = _host_path(url)
    parts = [p for p in path.split('/') if p]
    return '/'.join(parts[-2:]) if len(parts) >= 2 else ''


def _host_matches(host, wanted) -> bool:
    return host == wanted or host.endswith('.' + wanted)


# ---------------------------------------------------------------------------
# Audio list (static, committed) plus a background refresh of new episodes
# ---------------------------------------------------------------------------

_audio_lock = threading.Lock()
_audio_keys: dict[str, set] = {}
_audio_loaded = False


def _load_audio_list() -> None:
    global _audio_loaded
    with _audio_lock:
        if _audio_loaded:
            return
        try:
            data = json.loads(AUDIO_LIST_PATH.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning('opt-out audio list unreadable: %s', exc)
            data = {}
        for entry in OPTOUTS:
            keys = data.get(entry['key']) if isinstance(data, dict) else None
            _audio_keys.setdefault(entry['key'], set()).update(
                str(k).lower() for k in (keys or ()) if k)
        _audio_loaded = True


def remember_audio(entry_key, urls) -> int:
    """Add enclosure URLs of a blocked show to the in-memory list."""
    _load_audio_list()
    added = 0
    with _audio_lock:
        bucket = _audio_keys.setdefault(entry_key, set())
        for url in urls or ():
            key = audio_key(url)
            if key and key not in bucket:
                bucket.add(key)
                added += 1
    return added


# ---------------------------------------------------------------------------
# Matchers. Each returns the matching OPTOUTS entry, or None.
# ---------------------------------------------------------------------------

def match_feed(url):
    """A URL that is the show's feed or lives on one of its own hosts."""
    host, path = _host_path(url)
    if not host:
        return None
    key = f'{host}{path}'
    for entry in OPTOUTS:
        if key in {normalize_url(u) for u in entry['feed_urls']}:
            return entry
        if any(_host_matches(host, h) for h in entry['hosts']):
            return entry
        for prefix in entry['path_prefixes']:
            if key == prefix or key.startswith(prefix + '/'):
                return entry
    return None


def match_audio(url):
    host, path = _host_path(url)
    key = audio_key(url)
    if not host or not key:
        return None
    _load_audio_list()
    with _audio_lock:
        for entry in OPTOUTS:
            # The CDN itself, or a tracking redirect that embeds it
            # (op3.dev/e/mcdn.podbean.com/..., dts.podtrac.com/redirect.mp3/...).
            if not any(_host_matches(host, h) or f'/{h}/' in path
                       for h in entry['audio_hosts']):
                continue
            if key in _audio_keys.get(entry['key'], ()):
                return entry
    return None


def match_url(url):
    """Any pasted link: feed, show host, Apple/Spotify page or enclosure."""
    text = str(url or '').strip()
    if not text:
        return None
    entry = match_feed(text) or match_audio(text)
    if entry:
        return entry
    for m in re.finditer(r'/id(\d+)', text, re.I):
        entry = match_apple(m.group(1))
        if entry:
            return entry
    m = re.search(r'open\.spotify\.com/(?:[\w-]+/)*show/([A-Za-z0-9]+)', text)
    if m:
        return match_spotify(m.group(1))
    return None


def match_apple(apple_id):
    want = str(apple_id or '').strip()
    if not want:
        return None
    for entry in OPTOUTS:
        if want in entry['apple_ids']:
            return entry
    return None


def match_spotify(show_id):
    want = str(show_id or '').strip()
    if not want:
        return None
    for entry in OPTOUTS:
        if want in entry['spotify_show_ids']:
            return entry
    return None


def match_name(name):
    want = normalize_name(name)
    if not want:
        return None
    for entry in OPTOUTS:
        if want in {normalize_name(n) for n in entry['names']}:
            return entry
    return None


def match_fields(*, urls=(), names=(), apple_ids=(), spotify_ids=()):
    for url in urls:
        entry = match_url(url)
        if entry:
            return entry
    for name in names:
        entry = match_name(name)
        if entry:
            return entry
    for value in apple_ids:
        entry = match_apple(value)
        if entry:
            return entry
    for value in spotify_ids:
        entry = match_spotify(value)
        if entry:
            return entry
    return None


_URL_KEYS = ('audio_url', 'rss_url', 'feed_url', 'feedUrl', 'episodeUrl',
             'url', 'episode_link', 'show_link', 'link', 'apple_url',
             'spotify_url', 'collectionViewUrl', 'trackViewUrl')
_NAME_KEYS = ('podcast_name', 'publisher', 'collectionName', 'show', 'artist',
              'show_name')
_APPLE_KEYS = ('collectionId', 'itunes_id', 'apple_id')


def match_item(item):
    """Search results, catalog payloads, enqueue meta, show dicts."""
    if not isinstance(item, dict):
        return None
    urls = [item.get(k) for k in _URL_KEYS if item.get(k)]
    names = [item.get(k) for k in _NAME_KEYS if item.get(k)]
    if item.get('type') == 'show' or item.get('source') in ('curated', 'community'):
        names.append(item.get('name'))
    apple = [item.get(k) for k in _APPLE_KEYS if item.get(k)]
    return match_fields(urls=urls, names=names, apple_ids=apple)


def match_task(task):
    if task is None:
        return None
    urls = [getattr(task, attr, None) for attr in (
        'rss_url', 'source_audio_url', 'source_apple_url',
        'source_spotify_url', 'source_website_url')]
    return match_fields(urls=[u for u in urls if u],
                        names=[getattr(task, 'podcast_name', None)])


def filter_items(items):
    return [item for item in (items or []) if not match_item(item)]


def is_opted_out_task(task) -> bool:
    return match_task(task) is not None


def match_feed_content(feed_title, episodes, links=()):
    """After parsing a feed: the title, any link or any enclosure matches.

    When it matches, learn the enclosures so pasted episode audio is refused
    even before the next background refresh.
    """
    entry = match_name(feed_title) or match_fields(urls=[u for u in links if u])
    if entry is None:
        for ep in episodes or ():
            entry = match_audio(ep.get('audio_url'))
            if entry:
                break
    if entry is not None:
        remember_audio(entry['key'], [ep.get('audio_url') for ep in episodes or ()])
    return entry


def payload(extra=None):
    out = {'error': OPTOUT_MESSAGE, 'reason': OPTOUT_REASON}
    out.update(extra or {})
    return out


# ---------------------------------------------------------------------------
# Background refresh (server boot only)
# ---------------------------------------------------------------------------

_ENCLOSURE_RE = re.compile(r'<enclosure\b[^>]*\burl=["\']([^"\']+)["\']', re.I)
_refresh_started = False


def refresh_once(fetch=None) -> int:
    """Fetch each blocked feed and add its enclosures. Returns keys added."""
    if fetch is None:
        import requests

        def fetch(url):
            resp = requests.get(url, timeout=20, headers={
                'User-Agent': 'Podskrift opt-out refresh'})
            resp.raise_for_status()
            return resp.text[:20_000_000]
    added = 0
    for entry in OPTOUTS:
        for feed_url in entry['feed_urls']:
            try:
                body = fetch(feed_url)
            except Exception as exc:  # noqa: BLE001 - keep the static list
                logger.info('opt-out refresh failed for %s: %s', entry['key'], exc)
                continue
            added += remember_audio(entry['key'], _ENCLOSURE_RE.findall(body or ''))
    return added


def start_refresher() -> bool:
    global _refresh_started
    if _refresh_started:
        return False
    _refresh_started = True

    def loop():
        while True:
            try:
                refresh_once()
            except Exception:  # noqa: BLE001
                logger.exception('opt-out refresh crashed')
            time.sleep(REFRESH_INTERVAL_SEC)

    threading.Thread(target=loop, name='podcast-optout-refresh', daemon=True).start()
    return True
