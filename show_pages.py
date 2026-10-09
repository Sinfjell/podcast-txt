"""Curated + community podcast show landing pages.

Data: committed JSON from ops/seed-show-pages.py (popular charts). Community
shows are distinct podcast names from completed transcripts that have a feed —
names and feed URLs only, no user data. Episode lists are cached (memory +
on-disk last-good) so a slow feed never 500s the page.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import unicodedata
from pathlib import Path

logger = logging.getLogger(__name__)

SHOW_PAGES_PATH = Path(__file__).resolve().parent / 'data' / 'show_pages.json'
SHOW_FEED_CACHE_DIR = Path(
    os.getenv('SHOW_FEED_CACHE_DIR')
    or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'show_feed_cache')
)
SHOW_FEED_CACHE_TTL = int(os.getenv('SHOW_FEED_CACHE_TTL', str(6 * 3600)))
SHOW_FEED_FETCH_TIMEOUT = float(os.getenv('SHOW_FEED_FETCH_TIMEOUT', '6'))
SHOW_EPISODE_LIMIT = 10
#: Failed/empty fetches are retried after this long, not after the full TTL.
SHOW_FEED_ERROR_TTL = int(os.getenv('SHOW_FEED_ERROR_TTL', '900'))
#: At most this many live feed fetches at once across all request threads, so
#: a crawler sweeping 150 cold pages cannot tie up the gunicorn thread pool.
SHOW_FEED_MAX_CONCURRENT = int(os.getenv('SHOW_FEED_MAX_CONCURRENT', '2'))
#: Community shows (names + feed URLs taken from users' completed jobs) are
#: OFF unless explicitly enabled: a private/premium feed URL carries an auth
#: token, and with few users the list reveals what individuals transcribed.
SHOW_PAGES_COMMUNITY = os.getenv('SHOW_PAGES_COMMUNITY', '').strip().lower() in (
    '1', 'true', 'yes')
SHOW_PAGES_PER_LETTER_PAGE = 40

_curated_lock = threading.Lock()
_curated_mtime = None
_curated_by_slug: dict[str, dict] = {}
_curated_list: list[dict] = []

_mem_cache_lock = threading.Lock()
# slug -> {'fetched_at': float, 'episodes': list, 'error': str|None}
_mem_feed_cache: dict[str, dict] = {}

_fetch_slots = threading.BoundedSemaphore(max(1, SHOW_FEED_MAX_CONCURRENT))
_inflight_lock = threading.Lock()
_inflight: set[str] = set()


def slugify_show_name(name: str) -> str:
    text = unicodedata.normalize('NFKD', name or '')
    text = text.encode('ascii', 'ignore').decode('ascii')
    text = text.lower()
    text = re.sub(r'[^a-z0-9]+', '-', text).strip('-')
    return text or 'podcast'


def _strip_html(raw: str) -> str:
    if not raw:
        return ''
    text = re.sub(r'<[^>]+>', ' ', raw)
    return re.sub(r'\s+', ' ', text).strip()


def load_curated_shows(path: Path | None = None) -> list[dict]:
    """Load curated shows from the committed JSON. Empty list if missing/bad."""
    global _curated_mtime, _curated_by_slug, _curated_list
    path = path or SHOW_PAGES_PATH
    try:
        mtime = path.stat().st_mtime
    except OSError:
        with _curated_lock:
            _curated_mtime = None
            _curated_by_slug = {}
            _curated_list = []
        return []

    with _curated_lock:
        if _curated_mtime == mtime and _curated_list:
            return list(_curated_list)
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning('Could not load show pages data: %s', exc)
            _curated_mtime = mtime
            _curated_by_slug = {}
            _curated_list = []
            return []

        shows_raw = data.get('shows') if isinstance(data, dict) else None
        if not isinstance(shows_raw, list):
            _curated_mtime = mtime
            _curated_by_slug = {}
            _curated_list = []
            return []

        cleaned = []
        by_slug = {}
        for raw in shows_raw:
            if not isinstance(raw, dict):
                continue
            slug = (raw.get('slug') or '').strip()
            name = (raw.get('name') or '').strip()
            feed_url = (raw.get('feed_url') or '').strip()
            if not slug or not name or not feed_url:
                continue
            show = {
                'slug': slug,
                'name': name,
                'author': (raw.get('author') or '').strip(),
                'itunes_id': str(raw.get('itunes_id') or '').strip(),
                'feed_url': feed_url,
                'artwork': (raw.get('artwork') or '').strip(),
                'language': (raw.get('language') or '').strip(),
                'description': _strip_html(raw.get('description') or ''),
                'source': 'curated',
            }
            if slug in by_slug:
                continue
            by_slug[slug] = show
            cleaned.append(show)
        cleaned.sort(key=lambda s: s['name'].casefold())
        _curated_mtime = mtime
        _curated_by_slug = by_slug
        _curated_list = cleaned
        return list(cleaned)


def curated_show(slug: str) -> dict | None:
    load_curated_shows()
    with _curated_lock:
        show = _curated_by_slug.get(slug)
        return dict(show) if show else None


def community_shows_from_db(db_session, TranscriptionTask, *, reserved_slugs=None,
                            reserved_names=None):
    """Distinct show names with a feed and ≥1 completed transcript.

    Returns list of dicts with slug, name, feed_url, source='community'.
    No user ids, emails, or transcript text.
    """
    reserved = set(reserved_slugs or ())
    reserved_name_keys = {n.casefold() for n in (reserved_names or ())}
    try:
        rows = (
            db_session.query(
                TranscriptionTask.podcast_name,
                TranscriptionTask.rss_url,
            )
            .filter(
                TranscriptionTask.status == 'completed',
                TranscriptionTask.podcast_name.isnot(None),
                TranscriptionTask.rss_url.isnot(None),
                TranscriptionTask.rss_url != '',
            )
            .distinct()
            .all()
        )
    except Exception as exc:  # noqa: BLE001 - page must still render curated
        logger.warning('community show query failed: %s', exc)
        return []

    # Prefer the first non-empty feed per normalized name; stable slug.
    by_name: dict[str, dict] = {}
    for name, rss_url in rows:
        name = (name or '').strip()
        rss_url = (rss_url or '').strip()
        if not name or not rss_url:
            continue
        key = name.casefold()
        if key in by_name or key in reserved_name_keys:
            continue
        by_name[key] = {'name': name, 'feed_url': rss_url}

    out = []
    used = set(reserved)
    for key in sorted(by_name.keys()):
        item = by_name[key]
        base = slugify_show_name(item['name'])
        slug = base
        n = 2
        while slug in used:
            slug = f'{base}-{n}'
            n += 1
        used.add(slug)
        out.append({
            'slug': slug,
            'name': item['name'],
            'author': '',
            'itunes_id': '',
            'feed_url': item['feed_url'],
            'artwork': '',
            'language': '',
            'description': '',
            'source': 'community',
        })
    return out


def all_shows(db_session=None, TranscriptionTask=None) -> list[dict]:
    """Curated shows plus community shows not already in the curated set."""
    curated = load_curated_shows()
    reserved = {s['slug'] for s in curated}
    curated_names = {s['name'] for s in curated}
    community = []
    if (SHOW_PAGES_COMMUNITY and db_session is not None
            and TranscriptionTask is not None):
        community = community_shows_from_db(
            db_session, TranscriptionTask,
            reserved_slugs=reserved,
            reserved_names=curated_names,
        )
    return curated + community


def find_show(slug: str, db_session=None, TranscriptionTask=None) -> dict | None:
    show = curated_show(slug)
    if show:
        return show
    if (not SHOW_PAGES_COMMUNITY or db_session is None
            or TranscriptionTask is None):
        return None
    curated = load_curated_shows()
    for candidate in community_shows_from_db(
            db_session, TranscriptionTask,
            reserved_slugs={s['slug'] for s in curated},
            reserved_names={s['name'] for s in curated}):
        if candidate['slug'] == slug:
            return candidate
    return None


def _cache_path(slug: str) -> Path:
    safe = re.sub(r'[^a-z0-9_-]+', '', slug) or 'show'
    return SHOW_FEED_CACHE_DIR / f'{safe}.json'


def _read_disk_cache(slug: str) -> dict | None:
    path = _cache_path(slug)
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _write_disk_cache(slug: str, payload: dict) -> None:
    try:
        SHOW_FEED_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path = _cache_path(slug)
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
        tmp.replace(path)
    except OSError as exc:
        logger.warning('show feed cache write failed for %s: %s', slug, exc)


def _public_episode(ep: dict) -> dict:
    """Episode fields safe for the public show page — never transcript text."""
    return {
        'index': ep.get('index'),
        'title': ep.get('title') or 'Episode',
        'published': ep.get('published') or '',
        'duration_min': ep.get('duration_min'),
        'description': _strip_html(ep.get('description') or '')[:240],
        'artwork': ep.get('artwork') or '',
        # audio_url is needed to start transcription via the existing flow;
        # it is not transcript text. Still only render it into form posts.
        'audio_url': ep.get('audio_url') or '',
    }


def _entry_ttl(entry: dict) -> int:
    return SHOW_FEED_CACHE_TTL if entry.get('episodes') else min(
        SHOW_FEED_ERROR_TTL, SHOW_FEED_CACHE_TTL)


def _cached_entry(slug: str) -> dict | None:
    """Newest cache entry for slug (memory first, then disk), or None."""
    with _mem_cache_lock:
        mem = _mem_feed_cache.get(slug)
    if mem is not None:
        return mem
    disk = _read_disk_cache(slug)
    if disk and isinstance(disk.get('episodes'), list):
        entry = {
            'fetched_at': float(disk.get('fetched_at') or 0),
            'episodes': disk['episodes'],
            'error': disk.get('error'),
        }
        with _mem_cache_lock:
            _mem_feed_cache.setdefault(slug, entry)
        return entry
    return None


def _do_fetch(show: dict, get_episodes_from_rss, is_fetchable_url) -> dict:
    """One live fetch; stores the result. Caller holds a fetch slot."""
    slug = show['slug']
    feed_url = show['feed_url']
    now = time.time()
    episodes, error = [], None
    if not is_fetchable_url(feed_url):
        error = 'feed_blocked'
    else:
        try:
            raw, err = get_episodes_from_rss(
                feed_url, timeout=SHOW_FEED_FETCH_TIMEOUT)
            if err or not raw:
                error = err or 'empty_feed'
            else:
                episodes = [_public_episode(ep) for ep in raw[:SHOW_EPISODE_LIMIT]]
        except Exception as exc:  # noqa: BLE001
            error = f'fetch_failed: {exc}'
            logger.info('show page feed fetch failed for %s: %s', slug, exc)

    if episodes:
        payload = {'fetched_at': now, 'episodes': episodes, 'error': None,
                   'feed_url': feed_url}
        with _mem_cache_lock:
            _mem_feed_cache[slug] = payload
        _write_disk_cache(slug, payload)
        return payload

    # Failure: keep last-good episodes (re-stamped so we back off for
    # SHOW_FEED_ERROR_TTL instead of refetching on every hit).
    prev = _cached_entry(slug)
    keep = list(prev['episodes']) if prev and prev.get('episodes') else []
    payload = {'fetched_at': now, 'episodes': keep, 'error': error,
               'feed_url': feed_url, 'stale': bool(keep)}
    with _mem_cache_lock:
        _mem_feed_cache[slug] = payload
    return payload


def _claim(slug: str) -> bool:
    with _inflight_lock:
        if slug in _inflight:
            return False
        _inflight.add(slug)
        return True


def _release(slug: str) -> None:
    with _inflight_lock:
        _inflight.discard(slug)


def _refresh_in_background(show, get_episodes_from_rss, is_fetchable_url):
    """Single-flight background refresh; bounded by the global fetch slots."""
    slug = show['slug']
    if not _claim(slug):
        return

    def _run():
        try:
            if not _fetch_slots.acquire(timeout=60):
                return
            try:
                _do_fetch(show, get_episodes_from_rss, is_fetchable_url)
            finally:
                _fetch_slots.release()
        except Exception:  # noqa: BLE001
            logger.exception('background show feed refresh failed for %s', slug)
        finally:
            _release(slug)

    threading.Thread(target=_run, daemon=True,
                     name=f'show-feed-{slug[:24]}').start()


def fetch_show_episodes(show: dict, *, get_episodes_from_rss, is_fetchable_url,
                        force_refresh=False):
    """Return (episodes, meta) where meta has cache/freshness flags.

    Never raises and never makes a request wait on a feed it does not have to:
    - fresh cache: served directly;
    - stale cache: served immediately, refreshed once in the background;
    - cold: fetched inline only if this request wins the per-slug claim and a
      global fetch slot is free; otherwise the page renders without episodes
      and a background refresh warms the cache.
    """
    slug = show['slug']
    now = time.time()
    entry = None if force_refresh else _cached_entry(slug)

    if entry is not None:
        age = now - float(entry.get('fetched_at') or 0)
        fresh = age < _entry_ttl(entry)
        if not fresh:
            _refresh_in_background(show, get_episodes_from_rss, is_fetchable_url)
        return list(entry.get('episodes') or []), {
            'from_cache': True,
            # Only flag 'stale' when the last live fetch FAILED and we are
            # showing last-good; a normal TTL refresh is invisible to users.
            'stale': bool(entry.get('stale')),
            'error': entry.get('error'),
        }

    if _claim(slug):
        got_slot = _fetch_slots.acquire(blocking=False)
        if got_slot:
            try:
                payload = _do_fetch(show, get_episodes_from_rss, is_fetchable_url)
            finally:
                _fetch_slots.release()
                _release(slug)
            return list(payload['episodes']), {
                'from_cache': False,
                'stale': bool(payload.get('stale')),
                'error': payload.get('error'),
            }
        _release(slug)

    _refresh_in_background(show, get_episodes_from_rss, is_fetchable_url)
    return [], {'from_cache': False, 'stale': False, 'error': 'warming'}


def group_shows_by_letter(shows: list[dict]) -> list[tuple[str, list[dict]]]:
    """A–Z then '#' for non-letter starts."""
    buckets: dict[str, list[dict]] = {}
    for show in shows:
        ch = (show['name'][:1] or '#').upper()
        if not ('A' <= ch <= 'Z'):
            ch = '#'
        buckets.setdefault(ch, []).append(show)
    letters = [c for c in sorted(buckets) if c != '#']
    if '#' in buckets:
        letters.append('#')
    return [(letter, buckets[letter]) for letter in letters]


def show_faq_entries(show_name: str, *, trial_minutes: int, credit_pack_price: str,
                     credit_pack_minutes: int, language_count: int) -> list[tuple[str, str]]:
    """Show-page FAQ — answers visible on the page (and mirrored in JSON-LD)."""
    free = (
        f'New Podskrift accounts get {trial_minutes} free minutes of transcription '
        f'on our OpenAI key. After that, buy a one-time pack '
        f'(${credit_pack_price} for {credit_pack_minutes} minutes) or add your own '
        f'OpenAI key. There is no subscription.'
        if trial_minutes > 0 else
        'Podskrift itself is free. Add your own OpenAI API key and pay OpenAI '
        'directly, or buy a one-time minute pack. There is no subscription.'
    )
    return [
        (
            f'Can I get a transcript of {show_name} episodes?',
            f'Yes. Open any recent episode below and press Transcribe. Podskrift '
            f'downloads the audio and returns plain text plus an .srt subtitle file. '
            f'Transcripts are private to your account — this page does not publish them.',
        ),
        (
            'Is it free?',
            free,
        ),
        (
            'Can I paste a Spotify or Apple Podcasts link instead?',
            'Yes. On the home page, paste a Spotify episode link, an Apple Podcasts '
            'link, or an RSS feed URL. Search by show or episode name also works.',
        ),
        (
            'What do I download?',
            'Plain text (.txt) and SubRip subtitles (.srt) with timestamps.',
        ),
        (
            'Which languages are supported?',
            f'Podskrift uses OpenAI Whisper and supports {language_count} languages '
            f'in the language picker, including English, Norwegian, German, Spanish, '
            f'and many more. You can name the language or leave auto-detect on.',
        ),
    ]


def referrer_source(referrer: str, utm_source: str = '') -> str:
    """Coarse acquisition bucket for show_page_viewed — never a full URL with PII."""
    utm = (utm_source or '').strip().lower()
    if utm:
        return utm[:64]
    if not referrer:
        return 'direct'
    host = ''
    try:
        from urllib.parse import urlparse
        host = (urlparse(referrer).hostname or '').lower()
    except ValueError:
        return 'other'
    if not host:
        return 'other'
    if 'chat.openai.com' in host or 'chatgpt.com' in host:
        return 'chatgpt'
    if 'perplexity' in host:
        return 'perplexity'
    if 'google.' in host or host == 'google.com':
        return 'google'
    if 'bing.' in host or host.endswith('bing.com'):
        return 'bing'
    if 'podskrift.com' in host:
        return 'internal'
    return 'other'
