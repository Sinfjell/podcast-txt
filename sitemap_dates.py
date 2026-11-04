"""Stable ``<lastmod>`` values for ``/sitemap.xml``.

Never emit "today" — that made every URL look freshly changed on each fetch
and is a discount signal to crawlers, not a freshness one.

Resolution order per page:

1. A date the page already declares (JSON-LD ``dateModified`` / landing
   ``updated``, newest ``changelog.json`` entry, ``show_pages.json``
   ``generated_at``).
2. Git commit date (``YYYY-MM-DD``) of the primary content/template file,
   looked up once per process so it is fixed for the life of a deploy.
3. A fixed constant for the page group.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
from datetime import date
from pathlib import Path

import landing_pages as landing_pages_mod

_LOG = logging.getLogger(__name__)
_ROOT = Path(__file__).resolve().parent

# W3C date (sitemap subset): YYYY-MM-DD
_DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')

# Group fallbacks when git is unavailable (e.g. bare checkout without history).
FALLBACK_CORE = '2026-09-01'
FALLBACK_LANDING = '2026-10-01'
FALLBACK_GUIDE = '2026-10-01'
FALLBACK_SHOW = '2026-10-09'
FALLBACK_MACHINE = '2026-09-01'  # llms.txt / llms-full.txt

#: endpoint → repo-relative content/template path for git lastmod.
ENDPOINT_SOURCES: dict[str, str] = {
    'index': 'templates/index.html',
    'podcasts_index': 'templates/podcasts_index.html',
    'pricing': 'templates/pricing.html',
    'ai_landing': 'templates/ai.html',
    'guides_index': 'templates/guides/index.html',
    'guide_chatgpt': 'templates/guides/chatgpt.html',
    'guide_claude': 'templates/guides/claude.html',
    'guide_ai_transcripts': 'templates/guide_ai_transcripts.html',
    'guide_spotify_transcript': 'templates/guides/how_to_get_spotify_podcast_transcript.html',
    'contact': 'templates/contact.html',
    'whats_new': 'changelog.json',
    'api_docs': 'docs/customer-api.md',
    'help_page': 'templates/help.html',
    'rss_help': 'templates/rss_help.html',
    'privacy': 'templates/privacy.html',
    'terms': 'templates/terms.html',
    'register': 'templates/register.html',
    'llms_txt': 'app.py',
    'llms_full_txt': 'app.py',
}

#: Guides (and similar) that publish an explicit JSON-LD dateModified.
#: Kept here so sitemap lastmod matches the page without importing app.
DECLARED_GUIDE_UPDATED: dict[str, str] = {
    'guide_spotify_transcript': '2026-11-04',
}

_LANDING_SOURCE = 'landing_pages.py'
_SHOW_PAGES_PATH = _ROOT / 'data' / 'show_pages.json'
_CHANGELOG_PATH = _ROOT / 'changelog.json'

_git_lock = threading.Lock()
_git_dates: dict[str, str | None] = {}
_show_generated_at: str | None = None
_show_generated_lock = threading.Lock()
_changelog_newest: str | None = None
_changelog_lock = threading.Lock()


def is_w3c_date(value: str) -> bool:
    """True when *value* is a real calendar YYYY-MM-DD."""
    if not value or not _DATE_RE.match(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _git_commit_date(relpath: str) -> str | None:
    """Last commit date for *relpath*, cached for the process lifetime."""
    with _git_lock:
        if relpath in _git_dates:
            return _git_dates[relpath]
    result: str | None = None
    try:
        proc = subprocess.run(
            ['git', 'log', '-1', '--format=%cs', '--', relpath],
            cwd=_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        raw = (proc.stdout or '').strip()
        if proc.returncode == 0 and is_w3c_date(raw):
            result = raw
    except Exception:  # noqa: BLE001 - lastmod must never break the sitemap
        _LOG.debug('git lastmod lookup failed for %s', relpath, exc_info=True)
    with _git_lock:
        _git_dates[relpath] = result
        return _git_dates[relpath]


def _newest_changelog_date() -> str | None:
    """Newest visible changelog entry date (same source as /whats-new)."""
    global _changelog_newest
    with _changelog_lock:
        if _changelog_newest is not None:
            return _changelog_newest or None
        newest = ''
        try:
            data = json.loads(_CHANGELOG_PATH.read_text(encoding='utf-8'))
            entries = data.get('entries') if isinstance(data, dict) else None
            if isinstance(entries, list):
                for raw in entries:
                    if not isinstance(raw, dict) or raw.get('hidden') is True:
                        continue
                    d = (raw.get('date') or '').strip()
                    if is_w3c_date(d) and d > newest:
                        newest = d
        except (OSError, json.JSONDecodeError):
            _LOG.debug('changelog lastmod read failed', exc_info=True)
        _changelog_newest = newest
        return newest or None


def show_pages_generated_date() -> str | None:
    """Date portion of ``data/show_pages.json`` ``generated_at``."""
    global _show_generated_at
    with _show_generated_lock:
        if _show_generated_at is not None:
            return _show_generated_at or None
        value = ''
        try:
            data = json.loads(_SHOW_PAGES_PATH.read_text(encoding='utf-8'))
            raw = (data.get('generated_at') or '') if isinstance(data, dict) else ''
            if isinstance(raw, str) and len(raw) >= 10:
                candidate = raw[:10]
                if is_w3c_date(candidate):
                    value = candidate
        except (OSError, json.JSONDecodeError):
            _LOG.debug('show_pages generated_at read failed', exc_info=True)
        _show_generated_at = value
        return value or None


def declared_landing_updated(endpoint: str) -> str | None:
    """ISO date a landing already publishes as ``dateModified``, if any."""
    return landing_pages_mod.declared_updated(endpoint)


def lastmod_for_endpoint(endpoint: str) -> str:
    """Stable W3C date for a named Flask endpoint in the sitemap."""
    if endpoint == 'whats_new':
        declared = _newest_changelog_date()
        if declared:
            return declared

    declared_guide = DECLARED_GUIDE_UPDATED.get(endpoint)
    if declared_guide and is_w3c_date(declared_guide):
        return declared_guide

    if endpoint.startswith('landing_'):
        declared = declared_landing_updated(endpoint)
        if declared and is_w3c_date(declared):
            return declared
        git = _git_commit_date(_LANDING_SOURCE)
        return git or FALLBACK_LANDING

    if endpoint in ('guide_chatgpt', 'guide_claude', 'guide_ai_transcripts',
                    'guide_spotify_transcript', 'guides_index'):
        source = ENDPOINT_SOURCES.get(endpoint)
        if source:
            git = _git_commit_date(source)
            if git:
                return git
        return FALLBACK_GUIDE

    if endpoint in ('llms_txt', 'llms_full_txt'):
        git = _git_commit_date(ENDPOINT_SOURCES[endpoint])
        return git or FALLBACK_MACHINE

    source = ENDPOINT_SOURCES.get(endpoint)
    if source:
        git = _git_commit_date(source)
        if git:
            return git
    return FALLBACK_CORE


def lastmod_for_show_page() -> str:
    """One lastmod for every curated ``/podcasts/<slug>`` URL."""
    declared = show_pages_generated_date()
    if declared:
        return declared
    git = _git_commit_date('data/show_pages.json')
    return git or FALLBACK_SHOW


def clear_caches_for_tests() -> None:
    """Drop process caches so unit tests can stub git / files."""
    global _show_generated_at, _changelog_newest
    with _git_lock:
        _git_dates.clear()
    with _show_generated_lock:
        _show_generated_at = None
    with _changelog_lock:
        _changelog_newest = None
