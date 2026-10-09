"""Where this process is running — production vs everything else.

PostHog and Sentry must only talk to the live projects from the production
host. Sandboxes, CI, pytest and local dev often inherit a copied `.env` with
real DSNs/keys; without a gate they collide with real users (same numeric
user ids, same project).

Detection order:

1. ``PODSKRIFT_ENV`` — ``production`` | ``staging`` | ``test`` | ``dev`` wins
   when set (explicit override; not required on the production server).
2. Non-production signals — pytest, CI, Cursor/cloud-agent, throwaway
   ``DATABASE_URL`` (``/tmp``, in-memory), Flask ``testing``.
3. Positive production markers from what prod already has (no new .env keys):
   app/DB under ``/var/www/vhosts/podskrift.nettsmed.dev``, or
   ``PUBLIC_BASE_URL`` host ``podskrift.com``.
4. Otherwise ``dev`` (safe default: analytics/error reporting stay off).
"""

from __future__ import annotations

import logging
import os
import subprocess
from urllib.parse import urlparse

VALID_ENVS = frozenset({'production', 'staging', 'test', 'dev'})

# Production layout on the Hetzner/Plesk host (see ops/podskrift.service.example).
PROD_APP_ROOT_MARKER = '/var/www/vhosts/podskrift.nettsmed.dev'
PROD_PUBLIC_HOSTS = frozenset({'podskrift.com', 'www.podskrift.com'})

_APP_ROOT = os.path.dirname(os.path.abspath(__file__))
_release_cache = None


def _env_name(raw):
    value = (raw or '').strip().lower()
    return value if value in VALID_ENVS else ''


def _database_url():
    return os.getenv('DATABASE_URL', '').strip()


def _non_production_signals():
    """True when this process is clearly not the live gunicorn service."""
    if os.environ.get('PYTEST_CURRENT_TEST'):
        return True
    if os.environ.get('CI') or os.environ.get('GITHUB_ACTIONS'):
        return True
    # Cursor Cloud Agent / local agent sandboxes.
    if os.environ.get('CURSOR_AGENT') or os.environ.get('CURSOR_CLOUD_AGENT'):
        return True
    if os.environ.get('CURSOR_TRACE_ID') and os.environ.get('CURSOR_REQUEST_ID'):
        # Extra belt for agent VMs that set request metadata but not CURSOR_AGENT.
        if os.environ.get('AGENT_TRANSCRIPTS') or os.environ.get('CURSOR_CONVERSATION_ID'):
            return True
    db = _database_url()
    if db in ('sqlite:///:memory:', 'sqlite://', 'sqlite:///:memory'):
        return True
    # Pytest /tmp DBs: sqlite:////tmp/... or sqlite:///tmp/...
    normalized = db.replace('\\', '/')
    if '/tmp/' in normalized or normalized.endswith('/tmp'):
        return True
    try:
        from flask import has_app_context, current_app
        if has_app_context() and current_app.testing:
            return True
    except Exception:  # noqa: BLE001 - flask optional at import time
        pass
    return False


def _production_markers():
    """Positive evidence we are on the live podskrift deploy."""
    root = _APP_ROOT.replace('\\', '/')
    if PROD_APP_ROOT_MARKER in root:
        return True
    db = _database_url().replace('\\', '/')
    if PROD_APP_ROOT_MARKER in db:
        return True
    if not db:
        default_db = os.path.join(_APP_ROOT, 'data', 'podcast.db').replace('\\', '/')
        if PROD_APP_ROOT_MARKER in default_db:
            return True
    public = os.getenv('PUBLIC_BASE_URL', '').strip()
    if public:
        host = (urlparse(public).hostname or '').lower()
        if host in PROD_PUBLIC_HOSTS:
            return True
    return False


def resolve_environment():
    """Return production|staging|test|dev for this process."""
    explicit = _env_name(os.getenv('PODSKRIFT_ENV'))
    if explicit:
        return explicit
    if _non_production_signals():
        if (os.environ.get('PYTEST_CURRENT_TEST')
                or os.environ.get('CI')
                or os.environ.get('GITHUB_ACTIONS')):
            return 'test'
        return 'dev'
    if _production_markers():
        return 'production'
    return 'dev'


def is_production():
    return resolve_environment() == 'production'


def release_sha():
    """Best-effort git commit for Sentry/PostHog ``release`` tagging.

    Cached after the first lookup. Prefers explicit env (``PODSKRIFT_RELEASE``,
    ``GIT_COMMIT``, …), then ``git rev-parse HEAD`` from the app root.
    """
    global _release_cache
    if _release_cache is not None:
        return _release_cache
    for key in ('PODSKRIFT_RELEASE', 'GIT_COMMIT', 'SOURCE_VERSION',
                'HEROKU_SLUG_COMMIT'):
        value = os.getenv(key, '').strip()
        if value:
            _release_cache = value
            return _release_cache
    try:
        out = subprocess.run(
            ['git', 'rev-parse', 'HEAD'],
            cwd=_APP_ROOT,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        sha = (out.stdout or '').strip()
        if out.returncode == 0 and sha:
            _release_cache = sha
            return _release_cache
    except Exception:  # noqa: BLE001 - release is optional metadata
        logging.getLogger(__name__).debug('git release lookup failed', exc_info=True)
    _release_cache = ''
    return _release_cache


def clear_release_cache():
    """Test helper: drop the cached git sha."""
    global _release_cache
    _release_cache = None
