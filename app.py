#!/usr/bin/env python3
"""
Podcast Transcriber Web App with OpenAI API

A Flask web application for transcribing podcast episodes from RSS feeds using OpenAI's Whisper API.
Supports user accounts, saved RSS feeds, and self-serve API keys.
"""

import collections
import glob
import hashlib
import hmac
import html as html_lib
import io
import json
import math
import os
import re
import secrets
import signal
import sqlite3
import shutil
import statistics
import subprocess
import ssl
import time
import threading
import unicodedata
import wave
from datetime import date, datetime, timedelta, timezone
from functools import wraps
from zoneinfo import ZoneInfo
import certifi
import requests
import feedparser
from markupsafe import Markup

# Fix CA bundle path for Python 3.14+ where certifi may ship without the PEM
if not os.path.exists(certifi.where()):
    _sys_ca = ssl.get_default_verify_paths().cafile
    if _sys_ca and os.path.exists(_sys_ca):
        os.environ.setdefault('REQUESTS_CA_BUNDLE', _sys_ca)
        os.environ.setdefault('SSL_CERT_FILE', _sys_ca)
from flask import (Flask, render_template, request, jsonify, send_file, flash,
                   redirect, url_for, Response, g, session, has_request_context,
                   make_response, abort)
import show_pages as show_pages_mod
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from urllib.parse import parse_qs, urljoin, urlparse, urlunparse, unquote
import uuid
from dotenv import load_dotenv
from openai import OpenAI, APIConnectionError, APITimeoutError
import httpx

from sqlalchemy import (event as sa_event, func as sa_func, inspect as sa_inspect, text,
                        update as sa_update, or_ as sa_or_)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError as SaIntegrityError

from models import (db, User, SavedFeed, TranscriptionTask, CreditPurchase,
                    TranscriptShare, EmailSentLog, SummaryEmailJob, SummaryEmailBudgetDay,
                    TrialBudgetDay,
                    PasswordResetToken,
                    OAuthClient, OAuthAuthorizationCode, OAuthAccessToken,
                    OAuthRefreshToken, OAuthJwtJti,
                    TASK_COLUMN_MIGRATIONS, USER_COLUMN_MIGRATIONS,
                    CREDIT_PURCHASE_COLUMN_MIGRATIONS,
                    SAVED_FEED_COLUMN_MIGRATIONS,
                    TRANSCRIPT_SHARE_COLUMN_MIGRATIONS,
                    PASSWORD_RESET_TOKEN_COLUMN_MIGRATIONS)
from observability import init_sentry, report_stale_task, report_task_failure
import analytics as product_analytics
from site_standards import init_site_standards
import email_notify
import mail as mailer
import summary as summary_mod
import mcp_server as mcp_server_mod
import oauth_server as oauth_server_mod
from admin_dashboard import admin_bp, ensure_admin_indexes, is_admin_user

try:
    import stripe
except ImportError:  # pragma: no cover - production must pip install; tests mock
    stripe = None

try:
    import sentry_sdk
except ImportError:  # pragma: no cover
    sentry_sdk = None

load_dotenv()
# Before the app exists, so the Flask integration hooks it, and before the boot
# sweep at the bottom of this module, which reports what it finds.
init_sentry()
product_analytics.init_posthog()

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'change-me-in-production')

# Database. DATABASE_URL lets tests point at a throwaway file -- importing this
# module runs migrations and the orphan sweep, which must never touch real data.
db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
os.makedirs(db_path, exist_ok=True)
app.config['SQLALCHEMY_DATABASE_URI'] = (
    os.getenv('DATABASE_URL') or f"sqlite:///{os.path.join(db_path, 'podcast.db')}"
)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db.init_app(app)
app.register_blueprint(admin_bp)


@sa_event.listens_for(Engine, 'connect')
def _sqlite_pragmas(dbapi_connection, connection_record):
    """WAL + a longer busy timeout.

    Production ran journal_mode=delete under `gunicorn --workers 2 --threads 4`,
    so every write blocked readers. WAL lets the status polls read while a
    transcription writes its progress.
    """
    if not isinstance(dbapi_connection, sqlite3.Connection):
        return
    # synchronous stays at the default FULL: this is the only copy of user
    # data, backups are nightly, and the write volume here is a handful of
    # progress rows, so NORMAL would trade durability for nothing.
    try:
        cur = dbapi_connection.cursor()
        cur.execute('PRAGMA journal_mode=WAL')
        cur.execute('PRAGMA busy_timeout=15000')
        cur.close()
    except sqlite3.Error as exc:
        # A read-only volume must degrade, not take the app down on every
        # connect -- but say so: without WAL this runs with writers blocking
        # readers, and ops/backup-db.sh assumes WAL is on.
        app.logger.warning('Could not apply SQLite pragmas: %s', exc)

# Login manager
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'
login_manager.login_message_category = 'info'


@login_manager.user_loader
def load_user(user_id):
    """Load by id, honouring session_version when present.

    Bare ids (pre-ship cookies and test helpers) stay valid only while
    ``session_version`` is still 0 — a password reset bumps the version and
    drops those sessions too.
    """
    raw = str(user_id or '')
    parts = raw.split(':', 1)
    try:
        uid = int(parts[0])
    except (TypeError, ValueError):
        return None
    user = db.session.get(User, uid)
    if user is None:
        return None
    current_ver = int(getattr(user, 'session_version', 0) or 0)
    if len(parts) == 1:
        return user if current_ver == 0 else None
    try:
        ver = int(parts[1])
    except ValueError:
        return None
    if ver != current_ver:
        return None
    return user


#: Canonical public origin. url_for(_external=True) builds from the request,
#: which behind Plesk's nginx is http:// on an https site -- so the canonical
#: link, the sitemap and the JSON-LD @id all pointed at URLs that 301 away.
#: Set explicitly rather than trusting X-Forwarded-*: those headers are only as
#: trustworthy as the proxy stripping them, and this needs no such assumption.
PUBLIC_BASE_URL = (os.getenv('PUBLIC_BASE_URL') or '').rstrip('/')

# Persistent login: session cookie lasts 90 days; Flask-Login remember cookie
# lasts a year. Secure flags follow the public origin so http:// test clients
# still receive cookies (production PUBLIC_BASE_URL is https://…).
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=90)
app.config['REMEMBER_COOKIE_DURATION'] = timedelta(days=365)
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['REMEMBER_COOKIE_SAMESITE'] = 'Lax'
_cookie_secure = PUBLIC_BASE_URL.startswith('https://')
app.config['SESSION_COOKIE_SECURE'] = _cookie_secure
app.config['REMEMBER_COOKIE_SECURE'] = _cookie_secure

def public_url(endpoint, **values):
    """Absolute URL for `endpoint`, on the configured public origin."""
    path = url_for(endpoint, **values)
    if PUBLIC_BASE_URL:
        return PUBLIC_BASE_URL + path
    return url_for(endpoint, _external=True, **values)


def _canonical_host_exempt(path):
    """True for Stripe webhooks, /api/*, MCP, email unsub, share links, health."""
    if path.startswith('/stripe/webhook') or path.startswith('/api/'):
        return True
    # Remote MCP (Streamable HTTP) — same host-bounce exemption as /api/*.
    if path == '/mcp' or path.startswith('/mcp/'):
        return True
    # OAuth discovery + token endpoints must not bounce (clients cache issuer).
    if path.startswith('/.well-known/') or path.startswith('/oauth/'):
        return True
    if path.startswith('/email/unsubscribe'):
        return True
    if path.startswith('/internal/'):
        return True
    # Public share links must resolve on whichever host they were opened
    # (www / bare / staging), not bounce through a host redirect that can
    # drop cookies or confuse chat-app link previews.
    if path.startswith('/t/'):
        return True
    if path in ('/health', '/healthz', '/ready', '/ping') or path.startswith('/health'):
        return True
    return False


def _canonical_redirect_target():
    """PUBLIC_BASE_URL + path + query (no trailing bare ?)."""
    path = request.path or '/'
    qs = request.query_string.decode('utf-8', errors='replace') if request.query_string else ''
    return PUBLIC_BASE_URL + path + (('?' + qs) if qs else '')


@app.before_request
def _ensure_shutdown_handlers():
    """Wrap gunicorn's SIGTERM handler once the worker has installed it."""
    if not _shutdown_handlers_installed:
        install_shutdown_handlers()


@app.before_request
def canonical_host_redirect():
    """Send www / staging hosts to PUBLIC_BASE_URL so cookies stay on one origin.

    GET/HEAD → 301; other methods → 308 (preserve method/body). Stripe webhooks,
    /api/*, and common health probes are left alone so probes and signed
    callbacks are not bounced.
    """
    if not PUBLIC_BASE_URL:
        return None
    canonical_host = (urlparse(PUBLIC_BASE_URL).netloc or '').lower()
    if not canonical_host or request.host.lower() == canonical_host:
        return None
    if _canonical_host_exempt(request.path or '/'):
        return None
    code = 301 if request.method in ('GET', 'HEAD') else 308
    return redirect(_canonical_redirect_target(), code=code)


# Global fallback OpenAI key
GLOBAL_OPENAI_KEY = os.getenv('OPENAI_API_KEY')

def _env_int(name, default):
    """Read an integer env var, falling back to `default` on junk (with a warning).

    Same reasoning as _env_minutes: a typo in a capacity limit must not silently
    restore a value the operator was trying to lower.
    """
    raw = os.getenv(name)
    if raw is None:
        return int(default)
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        app.logger.warning('%s=%r is not a number; using the default of %s',
                           name, raw, default)
        return int(default)


def free_disk_bytes(path='.'):
    """Bytes free on the volume holding `path`, or None if it cannot be read."""
    try:
        stat = os.statvfs(path)
        return stat.f_bavail * stat.f_frsize
    except (OSError, AttributeError, ValueError):
        return None


# Whisper pricing, used for the cost estimates shown in the UI and in
# trial-limit / settings copy. OpenAI's published rate; keep every "$0.36/hr"
# style figure derived from this constant so they cannot drift.
WHISPER_COST_PER_MINUTE = 0.006

# Post-transcript AI summaries (off until SUMMARY_ENABLED=1). Chat model for
# TL;DR + key points + short quotes — never meters trial/paid minutes.
SUMMARY_ENABLED = summary_mod.summary_enabled()
SUMMARY_MODEL = summary_mod.summary_model()

# Remote MCP at /mcp (off until MCP_ENABLED=1). Tools reuse /api/v1 helpers.
MCP_ENABLED = mcp_server_mod.mcp_enabled()
# OAuth 2.1 for ChatGPT / Claude.ai connectors (off until MCP_OAUTH_ENABLED=1).
MCP_OAUTH_ENABLED = oauth_server_mod.mcp_oauth_enabled()


def openai_whisper_cost_usd(minutes):
    """Rough USD cost at OpenAI's Whisper rate for `minutes` of audio."""
    return round(float(minutes) * WHISPER_COST_PER_MINUTE, 2)


#: Session key for an episode an anonymous visitor picked before signing up.
#: Cleared after resume (success or failure) so a stale stash cannot fire later.
PENDING_TRANSCRIPTION_KEY = 'pending_transcription'
# First-party preference cookie written by static/cookie-consent.js (12 months).
# Necessary to remember Accept/Decline — not a tracking cookie.
COOKIE_CONSENT_NAME = 'podskrift_cookie_consent'
COOKIE_CONSENT_ACCEPTED = 'accepted'
#: Relative path to return to after Stripe Checkout (cancel / success CTA).
#: Validated with safe_return_to(); never trust a raw absolute URL here.
BILLING_RETURN_TO_KEY = 'billing_return_to'


def safe_next_url(candidate, default=None):
    """Allow only same-origin relative paths. Reject open redirects.

    Absolute URLs, protocol-relative `//evil`, backslash tricks that browsers
    treat as `/` (`/\\evil.com`, `/%5Cevil.com`), and control characters all
    fall back to `default` (or the homepage). Flask-Login and our
    register/login forms all pass `?next=` through here.
    """
    fallback = default if default is not None else '/'
    if not candidate or not isinstance(candidate, str):
        return fallback
    raw = candidate.strip()
    # Decode twice so /%5Cevil and /%255Cevil are caught the same as /\evil.
    decoded = unquote(unquote(raw))
    if any(ord(ch) < 32 or ch == '\\' for ch in decoded):
        return fallback
    # Path-only: a single leading slash, then a character that is not / or \.
    if not decoded.startswith('/') or len(decoded) < 2 or decoded[1] in '/\\':
        # "/" alone is fine (home); anything else must be /<non-slash>.
        if decoded != '/':
            return fallback
    parsed = urlparse(decoded)
    if parsed.scheme or parsed.netloc:
        return fallback
    # Rebuild from path/query/fragment only — never trust a smuggled host.
    path = parsed.path or '/'
    if any(ord(ch) < 32 or ch == '\\' for ch in path):
        return fallback
    if not path.startswith('/') or path.startswith('//'):
        return fallback
    if path != '/' and path[1:2] in ('/', '\\'):
        return fallback
    safe = path
    if parsed.query:
        if any(ord(ch) < 32 or ch == '\\' for ch in parsed.query):
            return fallback
        safe += '?' + parsed.query
    if parsed.fragment:
        if any(ord(ch) < 32 or ch == '\\' for ch in parsed.fragment):
            return fallback
        safe += '#' + parsed.fragment
    # When a request is active, resolve against the current host and require
    # the netloc to stay put (catches any remaining join tricks).
    if has_request_context():
        absolute = urljoin(request.host_url, safe)
        if urlparse(absolute).netloc.lower() != urlparse(request.host_url).netloc.lower():
            return fallback
    return safe


def safe_return_to(candidate):
    """Relative same-origin path for post-checkout return, or None if unsafe.

    Rejects absolute URLs, protocol-relative hosts, and empty values. Used for
    Stripe cancel_url / session return_to — never store an unvalidated string.
    """
    if not candidate or not isinstance(candidate, str) or not candidate.strip():
        return None
    # Sentinel default: safe_next_url returns it unchanged only on rejection.
    rejected = '__unsafe_return_to__'
    result = safe_next_url(candidate.strip(), default=rejected)
    if result == rejected:
        return None
    return result


def trial_estimate_seconds(duration_min):
    """Seconds the enqueue path would reserve for a feed/iTunes duration.

    Matches enqueue_transcription: floored at one minute (a zero charge would
    look settled), falling back to TRIAL_UNKNOWN_ESTIMATE_SECONDS when the
    feed gave no length. Callers that only have a display estimate and want
    "no badge without a length" should check duration_min themselves first.
    """
    return max(60, int((duration_min or 0) * 60) or TRIAL_UNKNOWN_ESTIMATE_SECONDS)


def encode_partial_task_meta(partial_seconds, episode_seconds):
    """Serialize partial-preview metadata for the partial_meta column."""
    import json as _json
    return _json.dumps({
        'partial_seconds': int(partial_seconds),
        'episode_seconds': int(max(partial_seconds, episode_seconds)),
    }, separators=(',', ':'))


def task_partial_meta(task):
    """Return {partial_seconds, episode_seconds} for a partial preview, or None."""
    import json as _json
    if task is None:
        return None
    raw = getattr(task, 'partial_meta', None) or ''
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        data = _json.loads(raw)
        partial_seconds = int(data['partial_seconds'])
        episode_seconds = int(data['episode_seconds'])
    except (TypeError, ValueError, KeyError, _json.JSONDecodeError):
        return None
    if partial_seconds <= 0 or episode_seconds <= 0:
        return None
    return {
        'partial_seconds': partial_seconds,
        'episode_seconds': max(partial_seconds, episode_seconds),
    }


def task_is_partial(task):
    return task_partial_meta(task) is not None


def set_task_partial_meta(task, partial_seconds, episode_seconds):
    """Write partial-preview metadata onto `task` (caller commits)."""
    task.partial_meta = encode_partial_task_meta(partial_seconds, episode_seconds)


def partial_minutes_pair(meta):
    """(partial_minutes, episode_minutes) display ints from partial meta."""
    n = max(1, int(math.ceil(meta['partial_seconds'] / 60.0)))
    m = max(n, int(math.ceil(meta['episode_seconds'] / 60.0)))
    return n, m


def partial_transcript_note(meta):
    """Short plain-text note for copy/download of a partial preview."""
    n, m = partial_minutes_pair(meta)
    return f'Free preview: first {n} minutes of {m} minutes.'


def episode_needs_own_key(duration_min, remaining_minutes, paid_minutes=0):
    """True when a platform user cannot start this episode without buying/BYOK.

    No estimate → no badge. Own-key users pass remaining_minutes=None.
    When free trial alone cannot cover the full length but enough remains for a
    partial preview (and there is no paid balance), returns False — the start
    path will enqueue a free preview instead of a paywall.
    """
    if duration_min is None or remaining_minutes is None:
        return False
    try:
        duration_min = float(duration_min)
    except (TypeError, ValueError):
        return False
    if duration_min <= 0:
        return False
    estimate = trial_estimate_seconds(duration_min)
    paid_sec = max(0, int(paid_minutes or 0)) * 60
    free_sec = max(0, int(remaining_minutes)) * 60
    if TRIAL_MAX_EPISODE_SECONDS and estimate > TRIAL_MAX_EPISODE_SECONDS:
        if estimate <= paid_sec:
            return False
        # Over the free per-episode cap: partial preview only when the episode
        # is also longer than remaining free trial (same gate as enqueue).
        if (paid_sec <= 0 and free_sec >= TRIAL_PARTIAL_MIN_SECONDS
                and estimate > free_sec):
            return False
        return True
    if estimate <= free_sec + paid_sec:
        return False
    return not (paid_sec <= 0 and free_sec >= TRIAL_PARTIAL_MIN_SECONDS)


def trial_badge_remaining_minutes():
    """Free trial minutes left for the needs-own-key badge, or None to hide it.

    Anonymous visitors see the new-account grant. Logged-in trial users see
    what they have left. Own-key users get None (badge hidden). Paid minutes
    are exposed separately via trial_badge_paid_minutes(). When today's shared
    daily budget is empty, returns 0 so we do not advertise unusable minutes.
    """
    if current_user.is_authenticated and current_user.openai_api_key:
        return None
    if not trial_available() and not (
            current_user.is_authenticated and paid_balance_seconds(current_user) > 0):
        return None
    if current_user.is_authenticated:
        if trial_available():
            if not trial_daily_budget_available():
                return 0
            return trial_status(current_user)[2] // 60
        return 0
    if trial_available():
        return NEW_USER_TRIAL_SECONDS // 60
    return None


def trial_badge_paid_minutes():
    """Paid credit minutes for the badge, or 0 when none / own-key / anon."""
    if not current_user.is_authenticated or current_user.openai_api_key:
        return 0
    return paid_balance_seconds(current_user) // 60


def settings_openai_url():
    """In-app link to the OpenAI key field on Settings."""
    return url_for('settings') + '#openai'


def settings_credits_url():
    """In-app link to the paid-credits / buy section on Settings."""
    return url_for('settings') + '#credits'

# Keep a hanging Whisper call inside the stale-task window, so the client gives
# up before _fail_if_stale() presumses the task dead. Sized against the 900s floor:
# 420s read/write × WHISPER_CHUNK_ATTEMPTS (=2) = 840s, plus a small backoff
# margin. Client-level SDK retries are off (WHISPER_CLIENT_MAX_RETRIES=0); we
# own the retry loop per chunk so a timeout cannot stretch past this budget via
# opaque SDK retries, and so we can heartbeat between attempts.
# A steadily-progressing upload never trips the write timeout: 24 MB (the chunk
# cap) over 420s is only 57 KB/s.
WHISPER_TIMEOUT_SECONDS = 420.0
WHISPER_CHUNK_ATTEMPTS = 2
WHISPER_CLIENT_MAX_RETRIES = 0
# Back-compat alias used by older tests / docs; same meaning as "SDK retries".
WHISPER_MAX_RETRIES = WHISPER_CLIENT_MAX_RETRIES

# Connect + per-read idle timeout for enclosure downloads. stream=True applies
# the read timeout between iter_content chunks, so a stalled CDN cannot hang
# the worker indefinitely after the first byte.
DOWNLOAD_TIMEOUT_SECONDS = 30
# One short pause before retrying a transient download failure (5xx / timeout).
DOWNLOAD_RETRY_PAUSE_SEC = 0.4
# Permanent host refusals: the enclosure is gone or the CDN forbids the fetch.
# These are expected user-side outcomes, not app bugs — see SourceAudioUnavailable.
SOURCE_AUDIO_MISSING_STATUSES = frozenset({404, 410})
SOURCE_AUDIO_FORBIDDEN_STATUSES = frozenset({403})
SOURCE_AUDIO_TRANSIENT_STATUSES = frozenset({500, 502, 503, 504})

# Caps on the audio we will pull down from a client-supplied URL.
#
# Podcast CDNs stack measurement prefixes in front of the real file. A Modern
# Wisdom / Megaphone enclosure is already four hops today
# (byspotify → pscrb → claritas → traffic.megaphone → dcs), and the previous
# budget of 5 failed as soon as one more hop appeared — which is exactly the
# "Too many redirects while fetching audio" two organic users hit. requests'
# own default is 30; 20 leaves headroom without letting a runaway loop run
# forever.
MAX_REDIRECTS = 20
# The source ceiling. It is a term in the disk floor below, so raising it means
# raising that too -- see the ## Invariants section in CLAUDE.md.
MAX_AUDIO_BYTES = 500 * 1024 * 1024

# How many transcriptions this PROCESS will run at once. A job in flight holds
# its source plus the re-encoded parts, and re-encoding is the one CPU-hungry
# step in the pipeline (niced and single-threaded, but still real work).
#
# This is per gunicorn worker -- a threading.Semaphore cannot span processes --
# so the real ceiling is this times the worker count. Prod runs 2 workers, so
# the default of 1 admits 2 jobs at a time. Deliberately conservative: the box
# is a shared Plesk host with 50+ other services and 4 cores, so this is not
# our capacity to spend. Raise it once there is traffic that needs it.
#
# Over the limit is a hard refusal, not a queue: an unbounded queue is the same
# outage arriving later.
MAX_CONCURRENT_TRANSCRIPTIONS = max(1, _env_int('MAX_CONCURRENT_TRANSCRIPTIONS', 1))
_transcription_slots = threading.BoundedSemaphore(MAX_CONCURRENT_TRANSCRIPTIONS)

# Refuse to start when the volume is this close to full. The check does not
# reserve anything, so every concurrent request sees the same free space -- the
# floor therefore has to exceed everything admission control will admit at once:
# workers x MAX_CONCURRENT_TRANSCRIPTIONS x (MAX_AUDIO_BYTES + the parts), which
# test_the_disk_floor_clears_what_admission_control_admits keeps honest.
MIN_FREE_DISK_BYTES = max(0, _env_int('MIN_FREE_DISK_MB', 4096)) * 1024 * 1024

# Roughly how many seconds of audio Whisper gets through per second of wall clock.
# Only used to interpolate progress between chunk checkpoints -- the API gives us
# no streaming progress, so without an estimate the bar would sit still for minutes.
WHISPER_REALTIME_FACTOR = 12.0

# Historical ETA for MCP (and other agents): wall seconds per audio minute from
# recent completed jobs. Fallback when too few samples — full pipeline (download
# + split + Whisper), not Whisper-only realtime.
ETA_SAMPLE_LIMIT = max(10, _env_int('ETA_SAMPLE_LIMIT', 50))
ETA_LOOKBACK_DAYS = max(1, _env_int('ETA_LOOKBACK_DAYS', 7))
ETA_MIN_SAMPLES = max(1, _env_int('ETA_MIN_SAMPLES', 5))
# Optimistic / pessimistic full-pipeline seconds of wall clock per audio minute.
ETA_FALLBACK_SEC_PER_AUDIO_MIN_LOW = 10.0
ETA_FALLBACK_SEC_PER_AUDIO_MIN_HIGH = 40.0

# Share of the overall progress bar owned by each phase.
PHASE_SPANS = {
    'downloading': (0, 20),
    'splitting': (20, 30),
    'transcribing': (30, 100),
}

# Languages offered in the UI. '' means let Whisper auto-detect.
#: Languages offered in the picker. Whisper handles far more than this, but
#: naming a language beats auto-detect on short or accented audio, so the list
#: is what people can actually choose from.
#:
#: (code, native name, English name). Alphabetical by English name after
#: auto-detect -- a long list is only usable if it is findable, and the earlier
#: nine-language list was ordered by who happened to have signed up.
SUPPORTED_LANGUAGES_FULL = [
    ('', 'Auto-detect', 'Auto-detect'),
    ('ar', '\u0627\u0644\u0639\u0631\u0628\u064a\u0629', 'Arabic'),
    ('zh', '\u4e2d\u6587', 'Chinese'),
    ('cs', '\u010ce\u0161tina', 'Czech'),
    ('da', 'Dansk', 'Danish'),
    ('nl', 'Nederlands', 'Dutch'),
    ('en', 'English', 'English'),
    ('fi', 'Suomi', 'Finnish'),
    ('fr', 'Fran\u00e7ais', 'French'),
    ('de', 'Deutsch', 'German'),
    ('el', '\u0395\u03bb\u03bb\u03b7\u03bd\u03b9\u03ba\u03ac', 'Greek'),
    ('he', '\u05e2\u05d1\u05e8\u05d9\u05ea', 'Hebrew'),
    ('hi', '\u0939\u093f\u0928\u094d\u0926\u0940', 'Hindi'),
    ('hu', 'Magyar', 'Hungarian'),
    ('id', 'Bahasa Indonesia', 'Indonesian'),
    ('it', 'Italiano', 'Italian'),
    ('ja', '\u65e5\u672c\u8a9e', 'Japanese'),
    ('ko', '\ud55c\uad6d\uc5b4', 'Korean'),
    ('no', 'Norsk', 'Norwegian'),
    ('pl', 'Polski', 'Polish'),
    ('pt', 'Portugu\u00eas', 'Portuguese'),
    ('ro', 'Rom\u00e2n\u0103', 'Romanian'),
    ('ru', '\u0420\u0443\u0441\u0441\u043a\u0438\u0439', 'Russian'),
    ('es', 'Espa\u00f1ol', 'Spanish'),
    ('sv', 'Svenska', 'Swedish'),
    ('th', '\u0e44\u0e17\u0e22', 'Thai'),
    ('tr', 'T\u00fcrk\u00e7e', 'Turkish'),
    ('uk', '\u0423\u043a\u0440\u0430\u0457\u043d\u0441\u044c\u043a\u0430', 'Ukrainian'),
    ('vi', 'Ti\u1ebfng Vi\u1ec7t', 'Vietnamese'),
]

def language_choices():
    """(code, label) for the picker, labelled so the sort order is visible.

    The list is alphabetical by English name, but rendering only native names
    made that look random -- العربية, 中文, Čeština, Dansk, Nederlands. Showing
    "English (native)" is what makes a 28-item list scannable.
    """
    choices = []
    for code, native, english in SUPPORTED_LANGUAGES_FULL:
        if not code:
            choices.append((code, native))
        elif native == english:
            choices.append((code, english))
        else:
            choices.append((code, f'{english} ({native})'))
    return choices


#: (code, native name) -- kept for llms.txt, which lists native names.
SUPPORTED_LANGUAGES = [(code, native) for code, native, _ in SUPPORTED_LANGUAGES_FULL]
#: code -> English name, for llms.txt and the schema. Derived, so the two
#: cannot drift: an earlier version kept a second hand-written list.
LANGUAGE_ENGLISH_NAMES = {code: english for code, _, english in SUPPORTED_LANGUAGES_FULL if code}
VALID_LANGUAGE_CODES = {code for code, _ in SUPPORTED_LANGUAGES}
#: Whisper returns English language *names* ('english'); the picker stores ISO
#: codes ('en'). Map both directions so storage and retries stay consistent.
LANGUAGE_NAME_TO_CODE = {
    english.casefold(): code for code, english in LANGUAGE_ENGLISH_NAMES.items()
}


def normalize_language_code(value):
    """Return an ISO language code, or '' (auto-detect) when unknown.

    Accepts picker codes (`en`) and Whisper/legacy names (`english`). Unknown
    values become auto rather than being stored or sent to Whisper as junk.
    """
    if not value:
        return ''
    raw = str(value).strip()
    if not raw:
        return ''
    lowered = raw.casefold()
    if lowered in VALID_LANGUAGE_CODES:
        return lowered
    return LANGUAGE_NAME_TO_CODE.get(lowered, '')


def display_language(value):
    """Human label for a stored language: codes → English name; names stay as-is."""
    if not value:
        return ''
    code = normalize_language_code(value)
    if code and code in LANGUAGE_ENGLISH_NAMES:
        return LANGUAGE_ENGLISH_NAMES[code]
    # Legacy row that held a name we do not map, or free text — show as stored.
    return str(value).strip()


#: Origins we record on transcript_* analytics. Explicit meta wins; otherwise
#: derived from rss_url / audio-only path.
VALID_INPUT_ORIGINS = frozenset({
    'spotify', 'itunes_episode', 'rss', 'apple', 'audio',
})


def derive_input_origin(meta, rss_url=None):
    """Classify how the episode was chosen for analytics."""
    explicit = (meta.get('input_origin') or '').strip().lower()
    if explicit in VALID_INPUT_ORIGINS:
        return explicit
    feed = (rss_url or meta.get('rss_url') or '').strip()
    if feed:
        if 'podcasts.apple.com' in feed.lower():
            return 'apple'
        return 'rss'
    return 'audio'


def safe_download_basename(title):
    """Filename stem safe for Content-Disposition: no path seps or control chars."""
    base = (title or 'transcript').replace(' ', '_')
    # Strip separators and Windows-forbidden characters; keep letters/digits/_-.
    base = re.sub(r'[/\\:*?"<>|\x00-\x1f]+', '_', base)
    base = re.sub(r'_+', '_', base).strip('._')
    return base or 'transcript'


def build_openai_client(key):
    """Wrap a raw key in a configured OpenAI client, or None if there is no key."""
    if not key:
        return None
    # Explicit connect/read/write/pool timeouts: a bare float is also accepted
    # by httpx, but naming the four makes the hang budget obvious and keeps a
    # slow connect from borrowing the whole Whisper window.
    return OpenAI(
        api_key=key,
        timeout=httpx.Timeout(
            connect=30.0,
            read=WHISPER_TIMEOUT_SECONDS,
            write=WHISPER_TIMEOUT_SECONDS,
            pool=30.0,
        ),
        max_retries=WHISPER_CLIENT_MAX_RETRIES,
    )


# ---------------------------------------------------------------------------
# Trial metering
# ---------------------------------------------------------------------------
#
# A user with their own OpenAI key spends their own quota and is never metered.
# Everyone else transcribes on OUR key, which is real money -- so every second
# of audio is reserved against a per-account allowance before any request
# reaches Whisper. A daily global budget (Europe/Oslo midnight) caps what the
# whole service can spend that calendar day; an optional lifetime ceiling is
# a safety net only (unset / 0 = off).


def _env_minutes(name, default):
    """Read a minutes-valued env var, falling back to `default` on junk.

    Warns loudly: a typo in a spend ceiling otherwise silently restores the
    default, which is the permissive direction if the operator meant to lower it.
    """
    raw = os.getenv(name)
    if raw is None:
        return int(default)
    try:
        return max(0, int(float(raw)))
    except (TypeError, ValueError):
        app.logger.warning('%s=%r is not a number; using the default of %s minutes',
                           name, raw, default)
        return int(default)


def _env_minutes_optional(name):
    """Minutes-valued env var where unset/blank means disabled (returns 0).

    Distinct from ``_env_minutes``: a missing lifetime safety cap must not
    restore a permissive multi-thousand-minute default.
    """
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == '':
        return 0
    try:
        return max(0, int(float(raw)))
    except (TypeError, ValueError):
        app.logger.warning('%s=%r is not a number; treating as disabled (0)',
                           name, raw)
        return 0


#: Free audio minutes for accounts with NULL trial_seconds_limit (legacy /
#: pre-NEW_USER_TRIAL_MINUTES rows). Raising this lifts every NULL-limit user.
TRIAL_DEFAULT_SECONDS = _env_minutes('TRIAL_MINUTES', 180) * 60
#: Minutes stamped onto users.trial_seconds_limit at registration. Existing
#: rows keep their stored limit (or NULL → TRIAL_MINUTES); only new signups
#: get this grant, except the one-off 60 → 120 cohort lift below. When the
#: trial split is on, new signups get a hashed 50/50 variant instead.
NEW_USER_TRIAL_SECONDS = _env_minutes('NEW_USER_TRIAL_MINUTES', 120) * 60
#: A/B new-user trial grant. Hash of user id → one of TRIAL_SPLIT_VARIANTS.
#: Off → stamp NEW_USER_TRIAL_MINUTES and leave trial_variant NULL.
TRIAL_SPLIT_ENABLED = os.getenv('TRIAL_SPLIT_ENABLED', '1').strip().lower() not in (
    '0', 'false', 'no', 'off')


def _parse_trial_split_variants(raw):
    """Comma-separated minute labels → unique positive ints as strings."""
    out = []
    seen = set()
    for part in (raw or '').split(','):
        part = part.strip()
        if not part:
            continue
        try:
            minutes = int(part)
        except (TypeError, ValueError):
            continue
        if minutes <= 0:
            continue
        label = str(minutes)
        if label in seen:
            continue
        seen.add(label)
        out.append(label)
    return out or ['60', '120']


TRIAL_SPLIT_VARIANTS = _parse_trial_split_variants(
    os.getenv('TRIAL_SPLIT_VARIANTS', '60,120'))
#: The 60-minute signup grant (PR #54) ran from 2026-10-09 ~06:40 UTC until
#: the 120-minute default shipped. raise_60_minute_trial_cohort() lifts those
#: rows once at boot. Bounds are UTC, matching how users.created_at is stored;
#: the end bound keeps a later manual 3600 grant from being bumped on restart.
TRIAL_60_COHORT_SECONDS = 60 * 60
TRIAL_60_COHORT_CREATED_FROM = '2026-10-09 06:00:00'
TRIAL_60_COHORT_CREATED_BEFORE = '2026-10-11 00:00:00'
#: Shared free-trial budget for one Europe/Oslo calendar day. Resets at Oslo
#: midnight. Reservations count immediately (trial_budget_days); refunds and
#: failed-before-Whisper jobs release the day the task was started.
TRIAL_DAILY_SECONDS = _env_minutes('TRIAL_DAILY_MINUTES', 2000) * 60
#: Optional lifetime safety ceiling across ALL accounts. Unset or 0 = off.
#: Prefer TRIAL_DAILY_MINUTES for day-to-day cost control.
TRIAL_GLOBAL_SECONDS = _env_minutes_optional('TRIAL_GLOBAL_MINUTES') * 60
#: What to reserve when the feed publishes no itunes:duration. Reconciled
#: against the real duration after download, before a single Whisper call.
TRIAL_UNKNOWN_ESTIMATE_SECONDS = _env_minutes('TRIAL_UNKNOWN_ESTIMATE_MINUTES', 30) * 60
#: Longest single episode the trial will take on as a *full* free job. Default
#: tracks TRIAL_MINUTES (legacy grant), not NEW_USER_TRIAL_MINUTES. A shorter
#: remaining balance on a longer episode can still start a partial preview
#: (see TRIAL_PARTIAL_MIN_SECONDS) instead of this hard refusal.
#: Set the env var explicitly if you want them different.
TRIAL_MAX_EPISODE_SECONDS = _env_minutes(
    'TRIAL_MAX_EPISODE_MINUTES', TRIAL_DEFAULT_SECONDS // 60) * 60
#: Minimum remaining free trial required to start a partial preview when the
#: episode is longer than the balance. Below this, show the paywall instead of
#: a tiny stub transcript.
TRIAL_PARTIAL_MIN_SECONDS = _env_minutes('TRIAL_PARTIAL_MIN_MINUTES', 5) * 60
#: Kill switch. Set TRIAL_ENABLED=0 to stop handing out our key entirely.
TRIAL_ENABLED = os.getenv('TRIAL_ENABLED', '1').strip().lower() not in ('0', 'false', 'no', 'off')
#: Calendar timezone for the daily free-trial budget boundary.
TRIAL_BUDGET_TZ = ZoneInfo('Europe/Oslo')

# ---------------------------------------------------------------------------
# Stripe credit pack (optional; unset keys hide Buy and skip webhook wiring)
# ---------------------------------------------------------------------------
STRIPE_SECRET_KEY = (os.getenv('STRIPE_SECRET_KEY') or '').strip()
STRIPE_WEBHOOK_SECRET = (os.getenv('STRIPE_WEBHOOK_SECRET') or '').strip()
STRIPE_PRICE_ID = (os.getenv('STRIPE_PRICE_ID') or '').strip()
STRIPE_API_VERSION = (os.getenv('STRIPE_API_VERSION') or '2026-08-26.dahlia').strip()
STRIPE_AUTOMATIC_TAX = os.getenv('STRIPE_AUTOMATIC_TAX', '0').strip().lower() in (
    '1', 'true', 'yes', 'on')
STRIPE_TAX_CODE = (os.getenv('STRIPE_TAX_CODE') or 'txcd_10103000').strip()
STRIPE_TAX_BEHAVIOR = (os.getenv('STRIPE_TAX_BEHAVIOR') or 'inclusive').strip().lower()
#: Stripe as merchant of record: it computes, collects and remits VAT/sales
#: tax, so we need no OSS/UK/MVA registrations. Needs Managed Payments enabled
#: in the Dashboard first, and replaces STRIPE_AUTOMATIC_TAX when both are on.
STRIPE_MANAGED_PAYMENTS = os.getenv('STRIPE_MANAGED_PAYMENTS', '0').strip().lower() in (
    '1', 'true', 'yes', 'on')
#: One-time pack: 300 minutes (5 hours) for $5.00 USD.
CREDIT_PACK_MINUTES = 300
CREDIT_PACK_SECONDS = CREDIT_PACK_MINUTES * 60
CREDIT_PACK_AMOUNT_CENTS = 500
CREDIT_PACK_CURRENCY = 'usd'
CREDIT_PACK_LABEL = 'Buy 300 min for $5'
#: Shown under Buy buttons — do not fold into CREDIT_PACK_LABEL (SKU/analytics).
CREDIT_PACK_SUBLINE = 'One-time · 300 min · VAT incl.'
#: Secondary result-page offer (not a second green primary when Copy is shown).
RESULT_OFFER_LABEL = 'Transcribe your next episode: 300 min for $5'
#: Partial-preview checkout CTA.
UNLOCK_EPISODE_LABEL = 'Unlock the full episode'
#: BYOK failure → pack offer (plain sentence; button uses CREDIT_PACK_LABEL).
OWN_KEY_PACK_OFFER = 'Use Podskrift minutes instead: 300 min for $5'
#: Trust line near Buy CTAs. Methods match Managed Payments dynamic PMs
#: (cards + wallets); we never pass payment_method_types on the session.
CREDIT_PACK_PAYMENT_HINT = (
    'Card · Apple Pay · Google Pay · secure checkout by Stripe')
CREDIT_PACK_SKU = 'minutes_300_usd500_v1'
CREDIT_PACK_TAX_BEHAVIOR = STRIPE_TAX_BEHAVIOR if STRIPE_TAX_BEHAVIOR in (
    'inclusive', 'exclusive') else 'inclusive'
#: Soft nudge on the home banner when free+paid remaining is under this.
LOW_BALANCE_MINUTES = 30


def _env_hours(name, default):
    """Positive hours clamped to Stripe Checkout Session bounds (1–24)."""
    raw = os.getenv(name, str(default)).strip()
    try:
        return max(1, min(24, int(raw)))
    except (TypeError, ValueError):
        app.logger.warning('%s=%r is not an int; using %s', name, raw, default)
        return default


#: Checkout Session lifetime before expiry (and recovery email). Default 2h so
#: abandoned-checkout recovery can fire the same day. Stripe allows 30 min–24 h;
#: we keep a 1 h floor for simplicity.
CHECKOUT_EXPIRES_HOURS = _env_hours('CHECKOUT_EXPIRES_HOURS', 2)
#: Abandoned Checkout recovery (Stripe emails a resume link after expiry).
CHECKOUT_RECOVERY_ENABLED = os.getenv(
    'CHECKOUT_RECOVERY_ENABLED', '1').strip().lower() not in (
        '0', 'false', 'no', 'off')
#: consent_collection.promotions — Stripe supports this only for US merchants
#: and US customers. Off by default: non-US accounts (e.g. Norway) get
#: InvalidRequestError ("not available in your country") and checkout dies.
#: after_expiration.recovery still works without this param.
CHECKOUT_PROMOTIONS_CONSENT_ENABLED = os.getenv(
    'CHECKOUT_PROMOTIONS_CONSENT_ENABLED', '0').strip().lower() not in (
        '0', 'false', 'no', 'off')

if stripe is not None and STRIPE_API_VERSION:
    stripe.api_version = STRIPE_API_VERSION


def _stripe_rejected_optional_checkout_params(exc, params):
    """True when Stripe rejected optional recovery/consent Checkout knobs.

    Those params must never block a purchase; callers retry without them.
    """
    if 'after_expiration' not in params and 'consent_collection' not in params:
        return False
    msg = str(exc).lower()
    return (
        'consent_collection' in msg
        or 'after_expiration' in msg
        or ('promotions' in msg and 'not available' in msg)
    )


def stripe_webhook_enabled():
    return bool(STRIPE_SECRET_KEY and STRIPE_WEBHOOK_SECRET and stripe is not None)


def stripe_checkout_enabled():
    """True only when both secrets are set — never sell without a webhook."""
    return stripe_webhook_enabled()


def stripe_client():
    """Pinned StripeClient for Checkout retrieve/create. None when unconfigured."""
    if not STRIPE_SECRET_KEY or stripe is None:
        return None
    return stripe.StripeClient(
        STRIPE_SECRET_KEY,
        stripe_version=STRIPE_API_VERSION,
        max_network_retries=2,
    )


def _sentry_capture_message(message, level='error', **kwargs):
    """Best-effort Sentry message; never raises into the request path."""
    if sentry_sdk is None:
        return
    try:
        sentry_sdk.capture_message(message, level=level, **kwargs)
    except Exception:  # noqa: BLE001
        pass


class TrialExhausted(Exception):
    """Raised when a job would cost more trial allowance than is left.

    `scope` names the limit that refused it -- 'episode_length', 'user' or
    'global' -- for the trial_limit_hit analytics event.
    """

    def __init__(self, message, scope='user'):
        super().__init__(message)
        self.scope = scope


class SourceAudioUnavailable(Exception):
    """The podcast host permanently cannot or will not serve the episode audio.

    Expected user-side outcome (dead enclosure, private CDN, geo-block) — not
    an app bug. Fail the task with a clear message, refund any reservation,
    emit transcript_failed with a specific reason, and do not raise to Sentry.
    """

    REASON_MISSING = 'source_audio_missing'
    REASON_FORBIDDEN = 'source_audio_forbidden'

    _MESSAGES = {
        REASON_MISSING: (
            "The podcast host no longer serves this episode's audio. "
            "Try another episode."
        ),
        REASON_FORBIDDEN: (
            "The podcast host blocked access to this episode's audio. "
            "Try another episode."
        ),
    }

    def __init__(self, reason, status_code=None):
        if reason not in self._MESSAGES:
            raise ValueError(f'unknown source-audio reason: {reason!r}')
        super().__init__(self._MESSAGES[reason])
        self.reason = reason
        self.status_code = status_code


class TaskAbandoned(Exception):
    """Raised when a task was failed out from under the worker still running it."""


class ServerRestart(Exception):
    """Process shutdown (deploy SIGTERM) interrupted this job mid-flight.

    Not a corrupt file and not an app bug when auto-resume will pick it up.
    ``reason`` is always ``server_restart`` for analytics.
    """

    REASON = 'server_restart'
    MSG_AUTO = (
        "Our server restarted while processing this episode — "
        "we've restarted it automatically."
    )
    MSG_RETRY = (
        "Our server restarted while processing this episode — please try again."
    )

    def __init__(self, message=None, *, auto_resumed=True):
        super().__init__(message or (self.MSG_AUTO if auto_resumed else self.MSG_RETRY))
        self.reason = self.REASON
        self.auto_resumed = auto_resumed


#: Set when the worker receives SIGTERM/SIGINT so ffmpeg failures and long
#: loops stop at a safe point instead of blaming the user's audio file.
_shutting_down = threading.Event()
#: Set by gunicorn.conf.py ``on_starting`` in the arbiter before it forks, so
#: every worker of one server generation (including a worker respawned
#: mid-life after a timeout) shares the same start time. Unset for scripts
#: that import app (ops one-offs, tests).
_SERVER_STARTED_AT_ENV = os.getenv('PODSKRIFT_SERVER_STARTED_AT', '').strip()
try:
    _PROCESS_STARTED_AT = float(_SERVER_STARTED_AT_ENV) if _SERVER_STARTED_AT_ENV else time.time()
except ValueError:
    _SERVER_STARTED_AT_ENV = ''
    _PROCESS_STARTED_AT = time.time()
_shutdown_handlers_installed = False


def is_shutting_down():
    return _shutting_down.is_set()


def request_shutdown(signum=None, frame=None):
    """Mark this process as shutting down (gunicorn SIGTERM / Ctrl-C)."""
    _shutting_down.set()
    if signum is not None:
        app.logger.info('shutdown signal %s received; stopping transcription work',
                        signum)


def install_shutdown_handlers():
    """Chain SIGTERM/SIGINT so we set the shutdown flag without replacing gunicorn.

    Gunicorn's gthread worker calls ``init_signals`` *after* loading the app, so
    a handler installed at import time is overwritten. We install from the first
    request (and from ``post_worker_init`` when using gunicorn.conf.py), wrapping
    whatever handler is already registered.
    """
    global _shutdown_handlers_installed
    if os.getenv('PODSKRIFT_DISABLE_SHUTDOWN_HANDLERS', '').strip().lower() in (
            '1', 'true', 'yes'):
        return False
    wrapped_any = False
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            prev = signal.getsignal(sig)
            if getattr(prev, '_podskrift_shutdown', False):
                continue

            def handler(signum, frame, _prev=prev):
                request_shutdown(signum, frame)
                if callable(_prev) and _prev not in (signal.SIG_DFL, signal.SIG_IGN):
                    _prev(signum, frame)

            handler._podskrift_shutdown = True
            signal.signal(sig, handler)
            wrapped_any = True
        except (ValueError, OSError):
            # Not the main thread, or signals unsupported — ignore.
            pass
    if wrapped_any:
        _shutdown_handlers_installed = True
    return wrapped_any


def _ffmpeg_killed_by_signal(returncode, stderr=b''):
    """True when ffmpeg exited because the process/host sent it a kill signal."""
    if returncode is None:
        return False
    if returncode < 0:
        return True
    # 128 + signal number (shell convention): 143=SIGTERM, 137=SIGKILL, 130=SIGINT
    if returncode in (130, 137, 143, 255):
        return True
    detail = (stderr or b'').decode('utf-8', 'replace').lower()
    return any(
        token in detail
        for token in ('signal 15', 'signal 9', 'sigterm', 'sigkill', 'interrupted')
    )


def _raise_if_ffmpeg_shutdown(returncode, stderr=b''):
    """Raise ServerRestart when ffmpeg died from our shutdown or a kill signal."""
    if is_shutting_down() or _ffmpeg_killed_by_signal(returncode, stderr):
        raise ServerRestart(auto_resumed=True)


def trial_available():
    """Is there a trial to hand out at all?"""
    return bool(TRIAL_ENABLED and GLOBAL_OPENAI_KEY and TRIAL_DEFAULT_SECONDS > 0)


def advertised_trial_minutes():
    """Minutes promised to new signups on marketing surfaces, or None when off.

    Distinct from TRIAL_DEFAULT_SECONDS (NULL-limit fallback for existing rows).
    Marketing quotes NEW_USER_TRIAL_MINUTES; after signup, user-facing copy
    must use the account's stamped trial_seconds_limit (see trial_status).
    """
    if not trial_available() or NEW_USER_TRIAL_SECONDS <= 0:
        return None
    return NEW_USER_TRIAL_SECONDS // 60


def assign_trial_variant(user_id):
    """Deterministic (variant_label, seconds) for a new signup.

    When TRIAL_SPLIT_ENABLED, hash the user id into TRIAL_SPLIT_VARIANTS
    (stable across processes). When off, return (None, NEW_USER_TRIAL_SECONDS)
    so legacy behaviour and NULL trial_variant are preserved.
    """
    if not TRIAL_SPLIT_ENABLED or not TRIAL_SPLIT_VARIANTS:
        return None, NEW_USER_TRIAL_SECONDS
    digest = hashlib.sha256(
        f'podskrift-trial-split:{int(user_id)}'.encode('utf-8')
    ).digest()
    idx = int.from_bytes(digest[:8], 'big') % len(TRIAL_SPLIT_VARIANTS)
    label = TRIAL_SPLIT_VARIANTS[idx]
    return label, int(label) * 60


def user_trial_variant(user):
    """Stored trial_variant string, or None."""
    if user is None:
        return None
    raw = getattr(user, 'trial_variant', None)
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def trial_variant_props(user_or_id):
    """{'trial_variant': …} for analytics, or {} when unknown / unset."""
    user = user_or_id
    if isinstance(user_or_id, int):
        user = db.session.get(User, user_or_id)
    variant = user_trial_variant(user)
    if not variant:
        return {}
    return {'trial_variant': variant}


def trial_oslo_day_str(when=None):
    """Europe/Oslo calendar day as YYYY-MM-DD.

    ``when`` is a timezone-aware or naive-UTC datetime, or an ISO/SQLite
    datetime string; default is now.
    """
    if when is None:
        when = datetime.now(timezone.utc)
    elif isinstance(when, str):
        raw = when.strip().replace('Z', '+00:00')
        try:
            when = datetime.fromisoformat(raw)
        except ValueError:
            return trial_oslo_day_str(None)
    if isinstance(when, datetime) and when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    if isinstance(when, date) and not isinstance(when, datetime):
        return when.isoformat()
    return when.astimezone(TRIAL_BUDGET_TZ).date().isoformat()


def trial_oslo_day_bounds_naive_utc(day=None):
    """Half-open [start, end) naive-UTC datetimes for an Oslo calendar day.

    SQLite stores naive UTC timestamps; compare with these bounds.
    ``day`` is a YYYY-MM-DD string or ``date``; default is today in Oslo.
    """
    if day is None:
        day = trial_oslo_day_str()
    if isinstance(day, str):
        day = date.fromisoformat(day)
    start = datetime(day.year, day.month, day.day, tzinfo=TRIAL_BUDGET_TZ).astimezone(
        timezone.utc).replace(tzinfo=None)
    end = start + timedelta(days=1)
    return start, end


def trial_daily_used_seconds(day=None):
    """Trial seconds reserved/consumed against the Oslo-day budget ledger."""
    day = day or trial_oslo_day_str()
    return int(db.session.execute(text(
        'SELECT COALESCE(seconds_used, 0) FROM trial_budget_days WHERE day = :day'
    ), {'day': day}).scalar() or 0)


def trial_daily_remaining_seconds(day=None):
    """Seconds still available on today's (or ``day``'s) shared free-trial budget."""
    if TRIAL_DAILY_SECONDS <= 0:
        return 0
    return max(0, TRIAL_DAILY_SECONDS - trial_daily_used_seconds(day))


def trial_daily_budget_available():
    """True when today's shared free-trial budget still has room.

    Used by /health (`trial_available`) and UI counters. Lifetime safety cap
    (TRIAL_GLOBAL_SECONDS) is separate and optional.
    """
    if not trial_available() or TRIAL_DAILY_SECONDS <= 0:
        return False
    return trial_daily_used_seconds() < TRIAL_DAILY_SECONDS


def trial_global_pool_available():
    """Back-compat alias: today's daily budget still has room."""
    return trial_daily_budget_available()


def trial_status(user):
    """(limit, used, remaining) trial seconds for `user`."""
    limit = user.trial_seconds_limit
    if limit is None:
        limit = TRIAL_DEFAULT_SECONDS
    used = user.trial_seconds_used or 0
    return limit, used, max(0, limit - used)


def trial_refusal_scope(user, needed_seconds):
    """Which cap refused a reservation: 'user', 'daily', or 'global'."""
    _, _, remaining = trial_status(user)
    if remaining < needed_seconds:
        return 'user'
    if TRIAL_DAILY_SECONDS > 0 and trial_daily_remaining_seconds() < needed_seconds:
        return 'daily'
    return 'global'


def trial_global_used_seconds():
    """Lifetime trial seconds spent across every account (optional safety cap)."""
    return int(db.session.execute(text(
        'SELECT COALESCE(SUM(trial_seconds_used), 0) FROM users'
    )).scalar() or 0)


def _ensure_trial_budget_day(day):
    """Insert the ledger row for ``day`` if missing. Safe under worker races."""
    existing = db.session.get(TrialBudgetDay, day)
    if existing is not None:
        return
    try:
        db.session.add(TrialBudgetDay(day=day, seconds_used=0))
        db.session.commit()
    except Exception:  # noqa: BLE001 — race with another worker
        db.session.rollback()


def _trial_budget_day_for_timestamp(ts):
    """Oslo day string for a task ``started_at`` (naive UTC or aware)."""
    if ts is None:
        return trial_oslo_day_str()
    return trial_oslo_day_str(ts)


def resolve_openai_key(user):
    """Return (key, source) where source is 'user', 'trial', or None.

    A user's own key always wins -- it costs us nothing and has no cap.
    Otherwise the platform key is used when the free trial is on OR the user
    still has paid credit-pack minutes (paid runs on the same platform key).
    """
    own = getattr(user, 'openai_api_key', None) if user is not None else None
    if own:
        return own, 'user'
    paid = paid_balance_seconds(user) if user is not None else 0
    if GLOBAL_OPENAI_KEY and (trial_available() or paid > 0):
        return GLOBAL_OPENAI_KEY, 'trial'
    return None, None


def paid_balance_seconds(user):
    """Paid credit-pack seconds remaining for `user`."""
    if user is None:
        return 0
    return max(0, int(getattr(user, 'paid_seconds_balance', 0) or 0))


def _balance_analytics_props(user_id):
    """trial/paid minutes left for analytics (who is close to the paywall).

    Never raises: analytics must not affect a finished job.
    """
    try:
        user = db.session.get(User, int(user_id))
        if user is None:
            return {}
        props = {'paid_remaining_min': paid_balance_seconds(user) // 60}
        if not getattr(user, 'openai_api_key', None):
            props['trial_remaining_min'] = trial_status(user)[2] // 60
        return props
    except Exception:  # noqa: BLE001
        return {}


def trial_reserve(user_id, seconds, *, budget_day=None):
    """Atomically reserve `seconds` of allowance. True only if granted.

    Podskrift runs two gunicorn workers, so a threading.Lock would guard one
    process and let the other one through. The per-user cap, optional lifetime
    ceiling, and today's daily budget are evaluated inside one users UPDATE;
    the daily ledger bump then runs in the same transaction before commit.
    SQLite holds the write lock until commit, so parallel starts cannot both
    be told there is room that only one of them can have.
    """
    seconds = int(math.ceil(seconds))
    if seconds <= 0:
        return True
    # Daily budget of 0 (or unset kill) means no free trial room at all.
    if TRIAL_DAILY_SECONDS <= 0:
        return False
    day = budget_day or trial_oslo_day_str()
    _ensure_trial_budget_day(day)

    params = {
        'n': seconds,
        'uid': user_id,
        'default_limit': TRIAL_DEFAULT_SECONDS,
        'day': day,
        'daily_limit': TRIAL_DAILY_SECONDS,
    }
    lifetime_clause = ''
    if TRIAL_GLOBAL_SECONDS > 0:
        lifetime_clause = """
           AND (SELECT COALESCE(SUM(trial_seconds_used), 0) FROM users) + :n
               <= :global_limit
        """
        params['global_limit'] = TRIAL_GLOBAL_SECONDS

    result = db.session.execute(text(f"""
        UPDATE users
           SET trial_seconds_used = COALESCE(trial_seconds_used, 0) + :n
         WHERE id = :uid
           AND COALESCE(trial_seconds_used, 0) + :n
               <= COALESCE(trial_seconds_limit, :default_limit)
           AND (SELECT COALESCE(seconds_used, 0) FROM trial_budget_days
                 WHERE day = :day) + :n <= :daily_limit
           {lifetime_clause}
    """), params)
    if result.rowcount != 1:
        db.session.commit()
        return False

    db.session.execute(text("""
        UPDATE trial_budget_days
           SET seconds_used = seconds_used + :n
         WHERE day = :day
    """), {'n': seconds, 'day': day})
    db.session.commit()
    return True


def trial_release(user_id, seconds, *, budget_day=None):
    """Hand back reserved seconds that were never spent.

    ``budget_day`` is the Oslo day the reservation counted against (task
    ``started_at``); default is today.
    """
    seconds = int(seconds)
    if seconds <= 0:
        return
    day = budget_day or trial_oslo_day_str()
    db.session.execute(text("""
        UPDATE users
           SET trial_seconds_used = MAX(0, COALESCE(trial_seconds_used, 0) - :n)
         WHERE id = :uid
    """), {'n': seconds, 'uid': user_id})
    if TRIAL_DAILY_SECONDS > 0:
        db.session.execute(text("""
            UPDATE trial_budget_days
               SET seconds_used = MAX(0, seconds_used - :n)
             WHERE day = :day
        """), {'n': seconds, 'day': day})
    db.session.commit()


def paid_reserve(user_id, seconds):
    """Atomically debit paid credit-pack seconds. True only if granted.

    Not subject to the free-trial daily budget or lifetime ceiling — the user
    already paid.
    """
    seconds = int(math.ceil(seconds))
    if seconds <= 0:
        return True
    result = db.session.execute(text("""
        UPDATE users
           SET paid_seconds_balance = COALESCE(paid_seconds_balance, 0) - :n
         WHERE id = :uid
           AND COALESCE(paid_seconds_balance, 0) >= :n
    """), {'n': seconds, 'uid': user_id})
    db.session.commit()
    return result.rowcount == 1


def paid_release(user_id, seconds):
    """Hand back paid seconds that were reserved but not spent."""
    seconds = int(seconds)
    if seconds <= 0:
        return
    db.session.execute(text("""
        UPDATE users
           SET paid_seconds_balance = COALESCE(paid_seconds_balance, 0) + :n
         WHERE id = :uid
    """), {'n': seconds, 'uid': user_id})
    db.session.commit()


def platform_reserve(user_id, seconds, *, paid_only=False, budget_day=None):
    """Reserve platform minutes: free trial first, then paid.

    Returns (trial_seconds, paid_seconds) on success, or None if the full
    amount cannot be covered. `paid_only` skips the free trial (used for
    episodes over the free per-episode cap — those must not burn free minutes).

    Paid minutes are never limited by the free-trial daily budget or lifetime
    ceiling.
    """
    seconds = int(math.ceil(seconds))
    if seconds <= 0:
        return (0, 0)

    if paid_only:
        return (0, seconds) if paid_reserve(user_id, seconds) else None

    if trial_reserve(user_id, seconds, budget_day=budget_day):
        return (seconds, 0)

    user = db.session.get(User, user_id)
    if user is None:
        return None
    _, _, trial_rem = trial_status(user)
    daily_rem = trial_daily_remaining_seconds(budget_day)
    if TRIAL_GLOBAL_SECONDS > 0:
        lifetime_rem = max(0, TRIAL_GLOBAL_SECONDS - trial_global_used_seconds())
    else:
        lifetime_rem = trial_rem + seconds  # no lifetime cap
    trial_take = min(seconds, trial_rem, daily_rem, lifetime_rem)
    if trial_take > 0 and not trial_reserve(
            user_id, trial_take, budget_day=budget_day):
        trial_take = 0
    paid_need = seconds - trial_take
    if paid_need > 0 and not paid_reserve(user_id, paid_need):
        if trial_take:
            trial_release(user_id, trial_take, budget_day=budget_day)
        return None
    return (trial_take, paid_need)


def platform_release(user_id, trial_seconds, paid_seconds, *, budget_day=None):
    """Hand back a platform reservation that never reached Whisper spend."""
    trial_release(user_id, trial_seconds or 0, budget_day=budget_day)
    paid_release(user_id, paid_seconds or 0)


def _claim_task_charge(task_id, expected, new):
    """Move a task's reserved amount from `expected` to `new`. True if we won.

    The worker thread and the stale sweeper can both try to settle the same
    task; only the one that moves this column may move the user's balance.
    """
    result = db.session.execute(text("""
        UPDATE transcription_tasks
           SET trial_seconds_charged = :new
         WHERE id = :tid AND trial_settled = 0 AND trial_seconds_charged = :expected
    """), {'tid': task_id, 'new': int(new), 'expected': int(expected)})
    db.session.commit()
    return result.rowcount == 1


def _claim_task_platform_charges(task_id, expected_trial, expected_paid,
                                 new_trial, new_paid, settle=False):
    """Atomically rewrite trial+paid charges (and optionally settle). True if won."""
    settled_clause = ', trial_settled = 1' if settle else ''
    # Compare with COALESCE so NULL paid reads as 0 for own-key-adjacent rows.
    result = db.session.execute(text(f"""
        UPDATE transcription_tasks
           SET trial_seconds_charged = :new_trial,
               paid_seconds_charged = :new_paid
               {settled_clause}
         WHERE id = :tid AND trial_settled = 0
           AND COALESCE(trial_seconds_charged, 0) = :exp_trial
           AND COALESCE(paid_seconds_charged, 0) = :exp_paid
    """), {
        'tid': task_id,
        'new_trial': int(new_trial),
        'new_paid': int(new_paid),
        'exp_trial': int(expected_trial or 0),
        'exp_paid': int(expected_paid or 0),
    })
    db.session.commit()
    return result.rowcount == 1


def _pro_rata_platform_spend(trial_charged, paid_charged, chunk_total, chunk_index):
    """Return (spent_trial, spent_paid, refund_trial, refund_paid).

    chunk_index is written immediately BEFORE that chunk is uploaded, so
    index k means k+1 chunks have been sent and billed to us. Rounding
    toward charging is deliberate: refunding a chunk that did reach Whisper
    is exactly how a swept single-chunk episode -- every episode under 24 MB,
    so the common case -- came out free.
    """
    trial_charged = int(trial_charged or 0)
    paid_charged = int(paid_charged or 0)
    total = trial_charged + paid_charged
    if total <= 0:
        return 0, 0, 0, 0
    if (chunk_total or 0) > 0 and chunk_index is not None:
        started = min(int(chunk_total), max(0, int(chunk_index)) + 1)
        spent = int(total * started / chunk_total)
    else:
        spent = 0  # nothing reached Whisper yet
    spent_trial = min(trial_charged, spent)
    spent_paid = spent - spent_trial
    return spent_trial, spent_paid, trial_charged - spent_trial, paid_charged - spent_paid


def trial_refund_task(task):
    """Refund the part of a failed task we did not actually spend.

    Chunks already sent to Whisper were billed to us whatever happens to the
    task afterwards, so refunding the whole reservation would hand back money
    that is gone -- and the stale sweeper fires on tasks whose worker is often
    several chunks in. Refund the unstarted remainder instead, pro-rata on
    chunk progress. Safe to call repeatedly: the conditional UPDATE on the
    task is what decides which caller may move the balance.

    Spend is applied to free trial first, then paid (matching charge order).
    Unspent minutes are released to the same buckets (and the Oslo day the
    task was started on for the shared daily budget).
    """
    # Read the row rather than trusting the caller's copy. /status hands us an
    # object loaded at the top of the request; if the worker reconciled the
    # charge in between, a claim against the stale value silently matches
    # nothing and the user forfeits the allowance with no path to get it back.
    row = db.session.execute(text(
        'SELECT user_id, trial_seconds_charged, paid_seconds_charged, '
        'chunk_total, chunk_index, trial_settled, started_at '
        'FROM transcription_tasks WHERE id = :tid'
    ), {'tid': task.id}).first()
    if row is None:
        return 0
    (user_id, trial_charged, paid_charged, chunk_total, chunk_index,
     settled, started_at) = row
    trial_charged = int(trial_charged or 0)
    paid_charged = int(paid_charged or 0)
    if settled or (trial_charged + paid_charged) <= 0:
        return 0

    spent_trial, spent_paid, refund_trial, refund_paid = _pro_rata_platform_spend(
        trial_charged, paid_charged, chunk_total, chunk_index)

    # Settling is the claim, and it also pins the amounts we read.
    if not _claim_task_platform_charges(
            task.id, trial_charged, paid_charged, spent_trial, spent_paid,
            settle=True):
        return 0
    platform_release(
        user_id, refund_trial, refund_paid,
        budget_day=_trial_budget_day_for_timestamp(started_at))
    return refund_trial + refund_paid


def fail_task_and_refund(task_id, error_message):
    """Mark a task failed and settle its reservation before the failure is visible.

    When the task still holds a platform charge, one conditional UPDATE writes
    ``status='error'``, the settled pro-rata charges, and ``trial_settled=1``
    together — so a failed task is never observed with minutes still reserved.
    The user-balance release runs only if that claim wins (same idempotency as
    ``trial_refund_task``: a later call cannot double-credit).

    If there is nothing to settle, or another worker already settled / terminalised
    the row, still ensures ``status='error'`` (without clobbering completed /
    cancelled) and calls ``trial_refund_task`` as a no-op backstop.
    """
    row = db.session.execute(text(
        'SELECT user_id, trial_seconds_charged, paid_seconds_charged, '
        'chunk_total, chunk_index, trial_settled, started_at '
        'FROM transcription_tasks WHERE id = :tid'
    ), {'tid': task_id}).first()
    if row is None:
        return 0
    (user_id, trial_charged, paid_charged, chunk_total, chunk_index,
     settled, started_at) = row
    trial_charged = int(trial_charged or 0)
    paid_charged = int(paid_charged or 0)
    total_charged = trial_charged + paid_charged
    budget_day = _trial_budget_day_for_timestamp(started_at)

    if not settled and total_charged > 0:
        spent_trial, spent_paid, refund_trial, refund_paid = _pro_rata_platform_spend(
            trial_charged, paid_charged, chunk_total, chunk_index)
        now = datetime.now(timezone.utc)
        # One transaction: task settle+error and user-balance release commit
        # together, so neither "error with charge" nor "settled task / still
        # debiting the user" is observable.
        result = db.session.execute(text("""
            UPDATE transcription_tasks
               SET status = 'error', phase = 'error', error_message = :message,
                   trial_seconds_charged = :new_trial,
                   paid_seconds_charged = :new_paid,
                   trial_settled = 1,
                   heartbeat_at = :now
             WHERE id = :tid AND trial_settled = 0
               AND COALESCE(trial_seconds_charged, 0) = :exp_trial
               AND COALESCE(paid_seconds_charged, 0) = :exp_paid
               AND status NOT IN ('completed', 'error', 'cancelled')
        """), {
            'tid': task_id,
            'message': error_message,
            'new_trial': int(spent_trial),
            'new_paid': int(spent_paid),
            'exp_trial': trial_charged,
            'exp_paid': paid_charged,
            'now': now,
        })
        if result.rowcount == 1:
            if refund_trial:
                db.session.execute(text("""
                    UPDATE users
                       SET trial_seconds_used = MAX(
                           0, COALESCE(trial_seconds_used, 0) - :n)
                     WHERE id = :uid
                """), {'n': int(refund_trial), 'uid': user_id})
                if TRIAL_DAILY_SECONDS > 0:
                    _ensure_trial_budget_day(budget_day)
                    db.session.execute(text("""
                        UPDATE trial_budget_days
                           SET seconds_used = MAX(0, seconds_used - :n)
                         WHERE day = :day
                    """), {'n': int(refund_trial), 'day': budget_day})
            if refund_paid:
                db.session.execute(text("""
                    UPDATE users
                       SET paid_seconds_balance = COALESCE(
                           paid_seconds_balance, 0) + :n
                     WHERE id = :uid
                """), {'n': int(refund_paid), 'uid': user_id})
            db.session.commit()
            return refund_trial + refund_paid
        db.session.rollback()

    # Nothing charged, already settled/terminal, or lost the joint claim:
    # settle first (idempotent), then surface error so status='error' is never
    # committed ahead of the refund on this fall-through path either.
    task = db.session.get(TranscriptionTask, task_id)
    refunded = trial_refund_task(task) if task is not None else 0
    db.session.execute(text("""
        UPDATE transcription_tasks
           SET status = 'error', phase = 'error', error_message = :message,
               heartbeat_at = :now
         WHERE id = :tid AND status NOT IN ('completed', 'cancelled')
    """), {
        'tid': task_id,
        'message': error_message,
        'now': datetime.now(timezone.utc),
    })
    db.session.commit()
    return refunded


def settle_stranded_charges():
    """Settle charges on tasks that failed without anyone refunding them.

    A worker killed between reconciling a charge and settling it leaves a task
    that is already 'error' with an unsettled charge. The orphan sweep never
    revisits it -- that only looks at tasks still running -- so the user would
    forfeit those minutes for good.

    Returns how many stranded tasks it found, not how many it settled: both
    gunicorn workers run this at boot and see the same rows, and the
    conditional UPDATE inside trial_refund_task decides which one wins each.
    The count is for logging and tests; nothing branches on it.
    """
    stranded = TranscriptionTask.query.filter(
        TranscriptionTask.status.in_(TERMINAL_STATUSES),
        TranscriptionTask.trial_settled == False,      # noqa: E712 - SQL, not Python
        sa_or_(
            TranscriptionTask.trial_seconds_charged > 0,
            TranscriptionTask.paid_seconds_charged > 0,
        ),
    ).all()
    for task in stranded:
        trial_refund_task(task)
    return len(stranded)


def trial_reconcile_task(task_id, actual_seconds):
    """Match a task's reservation to the audio we actually downloaded.

    Runs after the download but BEFORE the first Whisper call, so an episode
    that turns out longer than the feed claimed costs us bandwidth, never API
    spend. Raises TrialExhausted when the real length will not fit — except for
    partial-preview jobs (and trial-only unknown-duration overruns), which
    return ``'trim'`` so the caller shortens the file to the reservation.

    Charge order: free trial first, then paid. Episodes over the free
    per-episode cap must be covered by paid minutes (or BYOK) — they do not
    burn free trial, and paid is not subject to the free-trial daily budget.
    """
    task = db.session.get(TranscriptionTask, task_id)
    if not task or task.trial_settled:
        return None
    trial_reserved = int(task.trial_seconds_charged or 0)
    paid_reserved = int(task.paid_seconds_charged or 0)
    if trial_reserved <= 0 and paid_reserved <= 0:
        # NULL/0 on both is an own-key task, nothing metered. Settled means the
        # sweeper got here first -- re-opening the charge would bill the user
        # for an episode that goes on to send nothing.
        return None
    total_reserved = trial_reserved + paid_reserved
    actual = int(math.ceil(max(0.0, actual_seconds or 0.0)))
    user_id = task.user_id
    budget_day = _trial_budget_day_for_timestamp(task.started_at)
    over_free_cap = bool(
        TRIAL_MAX_EPISODE_SECONDS and actual > TRIAL_MAX_EPISODE_SECONDS)
    is_partial = task_is_partial(task)

    if over_free_cap and paid_reserved <= 0 and trial_reserved > 0:
        # Partial previews intentionally reserve only N minutes of a longer
        # episode — do not try to switch the whole thing onto paid credits.
        if is_partial:
            return 'trim'
        # Started as a free-trial job; real audio exceeds the free per-episode
        # cap. Paid credits can still save it if they cover the full length.
        #
        # Critical order: debit paid WHILE still holding the trial reservation.
        # Releasing trial first, then failing the paid debit, then re-reserving
        # trial was a race: a concurrent job could take the freed minutes, the
        # re-reserve would fail, and writing trial_seconds_charged anyway would
        # invent a charge the balance never held — so the later refund would
        # hand the user free minutes they never had.
        estimate_min = actual // 60
        cost = openai_whisper_cost_usd(estimate_min)
        owner = db.session.get(User, user_id)
        refuse_msg = (
            f'This episode is {estimate_min} minutes — too long for the free '
            f'trial (max {TRIAL_MAX_EPISODE_SECONDS // 60}). '
            f'{CREDIT_PACK_LABEL if stripe_checkout_enabled() else "Buy more minutes"}, '
            f'or add your own OpenAI API key (about ${cost} at OpenAI\'s rate).'
        )
        if paid_balance_seconds(owner) >= actual:
            before_trial, before_paid = _platform_remaining_seconds(user_id)
            if not paid_reserve(user_id, actual):
                # Race: paid balance moved after the read. Keep the trial charge.
                raise TrialExhausted(refuse_msg, scope='episode_length')
            if not _claim_task_platform_charges(
                    task_id, trial_reserved, 0, 0, actual):
                # Someone else settled (or the charge moved). Hand the paid debit
                # back; the trial reservation on the user balance is still intact
                # and still matches trial_seconds_charged on the row.
                paid_release(user_id, actual)
                raise TrialExhausted(refuse_msg, scope='episode_length')
            # Paid is now on the task; only now release the free-trial reservation.
            trial_release(user_id, trial_reserved, budget_day=budget_day)
            _capture_minutes_exhausted_if_depleted(
                user_id, 'reconcile', before_trial, before_paid)
            return None
        # No paid cover: trim to the reservation (unknown / under-claimed
        # duration) rather than failing after the download already happened.
        set_task_partial_meta(task, total_reserved, actual)
        db.session.commit()
        return 'trim'

    if actual > total_reserved:
        extra = actual - total_reserved
        before_trial, before_paid = _platform_remaining_seconds(user_id)
        split = platform_reserve(
            user_id, extra, paid_only=over_free_cap, budget_day=budget_day)
        if split is None:
            # Partial job, or trial-only unknown-duration overrun: keep the
            # reservation and let the worker trim audio to it.
            if is_partial or (paid_reserved <= 0 and trial_reserved > 0
                              and not over_free_cap):
                if not is_partial:
                    set_task_partial_meta(task, total_reserved, actual)
                    db.session.commit()
                return 'trim'
            owner = db.session.get(User, user_id)
            estimate_min = actual // 60
            _, _, remaining = trial_status(owner) if owner else (0, 0, 0)
            cost = openai_whisper_cost_usd(estimate_min)
            if over_free_cap:
                raise TrialExhausted(
                    f'This episode is {estimate_min} minutes — longer than the paid '
                    f'minutes you have left. {CREDIT_PACK_LABEL}, or add your own '
                    f'OpenAI API key (about ${cost} for this one, billed by OpenAI).',
                    scope='user',
                )
            buy_bit = (
                f'{CREDIT_PACK_LABEL}, or '
                if stripe_checkout_enabled() else ''
            )
            raise TrialExhausted(
                f'This episode is about {estimate_min} minutes — longer than the '
                f'{remaining // 60} free minutes you have left. Pick a shorter '
                f'episode, {buy_bit}or add your own OpenAI API key (about ${cost} '
                f'for this one, billed by OpenAI).',
                scope=trial_refusal_scope(owner, extra) if owner else 'user',
            )
        extra_trial, extra_paid = split
        new_trial = trial_reserved + extra_trial
        new_paid = paid_reserved + extra_paid
        if not _claim_task_platform_charges(
                task_id, trial_reserved, paid_reserved, new_trial, new_paid):
            platform_release(
                user_id, extra_trial, extra_paid, budget_day=budget_day)
        else:
            _capture_minutes_exhausted_if_depleted(
                user_id, 'reconcile', before_trial, before_paid)
        return None
    if actual < total_reserved:
        # Shrink paid first (LIFO), then trial — reverse of charge order.
        shrink = total_reserved - actual
        new_paid = max(0, paid_reserved - shrink)
        shrink_paid = paid_reserved - new_paid
        shrink_trial = shrink - shrink_paid
        new_trial = trial_reserved - shrink_trial
        if _claim_task_platform_charges(
                task_id, trial_reserved, paid_reserved, new_trial, new_paid):
            platform_release(
                user_id, shrink_trial, shrink_paid, budget_day=budget_day)
        # Keep partial meta in sync with the shorter reservation.
        if is_partial:
            meta = task_partial_meta(task)
            if meta:
                set_task_partial_meta(
                    task, new_trial + new_paid, meta['episode_seconds'])
                db.session.commit()
    return None


# ---------------------------------------------------------------------------
# Abuse limits
# ---------------------------------------------------------------------------

#: Registrations allowed from one IP per window. 7 of 23 production accounts
#: were bot signups on a single throwaway domain, two of them in the same
#: second, because /register had no verification, rate limit or captcha.
REGISTER_MAX_PER_IP = 3
#: Peers whose X-Real-IP we trust. Plesk's nginx proxies over loopback and
#: sets X-Real-IP to $remote_addr, overwriting anything the client sent.
TRUSTED_PROXY_ADDRESSES = frozenset({'127.0.0.1', '::1'})
REGISTER_WINDOW_SECONDS = 3600
DISPOSABLE_EMAIL_DOMAINS = {
    'immenseignite.info',
}

_register_attempts = collections.defaultdict(list)
_register_lock = threading.Lock()

#: Share-link creates per user per window. Tokens are unguessable; this only
#: bounds accidental/abusive minting, not enumeration.
SHARE_CREATE_MAX_PER_USER = 20
SHARE_CREATE_WINDOW_SECONDS = 3600
#: 22 bytes → 176 bits of entropy (url-safe); requirement is >= 128-bit.
SHARE_TOKEN_BYTES = 22
UTM_SESSION_KEY = '_utm_source'
#: First human pageview in this browser session (path + Referer), for signup
#: attribution. Written once; coarse referrer bucket only reaches PostHog.
FIRST_LANDING_SESSION_KEY = '_first_landing'
FIRST_REFERRER_SESSION_KEY = '_first_referrer'

_share_create_attempts = collections.defaultdict(list)
_share_create_lock = threading.Lock()

#: Password-reset requests per email and per IP per hour. Same neutral UI
#: whether the address exists; the limit only throttles send/lookup work.
PASSWORD_RESET_MAX_PER_KEY = 5
PASSWORD_RESET_WINDOW_SECONDS = 3600
PASSWORD_RESET_TOKEN_BYTES = 32  # secrets.token_urlsafe(32)
PASSWORD_RESET_TTL_SECONDS = 60 * 60
#: Pad forgot-password responses so existence checks are harder to time.
PASSWORD_RESET_MIN_RESPONSE_SEC = 0.25
PASSWORD_RESET_NEUTRAL_MSG = (
    "If an account exists for that email, we've sent a reset link."
)

_password_reset_attempts = collections.defaultdict(list)
_password_reset_lock = threading.Lock()


def _client_ip():
    """Real client IP, from a source the client cannot forge.

    Deliberately does NOT read X-Forwarded-For[0]: Plesk nginx uses
    $proxy_add_x_forwarded_for, which appends the real peer to whatever the
    client sent, so an attacker-supplied value stays first and the rate limit
    can be bypassed by rotating the header. X-Real-IP is set by nginx to
    $remote_addr and overwrites any client-supplied value.
    """
    peer = request.remote_addr
    # Only a request that actually came through the local proxy may present a
    # forwarded address. Otherwise the header is just as attacker-controlled as
    # X-Forwarded-For was, and swapping one for the other fixes nothing.
    if peer in TRUSTED_PROXY_ADDRESSES:
        return request.headers.get('X-Real-IP') or peer
    return peer or 'unknown'


def register_reserve_slot(ip):
    """Atomically take one signup slot for this IP.

    Returns a release token, or None when no slots are left.

    Checking and recording must happen under one lock: with them split, a
    parallel burst from one address passed the check before any of it was
    recorded, and the limit did not bind at all. Callers MUST release the slot
    again if no account gets created, so a mistyped password costs nothing.
    """
    now = time.time()
    with _register_lock:
        seen = [t for t in _register_attempts.get(ip, ())
                if now - t[0] < REGISTER_WINDOW_SECONDS]
        if len(seen) >= REGISTER_MAX_PER_IP:
            _register_attempts[ip] = seen
            return None
        # A unique token, so a release removes its OWN reservation rather than
        # whichever is newest -- otherwise retained stamps skew older and the
        # window expires early.
        token = (now, uuid.uuid4().hex)
        seen.append(token)
        _register_attempts[ip] = seen
        if len(_register_attempts) > 10000:
            stale = [k for k, v in list(_register_attempts.items())
                     if not v or now - v[-1][0] > REGISTER_WINDOW_SECONDS]
            for k in stale:
                _register_attempts.pop(k, None)
        return token


def register_release_slot(ip, token):
    """Give back a reservation, for any attempt that did not create an account."""
    if token is None:
        return
    with _register_lock:
        held = _register_attempts.get(ip)
        if not held:
            return
        try:
            held.remove(token)
        except ValueError:
            return          # already pruned by the window
        if not held:
            _register_attempts.pop(ip, None)


def _password_reset_rate_limited(*keys):
    """True if any key has already hit PASSWORD_RESET_MAX_PER_KEY this hour.

    Records a hit for every key when under the limit so email and IP share one
    atomic check-and-record step (same hole register used to have when split).
    """
    now = time.time()
    with _password_reset_lock:
        for key in keys:
            if not key:
                continue
            seen = [t for t in _password_reset_attempts.get(key, ())
                    if now - t < PASSWORD_RESET_WINDOW_SECONDS]
            if len(seen) >= PASSWORD_RESET_MAX_PER_KEY:
                _password_reset_attempts[key] = seen
                return True
        for key in keys:
            if not key:
                continue
            seen = [t for t in _password_reset_attempts.get(key, ())
                    if now - t < PASSWORD_RESET_WINDOW_SECONDS]
            seen.append(now)
            _password_reset_attempts[key] = seen
        if len(_password_reset_attempts) > 10000:
            stale = [k for k, v in list(_password_reset_attempts.items())
                     if not v or now - v[-1] > PASSWORD_RESET_WINDOW_SECONDS]
            for k in stale:
                _password_reset_attempts.pop(k, None)
        return False


def _password_reset_pad(started_at):
    """Sleep so forgot-password responses take a similar time when not testing."""
    if app.config.get('TESTING'):
        return
    minimum = PASSWORD_RESET_MIN_RESPONSE_SEC
    if minimum <= 0:
        return
    elapsed = time.monotonic() - started_at
    if elapsed < minimum:
        time.sleep(minimum - elapsed)


def _hash_password_reset_token(token: str) -> str:
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def is_disposable_email(email):
    return email.rsplit('@', 1)[-1].lower() in DISPOSABLE_EMAIL_DOMAINS


# ---------------------------------------------------------------------------
# API key handling
# ---------------------------------------------------------------------------

def _is_openai_error(exc):
    """True for exceptions raised by the OpenAI SDK, which must never be shown raw."""
    return type(exc).__module__.split('.')[0] == 'openai'


def pack_covers_episode_line(estimate_min):
    """Episode-aware paywall subline when Stripe pack is available."""
    if not estimate_min or estimate_min <= 0:
        return ''
    minutes = int(estimate_min)
    price = f'{CREDIT_PACK_AMOUNT_CENTS / 100:.0f}'
    return (
        f'This episode is {minutes} min — {CREDIT_PACK_MINUTES} min for '
        f'${price} covers it and more.'
    )


def _own_key_pack_offer_suffix():
    """Appended to BYOK auth/billing errors when the credit pack is buyable."""
    if not stripe_checkout_enabled():
        return ''
    return f' {OWN_KEY_PACK_OFFER}.'


def describe_openai_error(exc, context='transcription', key_source=None):
    """Turn an OpenAI SDK exception into something a human can act on.

    Users were shown the raw error JSON, which is both unreadable and unsafe:
    OpenAI echoes the submitted key back in 401s, and people paste passwords
    into that field, so the raw text put a third party's password in our
    database. Never surface the provider's message verbatim.

    When `key_source` is ``'user'`` (BYOK), auth/billing copy also hints that
    removing the key falls back to Podskrift free or paid minutes, and (when
    Stripe is on) offers the credit pack so a zero-credit OpenAI account is
    not a dead end.
    """
    byok = key_source == 'user'
    remove_hint = (
        ' Or remove the key in Settings to use Podskrift free or paid minutes '
        'instead.'
    )
    pack_hint = _own_key_pack_offer_suffix() if byok else ''
    status = getattr(exc, 'status_code', None)
    if status == 401:
        msg = ('OpenAI rejected this key. It may have been deleted, or copied '
               'incompletely. Create a new one at platform.openai.com/api-keys '
               'and paste the whole thing.')
        if byok and context == 'transcription':
            return msg + remove_hint + pack_hint
        return msg
    if status == 429:
        code = product_analytics.openai_error_code(exc)
        if code == 'rate_limit_exceeded':
            if context == 'verify':
                return ('OpenAI rate-limited this check. Wait a moment and try '
                        'saving the key again.')
            return ('OpenAI rate-limited this request. Wait a minute, then use '
                    'Retry this episode. Nothing was charged by Podskrift.')
        if context == 'verify':
            # verify_openai_key replaces this with a fuller "Key saved — but…"
            # message; kept here so any other verify caller still gets billing help.
            return ('Your OpenAI account has no credit yet, so transcription won\'t '
                    'work. Add a payment method or prepaid credit at '
                    'platform.openai.com/account/billing.')
        msg = ('OpenAI refused the job: your OpenAI account has no credit left. '
               'Add credit at platform.openai.com/account/billing — it can take a '
               'minute to activate — then retry this episode.')
        if byok:
            msg += remove_hint + pack_hint
        return msg + ' Nothing was charged by Podskrift.'
    if status == 403:
        msg = ('Your OpenAI key is not allowed to use the Whisper API. Check its '
               'permissions at platform.openai.com.')
        if byok and context == 'transcription':
            return msg + remove_hint + pack_hint
        return msg
    if status and 500 <= status < 600:
        return 'OpenAI had a server error. Wait a moment and try again.'
    if isinstance(exc, APITimeoutError):
        if context == 'verify':
            return 'OpenAI did not respond in time. Try again in a moment.'
        return 'OpenAI did not respond in time. Try again, or pick a shorter episode.'
    if isinstance(exc, APIConnectionError):
        return 'Could not reach OpenAI. Check your connection and try again.'
    if context == 'verify':
        return 'Could not verify the key against OpenAI. Please try again.'
    return 'Transcription failed. Please try again.'


def looks_like_openai_key(key):
    """Cheap shape check, so obvious non-keys never reach OpenAI at all."""
    return bool(key) and key.startswith('sk-') and len(key) >= 20


#: Friendly copy when verify finds a key that authenticates but cannot bill Whisper.
OPENAI_NO_BILLING_SAVE_MSG = (
    'Key saved — but your OpenAI account has no credit yet, so '
    'transcription won\'t work. Add a payment method or prepaid credit '
    'at platform.openai.com/account/billing, then start your transcript.'
)

#: Checkout sources that mean "switch from a broken BYOK key to the pack".
BYOK_PACK_SOURCES = frozenset({'openai_no_billing', 'transcription_no_billing'})


def _silent_wav_bytes(duration_sec=1.0, sample_rate=16000):
    """~1s of mono PCM silence as WAV bytes (stdlib only; no ffmpeg)."""
    nframes = max(1, int(duration_sec * sample_rate))
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(b'\x00\x00' * nframes)
    return buf.getvalue()


def _probe_whisper_billing(client):
    """Minimal Whisper call — models.list() passes for zero-credit accounts."""
    audio = io.BytesIO(_silent_wav_bytes())
    # The SDK reads `.name` for the multipart filename / content type.
    audio.name = 'silence.wav'
    return client.audio.transcriptions.create(
        model='whisper-1',
        file=audio,
    )


def _openai_unreachable(exc):
    status = getattr(exc, 'status_code', None)
    return (status is not None and 500 <= status < 600) or isinstance(
        exc, (APIConnectionError, APITimeoutError))


def _is_no_billing_error(message):
    """True when our own error copy means OpenAI billing/quota, not rate limit."""
    if not message:
        return False
    msg = str(message).lower()
    if 'rate-limited' in msg or 'rate limit' in msg:
        return False
    return 'openai account has no credit' in msg or (
        'no credit' in msg and 'openai' in msg)


def verify_openai_key(key):
    """Check a key against OpenAI. Returns (ok, message, status).

    `status` is a coarse analytics tag, never key material:
      success — verified | no_billing | unverified_network
      failure — invalid_key | other (and rarely network-shaped rejects)

    Done at save time rather than at transcription time: previously the first
    signal that a key was wrong came minutes later, after picking an episode and
    waiting through a download. 15 of 16 production failures were this.

    models.list() alone is not enough: zero-credit accounts still list models,
    then fail the first Whisper job with 429/insufficient_quota. After list we
    probe with a ~1s silent WAV on whisper-1 (same model as production).
    """
    if key and str(key).startswith('psk_'):
        return False, (
            "That's a Podskrift developer key; paste your OpenAI key (starts with sk-)"
        ), 'invalid_key'
    if not looks_like_openai_key(key):
        return False, ('That\'s not an OpenAI API key. OpenAI keys start with "sk-" '
                       'and are created at platform.openai.com/api-keys. (Not your '
                       'OpenAI password, and not the Podskrift "psk_" key below.)'), 'invalid_key'
    try:
        client = OpenAI(api_key=key, timeout=15.0, max_retries=0)
        client.models.list()
    except Exception as e:
        # A 5xx or a connection failure means we could not CHECK the key, not
        # that OpenAI rejected it. Refusing the save there would make an OpenAI
        # outage look like the user's key is broken.
        if _openai_unreachable(e):
            return True, ('Key saved, but OpenAI could not be reached to verify it. '
                          'If transcription fails, re-check the key here.'), 'unverified_network'
        if getattr(e, 'status_code', None) == 429:
            # The key authenticated; the account is just out of credit or rate
            # limited. Refusing the save would leave them unable to store a
            # working key at all.
            return True, OPENAI_NO_BILLING_SAVE_MSG, 'no_billing'
        return (False, describe_openai_error(e, context='verify'),
                product_analytics.openai_fail_reason(e, looks_like_key=True))

    try:
        _probe_whisper_billing(client)
    except Exception as e:
        if _openai_unreachable(e):
            return True, ('Key saved, but OpenAI could not be reached to verify it. '
                          'If transcription fails, re-check the key here.'), 'unverified_network'
        status = getattr(e, 'status_code', None)
        code = product_analytics.openai_error_code(e)
        if status == 429 or code == 'insufficient_quota':
            return True, OPENAI_NO_BILLING_SAVE_MSG, 'no_billing'
        return (False, describe_openai_error(e, context='verify'),
                product_analytics.openai_fail_reason(e, looks_like_key=True))

    return True, 'Key saved and verified for Whisper transcription.', 'verified'


# ---------------------------------------------------------------------------
# Audio helpers
# ---------------------------------------------------------------------------

#: Statuses a task never leaves. The worker checks for these between chunks and
#: stops, which is what makes cancelling possible without killing a thread: a
#: daemon thread cannot be interrupted from outside, but it can be told to give
#: up at the next boundary it reaches.
TERMINAL_STATUSES = frozenset({'error', 'cancelled'})

#: Floor for how long a task may go without a progress write before it counts
#: as abandoned. The real window scales with the work in flight -- see
#: _stale_after_seconds() -- but is capped by the Whisper hang budget so a
#: mid-chunk hang cannot sit for tens of minutes before anything notices.
STALE_TASK_SECONDS = 15 * 60

#: How often the background watchdog scans for quiet tasks. Polling /status
#: and /active-jobs also sweep; this is the backstop when nobody is looking
#: (closed tab, API-only client, worker death after a deploy).
STALE_WATCHDOG_INTERVAL_SECONDS = int(os.getenv('STALE_WATCHDOG_INTERVAL_SECONDS', '60'))
_stale_watchdog_started = False


def _update_task(task_id, **kwargs):
    """Update a TranscriptionTask row. Must be called within an app context.

    TERMINAL_STATUSES are terminal. The stale sweeper runs in the other gunicorn
    worker and may fail a task -- settling its trial charge -- while this worker
    is still mid-episode, and a user may cancel one from their browser. Letting
    the worker write 'transcribing' (and later 'completed') over either verdict
    resurrects a task whose allowance has already been handed back, which is how
    a swept episode got transcribed for free -- and it is also what would make a
    cancel button lie. Read the status straight from the database: the session may
    still hold our own last write.
    """
    stmt = (
        sa_update(TranscriptionTask)
        .where(TranscriptionTask.id == task_id)
        .values(heartbeat_at=datetime.now(timezone.utc), **kwargs)
    )
    if kwargs.get('status') not in TERMINAL_STATUSES:
        stmt = stmt.where(TranscriptionTask.status.notin_(TERMINAL_STATUSES))
    result = db.session.execute(stmt)
    db.session.commit()
    return result.rowcount == 1


def download_audio(url, filename, task_id):
    """Download audio file from URL with progress reporting."""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                       'AppleWebKit/537.36 (KHTML, like Gecko) '
                       'Chrome/122.0.0.0 Safari/537.36',
        'Accept': 'audio/*,*/*',
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept-Encoding': 'gzip, deflate, br',
        'Connection': 'keep-alive',
        'Referer': 'https://podcasts.apple.com/'
    }

    def _fetch(request_headers):
        """GET the audio, revalidating the target on every redirect hop.

        Redirects are followed manually: `requests` follows them itself, which
        would let a public host 302 straight to a private address and slip past
        the check done on the original URL. A Session carries cookies across
        hops — some CDNs set one on an intermediate measurement host and
        expect it on the next. Authorization is dropped when the host changes,
        matching requests' own cross-host redirect policy.
        """
        current = url
        seen = set()
        redirects = 0
        session = requests.Session()
        hop_headers = dict(request_headers)
        while True:
            if not _is_fetchable_url(current):
                raise Exception('Audio URL points somewhere that cannot be fetched.')
            # Fragments are never sent; ignore them for loop detection.
            finger = current.split('#', 1)[0]
            if finger in seen:
                raise Exception('Redirect loop while fetching audio.')
            seen.add(finger)

            resp = session.get(
                current, stream=True, headers=hop_headers,
                timeout=DOWNLOAD_TIMEOUT_SECONDS, allow_redirects=False,
            )
            if resp.is_redirect or resp.is_permanent_redirect:
                location = resp.headers.get('location')
                resp.close()
                if not location:
                    raise Exception('Redirect without a target while fetching audio.')
                if redirects >= MAX_REDIRECTS:
                    raise Exception('Too many redirects while fetching audio.')
                redirects += 1
                next_url = urljoin(current, location)
                if urlparse(next_url).netloc.lower() != urlparse(current).netloc.lower():
                    hop_headers.pop('Authorization', None)
                current = next_url
                continue
            try:
                resp.raise_for_status()
            except requests.exceptions.HTTPError:
                resp.close()   # streamed responses hold the connection open
                raise
            return resp

    def _raise_permanent_or_generic(status, original_exc):
        """Map permanent host refusals to SourceAudioUnavailable; else generic."""
        if status in SOURCE_AUDIO_MISSING_STATUSES:
            raise SourceAudioUnavailable(
                SourceAudioUnavailable.REASON_MISSING, status_code=status)
        if status in SOURCE_AUDIO_FORBIDDEN_STATUSES:
            raise SourceAudioUnavailable(
                SourceAudioUnavailable.REASON_FORBIDDEN, status_code=status)
        raise Exception(
            f"HTTP error {status}" if status else f"HTTP error: {original_exc}")

    def _fetch_with_403_fallback():
        """First request; on 403, retry once with a plainer User-Agent.

        Permanent refusals on the fallback become SourceAudioUnavailable here.
        Transient HTTP errors are re-raised so the outer loop can retry once.
        """
        try:
            return _fetch(headers)
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            if status not in SOURCE_AUDIO_FORBIDDEN_STATUSES:
                raise
            try:
                return _fetch({'User-Agent': 'podcast-downloader/1.0', 'Accept': '*/*'})
            except requests.exceptions.HTTPError as e2:
                status2 = (
                    e2.response.status_code if e2.response is not None else None)
                if status2 in (SOURCE_AUDIO_MISSING_STATUSES
                               | SOURCE_AUDIO_FORBIDDEN_STATUSES):
                    _raise_permanent_or_generic(status2, e2)
                raise
            except requests.exceptions.RequestException as e2:
                raise Exception(f"Failed to download audio: {e2}") from e2

    # One retry on 5xx / timeout: podcast CDNs flap; a permanent 4xx must not.
    response = None
    last_exc = None
    for attempt in range(2):
        try:
            response = _fetch_with_403_fallback()
            break
        except SourceAudioUnavailable:
            raise
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            if (status in SOURCE_AUDIO_TRANSIENT_STATUSES and attempt == 0):
                last_exc = e
                time.sleep(DOWNLOAD_RETRY_PAUSE_SEC)
                continue
            _raise_permanent_or_generic(status, e)
        except requests.exceptions.Timeout as e:
            if attempt == 0:
                last_exc = e
                time.sleep(DOWNLOAD_RETRY_PAUSE_SEC)
                continue
            raise Exception(f"Failed to download audio: {e}") from e
        except requests.exceptions.RequestException as e:
            raise Exception(f"Failed to download audio: {e}") from e
    if response is None:
        raise Exception(
            f"Failed to download audio: {last_exc or 'transient host error'}"
        )

    total_size = int(response.headers.get('content-length', 0) or 0)
    downloaded = 0
    last_db_update = 0.0

    # Report bytes even when the CDN omits content-length -- without this the bar
    # sat at 0% for the whole download on every feed that doesn't send the header.
    _update_task(task_id, bytes_downloaded=0, bytes_total=total_size)

    # A streamed response holds its connection open, so every exit from this
    # loop has to close it -- the size cap, a cancellation, and a write failing
    # on a full disk, which is exactly the case the capacity limits exist for.
    try:
        with open(filename, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                if is_shutting_down():
                    raise ServerRestart(auto_resumed=True)
                if not chunk:
                    continue
                f.write(chunk)
                downloaded += len(chunk)
                if downloaded > MAX_AUDIO_BYTES:
                    raise Exception(
                        f'This episode is larger than the '
                        f'{MAX_AUDIO_BYTES // (1024 * 1024)} MB limit Podskrift will download.'
                    )
                now = time.time()
                if now - last_db_update >= 1:
                    # The return value is the cancel signal: _update_task refuses
                    # to write to a task that has reached a terminal status.
                    # Without this, Stop during a download let the whole episode
                    # download and then re-encode while the UI said it had
                    # stopped -- holding a concurrency slot and disk for nothing.
                    if not _update_task(task_id, bytes_downloaded=downloaded):
                        raise TaskAbandoned('Task was cancelled during download.')
                    last_db_update = now
    finally:
        response.close()

    _update_task(
        task_id,
        bytes_downloaded=downloaded,
        bytes_total=total_size or downloaded,
    )
    return filename


def probe_audio_duration(audio_file):
    """Measured duration in seconds, or None if ffprobe could not read the file.

    Uses ffprobe, which ships with the ffmpeg used for splitting.
    This previously used librosa, which pulled in scipy, llvmlite, sklearn,
    numba and numpy -- 398 MB of a 547 MB virtualenv for this one call.

    Kept separate from get_audio_duration() because billing must be able to
    tell "we measured 12 minutes" from "we guessed 12 minutes".
    """
    try:
        out = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'default=noprint_wrappers=1:nokey=1', audio_file],
            capture_output=True, text=True, timeout=60, check=True,
        )
        duration = float(out.stdout.strip())
        if duration > 0:
            return duration
    except (subprocess.SubprocessError, ValueError, OSError):
        pass
    return None


def estimate_audio_duration(audio_file):
    """Duration from file size when ffprobe cannot read the file.

    ~1 MB per minute of spoken-word audio. Deliberately not an average: for
    billing this is the only number left, so it has to be a number we are
    willing to charge for.
    """
    try:
        return (os.path.getsize(audio_file) / (1024 * 1024)) * 60
    except OSError:
        return 0.0


def get_audio_duration(audio_file):
    """Duration in seconds, measured if possible and estimated otherwise."""
    measured = probe_audio_duration(audio_file)
    if measured is not None:
        return measured
    return estimate_audio_duration(audio_file)


#: Whisper resamples to 16 kHz mono internally, so encoding to that throws away
#: nothing it would have used -- and it makes chunk size proportional to
#: duration, which stream-copying the source never was.
WHISPER_SAMPLE_RATE = 16000
WHISPER_AUDIO_BITRATE_KBPS = 48
#: 15 minutes at 48 kbps is ~5.4 MB, well inside OpenAI's 25 MB limit. An hour
#: per part would fit too, but parts are also the unit of two other things: the
#: progress bar's granularity, and how much a failed job is refunded. One part
#: means a job that dies after the first upload refunds nothing.
SEGMENT_SECONDS = 900
#: Hard check on what we actually produced. Belt to SEGMENT_SECONDS' braces.
WHISPER_MAX_UPLOAD_BYTES = 25 * 1024 * 1024
#: Re-encoding writes no heartbeat, so it has to finish inside the stale-task
#: floor or a live job gets swept as stuck. ffmpeg runs at roughly 100x
#: realtime here, so this is generous.
FFMPEG_TIMEOUT_SECONDS = 600
#: A part must fit the upload limit by construction, not by luck. Raising the
#: bitrate or the segment length without checking this would fail every episode
#: longer than one part with "could not be split into uploadable parts".
assert (SEGMENT_SECONDS * WHISPER_AUDIO_BITRATE_KBPS * 1000 / 8
        < WHISPER_MAX_UPLOAD_BYTES * 0.9), \
    'SEGMENT_SECONDS x WHISPER_AUDIO_BITRATE_KBPS does not fit WHISPER_MAX_UPLOAD_BYTES'

#: Roughly one second at the bitrate above. ffmpeg's segmenter cuts on packet
#: boundaries, so an episode that is an exact multiple of SEGMENT_SECONDS leaves
#: a crumb behind -- an hour-long file came out as 3600.0s plus a 0.144s tail.
#: Whisper rejects audio that short, and it would cost a whole extra request.
MIN_PART_BYTES = WHISPER_AUDIO_BITRATE_KBPS * 1000 // 8


def probe_audio_bitrate_kbps(audio_file):
    """Source bitrate in kbps, or None if ffprobe cannot say."""
    try:
        out = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_entries', 'format=bit_rate',
             '-of', 'default=noprint_wrappers=1:nokey=1', audio_file],
            capture_output=True, text=True, timeout=60, check=True,
        )
        kbps = int(float(out.stdout.strip())) // 1000
        return kbps if kbps > 0 else None
    except (subprocess.SubprocessError, ValueError, OSError):
        return None


def _ffmpeg_error(message, stderr=b'', *, returncode=None):
    """Log ffmpeg's own words, hand the user something they can act on.

    Shutdown / SIGTERM kills must not become "corrupt or unsupported format".
    """
    if returncode is not None or stderr:
        try:
            _raise_if_ffmpeg_shutdown(returncode if returncode is not None else 1, stderr)
        except ServerRestart:
            raise
    if is_shutting_down():
        raise ServerRestart(auto_resumed=True)
    detail = (stderr or b'').decode('utf-8', 'replace').strip()
    if detail:
        app.logger.error('ffmpeg failed: %s', detail[:2000])
    return RuntimeError(message)


def trim_audio_file(audio_file, max_seconds):
    """Rewrite `audio_file` to the first `max_seconds` of audio via ffmpeg.

    Used for partial trial previews (and unknown-duration overruns) so Whisper
    never sees more audio than we reserved. Prefers stream-copy; falls back to
    a light re-encode when the container rejects copy. The later
    prepare_audio_for_whisper() pass still re-encodes for Whisper either way.
    """
    max_seconds = max(1, int(math.ceil(float(max_seconds))))
    if shutil.which('ffmpeg') is None:
        raise _ffmpeg_error(
            'Audio processing is unavailable on the server right now. '
            'Please try again later, or contact support if it persists.')

    tmp_path = f'{audio_file}.trimtmp.mp3'

    def _run(cmd):
        try:
            return subprocess.run(
                cmd, capture_output=True, timeout=FFMPEG_TIMEOUT_SECONDS)
        except FileNotFoundError:
            raise _ffmpeg_error(
                'Audio processing is unavailable on the server right now. '
                'Please try again later, or contact support if it persists.')
        except subprocess.TimeoutExpired:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            raise _ffmpeg_error(
                'This episode took too long to process. Please try a shorter one.')

    copy = _run([
        'nice', '-n', '10', 'ffmpeg', '-v', 'error', '-y',
        '-i', audio_file, '-t', str(max_seconds),
        '-c', 'copy', '-vn', tmp_path,
    ])
    if copy.returncode != 0 or not os.path.exists(tmp_path) or os.path.getsize(tmp_path) < MIN_PART_BYTES:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        encoded = _run([
            'nice', '-n', '10', 'ffmpeg', '-v', 'error', '-y',
            '-i', audio_file, '-t', str(max_seconds),
            '-vn', '-ac', '1', '-ar', str(WHISPER_SAMPLE_RATE),
            '-b:a', f'{WHISPER_AUDIO_BITRATE_KBPS}k', '-threads', '1',
            tmp_path,
        ])
        if encoded.returncode != 0 or not os.path.exists(tmp_path):
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            err = encoded.stderr if encoded.returncode else copy.stderr
            code = encoded.returncode if encoded.returncode else copy.returncode
            raise _ffmpeg_error(
                'This audio file could not be processed. It may be corrupt or in an '
                'unsupported format.', err, returncode=code)

    os.replace(tmp_path, audio_file)
    return audio_file


def prepare_audio_for_whisper(audio_file, max_bytes=WHISPER_MAX_UPLOAD_BYTES):
    """Re-encode to 16 kHz mono MP3, split into hour-long parts if still large.

    Returns the parts and removes the source. One ffmpeg pass does both.

    Three earlier approaches each failed differently, and this is the shape that
    fixes all three at once rather than budgeting around them:

    - pydub decoded the whole episode to raw PCM in memory and had ffmpeg write
      a full WAV to TMPDIR first: ~1.9 GB of each for a three-hour episode, per
      concurrent job. ffmpeg streams; memory here is flat and small.
    - Stream-copying (`-c copy`) cut by time, but bytes are not proportional to
      time in a VBR file -- a 39 MB episode with a dense first half produced a
      27 MB part against a 24 MB target, over OpenAI's limit. A fixed output
      bitrate makes size proportional to duration by construction.
    - Stream-copying also derived coverage from ffprobe's duration and wrote
      through the source's extension. A concatenated MP3 (how dynamic ad
      insertion stitches segments) reports short, and 16 minutes of audio was
      silently never sent. And `.../stream.php?id=9` picked a `.php` muxer.
      `-f segment` walks the actual stream instead of trusting a duration, and
      the output container is always MP3 whatever the source was called.

    ffmpeg runs niced and single-threaded: production is a shared Plesk host
    with 50+ other services, and re-encoding is the one CPU-hungry step here.
    """
    if shutil.which('ffmpeg') is None:
        # Checked up front rather than caught: the nice(1) wrapper turns a
        # missing ffmpeg into exit 127, not a FileNotFoundError, so the
        # exception handler below would report it as a corrupt file.
        raise _ffmpeg_error(
            'Audio processing is unavailable on the server right now. '
            'Please try again later, or contact support if it persists.')

    base_name = os.path.splitext(audio_file)[0]
    pattern = f'{base_name}_part_%03d.mp3'
    produced_glob = f'{base_name}_part_*.mp3'

    # Never encode above the source's own rate: a 24 kbps feed re-encoded at 48
    # would come out twice its input size, and MIN_FREE_DISK_BYTES is sized on
    # the assumption that the parts are no larger than the source.
    bitrate = min(WHISPER_AUDIO_BITRATE_KBPS,
                  probe_audio_bitrate_kbps(audio_file) or WHISPER_AUDIO_BITRATE_KBPS)

    if is_shutting_down():
        raise ServerRestart(auto_resumed=True)

    try:
        result = subprocess.run(
            ['nice', '-n', '10', 'ffmpeg', '-v', 'error', '-y', '-i', audio_file,
             '-vn', '-ac', '1', '-ar', str(WHISPER_SAMPLE_RATE),
             '-b:a', f'{bitrate}k', '-threads', '1',
             '-f', 'segment', '-segment_time', str(SEGMENT_SECONDS),
             '-segment_format', 'mp3', '-reset_timestamps', '1', pattern],
            capture_output=True, timeout=FFMPEG_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        raise _ffmpeg_error(
            'Audio processing is unavailable on the server right now. '
            'Please try again later, or contact support if it persists.')
    except subprocess.TimeoutExpired:
        _cleanup_glob(produced_glob)
        if is_shutting_down():
            raise ServerRestart(auto_resumed=True)
        raise _ffmpeg_error('This episode took too long to process. Please try a shorter one.')

    parts = sorted(glob.glob(produced_glob))
    if result.returncode != 0 or not parts:
        _cleanup_glob(produced_glob)
        raise _ffmpeg_error(
            'This audio file could not be processed. It may be corrupt or in an '
            'unsupported format.', result.stderr, returncode=result.returncode)

    # Drop the segmenter's rounding crumb, but never the only part, and never
    # silently: this is the one place content could go missing without an error.
    if len(parts) > 1:
        crumbs = [p for p in parts if os.path.getsize(p) < MIN_PART_BYTES]
        if crumbs:
            app.logger.warning('discarding %s sub-second part(s) from %s',
                               len(crumbs), os.path.basename(audio_file))
            for crumb in crumbs:
                try:
                    os.remove(crumb)
                except OSError:
                    pass
            parts = [p for p in parts if p not in crumbs]

    oversize = [p for p in parts if os.path.getsize(p) > max_bytes]
    if oversize or not parts:
        _cleanup_glob(produced_glob)
        raise _ffmpeg_error(
            'This episode could not be split into uploadable parts. '
            'Please try a different one.')

    os.remove(audio_file)
    return parts


def _cleanup_glob(pattern):
    """Remove every file matching `pattern`, ignoring failures.

    Globbed rather than tracked: `ffmpeg -y` creates and writes its output
    before it fails, so the part in flight when it died is never in any list
    we built. On a box whose whole constraint is disk, that leaked.
    """
    for path in glob.glob(pattern):
        try:
            os.remove(path)
        except OSError:
            pass


def transcribe_audio(audio_file, task_id, openai_client, language=None):
    """Transcribe audio using OpenAI Whisper API.

    Progress is written as checkpoints (chunk index + when that chunk started);
    /status interpolates between them so the bar keeps moving while a single
    chunk is in flight. Whisper exposes no streaming progress of its own.
    """
    import json

    if not openai_client:
        raise Exception(
            "No OpenAI API key configured. "
            "Go to Settings and add your key, or ask the admin to set a global key."
        )

    # Measure the file we actually downloaded, BEFORE splitting removes it.
    # This is the number the trial is billed on, and it must not come from the
    # feed: itunes:duration on the RSS path and duration_min on the direct path
    # are both supplied by the client, so charging on either would let a caller
    # claim one minute and transcribe four hours on our key.
    measured_duration = probe_audio_duration(audio_file)
    billable_duration = (measured_duration if measured_duration is not None
                         else estimate_audio_duration(audio_file))
    full_episode_seconds = billable_duration

    task_row = db.session.get(TranscriptionTask, task_id)
    partial_info = task_partial_meta(task_row) if task_row else None
    reserved_seconds = 0
    if task_row is not None:
        reserved_seconds = (
            int(task_row.trial_seconds_charged or 0)
            + int(task_row.paid_seconds_charged or 0)
        )

    # Partial preview: never send more audio than we reserved. Trim before
    # reconcile so billing matches the file Whisper will see.
    if partial_info and reserved_seconds > 0 and billable_duration > reserved_seconds:
        # Remember the real length for the result-page "first N of M" copy.
        set_task_partial_meta(
            task_row, reserved_seconds,
            max(full_episode_seconds, partial_info['episode_seconds']))
        db.session.commit()
        trim_audio_file(audio_file, reserved_seconds)
        measured_duration = probe_audio_duration(audio_file)
        billable_duration = (
            measured_duration if measured_duration is not None
            else float(reserved_seconds)
        )

    # Last point at which refusing is still free: everything below this line
    # bills OpenAI. May return 'trim' for unknown-duration overruns that we
    # promote to a partial preview instead of failing.
    reconcile_action = trial_reconcile_task(task_id, billable_duration)
    if reconcile_action == 'trim':
        task_row = db.session.get(TranscriptionTask, task_id)
        reserved_seconds = 0
        if task_row is not None:
            reserved_seconds = (
                int(task_row.trial_seconds_charged or 0)
                + int(task_row.paid_seconds_charged or 0)
            )
            meta = task_partial_meta(task_row)
            if meta is None and reserved_seconds > 0:
                set_task_partial_meta(
                    task_row, reserved_seconds, full_episode_seconds)
                db.session.commit()
        if reserved_seconds > 0 and billable_duration > reserved_seconds:
            trim_audio_file(audio_file, reserved_seconds)
            measured_duration = probe_audio_duration(audio_file)
            billable_duration = (
                measured_duration if measured_duration is not None
                else float(reserved_seconds)
            )
            # Shrink the charge to the trimmed length (idempotent if already matched).
            trial_reconcile_task(task_id, billable_duration)

    if not _update_task(
        task_id,
        status='splitting',
        phase='splitting',
        phase_started_at=datetime.now(timezone.utc),
        progress=PHASE_SPANS['splitting'][0],
    ):
        raise TaskAbandoned('Task was cancelled before it could be prepared.')
    audio_chunks = prepare_audio_for_whisper(audio_file)

    # prepare_audio_for_whisper() removes the source once it has re-encoded it, so
    # EVERY exit from here on -- the abandonment raise included -- has to go
    # through this cleanup, or temp_audio_<uuid>_chunk_N.mp3 stays on disk
    # forever. The abandonment raise used to sit above this try and leaked up
    # to MAX_AUDIO_BYTES per occurrence.
    remaining = set(audio_chunks)
    upload_start = time.time()
    all_segments = []
    full_text = ""

    try:
        # For the ETA, a measurement of the whole file beats everything. The
        # feed's claim is only better than the size-based fallback, so it wins
        # only when ffprobe could not read the file at all.
        task = db.session.get(TranscriptionTask, task_id)
        feed_duration = task.audio_duration if task and task.audio_duration else None
        audio_duration = measured_duration or feed_duration or billable_duration

        # _update_task refuses to move a task out of 'error', so a False here
        # means the sweeper failed this task while we were splitting -- and
        # settled its charge. Splitting is still the widest window for that:
        # a long episode means several ffmpeg calls. Stop rather than
        # transcribe against an allowance already refunded.
        started = _update_task(
            task_id,
            status='transcribing',
            phase='transcribing',
            chunk_total=len(audio_chunks),
            # Left unset on purpose: the loop writes chunk_index immediately
            # before it uploads that chunk, so "unset" is the only honest way
            # to say nothing has been sent yet. trial_refund_task() reads it as
            # a billing signal, and a 0 here would charge for a chunk never sent.
            chunk_index=None,
            audio_duration=audio_duration,
            progress=PHASE_SPANS['transcribing'][0],
            phase_started_at=datetime.now(timezone.utc),
        )
        if not started:
            raise TaskAbandoned(
                'Task was marked failed while it was being split; stopping so '
                'it cannot bill against an allowance already refunded.'
            )

        upload_start = time.time()
        full_text, all_segments = _transcribe_chunks(
            audio_chunks, remaining, task_id, openai_client, language, audio_duration
        )
    finally:
        for leftover in remaining:
            if os.path.exists(leftover):
                try:
                    os.remove(leftover)
                except OSError:
                    pass

    elapsed = time.time() - upload_start

    _update_task(
        task_id,
        status='completed',
        phase='completed',
        progress=100,
        transcript_text=full_text,
        segments_json=json.dumps(all_segments) if all_segments else None,
        audio_duration=audio_duration,
        transcription_time=elapsed,
        completed_at=datetime.now(timezone.utc),
    )

    if os.path.exists(audio_file):
        os.remove(audio_file)


def _whisper_transcribe_chunk(openai_client, chunk_file, language, *,
                              task_id=None, chunk_label=None,
                              attempts=None):
    """Call Whisper on one chunk with a bounded retry budget.

    SDK max_retries is 0; this loop is the only retry path, so a hung request
    cannot stretch past WHISPER_TIMEOUT_SECONDS × attempts via opaque retries.
    Heartbeats between attempts keep the stale sweeper from killing a job that
    is still retrying. Non-transient errors (auth, billing, 4xx) are not retried.
    """
    attempts = WHISPER_CHUNK_ATTEMPTS if attempts is None else max(1, int(attempts))
    last_exc = None
    label = chunk_label or os.path.basename(chunk_file)

    for attempt in range(1, attempts + 1):
        try:
            with open(chunk_file, 'rb') as f:
                create_kwargs = {
                    'model': 'whisper-1',
                    'file': f,
                    'response_format': 'verbose_json',
                    'timestamp_granularities': ['segment'],
                }
                # Omitting `language` entirely is what makes Whisper auto-detect;
                # passing None or '' is rejected by the API.
                if language:
                    create_kwargs['language'] = language
                return openai_client.audio.transcriptions.create(**create_kwargs)
        except (APITimeoutError, APIConnectionError) as exc:
            last_exc = exc
            app.logger.warning(
                'Whisper %s attempt %s/%s failed (%s)',
                label, attempt, attempts, type(exc).__name__,
            )
            if attempt >= attempts:
                break
            # Prove we are still alive so a long retry budget cannot look like
            # a dead worker to the stale sweeper.
            if task_id:
                _update_task(task_id)
            time.sleep(min(2 ** (attempt - 1), 8))

    # Surface a clear, actionable message. describe_openai_error() still wins
    # for the raw SDK types in the worker except path; this wraps the final
    # raise so callers that stringify get the chunk context too.
    raise last_exc


def _transcribe_chunks(audio_chunks, remaining, task_id, openai_client, language,
                       audio_duration):
    """Send each chunk to Whisper, publishing partial text as it goes.

    Returns (full_text, segments). `remaining` is mutated as chunks are consumed
    so the caller can clean up whatever is left if this raises.

    Mid-chunk resume is not attempted here: part files live only for the life
    of the thread. Process death is recovered at boot by re-queuing the whole
    task (re-download + re-encode) via ``resume_interrupted_tasks``.
    """
    all_segments = []
    full_text = ""
    total = len(audio_chunks)

    for i, chunk_file in enumerate(audio_chunks):
        if is_shutting_down():
            raise ServerRestart(auto_resumed=True)
        # The stale sweeper may have given up on this task and refunded the
        # unspent allowance. Read the status straight from the database rather
        # than through the session, which may still hold our own last write.
        if db.session.execute(
            text('SELECT status FROM transcription_tasks WHERE id = :tid'),
            {'tid': task_id},
        ).scalar() in TERMINAL_STATUSES:
            raise TaskAbandoned(
                'Task reached a terminal state while it was still running '
                '(cancelled, or swept as stale); stopping so it cannot keep '
                'billing against an allowance already handed back.'
            )

        # This write is the same terminal-'error' guard as the check above,
        # one statement before the upload -- so acting on it closes the
        # check-to-upload window almost entirely, for free.
        if not _update_task(
            task_id,
            status=f'transcribing chunk {i + 1}/{total}',
            chunk_index=i,
            phase_started_at=datetime.now(timezone.utc),
            progress=_transcribe_checkpoint(i, total),
        ):
            raise TaskAbandoned(
                'Task was marked failed between chunks; stopping so it cannot '
                'keep billing against an allowance already refunded.'
            )

        # Exhausted retries raise APITimeoutError / APIConnectionError; the
        # worker marks the task failed, fires transcript_failed (reason=network),
        # and refunds unspent trial minutes.
        chunk_transcript = _whisper_transcribe_chunk(
            openai_client, chunk_file, language,
            task_id=task_id,
            chunk_label=f'chunk {i + 1}/{total}',
        )

        full_text += chunk_transcript.text + " "

        if hasattr(chunk_transcript, 'segments') and chunk_transcript.segments:
            chunk_dur = audio_duration / total
            offset = i * chunk_dur
            for seg in chunk_transcript.segments:
                all_segments.append({
                    'start': seg.start + offset,
                    'end': seg.end + offset,
                    'text': seg.text
                })

        detected = getattr(chunk_transcript, 'language', None)
        # Store ISO codes only. Whisper returns names like 'english'; the
        # picker sends 'en'. normalize_language_code maps both; unknown → auto.
        stored_lang = normalize_language_code(language or detected) or None
        # Publish the text we have so far so the page can show it streaming in
        # instead of an empty box. History only lists completed tasks, so a
        # partial write here is never user-visible as a finished transcript.
        _update_task(
            task_id,
            transcript_text=full_text.strip(),
            progress=_transcribe_checkpoint(i + 1, total),
            language=stored_lang,
        )

        os.remove(chunk_file)
        remaining.discard(chunk_file)

    return full_text.strip(), all_segments


def _transcribe_checkpoint(chunks_done, chunk_total):
    """Overall progress percentage at a chunk boundary."""
    lo, hi = PHASE_SPANS['transcribing']
    if not chunk_total:
        return lo
    return int(lo + (chunks_done / chunk_total) * (hi - lo))


def _seconds_since(dt):
    """Seconds elapsed since `dt`, treating naive values as UTC (SQLite gives us naive)."""
    if not dt:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0.0, time.time() - dt.timestamp())


def _asymptotic_fraction(elapsed, expected):
    """How far through a step we probably are, as a fraction that never reaches 1.

    Linear to 0.9 over the estimate, then asymptotic toward 0.99. A hard
    `min(0.97, ...)` cap would park the bar at 97% whenever Whisper runs slower
    than WHISPER_REALTIME_FACTOR -- the same frozen bar, just at a nicer number.
    """
    if expected <= 0:
        return 0.9
    if elapsed <= expected:
        return 0.9 * (elapsed / expected)
    overrun = (elapsed - expected) / expected
    return 0.9 + 0.09 * (1 - math.exp(-overrun))


def compute_live_progress(task):
    """Interpolate a task's progress between its last two checkpoints.

    Returns (percent, eta_seconds_or_None). The stored `progress` column is
    treated as a floor so the bar can never travel backwards between polls.
    """
    stored = task.progress or 0
    if task.status == 'completed':
        return 100, None
    if task.status in TERMINAL_STATUSES:
        return stored, None

    phase_elapsed = _seconds_since(task.phase_started_at)

    if task.phase == 'transcribing' and task.chunk_total:
        lo, hi = PHASE_SPANS['transcribing']
        # Expected wall-clock seconds for the chunk currently in flight
        per_chunk = (task.audio_duration or 0) / task.chunk_total / WHISPER_REALTIME_FACTOR
        per_chunk = max(per_chunk, 8.0)
        within = _asymptotic_fraction(phase_elapsed, per_chunk)
        done = ((task.chunk_index or 0) + within) / task.chunk_total
        percent = lo + done * (hi - lo)
        remaining_chunks = task.chunk_total - (task.chunk_index or 0) - within
        eta = max(0.0, per_chunk * remaining_chunks)
        return max(stored, int(percent)), eta

    if task.phase == 'downloading' and task.bytes_total:
        lo, hi = PHASE_SPANS['downloading']
        frac = min(1.0, (task.bytes_downloaded or 0) / task.bytes_total)
        rate = (task.bytes_downloaded or 0) / phase_elapsed if phase_elapsed > 1 else 0
        eta = ((task.bytes_total - (task.bytes_downloaded or 0)) / rate) if rate > 0 else None
        return max(stored, int(lo + frac * (hi - lo))), eta

    if task.phase == 'splitting':
        lo, hi = PHASE_SPANS['splitting']
        # No measurable signal here; creep toward the top of the band
        return max(stored, int(lo + _asymptotic_fraction(phase_elapsed, 30) * (hi - lo))), None

    return stored, None


def _aware_utc(dt):
    """Return ``dt`` as timezone-aware UTC, or None."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _percentile_sorted(sorted_vals, p):
    """Linear percentile on a non-empty sorted list (p in 0..100)."""
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    rank = (p / 100.0) * (len(sorted_vals) - 1)
    lo = int(math.floor(rank))
    hi = int(math.ceil(rank))
    if lo == hi:
        return float(sorted_vals[lo])
    frac = rank - lo
    return float(sorted_vals[lo]) * (1.0 - frac) + float(sorted_vals[hi]) * frac


def recent_transcription_rates(*, limit=None, lookback_days=None):
    """Wall-clock seconds per audio minute from recent completed jobs.

    Returns a sorted list of rates. Jobs without duration or timestamps, or
    with non-positive wall time, are skipped.
    """
    lim = int(limit if limit is not None else ETA_SAMPLE_LIMIT)
    days = int(lookback_days if lookback_days is not None else ETA_LOOKBACK_DAYS)
    since = datetime.now(timezone.utc) - timedelta(days=max(1, days))
    rows = (
        TranscriptionTask.query
        .filter(
            TranscriptionTask.status == 'completed',
            TranscriptionTask.completed_at.isnot(None),
            TranscriptionTask.started_at.isnot(None),
            TranscriptionTask.audio_duration.isnot(None),
            TranscriptionTask.audio_duration > 0,
            TranscriptionTask.completed_at >= since,
        )
        .order_by(TranscriptionTask.completed_at.desc())
        .limit(max(1, lim))
        .all()
    )
    rates = []
    for task in rows:
        start = _aware_utc(task.started_at)
        end = _aware_utc(task.completed_at)
        if start is None or end is None:
            continue
        wall = (end - start).total_seconds()
        audio_min = float(task.audio_duration) / 60.0
        if wall <= 0 or audio_min <= 0:
            continue
        # Ignore absurd outliers (clock skew / stuck jobs).
        rate = wall / audio_min
        if rate < 0.5 or rate > 600:
            continue
        rates.append(rate)
    rates.sort()
    return rates


def count_transcriptions_ahead(task):
    """How many in-flight jobs are ahead of ``task`` (global admission queue)."""
    if task is None or not getattr(task, 'id', None):
        return max(0, count_in_flight_transcriptions())
    start = _aware_utc(task.started_at) or datetime.now(timezone.utc)
    q = (
        TranscriptionTask.query
        .filter(
            ~TranscriptionTask.status.in_(['completed', *TERMINAL_STATUSES]),
            TranscriptionTask.id != task.id,
        )
    )
    ahead = 0
    for other in q.limit(200).all():
        other_start = _aware_utc(other.started_at) or start
        if other_start < start or (
                other_start == start and str(other.id) < str(task.id)):
            ahead += 1
    return ahead


def format_eta_window_text(low_sec, high_sec) -> str:
    """Human rough window — never a promise."""
    def _mins(sec):
        return max(1, int(round(max(0.0, float(sec)) / 60.0)))

    lo_m = _mins(low_sec)
    hi_m = _mins(high_sec)
    if hi_m < lo_m:
        hi_m = lo_m
    if lo_m == hi_m:
        unit = 'minute' if lo_m == 1 else 'minutes'
        return f'usually ready in about {lo_m} {unit}'
    return f'usually ready in about {lo_m}–{hi_m} minutes'


def estimate_transcription_eta(
        *,
        audio_duration_sec=None,
        progress_pct=None,
        queue_ahead=0,
        rates=None,
        min_samples=None):
    """Rough ETA window from recent run rates (p25–p75) × duration + queue.

    Returns a dict with ``eta_seconds_low`` / ``eta_seconds_high`` /
    ``eta_seconds`` (midpoint), ``eta_text``, ``eta_basis``
    (``historical``|``fallback``), and ``sample_count``. Not a SLA.
    """
    min_n = int(min_samples if min_samples is not None else ETA_MIN_SAMPLES)
    sample_rates = list(rates) if rates is not None else recent_transcription_rates()
    sample_count = len(sample_rates)
    if sample_count >= min_n:
        low_rate = _percentile_sorted(sample_rates, 25)
        mid_rate = _percentile_sorted(sample_rates, 50)
        high_rate = _percentile_sorted(sample_rates, 75)
        basis = 'historical'
    else:
        low_rate = ETA_FALLBACK_SEC_PER_AUDIO_MIN_LOW
        mid_rate = (
            ETA_FALLBACK_SEC_PER_AUDIO_MIN_LOW
            + ETA_FALLBACK_SEC_PER_AUDIO_MIN_HIGH) / 2.0
        high_rate = ETA_FALLBACK_SEC_PER_AUDIO_MIN_HIGH
        basis = 'fallback'

    try:
        audio_sec = float(audio_duration_sec) if audio_duration_sec else 0.0
    except (TypeError, ValueError):
        audio_sec = 0.0
    audio_min = max(audio_sec / 60.0, 1.0)  # at least one minute of work

    low = low_rate * audio_min
    mid = mid_rate * audio_min
    high = high_rate * audio_min

    # Queue: each job ahead adds a median-length episode at the mid rate.
    ahead = max(0, int(queue_ahead or 0))
    if ahead:
        typical_min = 45.0
        if sample_rates:
            # Prefer median wall seconds of a sample job if we have rates —
            # approximate via mid_rate * median audio minutes when available.
            typical_min = 45.0
        queue_add = ahead * mid_rate * typical_min
        low += queue_add * 0.7
        mid += queue_add
        high += queue_add * 1.2

    # Remaining work from progress (never claim "done" before completion).
    try:
        pct = float(progress_pct) if progress_pct is not None else 0.0
    except (TypeError, ValueError):
        pct = 0.0
    pct = max(0.0, min(99.0, pct))
    remaining_frac = max(0.05, 1.0 - (pct / 100.0))
    low *= remaining_frac
    mid *= remaining_frac
    high *= remaining_frac

    # Floor: never advertise sub-30s windows (polling noise).
    low = max(30.0, low)
    mid = max(low, mid)
    high = max(mid, high)

    low_i = int(round(low))
    mid_i = int(round(mid))
    high_i = int(round(high))
    return {
        'eta_seconds_low': low_i,
        'eta_seconds_high': high_i,
        'eta_seconds': mid_i,
        'eta_text': format_eta_window_text(low_i, high_i),
        'eta_basis': basis,
        'sample_count': sample_count,
        'queue_ahead': ahead,
        'seconds_per_audio_minute_p25': round(low_rate, 2),
        'seconds_per_audio_minute_p75': round(high_rate, 2),
    }


# Balance-fact: large paid/trial leftover — minutes only, no episode estimate.
BALANCE_FACT_LARGE_MINUTES = 300
BALANCE_FACT_DEFAULT_EPISODE_MIN = 45


def user_median_episode_minutes(user_id, *, limit=50, default=None):
    """Median completed episode length (minutes) for this user, or default."""
    if default is None:
        default = BALANCE_FACT_DEFAULT_EPISODE_MIN
    rows = (
        TranscriptionTask.query
        .filter(
            TranscriptionTask.user_id == user_id,
            TranscriptionTask.status == 'completed',
            TranscriptionTask.audio_duration.isnot(None),
            TranscriptionTask.audio_duration > 0,
        )
        .order_by(TranscriptionTask.completed_at.desc())
        .limit(max(1, int(limit)))
        .all()
    )
    mins = []
    for task in rows:
        try:
            mins.append(float(task.audio_duration) / 60.0)
        except (TypeError, ValueError):
            continue
    if not mins:
        return float(default)
    mins.sort()
    return float(_percentile_sorted(mins, 50) or default)


def balance_fact_for_user(user, *, pricing_url=None):
    """Short metered-balance fact for MCP / result page. None for BYOK.

    ChatGPT app-directory rules: facts + information link only — no checkout
    deep links or pushy upsell copy.
    """
    if user is None:
        return None
    _, key_source = resolve_openai_key(user)
    if key_source == 'user':
        return None
    trial_rem, paid_rem = _platform_remaining_seconds(user.id)
    minutes_left = max(0, (int(trial_rem) + int(paid_rem)) // 60)
    pricing = pricing_url or (
        public_url('pricing') if has_request_context()
        else f"{(os.getenv('PUBLIC_BASE_URL') or 'https://podskrift.com').rstrip('/')}/pricing"
    )
    fact = {
        'minutes_left': minutes_left,
        'pricing_url': pricing,
    }
    if minutes_left > BALANCE_FACT_LARGE_MINUTES:
        fact['approx_episodes_left'] = None
        fact['line'] = f'{minutes_left} min left. Pricing: {pricing}'
        return fact
    ep_min = user_median_episode_minutes(user.id)
    ep_min = max(5.0, float(ep_min))
    approx = int(minutes_left // ep_min) if ep_min else 0
    fact['approx_episodes_left'] = approx
    fact['typical_episode_min'] = round(ep_min, 1)
    if approx <= 0:
        ep_bit = 'less than 1 more episode at your usual length'
    elif approx == 1:
        ep_bit = 'about 1 more episode'
    else:
        ep_bit = f'about {approx} more episodes'
    fact['line'] = f'{minutes_left} min left ({ep_bit}). Pricing: {pricing}'
    return fact


def format_timestamp(seconds):
    """Format seconds to SRT timestamp (HH:MM:SS,mmm)."""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


# ---------------------------------------------------------------------------
# RSS helpers
# ---------------------------------------------------------------------------

def convert_apple_podcasts_url_to_rss(apple_url):
    """Convert Apple Podcasts URL to RSS feed URL."""
    try:
        import re
        match = re.search(r'/id(\d+)', apple_url)
        if not match:
            return None, "Could not extract podcast ID from URL"

        lookup_url = f"https://itunes.apple.com/lookup?id={match.group(1)}"
        resp = requests.get(lookup_url, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        if data.get('resultCount', 0) == 0:
            return None, "Podcast not found in iTunes database"

        rss_url = data['results'][0].get('feedUrl')
        if not rss_url:
            return None, "RSS feed URL not available"
        return rss_url, None
    except Exception as e:
        return None, f"Error converting URL: {e}"


def _parse_duration(raw):
    """Parse itunes:duration which can be seconds, MM:SS, or HH:MM:SS."""
    if not raw:
        return None
    raw = raw.strip()
    if ':' in raw:
        parts = raw.split(':')
        try:
            if len(parts) == 3:
                return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
            if len(parts) == 2:
                return int(parts[0]) * 60 + int(parts[1])
        except ValueError:
            return None
    try:
        return int(raw)
    except ValueError:
        return None


def _format_published(entry):
    """Render an episode date as YYYY-MM-DD, falling back to the raw RSS string."""
    parsed = entry.get('published_parsed') or entry.get('updated_parsed')
    if parsed:
        try:
            return time.strftime('%Y-%m-%d', parsed)
        except (TypeError, ValueError):
            pass
    return entry.get('published', 'Unknown date')


# Cap for the result-page "more episodes" fetch so a slow feed cannot stall
# the completed-transcript UI. The page loads this endpoint asynchronously.
RELATED_EPISODES_TIMEOUT = 8
RELATED_EPISODES_LIMIT = 5


#: Timed (show page) feed fetch: byte ceiling and newest-items early stop.
SHOW_FEED_MAX_BYTES = 8 * 1024 * 1024
SHOW_FEED_EARLY_STOP_ITEMS = 25


def get_episodes_from_rss(rss_url, *, timeout=None):
    """Parse RSS feed and return (episodes, error). Feed title lands on each episode.

    When ``timeout`` is set, the feed body is fetched with requests (SSRF-checked)
    so a hung host cannot block the caller indefinitely. The default path keeps
    feedparser's own fetch for existing callers.
    """
    try:
        if timeout is not None:
            if not _is_fetchable_url(rss_url):
                return None, "That feed URL cannot be fetched."
            # Show pages: per-hop SSRF revalidation on redirects, a byte cap,
            # and early stop after the newest items so a huge or slow feed
            # cannot pin a request thread or blow memory.
            body = _fetch_feed_capped(
                rss_url,
                max_bytes=SHOW_FEED_MAX_BYTES,
                early_stop_items=SHOW_FEED_EARLY_STOP_ITEMS,
                timeout=timeout,
            )
            if body is None:
                return None, "Could not fetch that feed."
            feed = feedparser.parse(body)
        else:
            feed = feedparser.parse(rss_url)
        if not feed.entries:
            return None, "No episodes found in RSS feed"

        feed_title = getattr(feed.feed, 'title', '') or ''
        feed_image = ''
        if getattr(feed.feed, 'image', None):
            feed_image = feed.feed.image.get('href', '') or ''

        episodes = []
        for i, entry in enumerate(feed.entries):
            audio_url = None
            if hasattr(entry, 'enclosures'):
                for enc in entry.enclosures:
                    if enc.get('type', '').startswith('audio/'):
                        audio_url = enc.href
                        break

            if not audio_url:
                continue

            desc = entry.get('description', '')
            duration_secs = _parse_duration(entry.get('itunes_duration', ''))
            duration_min = duration_secs / 60 if duration_secs else None
            artwork = feed_image
            if getattr(entry, 'image', None):
                artwork = entry.image.get('href', '') or feed_image

            episode_link = (entry.get('link') or '').strip() or None
            show_link = (getattr(feed.feed, 'link', None) or '').strip() or None
            episodes.append({
                'index': i,
                'title': entry.title,
                'published': _format_published(entry),
                'audio_url': audio_url,
                'description': desc[:200] + '...' if len(desc) > 200 else desc,
                'duration_min': round(duration_min, 1) if duration_min else None,
                'estimated_cost': (
                    round(duration_min * WHISPER_COST_PER_MINUTE, 3) if duration_min else None
                ),
                'artwork': artwork,
                'podcast_name': feed_title,
                'episode_link': episode_link,
                'show_link': show_link,
            })

        if not episodes:
            return None, "No playable audio episodes found in this feed"

        return episodes, None
    except Exception as e:
        return None, f"Error parsing RSS feed: {e}"


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

def _auth_next_arg():
    """Raw next= from query or form; validated later via safe_next_url."""
    return request.values.get('next') or ''


def _login_url_preserving_next():
    """/login, keeping a safe next= so returning users land on the transcript."""
    nxt = _auth_next_arg()
    if nxt:
        return url_for('login', next=nxt)
    return url_for('login')


def _auth_next_type(candidate=None):
    """Coarse next= bucket for analytics — never the raw path (may be a task id)."""
    raw = candidate if candidate is not None else _auth_next_arg()
    if not raw:
        return 'none'
    path = safe_next_url(raw, default='')
    if not path:
        return 'other'
    if path.startswith('/transcription/'):
        return 'transcription'
    if path.startswith('/resume-transcription'):
        return 'resume'
    if path.startswith('/settings'):
        return 'settings'
    if path.startswith('/history'):
        return 'history'
    if path.startswith('/oauth/'):
        return 'oauth'
    return 'other'


def _analytics_anon_id():
    """Stable anonymous distinct_id for pre-auth events (no email/IP)."""
    aid = session.get('_ph_anon')
    if not aid:
        aid = secrets.token_hex(16)
        session['_ph_anon'] = aid
    return f'anon:{aid}'


def _stash_utm_from_request():
    """Remember utm_source from the query string for the eventual signup event."""
    src = (request.args.get('utm_source') or '').strip()
    if not src:
        return
    session[UTM_SESSION_KEY] = src[:64]


def _stash_first_touch_from_request():
    """Remember first landing path + Referer once per browser session.

    Skips probes, webhooks, static assets, and non-GET so bots/health checks
    do not overwrite a real first page. Path only — never query string.
    """
    if FIRST_LANDING_SESSION_KEY in session:
        return
    if request.method not in ('GET', 'HEAD'):
        return
    path = request.path or '/'
    if path.startswith('/static/') or _canonical_host_exempt(path):
        return
    # Admin/debug surfaces are not acquisition landings.
    if path.startswith('/admin') or path.startswith('/design'):
        return
    session[FIRST_LANDING_SESSION_KEY] = path[:128]
    session[FIRST_REFERRER_SESSION_KEY] = (request.referrer or '')[:512]


def _signup_country():
    """ISO country for signup analytics when a header provides it; else None.

    Prefers Cloudflare CF-IPCountry (skip XX/T1 unknowns). Falls back to a
    region subtag on Accept-Language (e.g. nb-NO → NO). Never guesses.
    """
    try:
        cf = (request.headers.get('CF-IPCountry') or '').strip().upper()
        if len(cf) == 2 and cf.isalpha() and cf not in ('XX', 'T1'):
            return cf
        raw = (request.headers.get('Accept-Language') or '').split(',')[0]
        tag = raw.split(';')[0].strip().replace('_', '-')
        parts = tag.split('-')
        if len(parts) >= 2 and len(parts[1]) == 2 and parts[1].isalpha():
            return parts[1].upper()
    except Exception:  # noqa: BLE001
        return None
    return None


def _first_touch_analytics_props(*, utm=None):
    """Consume stashed first landing/referrer into coarse signup props."""
    if (FIRST_LANDING_SESSION_KEY not in session
            and FIRST_REFERRER_SESSION_KEY not in session):
        return {}
    props = {}
    landing = session.pop(FIRST_LANDING_SESSION_KEY, None)
    ref = session.pop(FIRST_REFERRER_SESSION_KEY, None) or ''
    if landing:
        props['first_landing'] = str(landing)[:128]
    props['first_referrer_source'] = show_pages_mod.referrer_source(
        ref, utm or '')
    return props


def _utm_source_for_signup():
    """utm_source from session (stashed) or the signup form/query, if any."""
    src = session.pop(UTM_SESSION_KEY, None)
    if not src:
        src = (request.values.get('utm_source') or '').strip() or None
    if not src:
        return None
    return str(src)[:64]


@app.before_request
def _stash_first_touch():
    """Remember the first navigable page + Referer for signup analytics."""
    _stash_first_touch_from_request()


def _capture_register_failed(reason):
    """register_failed — coarse reason only; never email/password."""
    product_analytics.capture(
        'register_failed',
        _analytics_anon_id(),
        {
            'reason': reason,
            '$process_person_profile': False,
        },
    )


def _capture_login_wall_shown(next_type):
    """login_wall_shown when /login is rendered for an anonymous visitor."""
    product_analytics.capture(
        'login_wall_shown',
        _analytics_anon_id(),
        {
            'next_type': next_type,
            '$process_person_profile': False,
        },
    )


def _redirect_after_auth():
    """Send a newly authenticated user to a safe next URL, or resume a stash."""
    if session.get(PENDING_TRANSCRIPTION_KEY):
        return redirect(url_for('resume_transcription'))
    return redirect(safe_next_url(_auth_next_arg(), url_for('index')))


def _register_template(**extra):
    pending = session.get(PENDING_TRANSCRIPTION_KEY)
    return render_template(
        'register.html',
        next=_auth_next_arg(),
        trial_minutes=advertised_trial_minutes(),
        pending=pending,
        **extra,
    )


def _persist_login(user):
    """Mark the session permanent and set the Flask-Login remember cookie."""
    session.permanent = True
    login_user(user, remember=True)


@app.route('/signup')
def signup_redirect():
    """Common guess for /register — keep the query string (e.g. ?next=…)."""
    target = url_for('register')
    qs = request.query_string.decode('utf-8', errors='replace') if request.query_string else ''
    if qs:
        target = f'{target}?{qs}'
    return redirect(target, code=301)


@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return _redirect_after_auth()

    _stash_utm_from_request()

    if request.method != 'POST':
        return _register_template()

    email = request.form.get('email', '').strip().lower()
    password = request.form.get('password', '')

    if not email or not password:
        _capture_register_failed('missing_fields')
        flash('Email and password are required.', 'error')
        return _register_template()

    ip = _client_ip()
    slot = register_reserve_slot(ip)
    if slot is None:
        _capture_register_failed('rate_limited')
        flash('Too many accounts created from this address. Try again later.', 'error')
        return _register_template()

    # The slot is held for the rest of this request and released unless an
    # account is actually created, so validation failures cost the user nothing
    # while a parallel burst still cannot exceed the limit.
    created = False
    try:
        if is_disposable_email(email):
            _capture_register_failed('disposable_email')
            flash('Please register with a real email address.', 'error')
            return _register_template()

        if '@' not in email or '.' not in email.rsplit('@', 1)[-1]:
            _capture_register_failed('invalid_email')
            flash('Please enter a valid email address.', 'error')
            return _register_template()

        if len(password) < 8:
            _capture_register_failed('password_too_short')
            flash('Password must be at least 8 characters.', 'error')
            return _register_template()

        if User.query.filter_by(email=email).first():
            _capture_register_failed('already_exists')
            login_href = html_lib.escape(_login_url_preserving_next(), quote=True)
            flash(Markup(
                'An account with this email already exists. '
                f'<a href="{login_href}">Log in instead</a>.'
            ), 'error')
            return _register_template()

        # Flush first so we have a stable user id for deterministic split
        # assignment; then stamp limit + variant before commit.
        user = User(email=email)
        user.set_password(password)
        db.session.add(user)
        db.session.flush()
        variant, grant_seconds = assign_trial_variant(user.id)
        user.trial_seconds_limit = grant_seconds
        user.trial_variant = variant
        db.session.commit()
        created = True

        _persist_login(user)
        granted_min = grant_seconds // 60
        signup_props = {
            # Coarse intent buckets only (never the raw next path / episode).
            'next_type': _auth_next_type(),
            'has_pending_transcript': bool(
                session.get(PENDING_TRANSCRIPTION_KEY)),
            'trial_granted_min': granted_min,
        }
        if variant:
            signup_props['trial_variant'] = variant
            signup_props['$set'] = {'trial_variant': variant}
        utm = _utm_source_for_signup()
        if utm:
            signup_props['utm_source'] = utm
        country = _signup_country()
        if country:
            signup_props['country'] = country
        signup_props.update(_first_touch_analytics_props(utm=utm))
        product_analytics.capture('user_signed_up', user.id, signup_props)
        if session.get(PENDING_TRANSCRIPTION_KEY):
            flash('Account created — starting your transcript.', 'success')
        else:
            flash(
                f'Account created — you have {granted_min} free minutes. '
                'Paste your link again to start.',
                'success',
            )
        return _redirect_after_auth()
    finally:
        if not created:
            register_release_slot(ip, slot)


@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return _redirect_after_auth()

    next_arg = _auth_next_arg()
    next_type = _auth_next_type(next_arg)

    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')

        user = User.query.filter_by(email=email).first()
        if user and user.check_password(password):
            _persist_login(user)
            return _redirect_after_auth()

        flash('Invalid email or password.', 'error')
    else:
        _capture_login_wall_shown(next_type)

    return render_template(
        'login.html',
        next=next_arg,
        login_for_transcript=(next_type == 'transcription'),
    )


def _password_reset_security_headers(resp):
    """Token pages must not be indexed or leak the credential via Referer."""
    resp.headers['X-Robots-Tag'] = 'noindex'
    resp.headers['Referrer-Policy'] = 'no-referrer'
    resp.headers['Cache-Control'] = 'no-store'
    return resp


def _forgot_password_template(**extra):
    resp = make_response(render_template(
        'forgot_password.html',
        mail_ready=mailer.mail_ready(),
        **extra,
    ))
    resp.headers['X-Robots-Tag'] = 'noindex'
    return resp


def _reset_password_template(token, *, expired=False, invalid=False,
                             status=200, **extra):
    resp = make_response(render_template(
        'reset_password.html',
        token=token,
        expired=expired,
        invalid=invalid,
        **extra,
    ), status)
    return _password_reset_security_headers(resp)


@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    """Request a password-reset email. Always the same neutral success copy."""
    if current_user.is_authenticated:
        return redirect(url_for('settings'))

    if request.method != 'POST':
        return _forgot_password_template()

    started = time.monotonic()
    if not validate_csrf_token():
        _password_reset_pad(started)
        flash('Something went wrong. Please try again.', 'error')
        return _forgot_password_template()

    if not mailer.mail_ready():
        _password_reset_pad(started)
        return _forgot_password_template()

    email = request.form.get('email', '').strip().lower()
    ip = _client_ip()
    # Rate-limit before any DB lookup. Under the limit we still show the
    # neutral message (no account enumeration via a distinct error).
    if _password_reset_rate_limited(f'ip:{ip}', f'email:{email}' if email else ''):
        _password_reset_pad(started)
        flash('Too many reset requests. Try again later.', 'error')
        return _forgot_password_template()

    user = User.query.filter_by(email=email).first() if email and '@' in email else None
    if user is not None:
        # Invalidate outstanding tokens for this user (single active link).
        now = datetime.now(timezone.utc)
        PasswordResetToken.query.filter(
            PasswordResetToken.user_id == user.id,
            PasswordResetToken.used_at.is_(None),
        ).update({'used_at': now}, synchronize_session=False)

        raw = secrets.token_urlsafe(PASSWORD_RESET_TOKEN_BYTES)
        row = PasswordResetToken(
            user_id=user.id,
            token_hash=_hash_password_reset_token(raw),
            expires_at=now + timedelta(seconds=PASSWORD_RESET_TTL_SECONDS),
        )
        db.session.add(row)
        db.session.commit()

        reset_url = public_url('reset_password', token=raw)
        # Security mail: send even if the user unsubscribed from digests.
        outcome = email_notify.send_password_reset_email(
            to=user.email, reset_url=reset_url, user_id=user.id)
        if outcome == mailer.SEND_SENT:
            product_analytics.capture('password_reset_requested', user.id)
            app.logger.info('password_reset_requested user_id=%s', user.id)
        else:
            # Do not surface delivery failure — same neutral copy either way.
            app.logger.warning(
                'password_reset email failed user_id=%s outcome=%s',
                user.id, outcome)

    _password_reset_pad(started)
    flash(PASSWORD_RESET_NEUTRAL_MSG, 'success')
    return _forgot_password_template(submitted=True)


@app.route('/reset-password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    """Show the new-password form (GET) or apply it (POST). Link-scanner safe."""
    token = (token or '').strip()
    if not token or len(token) > 128:
        return _reset_password_template('', invalid=True, status=404)

    token_hash = _hash_password_reset_token(token)
    row = PasswordResetToken.query.filter_by(token_hash=token_hash).first()
    now = datetime.now(timezone.utc)

    if row is None:
        return _reset_password_template(token, invalid=True, status=404)

    expires_at = row.expires_at
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    if row.used_at is not None:
        return _reset_password_template(token, invalid=True, status=404)

    if expires_at is not None and expires_at <= now:
        return _reset_password_template(token, expired=True, status=410)

    if request.method != 'POST':
        return _reset_password_template(token)

    if not validate_csrf_token():
        flash('Something went wrong. Please try again.', 'error')
        return _reset_password_template(token)

    password = request.form.get('password', '')
    password2 = request.form.get('password2', '')
    if len(password) < 8:
        flash('Password must be at least 8 characters.', 'error')
        return _reset_password_template(token)
    if password != password2:
        flash('Passwords do not match.', 'error')
        return _reset_password_template(token)

    user = db.session.get(User, row.user_id)
    if user is None:
        return _reset_password_template(token, invalid=True, status=404)

    # Claim the token first (conditional) so a double-submit cannot reset twice.
    claimed = db.session.execute(
        sa_update(PasswordResetToken)
        .where(
            PasswordResetToken.id == row.id,
            PasswordResetToken.used_at.is_(None),
        )
        .values(used_at=now)
    ).rowcount
    if not claimed:
        db.session.rollback()
        return _reset_password_template(token, invalid=True, status=404)

    user.set_password(password)
    user.session_version = int(getattr(user, 'session_version', 0) or 0) + 1
    # Supersede any other outstanding tokens for this account.
    PasswordResetToken.query.filter(
        PasswordResetToken.user_id == user.id,
        PasswordResetToken.id != row.id,
        PasswordResetToken.used_at.is_(None),
    ).update({'used_at': now}, synchronize_session=False)
    db.session.commit()

    product_analytics.capture('password_reset_completed', user.id)
    app.logger.info('password_reset_completed user_id=%s', user.id)

    # Confirmation mail is best-effort; reset already succeeded.
    try:
        email_notify.send_password_changed_email(to=user.email, user_id=user.id)
    except Exception:  # noqa: BLE001
        app.logger.exception(
            'password_changed email failed user_id=%s', user.id)

    _persist_login(user)
    flash('Your password has been updated.', 'success')
    resp = redirect(url_for('index'))
    return _password_reset_security_headers(resp)


def _stash_pending_from_request():
    """Pull episode fields off the request into the session. Returns an error or None."""
    language = normalize_language_code(request.form.get('language', ''))

    audio_url = (request.form.get('audio_url') or '').strip()
    rss_url = (request.form.get('rss_url') or '').strip() or None
    # Optional on the direct-audio path; never stash a private/unfetchable feed.
    if rss_url and not _is_fetchable_url(rss_url):
        rss_url = None
    episode_index_raw = request.form.get('episode_index')

    pending = {
        'language': language,
        'rss_url': rss_url,
        'episode_index': None,
        'title': (request.form.get('episode_title') or '').strip() or 'Episode',
        'audio_url': audio_url,
        'podcast_name': (request.form.get('podcast_name') or '').strip() or None,
        'artwork': (request.form.get('artwork') or '').strip() or None,
        'published': (request.form.get('published') or '').strip() or None,
        'duration_min': _positive_float_or_none(request.form.get('duration_min')),
        'input_origin': (request.form.get('input_origin') or '').strip() or None,
        'spotify_url': (request.form.get('spotify_url') or '').strip() or None,
        'apple_url': (request.form.get('apple_url') or '').strip() or None,
        'episode_link': (request.form.get('episode_link') or '').strip() or None,
        'website_url': (request.form.get('website_url') or '').strip() or None,
        'show_link': (request.form.get('show_link') or '').strip() or None,
    }

    if audio_url:
        if not _is_fetchable_url(audio_url):
            return 'That audio URL cannot be fetched.'
    elif rss_url and episode_index_raw not in (None, ''):
        try:
            pending['episode_index'] = int(episode_index_raw)
        except (TypeError, ValueError):
            return 'Invalid episode selection'
        parsed = urlparse(rss_url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname:
            return 'Invalid episode selection'
    else:
        return 'Pick an episode first'

    session[PENDING_TRANSCRIPTION_KEY] = pending
    session.modified = True
    return None


@app.route('/pending-transcription', methods=['POST'])
def pending_transcription():
    """Stash the chosen episode, then send anonymous visitors to sign up.

    ChatGPT visitors paste a Spotify link, hit Transcribe, and used to land on
    /login with the episode gone. We keep the pick in the session and resume
    after register/login.
    """
    err = _stash_pending_from_request()
    if err:
        flash(err, 'error')
        return redirect(url_for('index'))

    if current_user.is_authenticated:
        return redirect(url_for('resume_transcription'))

    # Prefer signup: every ChatGPT visitor in the sample signed up, not logged in.
    return redirect(url_for('register', next=url_for('resume_transcription')))


@app.route('/resume-transcription', methods=['GET', 'POST'])
@login_required
def resume_transcription():
    """Start the episode stashed before signup/login, if any."""
    pending = session.pop(PENDING_TRANSCRIPTION_KEY, None)
    if not pending:
        flash('Nothing to resume — search for an episode to transcribe.', 'info')
        return redirect(url_for('index'))

    language = normalize_language_code(pending.get('language') or '')
    rss_url = pending.get('rss_url')
    episode_index = pending.get('episode_index')
    pending_origin = pending.get('input_origin') or ''

    if rss_url is not None and episode_index is not None:
        episodes, error = get_episodes_from_rss(rss_url)
        if error or episode_index < 0 or episode_index >= len(episodes):
            flash(error or 'Invalid episode selection', 'error')
            return redirect(url_for('index'))
        episode = episodes[episode_index]
        meta = {
            'title': episode['title'],
            'audio_url': episode['audio_url'],
            'podcast_name': pending.get('podcast_name') or episode.get('podcast_name'),
            'artwork': episode.get('artwork') or pending.get('artwork'),
            'published': episode.get('published'),
            'duration_min': _positive_float_or_none(episode.get('duration_min')),
            'input_origin': pending_origin or 'rss',
            'spotify_url': pending.get('spotify_url') or '',
            'apple_url': pending.get('apple_url') or '',
            'episode_link': episode.get('episode_link') or pending.get('episode_link') or '',
            'show_link': episode.get('show_link') or pending.get('show_link') or '',
        }
    else:
        audio_url = (pending.get('audio_url') or '').strip()
        if not audio_url or not _is_fetchable_url(audio_url):
            flash('That audio URL cannot be fetched.', 'error')
            return redirect(url_for('index'))
        meta = {
            'title': pending.get('title') or 'Episode',
            'audio_url': audio_url,
            'podcast_name': pending.get('podcast_name'),
            'artwork': pending.get('artwork'),
            'published': pending.get('published'),
            'duration_min': _positive_float_or_none(pending.get('duration_min')),
            'input_origin': pending_origin,
            'spotify_url': pending.get('spotify_url') or '',
            'apple_url': pending.get('apple_url') or '',
            'episode_link': pending.get('episode_link') or '',
            'website_url': pending.get('website_url') or '',
            'show_link': pending.get('show_link') or '',
        }

    payload, status = enqueue_transcription(
        current_user, meta, rss_url=rss_url, language=language)
    if status != 200:
        # Surface the refusal (trial limit, capacity, …) the same way a direct
        # start would via flash, since this is a browser redirect not XHR.
        flash(payload.get('error') or 'Could not start transcription.', 'error')
        return redirect(url_for('index'))
    flash(
        "Your transcript has started. You can close this page; it'll be in History.",
        'success',
    )
    return redirect(url_for('transcription_page', task_id=payload['task_id']))


@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('Logged out.', 'info')
    # ph_reset tells the client SDK to call posthog.reset() so the next visitor
    # on this browser is not glued to the previous distinct_id.
    return redirect(url_for('index', ph_reset=1))


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def _openai_key_hint(user):
    """Last-4 hint for a saved OpenAI key. Never return the full key to a template."""
    key = getattr(user, 'openai_api_key', None) or ''
    if len(key) < 4:
        return ''
    return key[-4:]


def generate_csrf_token():
    """Session CSRF token for billing Buy POSTs and password-reset forms.

    Prefer calling only from templates that actually POST (Buy, forgot/
    reset password). Minting writes the session cookie.
    """
    token = session.get('_csrf_token')
    if not token:
        token = secrets.token_hex(32)
        session['_csrf_token'] = token
    return token


def validate_csrf_token():
    """True when the submitted csrf_token matches the session value."""
    expected = session.get('_csrf_token')
    got = (request.form.get('csrf_token')
           or request.headers.get('X-CSRF-Token')
           or '')
    if not expected or not got:
        return False
    return hmac.compare_digest(str(expected), str(got))


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _session_payment_intent_id(session_dict):
    pi = session_dict.get('payment_intent')
    if isinstance(pi, dict):
        return pi.get('id')
    if isinstance(pi, str) and pi:
        return pi
    return None


def _session_customer_country(session_dict):
    details = session_dict.get('customer_details') or {}
    address = details.get('address') or {}
    country = (address.get('country') or '').strip().upper()
    return country[:2] or None


def pack_session_matches(d):
    """Validate a retrieved Checkout Session bought our credit pack.

    Checks what was bought (line item / unit amount / pack marker), not only
    amount_total — so inclusive Stripe Tax (total stays 500) and exclusive
    tax (subtotal 500, total = subtotal + tax) both credit correctly.
    """
    if (d.get('currency') or '').lower() != CREDIT_PACK_CURRENCY:
        return False, 'currency'
    if (d.get('metadata') or {}).get('pack') != CREDIT_PACK_SKU:
        return False, 'pack_marker'
    items = ((d.get('line_items') or {}).get('data') or [])
    if len(items) != 1 or items[0].get('quantity') != 1:
        return False, 'line_items'
    price = items[0].get('price') or {}
    if isinstance(price, str):
        return False, 'price_unexpanded'
    if STRIPE_PRICE_ID and price.get('id') != STRIPE_PRICE_ID:
        return False, 'price_id'
    if price.get('unit_amount') != CREDIT_PACK_AMOUNT_CENTS:
        return False, 'unit_amount'
    td = d.get('total_details') or {}
    if (td.get('amount_discount') or 0) != 0:
        return False, 'discount'
    tax = td.get('amount_tax') or 0
    total, subtotal = d.get('amount_total'), d.get('amount_subtotal')
    behavior = (price.get('tax_behavior') or CREDIT_PACK_TAX_BEHAVIOR or '').lower()
    if behavior == 'inclusive':
        ok = total == CREDIT_PACK_AMOUNT_CENTS
    else:
        ok = (
            subtotal == CREDIT_PACK_AMOUNT_CENTS
            and total is not None
            and total == (subtotal or 0) + tax
        )
    if ok:
        return True, 'ok'
    return False, f'amount total={total} subtotal={subtotal} tax={tax}'


def _record_needs_review(session_dict, user_id, event_id, reason):
    """Persist a paid-but-unfulfillable session for manual reconciliation.

    Never changes the balance. Unique on stripe_session_id so duplicates are
    no-ops. Returns True when a new row was written.
    """
    session_id = session_dict.get('id')
    if not session_id:
        return False
    td = session_dict.get('total_details') or {}
    subtotal = session_dict.get('amount_subtotal')
    tax = td.get('amount_tax')
    total = session_dict.get('amount_total')
    try:
        amount_cents = int(subtotal if subtotal is not None else (total or 0))
    except (TypeError, ValueError):
        amount_cents = 0
    purchase = CreditPurchase(
        user_id=user_id,
        stripe_session_id=session_id,
        stripe_event_id=event_id,
        stripe_payment_intent_id=_session_payment_intent_id(session_dict),
        amount_cents=amount_cents,
        amount_subtotal_cents=_int_or_none(subtotal),
        amount_tax_cents=_int_or_none(tax),
        amount_total_cents=_int_or_none(total),
        currency=(session_dict.get('currency') or CREDIT_PACK_CURRENCY).lower(),
        customer_country=_session_customer_country(session_dict),
        minutes=0,
        status='needs_review',
    )
    db.session.add(purchase)
    try:
        db.session.commit()
        return True
    except SaIntegrityError:
        db.session.rollback()
        return False
    except Exception:
        db.session.rollback()
        app.logger.exception(
            'Failed to record needs_review for Stripe session %s (%s)',
            session_id, reason)
        return False


def _ph_uuid5(name):
    """Stable PostHog event uuid from a durable Stripe/object id."""
    return uuid.uuid5(uuid.NAMESPACE_URL, str(name))


def _consented_posthog_session_id(raw):
    """Return a client PostHog session id only when analytics consent is on.

    Server captures use the internal user id as distinct_id and never mint
    browser cookies. $session_id / Stripe ph_sid metadata are cookie-derived,
    so they are dropped unless podskrift_cookie_consent=accepted.
    """
    if not has_request_context():
        return ''
    if request.cookies.get(COOKIE_CONSENT_NAME) != COOKIE_CONSENT_ACCEPTED:
        return ''
    return (raw or '').strip()[:128]


def _capture_purchase_failed(reason, *, stage, user_id=None, session_id=None,
                             extra=None):
    """purchase_failed — never include email/key/card. Anonymous → stripe:<cs_id>."""
    props = {'stage': stage, 'reason': reason}
    if extra:
        props.update(extra)
    if user_id is not None:
        distinct_id = user_id
    elif session_id:
        distinct_id = f'stripe:{session_id}'
        props['$process_person_profile'] = False
    else:
        distinct_id = 'stripe:unknown'
        props['$process_person_profile'] = False
    if session_id:
        props.setdefault('checkout_session_id', session_id)
    product_analytics.capture('purchase_failed', distinct_id, props)


def _capture_checkout_returned(status, *, user_id, session_id=None, location=None,
                               extra=None):
    props = {'status': status}
    if session_id:
        props['checkout_session_id'] = session_id
    if location:
        props['location'] = location
    if extra:
        props.update(extra)
    product_analytics.capture('checkout_returned', user_id, props)


def _capture_checkout_expired(obj):
    """checkout_expired from the Stripe checkout.session.expired webhook.

    Abandoned checkouts are otherwise invisible: closing the Stripe tab never
    hits /billing/cancel. Server-side, no email/card; uuid5 dedupes retries.
    """
    d = obj.to_dict() if hasattr(obj, 'to_dict') else (
        obj if isinstance(obj, dict) else {})
    cs_id = d.get('id')
    meta = d.get('metadata') or {}
    ref = str(d.get('client_reference_id') or meta.get('user_id') or '').strip()
    props = {
        'checkout_session_id': cs_id,
        'location': meta.get('location') or meta.get('source') or None,
        'pack_sku': meta.get('pack') or None,
        'amount_cents': d.get('amount_total'),
        'currency': d.get('currency'),
        'recovery_enabled': bool(
            ((d.get('after_expiration') or {}).get('recovery') or {})
            .get('enabled')),
    }
    created, expires = d.get('created'), d.get('expires_at')
    if isinstance(created, int) and isinstance(expires, int) and expires >= created:
        props['open_minutes'] = (expires - created) // 60
    try:
        minutes = int(meta.get('minutes'))
        props['minutes'] = minutes
    except (TypeError, ValueError):
        pass
    if ref.isdigit():
        distinct_id = int(ref)
        props.update(_balance_analytics_props(distinct_id))
    else:
        distinct_id = f'stripe:{cs_id or "unknown"}'
        props['$process_person_profile'] = False
    product_analytics.capture(
        'checkout_expired', distinct_id, props,
        uuid=_ph_uuid5(f'expired:{cs_id}') if cs_id else None)


def _capture_stripe_webhook_error(reason, *, event_type=None, extra=None):
    props = {
        'reason': reason,
        '$process_person_profile': False,
    }
    if event_type:
        props['event_type'] = event_type
    if extra:
        props.update(extra)
    product_analytics.capture(
        'stripe_webhook_error', 'system:stripe-webhook', props)


def _platform_remaining_seconds(user_id):
    """Combined free-trial + paid seconds remaining for a user id."""
    user = db.session.get(User, user_id)
    if user is None:
        return 0, 0
    if trial_available():
        trial_rem = trial_status(user)[2]
    else:
        trial_rem = 0
    paid_rem = paid_balance_seconds(user)
    return max(0, int(trial_rem)), max(0, int(paid_rem))


def _capture_minutes_exhausted_if_depleted(user_id, source, before_trial, before_paid):
    """Fire minutes_exhausted only when a charge took trial+paid from >0 to 0.

    Dual-fires paid_minutes_exhausted for now. kind is trial|paid|both based on
    which balances crossed to zero.
    """
    after_trial, after_paid = _platform_remaining_seconds(user_id)
    before_total = before_trial + before_paid
    after_total = after_trial + after_paid
    if before_total <= 0 or after_total > 0:
        return
    trial_hit = before_trial > 0 and after_trial == 0
    paid_hit = before_paid > 0 and after_paid == 0
    if trial_hit and paid_hit:
        kind = 'both'
    elif paid_hit:
        kind = 'paid'
    else:
        kind = 'trial'
    props = {'kind': kind, 'source': source}
    product_analytics.capture('minutes_exhausted', user_id, props)
    product_analytics.capture('paid_minutes_exhausted', user_id, props)


def _days_since_purchase(purchase):
    created = purchase.created_at
    if created is None:
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - created
    return max(0, int(delta.total_seconds() // 86400))


def _capture_refund_processed(purchase, *, kind, refund_delta_cents,
                              amount_refunded_cents, full_refund,
                              seconds_clawed_back, claw_target, payment_intent,
                              event_id_for_uuid):
    """refund_processed + dual-fire purchase_refunded. Never email/key/card."""
    if purchase.user_id is None:
        return
    shortfall = max(0, int(claw_target) - int(seconds_clawed_back))
    props = {
        'kind': kind,
        'refund_delta_cents': int(refund_delta_cents),
        'amount_refunded_cents': int(amount_refunded_cents),
        'full_refund': bool(full_refund),
        'seconds_clawed_back': int(seconds_clawed_back),
        'seconds_shortfall': shortfall,
        'days_since_purchase': _days_since_purchase(purchase),
        'payment_intent': payment_intent,
    }
    event_uuid = _ph_uuid5(f'{event_id_for_uuid}:{amount_refunded_cents}')
    product_analytics.capture(
        'refund_processed', purchase.user_id, props, uuid=event_uuid)
    refunded_props = {
        'seconds_clawed_back': int(seconds_clawed_back),
        'amount_refunded_cents': int(amount_refunded_cents),
        'payment_intent': payment_intent,
    }
    if kind == 'dispute':
        refunded_props['reason'] = 'dispute'
    product_analytics.capture(
        'purchase_refunded', purchase.user_id, refunded_props, uuid=event_uuid)


def fulfill_checkout(session_id, event_id=None, fulfilled_via='webhook'):
    """Retrieve and fulfill a Checkout Session. Idempotent; safe from webhook
    and from /billing/success.

    Credits only when payment_status=='paid' and pack_session_matches.
    Mismatched paid sessions are recorded as needs_review + Sentry, and this
    still returns False so the webhook can reply 2xx (retries will not help).
    """
    if not session_id or not str(session_id).startswith('cs_'):
        return False
    client = stripe_client()
    if client is None:
        return False
    try:
        session_obj = client.v1.checkout.sessions.retrieve(
            session_id, params={'expand': ['line_items.data.price']})
    except Exception:
        app.logger.exception('Stripe Checkout Session retrieve failed (%s)', session_id)
        _capture_purchase_failed(
            'retrieve_error', stage='fulfill', session_id=session_id)
        raise
    d = session_obj.to_dict() if hasattr(session_obj, 'to_dict') else dict(session_obj)
    if d.get('mode') and d.get('mode') != 'payment':
        return False
    if d.get('payment_status') != 'paid':
        # Async methods complete unpaid first; wait for async_payment_succeeded.
        return False

    ok, reason = pack_session_matches(d)
    metadata = d.get('metadata') or {}
    user_id = _int_or_none(d.get('client_reference_id') or metadata.get('user_id'))
    if not ok or user_id is None:
        fail_reason = reason if not ok else 'missing_user'
        app.logger.error(
            'Stripe session %s not credited: %s', session_id, fail_reason)
        _record_needs_review(d, user_id, event_id, fail_reason)
        _sentry_capture_message(
            f'Stripe session {session_id} not credited: {fail_reason}',
            level='error',
            contexts={'stripe': {'session_id': session_id, 'reason': fail_reason}},
        )
        _capture_purchase_failed(
            fail_reason, stage='fulfill', user_id=user_id, session_id=session_id)
        return False

    td = d.get('total_details') or {}
    subtotal = d.get('amount_subtotal')
    tax = td.get('amount_tax')
    total = d.get('amount_total')
    try:
        amount_for_legacy = int(
            subtotal if subtotal is not None else total)
    except (TypeError, ValueError):
        amount_for_legacy = CREDIT_PACK_AMOUNT_CENTS
    currency = (d.get('currency') or CREDIT_PACK_CURRENCY).lower()
    minutes = CREDIT_PACK_MINUTES
    customer_country = _session_customer_country(d)
    payment_intent = _session_payment_intent_id(d)
    location = (metadata.get('location') or metadata.get('source') or '')[:64] or None
    ph_sid = (metadata.get('ph_sid') or '')[:128] or None

    prior_credited = CreditPurchase.query.filter_by(
        user_id=user_id, status='credited').count()

    purchase = CreditPurchase(
        user_id=user_id,
        stripe_session_id=session_id,
        stripe_event_id=event_id,
        stripe_payment_intent_id=payment_intent,
        amount_cents=amount_for_legacy,
        amount_subtotal_cents=_int_or_none(subtotal),
        amount_tax_cents=_int_or_none(tax),
        amount_total_cents=_int_or_none(total),
        currency=currency,
        customer_country=customer_country,
        minutes=minutes,
        status='credited',
    )
    db.session.add(purchase)
    try:
        db.session.flush()
    except SaIntegrityError:
        # Unique stripe_session_id — already fulfilled.
        db.session.rollback()
        return False

    result = db.session.execute(text("""
        UPDATE users
           SET paid_seconds_balance = COALESCE(paid_seconds_balance, 0) + :n
         WHERE id = :uid
    """), {'n': minutes * 60, 'uid': user_id})
    if result.rowcount != 1:
        db.session.rollback()
        app.logger.error(
            'Stripe session %s: no user %s to credit', session_id, user_id)
        _record_needs_review(d, user_id, event_id, 'user_missing')
        _sentry_capture_message(
            f'Stripe session {session_id} not credited: user_missing',
            level='error',
            contexts={'stripe': {'session_id': session_id, 'user_id': user_id}},
        )
        _capture_purchase_failed(
            'user_missing', stage='fulfill', user_id=user_id, session_id=session_id)
        return False
    try:
        db.session.commit()
    except SaIntegrityError:
        db.session.rollback()
        return False

    amount_total_cents = _int_or_none(total)
    amount_subtotal_cents = _int_or_none(subtotal)
    amount_tax_cents = _int_or_none(tax)
    person_set = {'has_purchased': True}
    variant_props = trial_variant_props(user_id)
    if variant_props.get('trial_variant'):
        person_set['trial_variant'] = variant_props['trial_variant']
    completed_props = {
        'amount_cents': amount_for_legacy,
        'amount_total_cents': amount_total_cents,
        'amount_subtotal_cents': amount_subtotal_cents,
        'amount_tax_cents': amount_tax_cents,
        'minutes': minutes,
        'currency': currency,
        'country': customer_country,
        'checkout_session_id': session_id,
        'payment_intent': payment_intent,
        'fulfilled_via': fulfilled_via,
        'location': location,
        'is_first_purchase': prior_credited == 0,
        'revenue': (amount_total_cents or amount_for_legacy or 0) / 100.0,
        '$set': person_set,
    }
    completed_props.update(variant_props)
    if ph_sid:
        completed_props['$session_id'] = ph_sid
    product_analytics.capture(
        'purchase_completed',
        user_id,
        completed_props,
        uuid=_ph_uuid5(session_id),
    )
    return True


def credit_user_from_checkout_session(session_obj, event_id=None):
    """Thin wrapper: fulfill by session id (retrieves from Stripe)."""
    if not session_obj:
        return False
    if isinstance(session_obj, dict):
        session_id = session_obj.get('id')
    else:
        session_id = getattr(session_obj, 'id', None)
    return fulfill_checkout(session_id, event_id=event_id)


def _clawback_paid_seconds(user_id, seconds):
    """Subtract up to `seconds` from paid balance; never below zero.

    Returns the number of seconds actually removed.
    """
    if not user_id or seconds <= 0:
        return 0
    user = db.session.get(User, user_id)
    if user is None:
        return 0
    balance = int(user.paid_seconds_balance or 0)
    take = min(balance, int(seconds))
    if take <= 0:
        return 0
    db.session.execute(text("""
        UPDATE users
           SET paid_seconds_balance = CASE
                 WHEN COALESCE(paid_seconds_balance, 0) < :n THEN 0
                 ELSE paid_seconds_balance - :n
               END
         WHERE id = :uid
    """), {'n': take, 'uid': user_id})
    return take


def _claim_purchase_snapshot(purchase):
    """Lock the refund state this worker computed from, or fail loudly.

    Refund and dispute handlers read the purchase, compute a claw-back from
    it, then write. Two gunicorn workers handling a redelivered event both read
    the same snapshot and would both claw back. This conditional UPDATE takes
    SQLite's write lock and only matches if nobody moved the row since we read
    it; the loser raises, the webhook answers 500, and Stripe's redelivery
    recomputes from the winner's state.
    """
    moved = db.session.execute(text("""
        UPDATE credit_purchases SET status = status
         WHERE id = :pid
           AND COALESCE(amount_refunded_cents, 0) = :refunded
           AND COALESCE(seconds_clawed_back, 0) = :clawed
           AND status = :status
    """), {
        'pid': purchase.id,
        'refunded': int(purchase.amount_refunded_cents or 0),
        'clawed': int(purchase.seconds_clawed_back or 0),
        'status': purchase.status,
    })
    if moved.rowcount != 1:
        db.session.rollback()
        raise RuntimeError(f'purchase {purchase.id} changed under a concurrent refund')


def handle_charge_refunded(charge_dict, event_id=None):
    """Claw back unused paid minutes pro rata to the newly refunded fraction.

    Idempotent on cumulative charge.amount_refunded vs purchase.amount_refunded_cents.
    Never pushes paid_seconds_balance below zero.
    """
    if not charge_dict:
        return False
    pi = charge_dict.get('payment_intent')
    if isinstance(pi, dict):
        pi = pi.get('id')
    if not pi:
        app.logger.error('charge.refunded missing payment_intent (event %s)', event_id)
        return False
    purchase = CreditPurchase.query.filter_by(
        stripe_payment_intent_id=pi, status='credited').first()
    if purchase is None:
        # Already fully refunded/disputed, or never credited (needs_review).
        purchase = CreditPurchase.query.filter_by(
            stripe_payment_intent_id=pi).order_by(CreditPurchase.id.desc()).first()
        if purchase is None or purchase.status == 'needs_review':
            return False
    try:
        amount_refunded = int(charge_dict.get('amount_refunded') or 0)
    except (TypeError, ValueError):
        amount_refunded = 0
    try:
        amount_captured = int(
            charge_dict.get('amount_captured')
            or charge_dict.get('amount')
            or purchase.amount_total_cents
            or purchase.amount_cents
            or 0)
    except (TypeError, ValueError):
        amount_captured = 0
    already = int(purchase.amount_refunded_cents or 0)
    delta = amount_refunded - already
    if delta <= 0 or amount_captured <= 0:
        return False
    _claim_purchase_snapshot(purchase)
    pack_seconds = int(purchase.minutes or 0) * 60
    remaining = pack_seconds - int(purchase.seconds_clawed_back or 0)
    charge_id = charge_dict.get('id') or pi
    full_refund = amount_refunded >= amount_captured
    if remaining <= 0:
        purchase.amount_refunded_cents = amount_refunded
        if full_refund:
            purchase.status = 'refunded'
            purchase.refunded_at = purchase.refunded_at or datetime.now(timezone.utc)
        db.session.commit()
        _capture_refund_processed(
            purchase,
            kind='refund',
            refund_delta_cents=delta,
            amount_refunded_cents=amount_refunded,
            full_refund=full_refund,
            seconds_clawed_back=0,
            claw_target=0,
            payment_intent=pi,
            event_id_for_uuid=f'{charge_id}:{amount_refunded}',
        )
        return False
    # Pro-rata on the newly refunded fraction of the captured amount.
    claw_secs = min(remaining, (pack_seconds * delta) // amount_captured)
    taken = _clawback_paid_seconds(purchase.user_id, claw_secs)
    purchase.seconds_clawed_back = int(purchase.seconds_clawed_back or 0) + taken
    purchase.amount_refunded_cents = amount_refunded
    if full_refund:
        purchase.status = 'refunded'
        purchase.refunded_at = datetime.now(timezone.utc)
    db.session.commit()
    _capture_refund_processed(
        purchase,
        kind='refund',
        refund_delta_cents=delta,
        amount_refunded_cents=amount_refunded,
        full_refund=full_refund,
        seconds_clawed_back=taken,
        claw_target=claw_secs,
        payment_intent=pi,
        event_id_for_uuid=f'{charge_id}:{amount_refunded}',
    )
    return True


def handle_dispute(dispute_dict, event_type, event_id=None):
    """Treat dispute.created like a full refund of remaining pack minutes."""
    if event_type != 'charge.dispute.created':
        app.logger.info('Stripe dispute event %s ignored (%s)', event_type, event_id)
        return False
    if not dispute_dict:
        return False
    pi = dispute_dict.get('payment_intent')
    if isinstance(pi, dict):
        pi = pi.get('id')
    # Some dispute payloads only carry the charge id; charge.payment_intent is
    # preferred when present on an expanded charge. Fall back to looking up by
    # charge id stored nowhere — require payment_intent.
    if not pi:
        app.logger.error(
            'charge.dispute.created missing payment_intent (event %s)', event_id)
        _sentry_capture_message(
            f'Stripe dispute {dispute_dict.get("id")} missing payment_intent',
            level='error',
        )
        return False
    purchase = CreditPurchase.query.filter_by(
        stripe_payment_intent_id=pi).order_by(CreditPurchase.id.desc()).first()
    if purchase is None or purchase.status == 'needs_review':
        return False
    if purchase.status == 'disputed':
        return False
    _claim_purchase_snapshot(purchase)
    pack_seconds = int(purchase.minutes or 0) * 60
    remaining = pack_seconds - int(purchase.seconds_clawed_back or 0)
    taken = _clawback_paid_seconds(purchase.user_id, remaining)
    purchase.seconds_clawed_back = int(purchase.seconds_clawed_back or 0) + taken
    purchase.status = 'disputed'
    purchase.refunded_at = datetime.now(timezone.utc)
    # Treat as fully refunded for amount tracking.
    captured = (
        purchase.amount_total_cents
        or purchase.amount_cents
        or CREDIT_PACK_AMOUNT_CENTS)
    already = int(purchase.amount_refunded_cents or 0)
    purchase.amount_refunded_cents = max(already, int(captured))
    amount_refunded = int(purchase.amount_refunded_cents or 0)
    delta = max(0, amount_refunded - already)
    db.session.commit()
    dispute_id = dispute_dict.get('id') or pi
    _capture_refund_processed(
        purchase,
        kind='dispute',
        refund_delta_cents=delta,
        amount_refunded_cents=amount_refunded,
        full_refund=True,
        seconds_clawed_back=taken,
        claw_target=remaining,
        payment_intent=pi,
        event_id_for_uuid=f'{dispute_id}:{amount_refunded}',
    )
    return True


@app.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    if request.method == 'POST':
        # Email preference toggle (separate from the OpenAI key field).
        if request.form.get('form') == 'email_prefs':
            want = request.form.get('email_transcript_ready') == '1'
            current_user.email_transcript_ready = want
            if want and current_user.email_unsubscribed_at is not None:
                # Re-enabling transcript-ready clears a prior global unsub.
                current_user.email_unsubscribed_at = None
            db.session.commit()
            product_analytics.capture(
                'alert_opt_in' if want else 'alert_opt_out',
                current_user.id,
                {'channel': 'transcript_ready'},
            )
            flash('Email preferences saved.', 'success')
            return redirect(url_for('settings') + '#email')

        api_key = request.form.get('openai_api_key', '').strip()

        # Empty field means "leave the saved key alone". Clearing used to be the
        # same path, which meant a Save with the masked blank input wiped the
        # key — and the full key must never be re-rendered into the form anyway.
        if not api_key:
            flash('No changes made.', 'success')
            return redirect(url_for('settings'))

        ok, message, status = verify_openai_key(api_key)
        caveat = ok and status != 'verified'
        if not ok:
            # Never store a rejected key: it is frequently a password, and it
            # would otherwise sit in the database and fail again at transcribe time.
            product_analytics.capture(
                'openai_key_validation_failed',
                current_user.id,
                {'reason': status or 'other'},
            )
            flash(message, 'error')
            return redirect(url_for('settings'))

        current_user.openai_api_key = api_key
        db.session.commit()
        product_analytics.capture(
            'openai_key_saved',
            current_user.id,
            {'status': status or 'verified'},
        )
        if status == 'no_billing':
            session['openai_key_no_billing'] = True
        else:
            session.pop('openai_key_no_billing', None)
        flash(message, 'warning' if caveat else 'success')
        return redirect(url_for('settings'))

    # One-shot plaintext after generate (session, not DB).
    new_api_key = session.pop('new_api_key', None)
    no_billing_warning = bool(session.pop('openai_key_no_billing', False))
    product_analytics.capture('settings_viewed', current_user.id)
    trial_ctx = _trial_context()
    buy_source = (
        'header_pill' if request.args.get('from') == 'header_pill' else 'settings')
    connected_apps = []
    if oauth_server_mod.mcp_oauth_enabled():
        connected_apps = oauth_server_mod.list_connected_apps(current_user.id)
    return render_template(
        'settings.html',
        trial=trial_ctx,
        new_api_key=new_api_key,
        openai_key_hint=_openai_key_hint(current_user),
        buy_source=buy_source,
        no_billing_warning=no_billing_warning,
        mcp_oauth_enabled=oauth_server_mod.mcp_oauth_enabled(),
        connected_apps=connected_apps,
    )


@app.route('/billing/checkout', methods=['POST'])
@login_required
def billing_checkout():
    """Create a Stripe Checkout Session for the one-time credit pack."""
    if not stripe_checkout_enabled():
        flash('Card payments are not available right now.', 'error')
        return redirect(url_for('settings') + '#credits')
    if not validate_csrf_token():
        _capture_purchase_failed(
            'csrf_invalid', stage='checkout_create', user_id=current_user.id)
        flash('That form expired. Please try again.', 'error')
        return redirect(url_for('settings') + '#credits')
    if current_user.openai_api_key:
        source_probe = (request.form.get('source') or '').strip()[:64]
        if source_probe in BYOK_PACK_SOURCES:
            # Broken / zero-credit BYOK key: pack minutes only apply on our key,
            # so clear theirs when they explicitly choose the pack from the
            # no_billing surfaces.
            current_user.openai_api_key = None
            db.session.commit()
        else:
            _capture_purchase_failed(
                'byok_user', stage='checkout_create', user_id=current_user.id)
            flash('You are on your own OpenAI key — paid minutes are not needed.', 'info')
            return redirect(url_for('settings'))

    source = (request.form.get('source') or 'settings').strip()[:64]
    ph_sid = _consented_posthog_session_id(request.form.get('ph_sid'))
    trial_ctx = _trial_context() or {}
    trial_remaining_min = trial_ctx.get('remaining_minutes')
    paid_remaining_min = trial_ctx.get('paid_minutes')

# Optional stash so /billing/success can offer "Start transcript: …".
    if (request.form.get('rss_url') or request.form.get('audio_url')):
        _stash_pending_from_request()

    # Prefer explicit return_to (buy modal / sticky bar); fall back to cancel_url.
    return_to = safe_return_to(
        request.form.get('return_to') or request.form.get('cancel_url') or '')
    if return_to:
        session[BILLING_RETURN_TO_KEY] = return_to
        session.modified = True
    else:
        session.pop(BILLING_RETURN_TO_KEY, None)

    next_path = return_to or safe_next_url(
        request.form.get('cancel_url') or (url_for('settings') + '#credits'),
        url_for('settings') + '#credits',
    )
    if PUBLIC_BASE_URL:
        success_url = (
            PUBLIC_BASE_URL.rstrip('/')
            + '/billing/success?session_id={CHECKOUT_SESSION_ID}')
        cancel_abs = (
            PUBLIC_BASE_URL.rstrip('/')
            + url_for('billing_cancel', next=next_path))
    else:
        success_url = (
            url_for('billing_success', _external=True)
            + '?session_id={CHECKOUT_SESSION_ID}')
        cancel_abs = url_for('billing_cancel', next=next_path, _external=True)

    line_item = {'quantity': 1}
    if STRIPE_PRICE_ID:
        line_item['price'] = STRIPE_PRICE_ID
    else:
        line_item['price_data'] = {
            'currency': CREDIT_PACK_CURRENCY,
            'unit_amount': CREDIT_PACK_AMOUNT_CENTS,
            'tax_behavior': CREDIT_PACK_TAX_BEHAVIOR,
            'product_data': {
                'name': f'Podskrift — {CREDIT_PACK_MINUTES} minutes',
                'description': f'{CREDIT_PACK_MINUTES} minutes of transcription '
                               f'({CREDIT_PACK_MINUTES // 60} hours)',
                'tax_code': STRIPE_TAX_CODE,
            },
        }

    meta = {
        'user_id': str(current_user.id),
        'minutes': str(CREDIT_PACK_MINUTES),
        'pack': CREDIT_PACK_SKU,
        'location': source,
        'source': source,
    }
    if ph_sid:
        meta['ph_sid'] = ph_sid
    if return_to:
        # Stripe metadata values are short strings; keep a relative path only.
        meta['return_to'] = return_to[:500]
    pi_meta = {
        'user_id': str(current_user.id),
        'pack': CREDIT_PACK_SKU,
        'location': source,
    }
    if ph_sid:
        pi_meta['ph_sid'] = ph_sid

    # Shorter than Stripe's 24h default so abandoned-checkout recovery emails
    # can fire the same day. expires_at is a Unix timestamp (seconds).
    expires_at = int(time.time()) + CHECKOUT_EXPIRES_HOURS * 3600
    params = {
        'mode': 'payment',
        'line_items': [line_item],
        'success_url': success_url,
        'cancel_url': cancel_abs,
        'client_reference_id': str(current_user.id),
        'customer_email': current_user.email,
        'customer_creation': 'always',
        # auto: Checkout collects the minimum address fields needed for tax
        # (Managed Payments / automatic_tax). required would force a full
        # street address on every buyer. Do not pass payment_method_types —
        # Managed Payments uses dynamic payment methods (card + wallets).
        'billing_address_collection': 'auto',
        'expires_at': expires_at,
        'metadata': meta,
        'payment_intent_data': {
            'metadata': pi_meta,
        },
    }
    if CHECKOUT_RECOVERY_ENABLED:
        # Stripe emails a one-time recovery URL after the session expires.
        # allow_promotion_codes on the recovery link matches our one-time pack.
        # Do not send consent_collection.promotions unless explicitly opted in —
        # that field is US-only and rejects Norwegian (and other non-US) accounts.
        params['after_expiration'] = {
            'recovery': {
                'enabled': True,
                'allow_promotion_codes': True,
            },
        }
        if CHECKOUT_PROMOTIONS_CONSENT_ENABLED:
            params['consent_collection'] = {
                'promotions': 'auto',
            }
    if STRIPE_MANAGED_PAYMENTS:
        # Managed Payments rejects automatic_tax: Stripe owns the tax.
        params['managed_payments'] = {'enabled': True}
    elif STRIPE_AUTOMATIC_TAX:
        params['automatic_tax'] = {'enabled': True}

    try:
        client = stripe_client()
        if client is None:
            raise RuntimeError('Stripe client unavailable')
        try:
            checkout_session = client.v1.checkout.sessions.create(
                params=params,
                options={
                    'idempotency_key': (
                        f'checkout-{current_user.id}-{uuid.uuid4()}'),
                },
            )
        except Exception as recovery_exc:  # noqa: BLE001
            # Optional recovery/consent knobs must not block purchase (PODSKRIFT-W).
            if not _stripe_rejected_optional_checkout_params(
                    recovery_exc, params):
                raise
            app.logger.warning(
                'Stripe rejected optional checkout recovery params; '
                'retrying without them: %s', recovery_exc)
            params.pop('after_expiration', None)
            params.pop('consent_collection', None)
            checkout_session = client.v1.checkout.sessions.create(
                params=params,
                options={
                    'idempotency_key': (
                        f'checkout-{current_user.id}-{uuid.uuid4()}'),
                },
            )
    except Exception as exc:  # noqa: BLE001 - never surface Stripe internals
        app.logger.exception('Stripe Checkout Session create failed')
        _capture_purchase_failed(
            'stripe_api_error',
            stage='checkout_create',
            user_id=current_user.id,
            extra={'error_type': type(exc).__name__},
        )
        flash('Could not start checkout. Please try again in a moment.', 'error')
        return redirect(url_for('settings') + '#credits')

    cs_id = getattr(checkout_session, 'id', None) or (
        checkout_session.get('id') if isinstance(checkout_session, dict) else None)
    recovery_applied = bool(
        ((params.get('after_expiration') or {}).get('recovery') or {})
        .get('enabled'))
    started_props = {
        'location': source,
        'source': source,
        'checkout_session_id': cs_id,
        'amount_cents': CREDIT_PACK_AMOUNT_CENTS,
        'currency': CREDIT_PACK_CURRENCY,
        'minutes': CREDIT_PACK_MINUTES,
        'pack_sku': CREDIT_PACK_SKU,
        'managed_payments': bool(STRIPE_MANAGED_PAYMENTS),
        'trial_remaining_min': trial_remaining_min,
        'paid_remaining_min': paid_remaining_min,
        'expires_hours': CHECKOUT_EXPIRES_HOURS,
        'recovery_enabled': recovery_applied,
    }
    started_props.update(trial_variant_props(current_user))
    if ph_sid:
        started_props['$session_id'] = ph_sid
    product_analytics.capture(
        'checkout_started',
        current_user.id,
        started_props,
        uuid=_ph_uuid5(cs_id) if cs_id else None,
    )

    return redirect(checkout_session.url, code=303)


@app.route('/billing/cancel')
@login_required
def billing_cancel():
    """Stripe cancel_url target: record checkout_returned then redirect."""
    next_url = safe_next_url(
        request.args.get('next'), url_for('settings') + '#credits')
    _capture_checkout_returned('cancelled', user_id=current_user.id)
    return redirect(next_url)


@app.route('/billing/success')
@login_required
def billing_success():
    """Landing after Checkout. Also triggers fulfill_checkout (idempotent)."""
    session_id = (request.args.get('session_id') or '').strip()
    paid_ready = False
    pending = False
    returned_status = 'error'
    location = None
    if session_id.startswith('cs_') and stripe_checkout_enabled():
        try:
            fulfill_checkout(session_id, fulfilled_via='success_page')
        except Exception:
            app.logger.exception(
                'billing_success fulfill_checkout failed for %s', session_id)
            returned_status = 'error'
        # Re-read from Stripe for the UI; never trust the query string alone.
        try:
            client = stripe_client()
            if client is not None:
                s = client.v1.checkout.sessions.retrieve(session_id)
                d = s.to_dict() if hasattr(s, 'to_dict') else dict(s)
                meta = d.get('metadata') or {}
                location = (meta.get('location') or meta.get('source') or None)
                ref = str(d.get('client_reference_id') or '')
                if ref != str(current_user.id):
                    returned_status = 'not_owner'
                elif d.get('payment_status') == 'paid':
                    paid_ready = True
                    returned_status = 'paid'
                else:
                    pending = True
                    # unpaid vs pending (async): Stripe uses 'unpaid' until paid.
                    ps = (d.get('payment_status') or 'unpaid').lower()
                    returned_status = 'pending' if ps != 'unpaid' else 'unpaid'
                    if ps in ('processing', 'pending'):
                        returned_status = 'pending'
                # Prefer Stripe metadata return_to if session key was lost.
                meta_return = safe_return_to(meta.get('return_to') or '')
                if meta_return and not session.get(BILLING_RETURN_TO_KEY):
                    session[BILLING_RETURN_TO_KEY] = meta_return
                    session.modified = True
        except Exception:
            app.logger.exception(
                'billing_success session retrieve failed for %s', session_id)
            returned_status = 'error'
        _capture_checkout_returned(
            returned_status,
            user_id=current_user.id,
            session_id=session_id,
            location=location,
        )
    elif session_id:
        _capture_checkout_returned(
            'error', user_id=current_user.id, session_id=session_id)

    # Re-load balance after fulfill may have credited the account.
    try:
        db.session.expire(current_user)
    except Exception:
        pass
    paid_minutes = paid_balance_seconds(current_user) // 60
    trial_left = 0
    if trial_available() and not getattr(current_user, 'openai_api_key', None):
        trial_left = trial_status(current_user)[2] // 60
    balance_minutes = paid_minutes + trial_left

    pending_episode = session.get(PENDING_TRANSCRIPTION_KEY)
    return_to = safe_return_to(session.get(BILLING_RETURN_TO_KEY) or '')

    return render_template(
        'billing_success.html',
        paid_ready=paid_ready,
        pending=pending,
        paid_minutes=paid_minutes,
        balance_minutes=balance_minutes,
        credit_pack_minutes=CREDIT_PACK_MINUTES,
        pending_episode=pending_episode,
        return_to=return_to,
    )


@app.route('/stripe/webhook', methods=['POST'])
def stripe_webhook():
    """Verify and handle Stripe events. Credits via fulfill_checkout."""
    if not stripe_webhook_enabled():
        # Not configured: look like a missing route rather than a soft outage.
        return jsonify({'error': 'not found'}), 404
    payload = request.get_data(cache=False, as_text=False)
    sig_header = request.headers.get('Stripe-Signature', '')
    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except ValueError:
        _capture_stripe_webhook_error('payload_invalid')
        return jsonify({'error': 'invalid payload'}), 400
    except Exception as exc:  # SignatureVerificationError and friends
        # stripe.SignatureVerificationError when the SDK is installed.
        name = type(exc).__name__
        if 'Signature' in name or 'signature' in str(exc).lower():
            app.logger.warning(
                'Stripe webhook bad signature (%s)', name)
            _capture_stripe_webhook_error('signature_invalid')
            return jsonify({'error': 'invalid signature'}), 400
        app.logger.exception('Stripe webhook construct_event failed')
        _capture_stripe_webhook_error(
            'construct_failed', extra={'error_type': name})
        return jsonify({'error': 'webhook error'}), 400

    # stripe-python >= 15: Event is not a dict — use attribute access.
    etype = event.type
    obj = event.data.object
    obj_id = obj.id if hasattr(obj, 'id') else (obj.get('id') if isinstance(obj, dict) else None)
    try:
        if etype in (
            'checkout.session.completed',
            'checkout.session.async_payment_succeeded',
        ):
            fulfill_checkout(obj_id, event_id=event.id)
        elif etype == 'checkout.session.expired':
            _capture_checkout_expired(obj)
        elif etype == 'checkout.session.async_payment_failed':
            app.logger.warning('Async payment failed for %s', obj_id)
            _capture_purchase_failed(
                'async_payment_failed',
                stage='async_payment',
                session_id=obj_id,
            )
        elif etype == 'charge.refunded':
            charge = obj.to_dict() if hasattr(obj, 'to_dict') else (
                obj if isinstance(obj, dict) else dict(obj))
            handle_charge_refunded(charge, event_id=event.id)
        elif etype in ('charge.dispute.created', 'charge.dispute.closed'):
            dispute = obj.to_dict() if hasattr(obj, 'to_dict') else (
                obj if isinstance(obj, dict) else dict(obj))
            handle_dispute(dispute, etype, event_id=event.id)
    except Exception as exc:
        app.logger.exception(
            'Stripe webhook handling failed (%s %s)', etype, obj_id)
        _capture_stripe_webhook_error(
            'handler_exception',
            event_type=etype,
            extra={'error_type': type(exc).__name__},
        )
        return jsonify({'error': 'handler failed'}), 500
    return jsonify({'received': True}), 200


@app.route('/settings/openai-key/remove', methods=['POST'])
@login_required
def settings_remove_openai_key():
    """Explicitly drop the saved OpenAI key. Empty Save must not do this."""
    if not current_user.openai_api_key:
        flash('No OpenAI API key to remove.', 'info')
        return redirect(url_for('settings'))
    current_user.openai_api_key = None
    db.session.commit()
    flash('API key removed.', 'success')
    return redirect(url_for('settings'))


@app.route('/settings/api-key/generate', methods=['POST'])
@login_required
def settings_generate_api_key():
    """Create (or rotate) the user's one customer HTTP API key.

    Plaintext is shown once via the session and never stored — only the hash.
    Rotating invalidates the previous key immediately.
    """
    plaintext = mint_customer_api_key()
    current_user.api_key_hash = hash_customer_api_key(plaintext)
    current_user.api_key_prefix = customer_api_key_prefix(plaintext)
    current_user.api_key_created_at = datetime.now(timezone.utc)
    db.session.commit()
    session['new_api_key'] = plaintext
    flash('API key created. Copy it now — it will not be shown again.', 'success')
    return redirect(url_for('settings'))


@app.route('/settings/api-key/revoke', methods=['POST'])
@login_required
def settings_revoke_api_key():
    """Drop the active customer API key. Subsequent API calls get 401."""
    if not current_user.api_key_hash:
        flash('No API key to revoke.', 'info')
        return redirect(url_for('settings'))
    current_user.api_key_hash = None
    current_user.api_key_prefix = None
    current_user.api_key_created_at = None
    db.session.commit()
    flash('API key revoked. It can no longer be used.', 'success')
    return redirect(url_for('settings'))


def _apply_global_unsubscribe(user):
    """Stamp global unsub + turn off transcript-ready. Idempotent."""
    user.email_unsubscribed_at = user.email_unsubscribed_at or datetime.now(timezone.utc)
    user.email_transcript_ready = False
    db.session.commit()
    product_analytics.capture('unsubscribe', user.id, {'channel': 'all'})


def _schedule_transcript_ready_email(task_id, user_id):
    """Fire-and-forget transcript-ready mail; never blocks the worker thread."""
    def _run():
        with app.app_context():
            try:
                owner = db.session.get(User, user_id)
                task = db.session.get(TranscriptionTask, task_id)
                if owner is None or task is None:
                    return
                email_notify.notify_transcript_ready(
                    db=db,
                    user=owner,
                    task=task,
                    EmailSentLog=EmailSentLog,
                    public_base_url=PUBLIC_BASE_URL,
                    secret_key=app.secret_key,
                )
            except Exception:  # noqa: BLE001
                app.logger.exception(
                    'scheduled transcript-ready email failed for %s', task_id)

    threading.Thread(
        target=_run, daemon=True, name=f'transcript-ready-{task_id[:8]}',
    ).start()


def _openai_key_for_task_owner(user_id):
    """Same key that funded transcription: BYOK wins, else platform key."""
    owner = db.session.get(User, user_id)
    key, _source = resolve_openai_key(owner)
    if key:
        return key
    return GLOBAL_OPENAI_KEY


def _schedule_transcript_summary(task_id, user_id):
    """Fire-and-forget AI summary after completion. Never fails the transcript."""
    if not summary_mod.summary_enabled():
        return

    def _run():
        with app.app_context():
            try:
                task = db.session.get(TranscriptionTask, task_id)
                if task is None or task.status != 'completed':
                    return
                key = _openai_key_for_task_owner(user_id)
                client = build_openai_client(key)
                summary_mod.summarize_task(
                    db=db,
                    task=task,
                    openai_client=client,
                    user_id=user_id,
                    retry=True,
                )
            except Exception:  # noqa: BLE001
                app.logger.exception(
                    'scheduled summary failed for %s', task_id)

    threading.Thread(
        target=_run, daemon=True, name=f'summary-{task_id[:8]}',
    ).start()


def _finalize_global_unsubscribe(user):
    """Stamp unsub and disable transcript-ready. Idempotent."""
    _apply_global_unsubscribe(user)
    # Clear leftover per-feed alert flags on any historical saved_feeds rows.
    SavedFeed.query.filter_by(user_id=user.id).update(
        {'email_new_episodes': False, 'email_summaries': False},
        synchronize_session=False)
    db.session.commit()


def _is_rfc8058_one_click_unsubscribe() -> bool:
    """True for List-Unsubscribe-Post bodies (RFC 8058), not the confirm form."""
    return (request.form.get('List-Unsubscribe') or '').strip() == 'One-Click'


@app.route('/email/unsubscribe/<token>', methods=['GET', 'POST'])
def email_unsubscribe(token):
    """Logged-out unsubscribe: GET confirms; POST applies (RFC 8058 or form).

    Link scanners must not unsubscribe on GET. Mail clients that honour
    List-Unsubscribe-Post send ``List-Unsubscribe=One-Click`` and skip the
    confirm page — that path still works without a second click.
    """
    user_id = email_notify.parse_unsubscribe_token(app.secret_key, token)
    if user_id is None:
        return render_template(
            'email_unsubscribed.html',
            ok=False,
            message='That unsubscribe link is invalid or expired.',
        ), 400
    user = db.session.get(User, user_id)
    if user is None:
        return render_template(
            'email_unsubscribed.html',
            ok=False,
            message='That account is no longer here.',
        ), 404

    if request.method == 'GET':
        return render_template(
            'email_unsubscribe_confirm.html',
            token=token,
        )

    # POST: RFC 8058 one-click, or the confirm-page button.
    if _is_rfc8058_one_click_unsubscribe() or request.form.get('confirm') == '1':
        _finalize_global_unsubscribe(user)
        if _is_rfc8058_one_click_unsubscribe():
            return ('', 200)
        return render_template(
            'email_unsubscribed.html',
            ok=True,
            message='You are unsubscribed from Podskrift emails.',
        )

    # Bare POST without One-Click / confirm — show confirm (do not unsub).
    return render_template(
        'email_unsubscribe_confirm.html',
        token=token,
    ), 400


@app.route('/go/transcribe')
@login_required
def go_transcribe():
    """Deep link with preselected episode + rss_url."""
    product_analytics.capture(
        'email_clicked',
        current_user.id,
        {'type': 'go_transcribe'},
    )
    rss_url = (request.args.get('rss_url') or '').strip()
    audio_url = (request.args.get('audio_url') or '').strip()
    episode_title = (request.args.get('episode_title') or '').strip() or 'Episode'
    podcast_name = (request.args.get('podcast_name') or '').strip()
    episode_index_raw = request.args.get('episode_index')
    if rss_url and not _is_fetchable_url(rss_url):
        flash('That feed URL cannot be fetched.', 'error')
        return redirect(url_for('index'))
    if audio_url and not _is_fetchable_url(audio_url):
        flash('That audio URL cannot be fetched.', 'error')
        return redirect(url_for('index'))
    if not audio_url and rss_url and episode_index_raw not in (None, ''):
        try:
            episode_index = int(episode_index_raw)
        except (TypeError, ValueError):
            episode_index = None
        episodes, error = get_episodes_from_rss(rss_url, timeout=20)
        if error or not episodes or episode_index is None:
            flash(error or 'Could not load that episode.', 'error')
            return redirect(url_for('index'))
        if episode_index < 0 or episode_index >= len(episodes):
            flash('That episode is no longer in the feed.', 'error')
            return redirect(url_for('index'))
        ep = episodes[episode_index]
        audio_url = ep.get('audio_url') or ''
        episode_title = ep.get('title') or episode_title
        podcast_name = ep.get('podcast_name') or podcast_name
    if not audio_url:
        flash('Missing episode details.', 'error')
        return redirect(url_for('index'))
    return render_template(
        'go_transcribe.html',
        rss_url=rss_url or '',
        audio_url=audio_url,
        episode_title=episode_title,
        podcast_name=podcast_name or '',
        languages=language_choices(),
    )


# ---------------------------------------------------------------------------
# Saved feeds (removed) — leave DB tables; redirect / 410 old URLs
# ---------------------------------------------------------------------------

@app.route('/feeds')
@app.route('/feeds/')
def feeds():
    """Former My Feeds page — permanently redirected home."""
    return redirect(url_for('index'), code=301)


@app.route('/feeds/add', methods=['GET', 'POST'])
@app.route('/feeds/<int:feed_id>/email-alerts', methods=['GET', 'POST'])
@app.route('/feeds/<int:feed_id>/email-summaries', methods=['GET', 'POST'])
@app.route('/feeds/delete/<int:feed_id>', methods=['GET', 'POST'])
@app.route('/feeds/use/<int:feed_id>', methods=['GET', 'POST'])
def feeds_gone(feed_id=None):
    """Former feed CRUD / toggle endpoints — gone."""
    return ('', 410)



def _trial_context():
    """Trial / paid figures for the templates, or None when neither applies."""
    if not current_user.is_authenticated:
        return None
    on_own_key = bool(current_user.openai_api_key)
    paid_seconds = paid_balance_seconds(current_user)
    paid_minutes = paid_seconds // 60
    if not trial_available() and paid_seconds <= 0 and not on_own_key:
        return None
    if trial_available():
        limit, used, remaining = trial_status(current_user)
    else:
        limit = used = remaining = 0
    remaining_minutes = remaining // 60
    daily_exhausted = (
        trial_available()
        and not on_own_key
        and paid_seconds <= 0
        and not trial_daily_budget_available()
    )
    # Per-user exhausted OR shared daily budget gone — both block free starts.
    exhausted = (
        (remaining <= 0 and paid_seconds <= 0)
        or daily_exhausted
    )
    total_minutes = remaining_minutes + paid_minutes
    low_balance = (
        not on_own_key
        and not exhausted
        and not daily_exhausted
        and total_minutes < LOW_BALANCE_MINUTES
    )
    return {
        'limit_minutes': limit // 60,
        'used_minutes': used // 60,
        # Hide the personal counter while the daily pool is empty so we do not
        # imply the user can still spend "X min left".
        'remaining_minutes': 0 if daily_exhausted else remaining_minutes,
        'paid_minutes': paid_minutes,
        'total_minutes': paid_minutes if daily_exhausted else total_minutes,
        'exhausted': exhausted,
        'daily_exhausted': daily_exhausted,
        'low_balance': low_balance,
        'on_own_key': on_own_key,
        'stripe_buy': stripe_checkout_enabled(),
        'buy_label': CREDIT_PACK_LABEL,
        'buy_subline': CREDIT_PACK_SUBLINE,
    }


def _nav_credits_context():
    """Compact balance + Buy visibility for the nav. Reuses trial/paid attrs
    already on the loaded user — no extra query beyond Flask-Login's load."""
    if not current_user.is_authenticated:
        return {
            'nav_minutes_left': None,
            'nav_daily_exhausted': False,
            'nav_show_buy': False,
        }
    on_own_key = bool(current_user.openai_api_key)
    if on_own_key:
        return {
            'nav_minutes_left': None,
            'nav_daily_exhausted': False,
            'nav_show_buy': False,
        }
    paid_minutes = paid_balance_seconds(current_user) // 60
    daily_exhausted = (
        trial_available()
        and paid_minutes <= 0
        and not trial_daily_budget_available()
    )
    if daily_exhausted:
        # Do not show a misleading personal "X min left" while the shared
        # daily budget is empty.
        return {
            'nav_minutes_left': None,
            'nav_daily_exhausted': True,
            'nav_show_buy': stripe_checkout_enabled(),
        }
    if trial_available():
        remaining_minutes = trial_status(current_user)[2] // 60
    else:
        remaining_minutes = 0
    # Always show a pill for metered users so "0 min left" is still obvious.
    return {
        'nav_minutes_left': remaining_minutes + paid_minutes,
        'nav_daily_exhausted': False,
        'nav_show_buy': stripe_checkout_enabled(),
    }


def _is_minutes_limit_error(message):
    """True when an error string is a trial/paid minutes refusal (no DB flag)."""
    if not message:
        return False
    msg = str(message).lower()
    return any(marker in msg for marker in (
        'minutes you have left',
        'too long for the free trial',
        'out of free minutes',
        'buy more minutes',
        CREDIT_PACK_LABEL.lower(),
        'not enough minutes',
        'trial is used up',
        "today's free minutes are used up",
        'free minutes refill at midnight',
        'handed out all the free minutes',
    ))


def _user_has_api_key():
    """Can the current user actually start a transcription right now?

    True on their own key, remaining free trial, or paid credit minutes. An
    exhausted trial with no paid balance counts as no key, which is what puts
    the "add your key / buy" prompt in front of the people who need it.
    """
    if not current_user.is_authenticated:
        return False
    if current_user.openai_api_key:
        return True
    if paid_balance_seconds(current_user) > 0:
        return True
    if trial_available():
        if not trial_daily_budget_available():
            return False
        return trial_status(current_user)[2] > 0
    return False


def _episode_paywall_needed():
    """Show the episode-picker "Out of free minutes" card?

    Only for logged-in users who cannot start (no own key, no trial, no paid).
    Logged-out visitors still have the signup/trial path — Start Transcription
    stashes the episode and sends them to register — so they must not see a
    paywall that claims they are out of minutes they have not claimed yet.
    """
    return current_user.is_authenticated and not _user_has_api_key()


def _show_openai_cost_estimates():
    """Dollar Whisper estimates are only meaningful for own-key users."""
    return (
        current_user.is_authenticated
        and bool(getattr(current_user, 'openai_api_key', None))
    )


def _minutes_limit_actions(user_id, location='enqueue', reason=None,
                           estimate_min=None):
    """CTA payload for out-of-minutes messages: Buy (if configured) + add key.

    Buy is the primary CTA when Stripe is on; BYOK is a secondary text link
    in the browser. Analytics for paywall/offer are browser-side
    (paywall_shown / offer_shown / paywall_buy_clicked / paywall_byok_clicked).
    """
    actions = []
    if stripe_checkout_enabled():
        actions.append({
            'label': CREDIT_PACK_LABEL,
            'url': url_for('billing_checkout'),
            'method': 'POST',
            'primary': True,
        })
    actions.append({
        'label': 'Add OpenAI key →',
        'url': settings_openai_url(),
        'method': 'GET',
        'primary': False,
    })
    # Keep the legacy single-action fields pointing at the primary CTA.
    primary = actions[0]
    cover = pack_covers_episode_line(estimate_min) if estimate_min else ''
    return {
        'actions': actions,
        'action_url': primary['url'] if primary['method'] == 'GET' else settings_credits_url(),
        'action_label': primary['label'],
        'buy_available': stripe_checkout_enabled(),
        'buy_label': CREDIT_PACK_LABEL if stripe_checkout_enabled() else None,
        'buy_url': url_for('billing_checkout') if stripe_checkout_enabled() else None,
        'paywall_reason': reason,
        'paywall_location': location,
        'paywall_cover_line': cover if stripe_checkout_enabled() else '',
        'byok_label': 'Add OpenAI key →',
        'byok_url': settings_openai_url(),
    }


def _capture_paid_minutes_exhausted(user_id, source):
    """Legacy alias — prefer _capture_minutes_exhausted_if_depleted after a charge."""
    product_analytics.capture(
        'paid_minutes_exhausted', user_id, {'source': source})


# ---------------------------------------------------------------------------
# Core routes
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    first_run = False
    if current_user.is_authenticated:
        # No transcription rows yet → lead with "start your first transcript"
        # instead of a Buy banner (activation before purchase).
        first_run = (
            TranscriptionTask.query.filter_by(user_id=current_user.id)
            .limit(1).first() is None
        )
    return render_template('index.html',
                           languages=language_choices(),
                           trial=_trial_context(),
                           first_run=first_run,
                           faq=faq_entries(),
                           trial_minutes=advertised_trial_minutes(),
                           structured_data=_structured_data())


def _annotate_episodes_for_trial(episodes):
    """Attach needs_own_key on each episode dict for the selection UI badge."""
    remaining = trial_badge_remaining_minutes()
    paid = trial_badge_paid_minutes()
    for ep in episodes:
        ep['needs_own_key'] = episode_needs_own_key(
            ep.get('duration_min'), remaining, paid_minutes=paid)
    return episodes


@app.route('/parse_rss', methods=['POST'])
def parse_rss():
    rss_url = request.form.get('rss_url')
    if not rss_url:
        flash('Please enter an RSS feed URL', 'error')
        return redirect(url_for('index'))

    episodes, error = get_episodes_from_rss(rss_url)
    if error:
        flash(error, 'error')
        return redirect(url_for('index'))

    _annotate_episodes_for_trial(episodes)
    episodes_to_show = episodes[:10]
    has_more = len(episodes) > 10
    preselected_index = None
    raw_idx = request.form.get('episode_index')
    want_audio = (request.form.get('episode_audio_url') or '').strip()
    if want_audio:
        # Show pages cache feeds for hours; a new release shifts indexes, so
        # match the exact episode by enclosure URL before trusting the index.
        for ep in episodes:
            if ep.get('audio_url') == want_audio:
                preselected_index = ep.get('index')
                break
    if preselected_index is None and raw_idx not in (None, ''):
        try:
            candidate = int(raw_idx)
            if any(ep.get('index') == candidate for ep in episodes):
                preselected_index = candidate
        except (TypeError, ValueError):
            preselected_index = None
    return render_template(
        'episode_selection.html',
        episodes=episodes_to_show,
        all_episodes=episodes,
        rss_url=rss_url,
        has_more=has_more,
        needs_api_key=_episode_paywall_needed(),
        show_openai_cost=_show_openai_cost_estimates(),
        podcast_name=episodes[0].get('podcast_name') or '',
        artwork=episodes[0].get('artwork') or '',
        languages=language_choices(),
        preselected_index=preselected_index,
    )


def _capture_trial_limit_hit(user_id, scope, stage, source,
                             estimate_min=None, remaining_min=None):
    """The buying signal: a trial user wanted more than the free allowance gives.

    scope: episode_length | user | daily | global. stage: start (refused before
    the job existed) or reconcile (the real audio turned out longer than the
    feed said).
    """
    props = {'scope': scope, 'stage': stage, 'source': source}
    if estimate_min is not None:
        props['estimate_min'] = int(estimate_min)
    if remaining_min is not None:
        props['remaining_min'] = int(remaining_min)
    props.update(trial_variant_props(user_id))
    product_analytics.capture('trial_limit_hit', user_id, props)


def _capture_trial_daily_budget_exhausted(user_id, source='web'):
    """Fire trial_daily_budget_exhausted once per Oslo day (first hit wins).

    Stable uuid keyed by day so PostHog dedupes across workers/retries.
    """
    day = trial_oslo_day_str()
    product_analytics.capture(
        'trial_daily_budget_exhausted',
        user_id or 'system',
        {
            'day': day,
            'source': source,
            'daily_limit_min': TRIAL_DAILY_SECONDS // 60,
            'daily_used_min': trial_daily_used_seconds(day) // 60,
        },
        uuid=f'trial-daily-budget-exhausted-{day}',
    )


def trial_daily_exhausted_message():
    """User-facing copy when today's shared free-trial budget is gone."""
    buy = (
        f'Buy {CREDIT_PACK_MINUTES} min for $5 to continue now.'
        if stripe_checkout_enabled()
        else 'Add your own OpenAI API key in Settings to keep transcribing.'
    )
    return (
        "Today's free minutes are used up — they refill at midnight "
        f"(Norway time). {buy}"
    )


def _find_existing_web_task(user_id, source_audio_url):
    """Return a same-user task for this audio that is still live or finished.

    Error/cancelled tasks are intentionally omitted so the user can re-run.
    Completed *partial* previews are also omitted so buying minutes can start
    a full run of the same source_audio_url. Used only on the web path to
    avoid double-reserving trial minutes when someone restarts an episode
    that is already queued, running, or done.
    """
    if not source_audio_url:
        return None
    task = (
        TranscriptionTask.query
        .filter(
            TranscriptionTask.user_id == user_id,
            TranscriptionTask.source_audio_url == source_audio_url,
            ~TranscriptionTask.status.in_(TERMINAL_STATUSES),
        )
        .order_by(TranscriptionTask.started_at.desc())
        .first()
    )
    if task is not None and task.status == 'completed' and task_is_partial(task):
        return None
    return task


def _completed_transcript_count(user_id):
    return (
        TranscriptionTask.query
        .filter_by(user_id=user_id, status='completed')
        .count()
    )


def normalize_task_source(source):
    """Canonical enqueue channel: web | mcp | api (default web)."""
    s = (source or 'web').strip().lower()
    if s in ('web', 'mcp', 'api'):
        return s
    return 'web'


def task_source_via_label(source, source_client=None):
    """User-facing 'via …' label for History / admin, or None if not MCP."""
    if (source or '').strip().lower() != 'mcp':
        return None
    name = (source_client or '').strip()
    if not name:
        return 'via MCP'
    lower = name.lower()
    if 'chatgpt' in lower or lower in ('openai', 'openai chatgpt'):
        return 'via ChatGPT'
    if 'claude' in lower or 'anthropic' in lower:
        return 'via Claude'
    # Keep short; connector names can be long CIMD URLs in edge cases.
    if len(name) > 40:
        name = name[:37] + '…'
    return f'via {name}'


def task_source_label(source, source_client=None):
    """MCP/list label: 'via ChatGPT' / 'via Claude' / 'via MCP' / 'web' / 'api'."""
    via = task_source_via_label(source, source_client)
    if via:
        return via
    s = (source or 'web').strip().lower()
    if s == 'api':
        return 'api'
    return 'web'


def history_list_status(task):
    """User-facing History bucket: in_progress | failed | completed | None."""
    status = agent_transcript_status(task)
    if status == 'pending':
        return 'in_progress'
    if status == 'failed':
        return 'failed'
    if status == 'ready' or (task.status or '') == 'completed':
        return 'completed'
    return None


def short_task_error(task, limit=120):
    """Short, safe failure line for History (never echo raw key material)."""
    raw = (getattr(task, 'error_message', None) or '').strip()
    if not raw:
        return 'Transcription failed.'
    # Keys sometimes land in OpenAI 401 bodies; never show long opaque tokens.
    if re.search(r'\bsk-[A-Za-z0-9_-]{8,}\b', raw) or 'api key' in raw.lower():
        return 'Transcription failed.'
    if len(raw) > limit:
        return raw[: limit - 1].rstrip() + '…'
    return raw


def enqueue_transcription(user, meta, rss_url=None, language='', source='web',
                          source_client=None):
    """Start Whisper for one episode on behalf of `user`.

    Shared by the UI form and the agent write API so trial reservation,
    admission control, and the worker thread stay one code path. Returns
    ``(payload_dict, http_status)``. On success payload is
    ``{'task_id': ...}``; on refusal it carries ``error``.

    `meta` keys: title, audio_url, podcast_name, artwork, published, duration_min,
    and optional listen URLs: spotify_url, apple_url, episode_link / website_url /
    show_link.
    `source` is 'web', 'mcp', or 'api' — stored on the task and used for analytics.
    `source_client` is an optional connector display name (MCP OAuth clients).
    """
    language = normalize_language_code(language)
    source = normalize_task_source(source)
    client_label = (source_client or '').strip()[:64] or None

    audio_url = (meta.get('audio_url') or '').strip()
    if not audio_url or not _is_fetchable_url(audio_url):
        return {'error': 'That audio URL cannot be fetched.'}, 400

    # Platform-labelled links are rebuilt from parsed ids only, so a form
    # value cannot put an arbitrary URL behind "Listen on Spotify/Apple".
    spotify_url = canonical_spotify_episode_url(meta.get('spotify_url') or '')
    apple_url = canonical_apple_podcasts_url(meta.get('apple_url') or '')
    website_url = website_url_from_episode_meta(meta)

    # Snapshot before the worker thread: callers may pass flask_login's
    # current_user LocalProxy, which is None outside a request context.
    # Evaluating .id inside the thread is what turned completed jobs into
    # status=error with "'NoneType' object has no attribute 'id'".
    user_id = user.id

    # Web only: restarting a live or finished episode must not reserve again.
    # Checked before admission/reserve so a redirect costs nothing.
    if source == 'web':
        existing = _find_existing_web_task(user_id, audio_url)
        if existing is not None:
            return {'task_id': existing.id, 'existing': True}, 200

    api_key, key_source = resolve_openai_key(user)
    if not api_key:
        return {
            'error': 'No OpenAI API key configured. Add your key in Settings.'
        }, 400
    openai_client = build_openai_client(api_key)

    duration_min = meta.get('duration_min')
    try:
        duration_min = float(duration_min) if duration_min is not None else None
    except (TypeError, ValueError):
        duration_min = None
    input_origin = derive_input_origin(meta, rss_url=rss_url)
    podcast_name = meta.get('podcast_name') or None
    has_feed = bool((rss_url or '').strip())
    # Anticipated rank: completed so far + this new job.
    nth_transcript = _completed_transcript_count(user_id) + 1
    # Every analytics event for this job carries the same labels.
    ph_props = {
        'key_source': key_source,
        'source': source,
        'duration_min': duration_min,
        'language': language or None,
        'input_origin': input_origin,
        'has_feed': has_feed,
        'podcast_name': podcast_name,
        'nth_transcript': nth_transcript,
    }

    # Admission control, before anything is reserved or written, so a refusal
    # has nothing to unwind. This box is shared with 50+ other services, so
    # running it out of disk is their outage too.
    #
    # Own-key users are capped alongside trial users on purpose: the disk is
    # ours either way, and paying with your own OpenAI key does not make the
    # volume bigger. Revisit if paying customers start losing to free ones.
    free_bytes = free_disk_bytes(os.path.dirname(os.path.abspath(__file__)))
    if free_bytes is not None and free_bytes < MIN_FREE_DISK_BYTES:
        app.logger.error('refusing transcription: only %.1f GB free on the app volume',
                         free_bytes / (1024 ** 3))
        return {'error': (
            'Podskrift is out of disk space right now. Please try again later.'
        )}, 503
    if not _transcription_slots.acquire(blocking=False):
        # Logged because this is the only way to learn the cap is being hit, or
        # whether the default is the right number, short of user complaints.
        app.logger.warning('refusing transcription: worker at its limit of %s',
                           MAX_CONCURRENT_TRANSCRIPTIONS)
        episodes = 'episode' if MAX_CONCURRENT_TRANSCRIPTIONS == 1 else 'episodes'
        return {'error': (
            f'Podskrift is already transcribing {MAX_CONCURRENT_TRANSCRIPTIONS} '
            f'{episodes} right now. Please try again in a few minutes.'
        )}, 503
    slot_held = True
    try:

        # Reserve the allowance BEFORE the job exists, so a refusal leaves nothing
        # behind. The feed's duration is only an estimate; trial_reconcile_task()
        # corrects it against the real audio before anything reaches Whisper.
        trial_charge = None
        paid_charge = None
        partial_job = False
        if key_source == 'trial':
            # Floored at a minute: a zero reservation would quietly make the
            # task look unmetered (NULL/0 charges).
            estimate = trial_estimate_seconds(meta.get('duration_min'))
            estimate_min = estimate // 60
            over_free_cap = bool(
                TRIAL_MAX_EPISODE_SECONDS and estimate > TRIAL_MAX_EPISODE_SECONDS)
            before_trial, before_paid = _platform_remaining_seconds(user_id)
            paid_bal = before_paid
            can_cover_full = (
                (paid_bal >= estimate) if over_free_cap
                else (before_trial + paid_bal) >= estimate
            )

            if can_cover_full:
                if over_free_cap:
                    # Full free jobs must not cover over-long episodes; paid can.
                    split = platform_reserve(user_id, estimate, paid_only=True)
                else:
                    split = platform_reserve(user_id, estimate)
                if split is None:
                    # Race with another worker — fall through to refusal below.
                    can_cover_full = False
                else:
                    trial_charge, paid_charge = split
                    _capture_minutes_exhausted_if_depleted(
                        user_id, source, before_trial, before_paid)

            if trial_charge is None and paid_charge is None:
                # Partial free preview only when the episode is longer than the
                # account's remaining trial (not when the per-episode max is the
                # sole blocker — that stays the episode_length paywall). Daily
                # budget remaining also caps the preview length.
                daily_rem = trial_daily_remaining_seconds()
                if TRIAL_GLOBAL_SECONDS > 0:
                    lifetime_rem = max(
                        0, TRIAL_GLOBAL_SECONDS - trial_global_used_seconds())
                else:
                    lifetime_rem = before_trial
                partial_n = min(before_trial, daily_rem, lifetime_rem)
                episode_exceeds_remaining = estimate > before_trial
                if (paid_bal <= 0
                        and episode_exceeds_remaining
                        and partial_n >= TRIAL_PARTIAL_MIN_SECONDS
                        and trial_reserve(user_id, partial_n)):
                    trial_charge, paid_charge = partial_n, 0
                    partial_job = True
                    ph_props['partial'] = True
                    ph_props['partial_minutes'] = partial_n // 60
                    ph_props['episode_minutes'] = estimate_min
                    _capture_minutes_exhausted_if_depleted(
                        user_id, source, before_trial, before_paid)
                else:
                    _, _, remaining = trial_status(user)
                    if over_free_cap and not (
                            paid_bal <= 0 and episode_exceeds_remaining
                            and before_trial >= TRIAL_PARTIAL_MIN_SECONDS):
                        scope = 'episode_length'
                        _capture_trial_limit_hit(
                            user_id, scope, 'start', source,
                            estimate_min=estimate_min, remaining_min=remaining // 60)
                        cost = openai_whisper_cost_usd(estimate_min)
                        cover = pack_covers_episode_line(estimate_min)
                        cover_bit = f' {cover}' if cover and stripe_checkout_enabled() else ''
                        payload = {
                            'error': (
                                f'This episode is {estimate_min} minutes — too long for the '
                                f'free trial (max {TRIAL_MAX_EPISODE_SECONDS // 60}). '
                                f'{CREDIT_PACK_LABEL if stripe_checkout_enabled() else "Buy more minutes"}, '
                                f'or add your own OpenAI API key (about ${cost} at '
                                f'OpenAI\'s rate).{cover_bit}'
                            ),
                        }
                        payload.update(_minutes_limit_actions(
                            user_id, 'enqueue_episode_length', reason='episode_too_long',
                            estimate_min=estimate_min))
                        return payload, 402

                    scope = trial_refusal_scope(user, estimate)
                    if remaining >= estimate and paid_balance_seconds(user) <= 0:
                        # Account could afford it — shared budget (or lifetime) blocked.
                        scope = (
                            'daily' if daily_rem < max(estimate, TRIAL_PARTIAL_MIN_SECONDS)
                            else 'global')
                    # Daily budget blocked a partial that the account could afford.
                    if (paid_bal <= 0 and episode_exceeds_remaining
                            and before_trial >= TRIAL_PARTIAL_MIN_SECONDS
                            and daily_rem < TRIAL_PARTIAL_MIN_SECONDS):
                        scope = 'daily'
                    _capture_trial_limit_hit(
                        user_id, scope, 'start', source,
                        estimate_min=estimate_min, remaining_min=remaining // 60)
                    if scope == 'user':
                        cost = openai_whisper_cost_usd(estimate_min)
                        paid_left = paid_balance_seconds(user) // 60
                        paid_bit = (
                            f' (and {paid_left} paid)' if paid_left else '')
                        buy_bit = (
                            f'{CREDIT_PACK_LABEL.lower()}, or '
                            if stripe_checkout_enabled() else '')
                        cover = pack_covers_episode_line(estimate_min)
                        cover_bit = (
                            f' {cover}' if cover and stripe_checkout_enabled() else '')
                        message = (
                            f'This episode is about {estimate_min} minutes — longer than '
                            f'the {remaining // 60} free minutes you have left{paid_bit}. '
                            f'Pick a shorter episode, or {buy_bit}'
                            f'add your own OpenAI API key '
                            f'(about ${cost} for this one, billed by OpenAI).'
                            f'{cover_bit}'
                        )
                        if remaining <= 0 and paid_left <= 0:
                            paywall_reason = (
                                'paid_exhausted' if before_paid > 0 and before_trial <= 0
                                else 'trial_exhausted')
                        else:
                            paywall_reason = 'low_balance'
                    else:
                        paywall_reason = 'daily_cap' if scope == 'daily' else 'global_cap'
                        message = trial_daily_exhausted_message()
                        if scope == 'daily':
                            _capture_trial_daily_budget_exhausted(user_id, source)
                    payload = {'error': message}
                    payload.update(_minutes_limit_actions(
                        user_id, 'enqueue', reason=paywall_reason,
                        estimate_min=estimate_min))
                    return payload, 402

        task_id = str(uuid.uuid4())
        try:
            task = TranscriptionTask(
                id=task_id,
                user_id=user_id,
                episode_title=meta.get('title') or 'Episode',
                rss_url=rss_url,
                status='downloading',
                phase='downloading',
                phase_started_at=datetime.now(timezone.utc),
                podcast_name=meta.get('podcast_name'),
                artwork_url=meta.get('artwork'),
                episode_published=meta.get('published'),
                source_audio_url=audio_url,
                source_spotify_url=spotify_url,
                source_apple_url=apple_url,
                source_website_url=website_url,
                # Feed duration is the best ETA source we have, and it is available
                # before a single byte is downloaded. For partials, store the
                # reserved preview length so the progress ETA matches the work.
                audio_duration=(
                    float(trial_charge) if partial_job and trial_charge
                    else ((meta['duration_min'] * 60) if meta.get('duration_min') else None)
                ),
                language=language or None,
                trial_seconds_charged=trial_charge,
                paid_seconds_charged=paid_charge,
                partial_meta=(
                    encode_partial_task_meta(trial_charge, estimate)
                    if partial_job and trial_charge else None
                ),
                source=source,
                source_client=client_label if source == 'mcp' else None,
            )
            db.session.add(task)
            db.session.commit()
        except Exception:
            db.session.rollback()
            if trial_charge or paid_charge:
                platform_release(user_id, trial_charge or 0, paid_charge or 0)
            raise

        parsed_url = urlparse(meta['audio_url'])
        audio_filename = f"temp_audio_{task_id}" + (os.path.splitext(parsed_url.path)[1] or '.mp3')
        source_url = meta['audio_url']

        def transcribe_thread():
            try:
                # The try/finally wraps the context, not the other way round: an
                # error raised while entering app_context() -- a MemoryError under
                # exactly the pressure this cap exists to prevent -- would otherwise
                # skip the release and wedge this worker at 503 for good.
                with app.app_context():
                    try:
                        download_audio(source_url, audio_filename, task_id)
                        transcribe_audio(audio_filename, task_id, openai_client, language=language)
                    except TaskAbandoned:
                        # The sweeper wrote the error and settled the charge. Refund
                        # anyway: it is idempotent, and it is the backstop if anything
                        # re-opened the charge between the sweep and our abort.
                        abandoned = db.session.get(TranscriptionTask, task_id)
                        if abandoned:
                            trial_refund_task(abandoned)
                            product_analytics.capture(
                                'transcript_failed',
                                abandoned.user_id,
                                {**ph_props, 'reason': 'abandoned'},
                            )
                    except ServerRestart as e:
                        # Deploy/SIGTERM interrupted work. Leave the row
                        # non-terminal so boot resume can re-queue it once;
                        # if we already resumed once, fail with a clear message.
                        interrupted = db.session.get(TranscriptionTask, task_id)
                        attempts = int(
                            getattr(interrupted, 'resume_attempts', 0) or 0
                        ) if interrupted else 0
                        if interrupted is not None and attempts >= 1:
                            fail_task_and_refund(task_id, ServerRestart.MSG_RETRY)
                            trial_refund_task(interrupted)
                            product_analytics.capture(
                                'transcript_failed',
                                interrupted.user_id,
                                {**ph_props, 'reason': ServerRestart.REASON},
                            )
                            report_task_failure(
                                e, task_id=task_id, key_source=key_source)
                        else:
                            app.logger.warning(
                                'task %s interrupted by server restart '
                                '(resume_attempts=%s); leaving for boot resume',
                                task_id, attempts,
                            )
                    except Exception as e:
                        # Settle the reservation in the same write as status='error'
                        # (see fail_task_and_refund) so a failed task is never
                        # observed with minutes still charged.
                        error_message = (
                            describe_openai_error(e, key_source=key_source)
                            if _is_openai_error(e) else str(e))
                        fail_task_and_refund(task_id, error_message)
                        failed = db.session.get(TranscriptionTask, task_id)
                        reason = None
                        if failed:
                            # Idempotent backstop — cannot double-credit once settled.
                            trial_refund_task(failed)
                            if isinstance(e, TrialExhausted):
                                reason = 'trial_exhausted'
                                est_min = None
                                rem_min = None
                                if failed.audio_duration:
                                    est_min = int(failed.audio_duration) // 60
                                owner = db.session.get(User, failed.user_id)
                                if owner:
                                    rem_min = trial_status(owner)[2] // 60
                                _capture_trial_limit_hit(
                                    failed.user_id, e.scope, 'reconcile', source,
                                    estimate_min=est_min, remaining_min=rem_min)
                            elif isinstance(e, SourceAudioUnavailable):
                                reason = e.reason
                            elif _is_openai_error(e):
                                reason = product_analytics.openai_fail_reason(
                                    e, key_source=key_source)
                            else:
                                reason = 'other'
                            product_analytics.capture(
                                'transcript_failed',
                                failed.user_id,
                                {**ph_props, 'reason': reason},
                            )
                        # After the refund, and it cannot raise: reporting must
                        # never cost a user the allowance they are owed.
                        # Dead enclosure / host block, and BYOK auth/billing
                        # (user's OpenAI account), are expected user-side noise
                        # — log them, but do not fire the Sentry error alert.
                        # Platform-key quota/auth failures still go to Sentry.
                        if isinstance(e, SourceAudioUnavailable):
                            app.logger.warning(
                                'Source audio unavailable for task %s: %s '
                                '(HTTP %s)',
                                task_id, e.reason, e.status_code)
                        elif reason in ('own_key_no_credit', 'own_key_invalid'):
                            app.logger.warning(
                                'Own-key OpenAI account error for task %s: %s',
                                task_id, reason)
                        else:
                            report_task_failure(
                                e, task_id=task_id, key_source=key_source)
                    else:
                        # Outside the except that marks the job failed: analytics
                        # must never be able to turn a completed transcript into
                        # status=error (and must not trigger a trial refund).
                        # user_id is a plain int snapshotted before the thread;
                        # capture() also swallows, but keep a local guard so a
                        # broken monkeypatch / SDK cannot fail the job either.
                        try:
                            done_props = dict(ph_props)
                            finished = db.session.get(TranscriptionTask, task_id)
                            if finished is not None:
                                if finished.audio_duration:
                                    done_props['duration_min'] = round(
                                        finished.audio_duration / 60.0, 2)
                                if finished.language:
                                    done_props['language'] = normalize_language_code(
                                        finished.language) or None
                                if finished.podcast_name:
                                    done_props['podcast_name'] = finished.podcast_name
                                # Incl. this completion (status is already completed).
                                done_props['nth_transcript'] = (
                                    _completed_transcript_count(user_id))
                                done_props.update(_balance_analytics_props(user_id))
                                meta = task_partial_meta(finished)
                                if meta:
                                    n_min, m_min = partial_minutes_pair(meta)
                                    done_props['partial'] = True
                                    done_props['partial_minutes'] = n_min
                                    done_props['episode_minutes'] = m_min
                            product_analytics.capture(
                                'transcript_completed', user_id, done_props)
                            # Transcript-ready email off the critical path so
                            # Mailgun latency / retries cannot hold the worker.
                            if finished is not None:
                                _schedule_transcript_ready_email(
                                    task_id, user_id)
                                # Optional AI summary — never fails the transcript.
                                _schedule_transcript_summary(task_id, user_id)
                        except Exception:  # noqa: BLE001
                            app.logger.exception(
                                'transcript_completed analytics failed for %s',
                                task_id)
                    finally:
                        if os.path.exists(audio_filename):
                            try:
                                os.remove(audio_filename)
                            except OSError:
                                pass
            except Exception:
                # The context itself failed, so there is no way to record this
                # on the task. The stale sweeper will fail it and refund the
                # allowance; this log line is the only trace of why.
                app.logger.exception(
                    'transcription worker for %s died before it could start', task_id)
            finally:
                # Whatever happened, this job is done holding disk. Releasing here
                # rather than in the request is the whole point: the cap tracks
                # work in flight, not requests served.
                _transcription_slots.release()

        thread = threading.Thread(target=transcribe_thread)
        thread.daemon = True
        thread.start()
        # The thread's finally owns the slot from here on.
        slot_held = False

        product_analytics.capture('transcript_started', user_id, ph_props)
        return {'task_id': task_id}, 200
    finally:
        # Handed to the worker thread on success -- slot_held goes False only
        # AFTER thread.start() returns, so a thread that fails to start (the
        # box out of threads is exactly when this matters) still releases here.
        # Returned here on every refusal and any exception too, so a rejected
        # request cannot leak a slot for the life of the process.
        if slot_held:
            _transcription_slots.release()


@app.route('/start_transcription', methods=['POST'])
@login_required
def start_transcription():
    """Start a transcription from either an RSS feed + index, or a direct audio URL.

    The direct form is what episode search results post, so an episode found by
    name never has to be located a second time inside its feed.
    """
    # Auto-detect, not Norwegian. Defaulting to 'no' meant a Japanese listener
    # who took the default had Whisper TOLD the audio was Norwegian -- which it
    # obeys as a hard constraint, so the result is phonetic nonsense that we
    # still paid for. The same reasoning makes it the right fallback for an
    # unrecognised value: guessing beats asserting something we cannot know.
    # Also maps Whisper/legacy names ('english') so a retry of an old task works.
    language = normalize_language_code(request.form.get('language', ''))

    audio_url = request.form.get('audio_url')
    rss_url = (request.form.get('rss_url') or '').strip() or None
    # Direct-audio starts may also carry the show feed (search / Spotify). Drop
    # anything we would refuse to fetch — never persist a private URL.
    if rss_url and not _is_fetchable_url(rss_url):
        rss_url = None

    if audio_url:
        if not _is_fetchable_url(audio_url):
            return jsonify({'error': 'That audio URL cannot be fetched.'}), 400
        meta = {
            'title': request.form.get('episode_title') or 'Episode',
            'audio_url': audio_url,
            'podcast_name': request.form.get('podcast_name'),
            'artwork': request.form.get('artwork'),
            'published': request.form.get('published'),
            'duration_min': _positive_float_or_none(request.form.get('duration_min')),
            'input_origin': request.form.get('input_origin') or '',
            'spotify_url': request.form.get('spotify_url') or '',
            'apple_url': request.form.get('apple_url') or '',
            'episode_link': request.form.get('episode_link') or '',
            'website_url': request.form.get('website_url') or '',
            'show_link': request.form.get('show_link') or '',
        }
    else:
        if not rss_url or request.form.get('episode_index') in (None, ''):
            return jsonify({'error': 'Pick an episode first'}), 400
        try:
            episode_index = int(request.form.get('episode_index'))
        except (TypeError, ValueError):
            return jsonify({'error': 'Invalid episode selection'}), 400

        episodes, error = get_episodes_from_rss(rss_url)
        if error or episode_index < 0 or episode_index >= len(episodes):
            return jsonify({'error': 'Invalid episode selection'}), 400

        episode = episodes[episode_index]
        meta = {
            'title': episode['title'],
            'audio_url': episode['audio_url'],
            'podcast_name': request.form.get('podcast_name'),
            'artwork': episode.get('artwork') or request.form.get('artwork'),
            'published': episode.get('published'),
            'duration_min': _positive_float_or_none(episode.get('duration_min')),
            # RSS picker path (incl. Apple→RSS): prefer an explicit form value.
            'input_origin': request.form.get('input_origin') or 'rss',
            'spotify_url': request.form.get('spotify_url') or '',
            'apple_url': request.form.get('apple_url') or '',
            'episode_link': episode.get('episode_link') or request.form.get('episode_link') or '',
            'show_link': episode.get('show_link') or request.form.get('show_link') or '',
        }

    payload, status = enqueue_transcription(
        current_user, meta, rss_url=rss_url, language=language)
    return jsonify(payload), status


def _is_fetchable_url(raw):
    """Allow only public http(s) URLs.

    The direct-episode path takes an audio URL from the client and the server
    fetches it, so without this an authenticated user could point Podskrift at
    localhost or a link-local metadata endpoint and read the response back as a
    transcript.
    """
    import ipaddress
    import socket

    try:
        parsed = urlparse(raw)
    except ValueError:
        return False
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        return False

    try:
        infos = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror:
        return False

    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return False
    return True


def _positive_float_or_none(raw):
    """Parse a duration. Zero and negatives are treated as "not stated".

    A negative duration_min would otherwise reserve nothing (trial_reserve
    grants any non-positive request) and hand the segment offsets a negative
    chunk length.
    """
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _whisper_hang_budget_seconds():
    """Wall-clock seconds a single chunk may stay quiet while still being alive.

    Matches the hard timeout × retry budget in `_whisper_transcribe_chunk`, plus
    a small margin for backoff and scheduling. Anything quieter than this with
    chunks in flight is a dead worker, not a slow API.
    """
    return WHISPER_TIMEOUT_SECONDS * WHISPER_CHUNK_ATTEMPTS + 120


def _stale_after_seconds(task):
    """How long this particular task may stay quiet before it is presumed dead.

    The heartbeat is written per chunk (and between Whisper retries), so the
    window has to clear one in-flight attempt. It used to scale with
    `per_chunk * 8` unbounded, which let a hung mid-chunk job sit far longer
    than the Whisper timeout before /active-jobs noticed. Cap the transcribing
    window at the hard hang budget; keep the longer scaled windows for
    splitting/download where ffmpeg or a large pull is still silent work.
    """
    hang_budget = _whisper_hang_budget_seconds()
    if task.chunk_total and task.audio_duration:
        per_chunk = task.audio_duration / task.chunk_total / WHISPER_REALTIME_FACTOR
        return max(STALE_TASK_SECONDS, min(per_chunk * 8, hang_budget))
    if task.chunk_total:
        # Transcribing but duration unknown -- still bound by the Whisper budget.
        return max(STALE_TASK_SECONDS, hang_budget)
    if task.audio_duration:
        # Splitting sets no chunk_total yet, and cutting a large episode is
        # silent work -- scale off the episode length instead.
        return max(STALE_TASK_SECONDS, task.audio_duration / 5)
    if task.bytes_downloaded:
        # audio_duration is not written until the download has finished and the
        # file has been probed -- the phase this window has to cover. The
        # bytes already on disk are the only signal left. ~1 MB per minute of
        # spoken-word audio, same /5 factor as above.
        return max(STALE_TASK_SECONDS, (task.bytes_downloaded / (1024 * 1024)) * 60 / 5)
    return STALE_TASK_SECONDS


def _task_key_source_for_analytics(task):
    """Best-effort key_source for a task row (stale sweep has no enqueue context)."""
    if task.trial_seconds_charged is not None or (task.paid_seconds_charged or 0) > 0:
        return 'trial'
    return 'user'


def _fail_if_stale(task, source='poll'):
    """Fail a task whose worker has stopped writing progress.

    Called from /status, /active-jobs, and the background watchdog. Boot
    recovery uses ``resume_interrupted_tasks`` instead (re-queue once). Chunk
    files are gone with the dead thread, so resume re-downloads from
    ``source_audio_url``.
    """
    if task.status == 'completed' or task.status in TERMINAL_STATUSES:
        return False
    quiet = _seconds_since(task.heartbeat_at or task.started_at)
    if quiet <= _stale_after_seconds(task):
        return False
    last_status = task.status
    # One conditional UPDATE, like every other status write here. This used to
    # be a read-check-write on a session-cached row, and it is the one path that
    # can clobber a task that finished inside the window -- turning a completed
    # transcript into an error while keeping the full charge, which invites a
    # paid re-run. The live job bar polls this from every open tab, so it fires
    # roughly 15x more often than it used to.
    claimed = db.session.execute(text("""
        UPDATE transcription_tasks
           SET status = 'error', phase = 'error', error_message = :message
         WHERE id = :tid AND status NOT IN ('completed', 'error', 'cancelled')
    """), {
        'tid': task.id,
        'message': ('Transcription stopped making progress and was stopped. '
                    'Please try again.'),
    }).rowcount == 1
    db.session.commit()
    db.session.expire(task)
    if not claimed:
        return False
    trial_refund_task(task)
    report_stale_task(task.id, last_status, quiet, source=source)
    # Poll/watchdog/boot paths used to skip analytics, so a hung job that never
    # raised in the worker was invisible to the transcript_failed funnel.
    try:
        product_analytics.capture(
            'transcript_failed',
            task.user_id,
            {
                'reason': 'stale',
                'key_source': _task_key_source_for_analytics(task),
            },
        )
    except Exception:  # noqa: BLE001 - analytics must never undo the refund
        app.logger.exception(
            'transcript_failed analytics failed for stale task %s', task.id)
    return True


def _sweep_stale_tasks(source='watchdog'):
    """Fail every running task that has gone quiet past its stale window.

    Safe for concurrent callers (two gunicorn workers, watchdog + poll): each
    claim is one conditional UPDATE inside `_fail_if_stale`.
    """
    running = TranscriptionTask.query.filter(
        ~TranscriptionTask.status.in_(['completed', *TERMINAL_STATUSES]),
    ).all()
    failed = 0
    for task in running:
        if _fail_if_stale(task, source=source):
            failed += 1
    return failed


def _stale_watchdog_loop():
    """Background backstop when no client is polling /status or /active-jobs."""
    while True:
        time.sleep(STALE_WATCHDOG_INTERVAL_SECONDS)
        try:
            with app.app_context():
                _sweep_stale_tasks(source='watchdog')
        except Exception:  # noqa: BLE001 - never let the watchdog thread die quietly
            app.logger.exception('stale-task watchdog sweep failed')


def start_stale_watchdog():
    """Start the daemon watchdog once per process. No-op when disabled.

    The test suite sets PODSKRIFT_DISABLE_WATCHDOG=1 before importing this
    module so a sleeping thread cannot race the throwaway DB.
    """
    global _stale_watchdog_started
    if _stale_watchdog_started:
        return False
    if os.getenv('PODSKRIFT_DISABLE_WATCHDOG', '').strip().lower() in (
            '1', 'true', 'yes'):
        return False
    thread = threading.Thread(
        target=_stale_watchdog_loop, name='stale-task-watchdog', daemon=True,
    )
    thread.start()
    _stale_watchdog_started = True
    return True


def count_in_flight_transcriptions():
    """Queued / running transcriptions (anything not completed or terminal)."""
    return (
        TranscriptionTask.query
        .filter(~TranscriptionTask.status.in_(['completed', *TERMINAL_STATUSES]))
        .count()
    )


def _task_heartbeat_ts(task):
    hb = task.heartbeat_at or task.started_at
    if hb is None:
        return None
    if hb.tzinfo is None:
        hb = hb.replace(tzinfo=timezone.utc)
    return hb.timestamp()


def _task_is_orphaned(task):
    """True when the task's last heartbeat predates this process.

    A live job owned by another gunicorn worker keeps heartbeating after we
    boot; an interrupted job does not.
    """
    ts = _task_heartbeat_ts(task)
    if ts is None:
        return True
    return ts < (_PROCESS_STARTED_AT - 0.5)


def _claim_task_for_resume(task_id):
    """Mark one orphaned task for a single automatic re-queue. True if we won.

    Keeps trial/paid reservations untouched. Resets progress fields so the
    worker re-downloads and re-encodes (chunk files died with the old process).
    """
    now = datetime.now(timezone.utc)
    result = db.session.execute(text("""
        UPDATE transcription_tasks
           SET resume_attempts = COALESCE(resume_attempts, 0) + 1,
               status = 'downloading',
               phase = 'downloading',
               progress = 0,
               download_progress = 0,
               chunk_index = NULL,
               chunk_total = NULL,
               bytes_downloaded = NULL,
               bytes_total = NULL,
               error_message = NULL,
               phase_started_at = :now,
               heartbeat_at = :now
         WHERE id = :tid
           AND COALESCE(resume_attempts, 0) = 0
           AND status NOT IN ('completed', 'error', 'cancelled')
           AND COALESCE(trial_settled, 0) = 0
    """), {'tid': task_id, 'now': now})
    db.session.commit()
    return result.rowcount == 1


def _ph_props_for_task(task, key_source, source='resume'):
    """Analytics labels for a resumed (or otherwise continued) task."""
    duration_min = None
    if task.audio_duration:
        duration_min = round(float(task.audio_duration) / 60.0, 2)
    return {
        'key_source': key_source,
        'source': source,
        'duration_min': duration_min,
        'language': task.language or None,
        'input_origin': 'resume',
        'has_feed': bool((task.rss_url or '').strip()),
        'podcast_name': task.podcast_name or None,
        'nth_transcript': _completed_transcript_count(task.user_id) + 1,
        'resumed': True,
    }


def _spawn_worker_for_existing_task(task):
    """Start the download+Whisper thread for a row that already holds a reservation.

    Returns True when the thread was started (and owns the capacity slot).
    """
    source_url = (task.source_audio_url or '').strip()
    if not source_url or not _is_fetchable_url(source_url):
        fail_task_and_refund(
            task.id,
            'This episode could not be restarted automatically — please try again.',
        )
        report_task_failure(
            RuntimeError('resume missing source_audio_url'),
            task_id=task.id, key_source='unknown',
        )
        return False

    user = db.session.get(User, task.user_id)
    if user is None:
        fail_task_and_refund(task.id, ServerRestart.MSG_RETRY)
        return False

    api_key, key_source = resolve_openai_key(user)
    if not api_key:
        fail_task_and_refund(
            task.id,
            'No OpenAI API key configured. Add your key in Settings.',
        )
        return False

    if not _transcription_slots.acquire(blocking=False):
        app.logger.warning(
            'resume deferred for %s: worker at its concurrent limit', task.id)
        return False

    openai_client = build_openai_client(api_key)
    task_id = task.id
    language = task.language or ''
    ph_props = _ph_props_for_task(task, key_source)
    parsed_url = urlparse(source_url)
    audio_filename = (
        f'temp_audio_{task_id}'
        + (os.path.splitext(parsed_url.path)[1] or '.mp3')
    )
    user_id = task.user_id
    source = 'resume'

    def transcribe_thread():
        try:
            with app.app_context():
                try:
                    download_audio(source_url, audio_filename, task_id)
                    transcribe_audio(
                        audio_filename, task_id, openai_client, language=language)
                except TaskAbandoned:
                    abandoned = db.session.get(TranscriptionTask, task_id)
                    if abandoned:
                        trial_refund_task(abandoned)
                        product_analytics.capture(
                            'transcript_failed',
                            abandoned.user_id,
                            {**ph_props, 'reason': 'abandoned'},
                        )
                except ServerRestart as e:
                    interrupted = db.session.get(TranscriptionTask, task_id)
                    attempts = int(
                        getattr(interrupted, 'resume_attempts', 0) or 0
                    ) if interrupted else 0
                    if interrupted is not None and attempts >= 1:
                        fail_task_and_refund(task_id, ServerRestart.MSG_RETRY)
                        trial_refund_task(interrupted)
                        product_analytics.capture(
                            'transcript_failed',
                            interrupted.user_id,
                            {**ph_props, 'reason': ServerRestart.REASON},
                        )
                        report_task_failure(
                            e, task_id=task_id, key_source=key_source)
                    else:
                        app.logger.warning(
                            'resumed task %s interrupted again before boot '
                            'marker; leaving for next resume',
                            task_id,
                        )
                except Exception as e:
                    error_message = (
                        describe_openai_error(e, key_source=key_source)
                        if _is_openai_error(e) else str(e))
                    fail_task_and_refund(task_id, error_message)
                    failed = db.session.get(TranscriptionTask, task_id)
                    reason = 'other'
                    if failed:
                        trial_refund_task(failed)
                        if isinstance(e, TrialExhausted):
                            reason = 'trial_exhausted'
                        elif isinstance(e, SourceAudioUnavailable):
                            reason = e.reason
                        elif _is_openai_error(e):
                            reason = product_analytics.openai_fail_reason(
                                e, key_source=key_source)
                        product_analytics.capture(
                            'transcript_failed',
                            failed.user_id,
                            {**ph_props, 'reason': reason},
                        )
                    if isinstance(e, SourceAudioUnavailable):
                        app.logger.warning(
                            'Source audio unavailable for resumed task %s: %s',
                            task_id, e.reason)
                    elif reason in ('own_key_no_credit', 'own_key_invalid'):
                        app.logger.warning(
                            'Own-key OpenAI account error for resumed task %s: %s',
                            task_id, reason)
                    else:
                        report_task_failure(
                            e, task_id=task_id, key_source=key_source)
                else:
                    try:
                        done_props = dict(ph_props)
                        finished = db.session.get(TranscriptionTask, task_id)
                        if finished is not None:
                            if finished.audio_duration:
                                done_props['duration_min'] = round(
                                    finished.audio_duration / 60.0, 2)
                            if finished.language:
                                done_props['language'] = normalize_language_code(
                                    finished.language) or None
                            done_props['nth_transcript'] = (
                                _completed_transcript_count(user_id))
                            done_props.update(_balance_analytics_props(user_id))
                        product_analytics.capture(
                            'transcript_completed', user_id, done_props)
                        if finished is not None:
                            _schedule_transcript_ready_email(task_id, user_id)
                            # Optional AI summary — never fails the transcript.
                            _schedule_transcript_summary(task_id, user_id)
                    except Exception:  # noqa: BLE001
                        app.logger.exception(
                            'transcript_completed analytics failed for resumed %s',
                            task_id)
                finally:
                    if os.path.exists(audio_filename):
                        try:
                            os.remove(audio_filename)
                        except OSError:
                            pass
        except Exception:
            app.logger.exception(
                'resumed transcription worker for %s died before it could start',
                task_id)
        finally:
            _transcription_slots.release()

    thread = threading.Thread(
        target=transcribe_thread, name=f'resume-{task_id[:8]}', daemon=True)
    thread.start()
    product_analytics.capture('transcript_started', user_id, ph_props)
    app.logger.info('resumed interrupted transcription %s', task_id)
    return True


def resume_interrupted_tasks():
    """Re-queue tasks left mid-flight by a previous process death. Once each.

    Idempotent across two gunicorn workers: the conditional UPDATE on
    ``resume_attempts`` is the claim. Reservations stay on the row (no
    re-reserve / no double charge). A task that fails again after resume is
    failed with ServerRestart.MSG_RETRY and reported to Sentry.
    """
    running = (
        TranscriptionTask.query
        .filter(~TranscriptionTask.status.in_(['completed', *TERMINAL_STATUSES]))
        .all()
    )
    resumed = 0
    failed_second = 0
    for task in running:
        if not _task_is_orphaned(task):
            continue
        attempts = int(task.resume_attempts or 0)
        if attempts >= 1:
            fail_task_and_refund(task.id, ServerRestart.MSG_RETRY)
            db.session.expire(task)
            report_task_failure(
                ServerRestart(auto_resumed=False),
                task_id=task.id,
                key_source=_task_key_source_for_analytics(task),
            )
            try:
                product_analytics.capture(
                    'transcript_failed',
                    task.user_id,
                    {
                        'reason': ServerRestart.REASON,
                        'key_source': _task_key_source_for_analytics(task),
                        'resumed': True,
                    },
                )
            except Exception:  # noqa: BLE001
                app.logger.exception(
                    'transcript_failed analytics failed for resume-exhausted %s',
                    task.id)
            failed_second += 1
            continue
        if not _claim_task_for_resume(task.id):
            continue
        db.session.expire(task)
        claimed = db.session.get(TranscriptionTask, task.id)
        if claimed is None:
            continue
        if _spawn_worker_for_existing_task(claimed):
            resumed += 1
        else:
            # No capacity slot — leave downloading/resume_attempts=1; the
            # stale watchdog or a later boot will settle it if still stuck.
            app.logger.warning(
                'claimed %s for resume but could not start a worker', task.id)
    if resumed or failed_second:
        app.logger.info(
            'boot resume: started=%s failed_after_resume=%s',
            resumed, failed_second)
    return {'resumed': resumed, 'failed_second': failed_second}


@app.route('/internal/in-flight')
def internal_in_flight():
    """Local-only count of queued/running transcriptions for deploy drain."""
    remote = (request.remote_addr or '').strip()
    # Public traffic also arrives from 127.0.0.1 (Plesk nginx/Apache proxy),
    # but always carries forwarding headers; the drain script's direct curl
    # to 127.0.0.1:5002 never does.
    proxied = any(request.headers.get(h) for h in (
        'X-Real-IP', 'X-Forwarded-For', 'X-Forwarded-Host', 'Forwarded'))
    if remote not in ('127.0.0.1', '::1') or proxied:
        return jsonify({'error': 'forbidden'}), 403
    n = count_in_flight_transcriptions()
    return jsonify({'ok': True, 'in_flight': n})


@app.route('/status/<task_id>')
@login_required
def get_status(task_id):
    task = db.session.get(TranscriptionTask, task_id)
    if not task or task.user_id != current_user.id:
        return jsonify({'error': 'Task not found'}), 404

    _fail_if_stale(task)
    # Touch last_polled_at so transcript-ready email can tell "still watching"
    # from "left the page". Best-effort; never block the status payload.
    try:
        task.last_polled_at = datetime.now(timezone.utc)
        db.session.commit()
    except Exception:  # noqa: BLE001
        db.session.rollback()
    percent, eta = compute_live_progress(task)
    elapsed = _seconds_since(task.started_at)

    result = {
        'status': task.status,
        'phase': task.phase or task.status,
        'progress': percent,
        'episode_title': task.episode_title,
        'podcast_name': task.podcast_name,
        'artwork_url': task.artwork_url,
        'episode_published': task.episode_published,
        'audio_duration': task.audio_duration,
        'chunk_index': task.chunk_index,
        'chunk_total': task.chunk_total,
        'bytes_downloaded': task.bytes_downloaded,
        'bytes_total': task.bytes_total,
        'elapsed_seconds': int(elapsed),
        'eta_seconds': int(eta) if eta is not None else None,
        'estimated_cost': (
            round((task.audio_duration / 60) * WHISPER_COST_PER_MINUTE, 3)
            if task.audio_duration else None
        ),
        # Boolean only — the result page uses this when the async
        # related-episodes fetch times out or fails.
        'has_rss': bool((task.rss_url or '').strip()),
        'listen_links': listen_links_for_task(task),
    }

    if task.status == 'cancelled':
        result['cancelled'] = True
    elif task.status == 'error':
        err = task.error_message or 'Unknown error'
        result['error'] = err
        own_key_pack = (
            OWN_KEY_PACK_OFFER.lower() in err.lower()
            or 'free or paid minutes' in err.lower()
        )
        if _is_no_billing_error(err) or (
                own_key_pack and 'rejected this key' in err.lower()):
            # BYOK auth/billing — offer pack + retry. Never echo key material.
            if _is_no_billing_error(err):
                result['no_billing'] = True
            else:
                result['own_key_invalid'] = True
            if task.source_audio_url:
                duration_min = None
                if task.audio_duration:
                    duration_min = max(1, int(round(task.audio_duration / 60.0)))
                result['retry'] = {
                    'audio_url': task.source_audio_url,
                    'episode_title': task.episode_title or 'Episode',
                    'podcast_name': task.podcast_name or '',
                    'artwork': task.artwork_url or '',
                    'published': task.episode_published or '',
                    'rss_url': task.rss_url or '',
                    'duration_min': (
                        str(duration_min) if duration_min is not None else ''),
                    # Always an ISO code (or '') so start_transcription accepts it.
                    'language': normalize_language_code(task.language) or '',
                }
            if stripe_checkout_enabled():
                result['buy_available'] = True
                result['buy_label'] = CREDIT_PACK_LABEL
                result['pack_offer'] = OWN_KEY_PACK_OFFER
        elif _is_minutes_limit_error(err):
            result['minutes_error'] = True
            reason = 'trial_exhausted'
            low = err.lower()
            if 'too long for the free trial' in low:
                reason = 'episode_too_long'
            elif ("today's free minutes are used up" in low
                  or 'refill at midnight' in low):
                reason = 'daily_cap'
            elif 'handed out all the free minutes' in low:
                reason = 'global_cap'
            elif 'paid minutes you have left' in low:
                reason = 'paid_exhausted'
            elif 'minutes you have left' in low:
                reason = 'low_balance'
            estimate_min = None
            if task.audio_duration:
                estimate_min = max(1, int(round(task.audio_duration / 60.0)))
            result.update(_minutes_limit_actions(
                task.user_id, 'reconcile_status', reason=reason,
                estimate_min=estimate_min))

    # Partial text so the page fills in as chunks land, rather than staying empty
    if task.transcript_text and task.status != 'completed':
        result['partial_text'] = task.transcript_text

    if task.status == 'cancelled' and task.transcript_text:
        result['download_txt'] = url_for('download_file', task_id=task_id, file_type='txt')
        result['transcript_text'] = task.transcript_text

    if task.status == 'completed':
        result['download_txt'] = url_for('download_file', task_id=task_id, file_type='txt')
        result['download_srt'] = url_for('download_file', task_id=task_id, file_type='srt')
        result['transcript_text'] = task.transcript_text or ''
        if task.transcription_time:
            result['actual_transcription_time'] = f"{task.transcription_time:.1f} seconds"
        if task.language:
            # Display name for UI; old rows with names still look fine.
            result['language'] = display_language(task.language)
        # Optional AI summary (flagged; status may still be pending).
        result['summary_status'] = getattr(task, 'summary_status', None)
        if (result['summary_status'] is None and summary_mod.summary_enabled()
                and task.status == 'completed' and task.completed_at is not None):
            # The summary thread starts just after completion; report it as
            # pending for a short window so the page keeps checking for it.
            done_at = task.completed_at
            if done_at.tzinfo is None:
                done_at = done_at.replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - done_at).total_seconds() < 120:
                result['summary_status'] = 'pending'
        if getattr(task, 'summary_status', None) == 'ready':
            parsed = summary_mod.parse_summary_json(
                getattr(task, 'summary_json', None))
            if parsed:
                result['summary'] = parsed
        partial = task_partial_meta(task)
        if partial:
            n_min, m_min = partial_minutes_pair(partial)
            result['partial'] = True
            result['partial_minutes'] = n_min
            result['episode_minutes'] = m_min
            result['partial_note'] = partial_transcript_note(partial)
            # Enough for the result page to offer Buy / finish-full without
            # another round trip. duration_min is the full episode when known.
            result['finish'] = {
                'audio_url': task.source_audio_url or '',
                'episode_title': task.episode_title or 'Episode',
                'podcast_name': task.podcast_name or '',
                'artwork': task.artwork_url or '',
                'published': task.episode_published or '',
                'rss_url': task.rss_url or '',
                'duration_min': str(m_min),
                'language': normalize_language_code(task.language) or '',
            }
            trial_left, paid_left = _platform_remaining_seconds(task.user_id)
            result['paid_remaining_min'] = paid_left // 60
            result['trial_remaining_min'] = trial_left // 60
            result['can_finish_full'] = paid_left > 0 or bool(
                getattr(current_user, 'openai_api_key', None))
            if stripe_checkout_enabled():
                result['buy_available'] = True
                result['buy_label'] = UNLOCK_EPISODE_LABEL
                result['unlock_label'] = UNLOCK_EPISODE_LABEL
        elif (
            stripe_checkout_enabled()
            and not bool(getattr(current_user, 'openai_api_key', None))
        ):
            # Calm secondary offer after a full transcript — not a second primary.
            result['result_offer'] = True
            result['result_offer_label'] = RESULT_OFFER_LABEL
            result['buy_available'] = True
            result['buy_label'] = CREDIT_PACK_LABEL
        # One-line balance fact under the completed transcript (metered only).
        fact = balance_fact_for_user(current_user)
        if fact:
            result['balance_fact'] = fact

    return jsonify(result)


@app.route('/cancel/<task_id>', methods=['POST'])
@login_required
def cancel_transcription(task_id):
    """Cancel a running transcription.

    A daemon thread cannot be interrupted from outside, so this does not try:
    it writes a terminal status and lets the worker notice at its next chunk
    boundary. _update_task refuses to move a task out of a terminal status, so
    the worker cannot resurrect it in the meantime -- and the refund is settled
    here, immediately, rather than whenever the thread gets around to stopping.
    """
    task = db.session.get(TranscriptionTask, task_id)
    if not task or task.user_id != current_user.id:
        return jsonify({'error': 'Task not found'}), 404

    # One conditional UPDATE, not read-then-write. A job that finished inside
    # that window was being stamped 'cancelled' -- keeping the full charge while
    # its transcript fell out of history and its download 404'd. Every other
    # status write in this file has the same shape for the same reason.
    claimed = db.session.execute(text("""
        UPDATE transcription_tasks
           SET status = 'cancelled', phase = 'cancelled', error_message = 'Cancelled.'
         WHERE id = :tid AND status NOT IN ('completed', 'error', 'cancelled')
    """), {'tid': task_id}).rowcount == 1
    db.session.commit()
    db.session.expire(task)

    if not claimed:
        status = task.status
        if status == 'completed':
            return jsonify({'error': 'That transcription already finished.'}), 409
        if status == 'error':
            # It failed on its own. Reporting that as a cancellation would hide
            # the reason from someone who clicked Stop a moment too late.
            return jsonify({'status': 'error', 'cancelled': False,
                            'error': task.error_message or 'Transcription failed.'}), 409
        # Already cancelled -- a double-click, not an error.
        return jsonify({'status': 'cancelled', 'cancelled': True, 'refunded_seconds': 0})

    refunded = trial_refund_task(task)
    app.logger.info('task %s cancelled by user %s', task_id, current_user.id)
    return jsonify({'status': 'cancelled', 'cancelled': True,
                    'refunded_seconds': refunded})


def active_tasks_for(user):
    """Transcriptions this user has in flight, newest first.

    The work survives leaving the page -- it runs server-side and is written to
    SQLite -- but nothing in the UI said so, so coming back looked like the job
    had vanished and people started it again.
    """
    if not user.is_authenticated:
        return []
    running = TranscriptionTask.query.filter(
        TranscriptionTask.user_id == user.id,
        ~TranscriptionTask.status.in_(['completed', *TERMINAL_STATUSES]),
    ).order_by(TranscriptionTask.started_at.desc()).limit(5).all()
    # _fail_if_stale is what notices a task whose worker died, and it only runs
    # when something asks about that task. Ask here, or a dead job would sit in
    # this list forever inviting the user to wait for nothing.
    return [t for t in running if not _fail_if_stale(t)]


@app.route('/active-jobs')
@login_required
def active_jobs():
    """What this user has in flight, for the indicator carried on every page.

    Deliberately small: this is polled from every page, so it returns only what
    the bar draws. It is also the only thing that runs _fail_if_stale while the
    user is somewhere else in the app, which is what makes a job killed by a
    deploy stop claiming to be alive.
    """
    jobs = []
    for task in active_tasks_for(current_user):
        percent, eta = compute_live_progress(task)
        jobs.append({
            'id': task.id,
            'title': task.episode_title,
            'podcast_name': task.podcast_name,
            'artwork_url': task.artwork_url,
            'percent': percent,
            'eta_seconds': int(eta) if eta is not None else None,
            'phase': task.phase or task.status,
        })
    return jsonify({'jobs': jobs})


@app.route('/download/<task_id>/<file_type>')
@login_required
def download_file(task_id, file_type):
    import json as _json
    from io import BytesIO

    task = db.session.get(TranscriptionTask, task_id)
    # A cancelled task keeps whatever was transcribed before it stopped, and the
    # user was charged pro-rata for exactly that -- so it has to be reachable.
    if not task or task.user_id != current_user.id:
        return "File not found", 404
    # A cancelled task keeps whatever was transcribed before it stopped, and the
    # user was charged pro-rata for exactly that -- so it has to be reachable.
    # Only .txt: segments_json is normally NULL on a cancelled task, and an .srt
    # built from nothing is a one-line stub pretending to be a transcript.
    if task.status == 'cancelled':
        if file_type != 'txt' or not task.transcript_text:
            return "File not found", 404
    elif task.status != 'completed':
        return "File not found", 404

    safe_title = safe_download_basename(task.episode_title)
    partial = task_partial_meta(task) if task.status == 'completed' else None
    note = partial_transcript_note(partial) if partial else ''

    if file_type == 'txt':
        body = task.transcript_text or ''
        if note:
            body = f'{note}\n\n{body}' if body else note
        # Optional Summary section at the top when ready (srt stays unchanged).
        if getattr(task, 'summary_status', None) == 'ready':
            parsed = summary_mod.parse_summary_json(
                getattr(task, 'summary_json', None))
            if parsed and (parsed.get('tldr') or parsed.get('key_points')):
                section = summary_mod.format_summary_for_txt(parsed)
                body = f'{section}\n{body}' if body else section
        content = body.encode('utf-8')
        return send_file(
            BytesIO(content),
            as_attachment=True,
            download_name=f"{safe_title}.txt",
            mimetype='text/plain',
        )
    elif file_type == 'srt':
        lines = []
        index = 1
        if note:
            lines.extend([
                f'{index}',
                '00:00:00,000 --> 00:00:00,500',
                note,
                '',
            ])
            index += 1
        if task.segments_json:
            try:
                segments = _json.loads(task.segments_json)
            except (_json.JSONDecodeError, TypeError):
                segments = []
            for seg in segments:
                lines.append(f"{index}")
                lines.append(
                    f"{format_timestamp(seg['start'])} --> {format_timestamp(seg['end'])}")
                lines.append((seg.get('text') or '').strip())
                lines.append('')
                index += 1
        else:
            lines.extend([
                f'{index}',
                '00:00:00,500 --> 00:00:01,500' if note else '00:00:00,000 --> 00:00:01,000',
                task.transcript_text or '',
                '',
            ])
        content = '\n'.join(lines).encode('utf-8')
        return send_file(
            BytesIO(content),
            as_attachment=True,
            download_name=f"{safe_title}.srt",
            mimetype='text/srt',
        )
    else:
        return "Invalid file type", 400


@app.route('/transcription/<task_id>')
@login_required
def transcription_page(task_id):
    task = db.session.get(TranscriptionTask, task_id)
    if not task or task.user_id != current_user.id:
        return "Task not found", 404
    if (request.args.get('utm_source') or '').strip().lower() == 'email':
        campaign = (request.args.get('utm_campaign') or '').strip()[:64] or 'unknown'
        product_analytics.capture(
            'email_clicked',
            current_user.id,
            {'type': campaign},
        )
    return render_template(
        'transcription.html',
        task_id=task_id,
        listen_links=listen_links_for_task(task),
    )


def _task_owned_or_404(task_id):
    """Return the caller's TranscriptionTask, or a (json, status) error pair."""
    task = db.session.get(TranscriptionTask, task_id)
    if not task or task.user_id != current_user.id:
        return None, (jsonify({'error': 'Task not found'}), 404)
    return task, None


def mint_share_token():
    """Unguessable url-safe token (>=128-bit entropy)."""
    return secrets.token_urlsafe(SHARE_TOKEN_BYTES)


def share_create_reserve(user_id):
    """Atomically take one share-create slot for this user, or None if capped."""
    now = time.time()
    key = int(user_id)
    with _share_create_lock:
        seen = [t for t in _share_create_attempts.get(key, ())
                if now - t[0] < SHARE_CREATE_WINDOW_SECONDS]
        if len(seen) >= SHARE_CREATE_MAX_PER_USER:
            _share_create_attempts[key] = seen
            return None
        token = (now, uuid.uuid4().hex)
        seen.append(token)
        _share_create_attempts[key] = seen
        if len(_share_create_attempts) > 10000:
            stale = [k for k, v in list(_share_create_attempts.items())
                     if not v or now - v[-1][0] > SHARE_CREATE_WINDOW_SECONDS]
            for k in stale:
                _share_create_attempts.pop(k, None)
        return token


def share_create_release(user_id, token):
    if token is None:
        return
    key = int(user_id)
    with _share_create_lock:
        held = _share_create_attempts.get(key)
        if not held:
            return
        try:
            held.remove(token)
        except ValueError:
            return
        if not held:
            _share_create_attempts.pop(key, None)


def safe_public_http_url(raw, *, max_len=1024):
    """Return a cleaned public http(s) URL, or None.

    Rejects javascript:, data:, and anything that is not http/https. Used for
    outbound listen links on share / result pages (never echo unvalidated URLs).
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text or len(text) > max_len:
        return None
    # Block scheme-relative and sneaky whitespace / control characters.
    if re.search(r'[\x00-\x1f\x7f]', text):
        return None
    try:
        parsed = urlparse(text)
    except ValueError:
        return None
    scheme = (parsed.scheme or '').lower()
    if scheme not in ('http', 'https'):
        return None
    if not parsed.netloc or '@' in parsed.netloc:
        return None
    # Rebuild from parts so a mixed javascript: payload cannot survive into an href.
    cleaned = urlunparse((
        scheme, parsed.netloc.lower(), parsed.path or '',
        parsed.params, parsed.query, parsed.fragment,
    ))
    return cleaned if len(cleaned) <= max_len else None


def canonical_spotify_episode_url(raw):
    """https://open.spotify.com/episode/<id> when raw is an episode link."""
    kind, spotify_id = parse_spotify_url(raw)
    if kind != 'episode' or not spotify_id:
        return None
    return f'https://open.spotify.com/episode/{spotify_id}'


def canonical_apple_podcasts_url(raw):
    """Normalised podcasts.apple.com show/episode URL, or None."""
    cleaned = safe_public_http_url(raw)
    if not cleaned or urlparse(cleaned).hostname != 'podcasts.apple.com':
        return None
    show_id, episode_id = parse_apple_podcasts_url(cleaned)
    if not show_id:
        return None
    base = f'https://podcasts.apple.com/podcast/id{show_id}'
    if episode_id:
        return f'{base}?i={episode_id}'
    return base


#: feed_url → apple show/episode URL ('' = looked up, none found).
_apple_feed_url_cache = {}
_apple_feed_url_cache_lock = threading.Lock()
APPLE_FEED_LOOKUP_TIMEOUT_SEC = 2.0


def _norm_title(value):
    return re.sub(r'\s+', ' ', (value or '')).strip().casefold()


def apple_url_for_feed(feed_url, podcast_name=None, *, episode_title=None,
                       audio_url=None, timeout=APPLE_FEED_LOOKUP_TIMEOUT_SEC):
    """Apple Podcasts URL for a feed (episode-level when it can match), or None.

    Only the show *name* is sent to Apple's search, never the feed URL or its
    path (private feeds carry tokens there). A hit must have exactly the same
    feedUrl, which also proves the feed is in Apple's public directory.
    Cached per process (misses too). Never raises. Do not call from a request
    thread: use schedule_listen_link_backfill().
    """
    feed = safe_public_http_url(feed_url)
    name = (podcast_name or '').strip()
    if not feed or len(name) < 2:
        return None
    want_feed = feed.rstrip('/').lower()
    audio = safe_public_http_url(audio_url) or ''
    cache_key = (want_feed, audio, _norm_title(episode_title))
    with _apple_feed_url_cache_lock:
        if cache_key in _apple_feed_url_cache:
            return _apple_feed_url_cache[cache_key] or None

    found = None
    try:
        show_id = None
        try:
            resp = requests.get(
                'https://itunes.apple.com/search',
                timeout=timeout,
                params={'term': name[:200], 'media': 'podcast',
                        'entity': 'podcast', 'limit': 25},
            )
            resp.raise_for_status()
            results = resp.json().get('results') or []
        except (requests.RequestException, ValueError, TypeError):
            results = None
        for item in results or []:
            item_feed = safe_public_http_url(item.get('feedUrl') or '')
            if item_feed and item_feed.rstrip('/').lower() == want_feed:
                cid = str(item.get('collectionId') or '')
                if re.fullmatch(r'\d+', cid):
                    show_id = cid
                    break
        if show_id:
            found = f'https://podcasts.apple.com/podcast/id{show_id}'
            want_title = _norm_title(episode_title)
            if audio or want_title:
                try:
                    eps = _itunes_lookup(show_id, entity='podcastEpisode',
                                         limit=200)
                except Exception:  # noqa: BLE001
                    eps = []
                for ep in eps or []:
                    if ep.get('wrapperType') != 'podcastEpisode':
                        continue
                    tid = str(ep.get('trackId') or '')
                    if not re.fullmatch(r'\d+', tid):
                        continue
                    ep_audio = safe_public_http_url(ep.get('episodeUrl') or '')
                    if ((audio and ep_audio == audio)
                            or (want_title
                                and _norm_title(ep.get('trackName')) == want_title)):
                        found = f'{found}?i={tid}'
                        break
    except Exception:  # noqa: BLE001 - never break share/result render
        app.logger.exception('apple feed lookup failed')
        found = None
        return None  # do not cache unexpected failures

    with _apple_feed_url_cache_lock:
        if results is not None or found:
            _apple_feed_url_cache[cache_key] = found or ''
    return found


def website_url_from_episode_meta(meta):
    """Prefer episode <link>, then show link; never the audio file itself."""
    audio = safe_public_http_url(meta.get('audio_url') or '')
    for key in ('episode_link', 'website_url', 'show_link'):
        url = safe_public_http_url(meta.get(key) or '')
        if url and url != audio:
            return url
    return None


def listen_links_from_fields(*, spotify_url=None, apple_url=None,
                             website_url=None, audio_url=None):
    """Build ordered listen-link dicts from stored/resolved URLs."""
    links = []
    spotify = canonical_spotify_episode_url(spotify_url or '')
    if spotify:
        links.append({
            'platform': 'spotify',
            'label': 'Listen on Spotify',
            'url': spotify,
        })
    apple = canonical_apple_podcasts_url(apple_url or '')
    if apple:
        links.append({
            'platform': 'apple',
            'label': 'Listen on Apple Podcasts',
            'url': apple,
        })
    website = safe_public_http_url(website_url)
    if website:
        links.append({
            'platform': 'website',
            'label': "Listen on the podcast's website",
            'url': website,
        })
    audio = safe_public_http_url(audio_url)
    if audio:
        links.append({
            'platform': 'audio',
            'label': 'Play audio',
            'url': audio,
            'is_audio': True,
        })
    return links


def listen_links_for_task(task):
    """Listen destinations already stored on the task (no network)."""
    if task is None:
        return []
    return listen_links_from_fields(
        spotify_url=getattr(task, 'source_spotify_url', None),
        apple_url=getattr(task, 'source_apple_url', None),
        website_url=getattr(task, 'source_website_url', None),
        audio_url=getattr(task, 'source_audio_url', None),
    )


def public_listen_links_for_task(task):
    """Listen links safe for the public /t/ page.

    The raw audio URL is only shown when the episode is known to be in a
    public directory (Spotify/Apple link stored). Private/premium feeds put
    access tokens in enclosure URLs, and a share link must not hand those out.
    """
    links = listen_links_for_task(task)
    if task is None:
        return links
    public = bool(getattr(task, 'source_spotify_url', None)
                  or getattr(task, 'source_apple_url', None))
    if public:
        return links
    return [L for L in links if not L.get('is_audio')]


def ensure_task_listen_links(task, *, timeout=APPLE_FEED_LOOKUP_TIMEOUT_SEC):
    """Fill a missing Apple URL from the feed; persist if found.

    Network call: run from schedule_listen_link_backfill(), never inline in a
    request thread (except under TESTING).
    """
    if task is None:
        return listen_links_for_task(task)
    if not getattr(task, 'source_apple_url', None) and (task.rss_url or '').strip():
        apple = apple_url_for_feed(
            task.rss_url, task.podcast_name,
            episode_title=task.episode_title,
            audio_url=getattr(task, 'source_audio_url', None),
            timeout=timeout)
        if apple:
            task.source_apple_url = apple
            try:
                db.session.commit()
            except Exception:  # noqa: BLE001
                db.session.rollback()
                app.logger.exception('could not persist listen links for task %s',
                                     getattr(task, 'id', '?'))
    return listen_links_for_task(task)


_listen_backfill_inflight = set()
_listen_backfill_lock = threading.Lock()


def _listen_backfill_worker(task_id):
    try:
        with app.app_context():
            try:
                task = db.session.get(TranscriptionTask, task_id)
                if task is not None:
                    ensure_task_listen_links(task)
            finally:
                db.session.remove()
    except Exception:  # noqa: BLE001
        app.logger.exception('listen-link backfill failed for %s', task_id)
    finally:
        with _listen_backfill_lock:
            _listen_backfill_inflight.discard(task_id)


def schedule_listen_link_backfill(task):
    """Look up a missing Apple link off the request thread (single-flight)."""
    if task is None or getattr(task, 'source_apple_url', None):
        return
    if not (task.rss_url or '').strip():
        return
    task_id = task.id
    with _listen_backfill_lock:
        if task_id in _listen_backfill_inflight:
            return
        _listen_backfill_inflight.add(task_id)
    if app.config.get('TESTING'):
        _listen_backfill_worker(task_id)
        return
    threading.Thread(target=_listen_backfill_worker, args=(task_id,),
                     daemon=True, name=f'listen-links-{task_id[:8]}').start()


def _active_share_for_task(task_id):
    """Non-revoked TranscriptShare for task_id, or None."""
    return TranscriptShare.query.filter_by(
        task_id=task_id, revoked_at=None,
    ).first()


def _share_public_url(token):
    """Absolute public URL for a share token (prefers PUBLIC_BASE_URL)."""
    return public_url('shared_transcript', token=token)


def _share_payload(share):
    if not share or share.revoked_at is not None:
        return {'shared': False, 'url': None, 'token': None}
    return {
        'shared': True,
        'url': _share_public_url(share.token),
        'token': share.token,
    }


def share_partial_meta(task):
    """Display metadata for a free-preview transcript on the share page, or None.

    Reads the canonical ``partial_meta`` column (PR #59) so a partial preview
    is never presented publicly as the full episode.
    """
    meta = task_partial_meta(task)
    if not meta:
        return None
    n, m = partial_minutes_pair(meta)
    return {
        'partial': True,
        'partial_minutes': n,
        'episode_minutes': m,
        'note': partial_transcript_note(meta),
    }


def _readable_share_segments(task):
    """Timestamped lines for the public share page, or [] when unavailable."""
    if not task.segments_json:
        return []
    try:
        segments = json.loads(task.segments_json)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(segments, list):
        return []
    lines = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        text = (seg.get('text') or '').strip()
        if not text:
            continue
        start = seg.get('start')
        try:
            ts = format_timestamp(float(start)) if start is not None else ''
        except (TypeError, ValueError):
            ts = ''
        # format_timestamp is SRT-style (HH:MM:SS,mmm); show a short clock.
        if ts and ',' in ts:
            ts = ts.split(',', 1)[0]
        lines.append({'start': ts, 'text': text})
    return lines


@app.route('/transcription/<task_id>/share', methods=['GET', 'POST'])
@login_required
def transcription_share(task_id):
    """Opt-in public share link for a completed transcript (owner only)."""
    task, err = _task_owned_or_404(task_id)
    if err:
        return err

    if request.method == 'GET':
        return jsonify(_share_payload(_active_share_for_task(task.id)))

    if task.status != 'completed' or not (task.transcript_text or '').strip():
        return jsonify({'error': 'Only completed transcripts can be shared'}), 400

    existing = _active_share_for_task(task.id)
    if existing:
        return jsonify(_share_payload(existing))

    slot = share_create_reserve(current_user.id)
    if slot is None:
        return jsonify({
            'error': 'Too many share links created. Try again later.',
        }), 429

    created = False
    try:
        # Reuse a revoked row for this task (unique task_id) with a new token.
        share = TranscriptShare.query.filter_by(task_id=task.id).first()
        token = mint_share_token()
        now = datetime.now(timezone.utc)
        if share:
            share.token = token
            share.user_id = current_user.id
            share.created_at = now
            share.revoked_at = None
        else:
            share = TranscriptShare(
                token=token,
                task_id=task.id,
                user_id=current_user.id,
                created_at=now,
            )
            db.session.add(share)
        db.session.commit()
        created = True
        # Best-effort Apple link from the feed, off the request thread.
        schedule_listen_link_backfill(task)
        product_analytics.capture(
            'share_link_created',
            current_user.id,
            {'partial': bool(share_partial_meta(task))},
        )
        return jsonify(_share_payload(share))
    finally:
        if not created:
            share_create_release(current_user.id, slot)


@app.route('/transcription/<task_id>/share/revoke', methods=['POST'])
@login_required
def transcription_share_revoke(task_id):
    """Revoke the public share link for a transcript (owner only)."""
    task, err = _task_owned_or_404(task_id)
    if err:
        return err

    share = _active_share_for_task(task.id)
    if not share:
        return jsonify({'shared': False, 'url': None, 'token': None})

    share.revoked_at = datetime.now(timezone.utc)
    db.session.commit()
    product_analytics.capture('share_link_revoked', current_user.id)
    return jsonify({'shared': False, 'url': None, 'token': None})


def _public_share_or_404(token):
    """Active share + completed task for token, or None."""
    token = (token or '').strip()
    if not token or len(token) > 64:
        return None, None
    share = TranscriptShare.query.filter_by(token=token, revoked_at=None).first()
    if not share:
        return None, None
    task = db.session.get(TranscriptionTask, share.task_id)
    if not task or task.status != 'completed' or not (task.transcript_text or '').strip():
        return None, None
    return share, task


def _share_response_headers(resp):
    """Headers for public share responses.

    noindex: shared transcripts are never search results. no-referrer: the
    token is the credential, so it must not leak to the artwork CDN or any
    outbound link. no-store: a revoked link must not keep rendering from a
    browser or proxy cache.
    """
    resp.headers['X-Robots-Tag'] = 'noindex'
    resp.headers['Referrer-Policy'] = 'no-referrer'
    resp.headers['Cache-Control'] = 'no-store'
    return resp


@app.route('/t/<token>')
def shared_transcript(token):
    """Public, unguessable share page for a completed transcript."""
    share, task = _public_share_or_404(token)
    if not share:
        return _share_response_headers(make_response("Not found", 404))

    partial_meta = share_partial_meta(task)
    segments = _readable_share_segments(task)
    # Only stored listen URLs are rendered (no network on this path). A
    # missing Apple link is looked up in the background for the next view.
    # In tests the backfill runs inline, so re-read after scheduling.
    schedule_listen_link_backfill(task)
    listen_links = public_listen_links_for_task(task)
    signup_url = url_for('register', utm_source='share')
    product_analytics.capture(
        'shared_transcript_viewed',
        _analytics_anon_id(),
        {
            'partial': bool(partial_meta),
            'has_timestamps': bool(segments),
            'has_listen_links': bool(listen_links),
            '$process_person_profile': False,
        },
    )
    episode = task.episode_title or 'Episode'
    podcast = task.podcast_name or ''
    og_title = f'{episode} — {podcast}' if podcast else f'{episode} — Podskrift'
    og_description = (
        f'Transcript of {episode}'
        + (f' from {podcast}' if podcast else '')
        + '. Shared via Podskrift.'
    )
    resp = make_response(render_template(
        'shared_transcript.html',
        task=task,
        segments=segments,
        partial_meta=partial_meta,
        listen_links=listen_links,
        signup_url=signup_url,
        download_txt_url=url_for('shared_transcript_download',
                                 token=token, file_type='txt'),
        download_srt_url=(
            url_for('shared_transcript_download', token=token, file_type='srt')
            if task.segments_json else None
        ),
        og_title=og_title,
        og_description=og_description,
    ))
    _share_response_headers(resp)
    return resp


@app.route('/t/<token>/download/<file_type>')
def shared_transcript_download(token, file_type):
    """Download .txt / .srt for a public share (no login)."""
    from io import BytesIO

    share, task = _public_share_or_404(token)
    if not share:
        return "File not found", 404
    if file_type not in ('txt', 'srt'):
        return "Invalid file type", 400
    if file_type == 'srt' and not task.segments_json:
        return "File not found", 404

    safe_title = safe_download_basename(task.episode_title)
    if file_type == 'txt':
        body = task.transcript_text or ''
        partial = share_partial_meta(task)
        if partial:
            body = partial['note'] + '\n\n' + body
        content = body.encode('utf-8')
        resp = send_file(
            BytesIO(content),
            as_attachment=True,
            download_name=f'{safe_title}.txt',
            mimetype='text/plain',
        )
    else:
        srt = _segments_to_srt(
            task.segments_json, task.transcript_text or '',
        )
        if not srt:
            return "File not found", 404
        resp = send_file(
            BytesIO(srt.encode('utf-8')),
            as_attachment=True,
            download_name=f'{safe_title}.srt',
            mimetype='text/srt',
        )
    _share_response_headers(resp)
    return resp


@app.route('/transcription/<task_id>/related-episodes')
@login_required
def related_episodes(task_id):
    """JSON: other recent episodes from the task's RSS feed (async result-page UI).

    Hidden gracefully when the task has no feed URL. Timed so a slow host cannot
    stall the completed-transcript page.
    """
    task, err = _task_owned_or_404(task_id)
    if err:
        return err

    rss_url = (task.rss_url or '').strip()
    podcast_name = task.podcast_name or ''
    # Older search/Spotify starts never stored the feed. Recover it from the
    # public directory by exact show name so related episodes work.
    if not rss_url and podcast_name.strip():
        try:
            shows = _public_shows_named(podcast_name.strip())
        except requests.RequestException:
            shows = []
        for show in shows:
            feed = (show.get('feedUrl') or '').strip()
            if feed and _is_fetchable_url(feed):
                task.rss_url = feed
                db.session.commit()
                rss_url = feed
                break

    if not rss_url:
        return jsonify({
            'has_feed': False,
            'podcast_name': podcast_name,
            'episodes': [],
        })

    episodes, error = get_episodes_from_rss(
        rss_url, timeout=RELATED_EPISODES_TIMEOUT)
    if error or not episodes:
        return jsonify({
            'has_feed': True,
            'podcast_name': podcast_name,
            'episodes': [],
            'error': error or 'No episodes found',
        })

    current_audio = (task.source_audio_url or '').strip()
    current_title = (task.episode_title or '').strip()
    others = []
    for ep in episodes:
        if current_audio and ep.get('audio_url') == current_audio:
            continue
        if not current_audio and current_title and ep.get('title') == current_title:
            continue
        others.append({
            'title': ep.get('title') or 'Episode',
            'published': ep.get('published') or '',
            'audio_url': ep.get('audio_url'),
            'duration_min': ep.get('duration_min'),
            'artwork': ep.get('artwork') or '',
            'podcast_name': ep.get('podcast_name') or podcast_name,
            'index': ep.get('index'),
        })
        if len(others) >= RELATED_EPISODES_LIMIT:
            break

    if not podcast_name and episodes:
        podcast_name = episodes[0].get('podcast_name') or ''

    return jsonify({
        'has_feed': True,
        'podcast_name': podcast_name,
        'episodes': others,
    })


@app.route('/transcription/<task_id>/follow', methods=['GET', 'POST'])
@login_required
def follow_task_podcast(task_id):
    """Former one-click follow endpoint — gone."""
    return ('', 410)


@app.route('/history')
@login_required
def history():
    query = ' '.join(request.args.get('q', '').split())[:TRANSCRIPT_QUERY_MAX]
    if query:
        return render_template('history.html', query=query,
                               matches=search_transcripts(current_user.id, query),
                               transcriptions=[], total_cost=0,
                               cost_per_minute=WHISPER_COST_PER_MINUTE,
                               has_active=False)
    # In-progress first (so MCP/ChatGPT jobs are visible while Whisper runs),
    # then recent failures, then completed — same page Sindre already checks.
    active = active_tasks_for(current_user)
    # active_tasks_for caps at 5 for the job bar; History can show a few more.
    if len(active) >= 5:
        more_active = (TranscriptionTask.query.filter(
            TranscriptionTask.user_id == current_user.id,
            ~TranscriptionTask.status.in_(['completed', *TERMINAL_STATUSES]),
            ~TranscriptionTask.id.in_([t.id for t in active] or ['']),
        ).order_by(TranscriptionTask.started_at.desc()).limit(15).all())
        active = active + [t for t in more_active if not _fail_if_stale(t)]
    failed = (TranscriptionTask.query.filter_by(
        user_id=current_user.id, status='error'
    ).order_by(TranscriptionTask.started_at.desc()).limit(20).all())
    completed = (TranscriptionTask.query.filter_by(
        user_id=current_user.id, status='completed'
    ).order_by(TranscriptionTask.completed_at.desc()).limit(50).all())

    rows = []
    for task in active:
        percent, eta = compute_live_progress(task)
        rows.append({
            'task': task,
            'kind': 'in_progress',
            'progress': percent,
            'eta_seconds': int(eta) if eta is not None else None,
        })
    for task in failed:
        rows.append({
            'task': task,
            'kind': 'failed',
            'progress': None,
            'eta_seconds': None,
            'error_short': short_task_error(task),
        })
    for task in completed:
        rows.append({
            'task': task,
            'kind': 'completed',
            'progress': None,
            'eta_seconds': None,
        })

    total_cost = sum(
        (t.audio_duration / 60) * WHISPER_COST_PER_MINUTE
        for t in completed if t.audio_duration
    )
    return render_template(
        'history.html', transcriptions=rows, query='',
        total_cost=total_cost,
        cost_per_minute=WHISPER_COST_PER_MINUTE,
        has_active=bool(active),
    )


# ---------------------------------------------------------------------------
# Transcript search  (TSK-20441)
# ---------------------------------------------------------------------------

TRANSCRIPT_QUERY_MAX = 200
TRANSCRIPT_SEARCH_LIMIT = 50
SNIPPET_RADIUS = 90


def _snippet(text, match, radius=SNIPPET_RADIUS):
    """(before, hit, after) around a regex match, trimmed to word boundaries.

    Returned in parts so the template can wrap the hit in <mark> while Jinja
    still escapes all three -- transcript text is untrusted.
    """
    start, end = match.span()
    lo, hi = max(0, start - radius), min(len(text), end + radius)
    before, after = text[lo:start], text[end:hi]
    if lo > 0:
        before = '…' + before.split(' ', 1)[-1] if ' ' in before else '…' + before
    if hi < len(text):
        after = (after.rsplit(' ', 1)[0] if ' ' in after else after) + '…'
    # Collapse runs of whitespace but keep the edges: split()/join would glue
    # the words either side of the hit onto it ("detregn over Østlandetog").
    return (re.sub(r'\s+', ' ', before), match.group(0), re.sub(r'\s+', ' ', after))


def search_transcripts(user_id, query, limit=TRANSCRIPT_SEARCH_LIMIT):
    """The user's completed transcripts containing `query`, newest first.

    A scan in Python rather than SQL LIKE: SQLite's LIKE only folds ASCII case,
    so "Østlandet" would miss "østlandet". Only this user's completed rows are
    read, streamed in batches, which is fine at the current per-user scale.
    Words match across any run of whitespace or punctuation, so a phrase hits
    across a line break and across the commas and colons Whisper puts in.
    """
    words = [w for w in re.split(r'\W+', query) if w]
    if not words:
        return []
    pattern = re.compile(r'\W+'.join(map(re.escape, words)), re.IGNORECASE)
    T = TranscriptionTask
    rows = (db.session.query(T.id, T.episode_title, T.podcast_name, T.completed_at,
                             T.started_at, T.transcript_text)
            .filter(T.user_id == user_id, T.status == 'completed')
            .order_by(T.completed_at.desc()).yield_per(20))
    matches = []
    for row in rows:
        text = row.transcript_text or ''
        hit = pattern.search(text)
        if not hit and not pattern.search(row.episode_title or ''):
            continue
        matches.append({
            'id': row.id,
            'episode_title': row.episode_title,
            'podcast_name': row.podcast_name,
            'when': row.completed_at or row.started_at,
            'hits': len(pattern.findall(text)),
            'snippet': _snippet(text, hit) if hit else None,
        })
        if len(matches) >= limit:
            break
    return matches


# ---------------------------------------------------------------------------
# HTTP API auth  (agent CoS key + per-user customer keys)
#
# AGENT_API_KEY remains Sindre/CoS-only (env secret → AGENT_API_USER_ID).
# Customers mint their own key in Settings; we store only a SHA-256 hash.
# Same endpoints; jobs and trial quota belong to the authenticated user.
# ---------------------------------------------------------------------------

AGENT_API_TZ = ZoneInfo('Europe/Oslo')
AGENT_EPISODE_LIMIT = 50
# Cheap guard against a runaway CoS loop starting dozens of Whisper jobs.
AGENT_WRITE_MAX_PER_WINDOW = int(os.getenv('AGENT_WRITE_MAX_PER_WINDOW', '10'))
AGENT_WRITE_WINDOW_SECONDS = int(os.getenv('AGENT_WRITE_WINDOW_SECONDS', '60'))
# Cap simultaneous in-flight agent jobs for the scoped user (pending phases).
AGENT_MAX_IN_FLIGHT = int(os.getenv('AGENT_MAX_IN_FLIGHT', '2'))
# Task statuses that mean Whisper has not finished (or not started) yet.
_AGENT_PENDING_STATUSES = frozenset({
    'pending', 'downloading', 'splitting', 'transcribing',
})
_agent_write_attempts = collections.defaultdict(list)
_agent_write_lock = threading.Lock()

# Customer keys are high-entropy random tokens; SHA-256 is fine for lookup
# (unlike passwords). Prefix makes them greppable and distinct from OpenAI sk-.
CUSTOMER_API_KEY_PREFIX = 'psk_'


def mint_customer_api_key():
    """Fresh plaintext customer API key. Caller shows it once, then hashes."""
    return CUSTOMER_API_KEY_PREFIX + secrets.token_urlsafe(32)


def hash_customer_api_key(plaintext):
    """SHA-256 hex digest of a customer API key (never store plaintext)."""
    return hashlib.sha256(plaintext.encode('utf-8')).hexdigest()


def customer_api_key_prefix(plaintext):
    """Short display prefix for Settings (not enough to authenticate)."""
    return (plaintext or '')[:12]


def _lookup_user_by_api_key(plaintext):
    """User owning this customer key, or None. Revoke clears the hash → None."""
    if not plaintext:
        return None
    digest = hash_customer_api_key(plaintext)
    return User.query.filter_by(api_key_hash=digest).first()


def _agent_configured_key():
    """The shared CoS agent secret, or '' when that path is disabled.

    Read from the environment on every call so tests can monkeypatch and so a
    restart is not required to rotate the key under gunicorn's preload.
    """
    return (os.getenv('AGENT_API_KEY') or '').strip()


def _agent_scope_user_id():
    """user_id agent (CoS) reads/writes are limited to.

    Reads: when unset the agent key can see every account's tasks
    (single-operator box). Writes: required -- enqueue always runs as this
    user so Whisper minutes hit Sindre's trial pool.
    Set AGENT_API_USER_ID in production.
    """
    raw = (os.getenv('AGENT_API_USER_ID') or '').strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _agent_write_user():
    """User row for CoS agent writes, or (None, error_payload, status)."""
    uid = _agent_scope_user_id()
    if uid is None:
        return None, {
            'error': 'AGENT_API_USER_ID is not configured; agent writes need a '
                     'scoped account.',
        }, 403
    user = db.session.get(User, uid)
    if user is None:
        return None, {
            'error': 'AGENT_API_USER_ID does not match any user account.',
        }, 403
    return user, None, None


def _api_write_user():
    """User row for API writes (customer/OAuth owner or CoS agent scope)."""
    kind = getattr(g, 'api_auth_kind', None)
    if kind in ('customer', 'oauth'):
        uid = getattr(g, 'api_user_id', None)
        user = db.session.get(User, uid) if uid is not None else None
        if user is None:
            # Key was revoked or account deleted between auth and write.
            return None, {'error': 'Unauthorized'}, 401
        return user, None, None
    return _agent_write_user()


def _api_write_rate_limit_ok():
    """True if this process still has room for another API write.

    CoS agent shares one bucket; each customer/OAuth user is limited separately
    so one account cannot starve another inside the same gunicorn worker.
    """
    now = time.time()
    kind = getattr(g, 'api_auth_kind', None)
    if kind in ('customer', 'oauth'):
        bucket = f'customer:{getattr(g, "api_user_id", None)}'
    else:
        bucket = 'agent'
    with _agent_write_lock:
        seen = [t for t in _agent_write_attempts.get(bucket, ())
                if now - t < AGENT_WRITE_WINDOW_SECONDS]
        if len(seen) >= AGENT_WRITE_MAX_PER_WINDOW:
            _agent_write_attempts[bucket] = seen
            return False
        seen.append(now)
        _agent_write_attempts[bucket] = seen
        return True


# Back-compat name used by older tests / call sites.
_agent_write_rate_limit_ok = _api_write_rate_limit_ok


def _agent_in_flight_count(user_id):
    """How many of this user's tasks are still in a pending Whisper phase."""
    tasks = (TranscriptionTask.query
             .filter(TranscriptionTask.user_id == user_id)
             .order_by(TranscriptionTask.started_at.desc())
             .limit(50)
             .all())
    n = 0
    for task in tasks:
        if agent_transcript_status(task) == 'pending':
            n += 1
    return n


def _extract_agent_api_key():
    """Bearer token or X-Api-Key header. Empty string if neither was sent."""
    auth = request.headers.get('Authorization', '')
    if auth.lower().startswith('bearer '):
        return auth[7:].strip()
    return (request.headers.get('X-Api-Key') or '').strip()


def require_api_auth(view):
    """401 unless Authorization/X-Api-Key is the CoS agent key or a customer key.

    Sets ``g.api_auth_kind`` to ``'agent'`` or ``'customer'`` and
    ``g.api_user_id`` to the scoped user (always set for customers; for the
    agent key, only when AGENT_API_USER_ID is configured).

    Customer keys work even when AGENT_API_KEY is unset. Missing/wrong key →
    the same 401 either way (fail closed; do not advertise which paths exist).
    Revoking a customer key clears the hash, so the next request is 401.
    """
    @wraps(view)
    def wrapped(*args, **kwargs):
        provided = _extract_agent_api_key()
        if not provided:
            return jsonify({'error': 'Unauthorized'}), 401

        expected = _agent_configured_key()
        # compare_digest raises on length mismatch; customer keys are longer
        # than a typical AGENT_API_KEY, so gate on equal length first.
        agent_ok = (
            bool(expected)
            and len(provided) == len(expected)
            and hmac.compare_digest(provided, expected)
        )
        customer = None if agent_ok else _lookup_user_by_api_key(provided)

        if agent_ok:
            g.api_auth_kind = 'agent'
            g.api_user_id = _agent_scope_user_id()
            return view(*args, **kwargs)
        if customer is not None:
            g.api_auth_kind = 'customer'
            g.api_user_id = customer.id
            return view(*args, **kwargs)
        return jsonify({'error': 'Unauthorized'}), 401
    return wrapped


# Historical name — agent-only era. Same decorator; customer keys also pass.
require_agent_api_key = require_api_auth


def agent_transcript_status(task):
    """Map a TranscriptionTask row to none|pending|ready|failed.

    The agent API talks about episodes/transcripts, not internal job phases.
    `ready` means there is text to return; `pending` means work is still in
    flight; `failed` means it stopped without a usable transcript; `none` is
    reserved for rows that never produced text (e.g. completed empty).
    """
    # "transcribing chunk 2/5" → "transcribing"
    status = (task.status or '').split()[0]
    text = (task.transcript_text or '').strip()
    if status == 'completed':
        return 'ready' if text else 'none'
    if status in _AGENT_PENDING_STATUSES or status.startswith('transcribing'):
        return 'pending'
    if status == 'cancelled':
        # Partial text was billed pro-rata and is downloadable in the UI --
        # treat it as ready so CoS can still pull what exists.
        return 'ready' if text else 'failed'
    if status == 'error':
        return 'failed'
    return 'none'


def _like_contains(value):
    """Escape LIKE wildcards so a publisher filter cannot match everything."""
    return (value.replace('\\', '\\\\')
                 .replace('%', '\\%')
                 .replace('_', '\\_'))


def _parse_agent_date(value):
    """ISO calendar date (YYYY-MM-DD). Returns date or None."""
    if not value:
        return None
    value = value.strip()
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _episode_published_date(published):
    """Best-effort calendar date for episode_published, in Europe/Oslo.

    Feed parsing stores YYYY-MM-DD already; older rows may hold a raw RSS
    string or a timestamp. Anything unparseable returns None so a date filter
    does not invent a match.
    """
    if not published:
        return None
    raw = published.strip()
    if raw.lower().startswith('unknown'):
        return None
    # Fast path: the format _format_published writes.
    if len(raw) >= 10 and raw[4] == '-' and raw[7] == '-':
        try:
            return date.fromisoformat(raw[:10])
        except ValueError:
            pass
    for fmt in ('%a, %d %b %Y %H:%M:%S %z',
                '%a, %d %b %Y %H:%M:%S %Z',
                '%Y-%m-%dT%H:%M:%S%z',
                '%Y-%m-%dT%H:%M:%SZ',
                '%Y-%m-%d %H:%M:%S'):
        try:
            dt = datetime.strptime(raw.replace('Z', '+0000') if fmt.endswith('%z')
                                   and raw.endswith('Z') else raw, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(AGENT_API_TZ).date()
        except ValueError:
            continue
    return None


def _agent_task_query():
    """Base query for API reads, scoped to the authenticated principal.

    Customer keys are always limited to their owner. The CoS agent key is
    limited when AGENT_API_USER_ID is set; otherwise (legacy single-operator)
    it can see every account's tasks.
    """
    q = TranscriptionTask.query
    uid = getattr(g, 'api_user_id', None)
    kind = getattr(g, 'api_auth_kind', None)
    if kind == 'customer':
        # Fail closed: a customer auth without a user id sees nothing.
        if uid is None:
            return q.filter(TranscriptionTask.user_id == -1)
        return q.filter(TranscriptionTask.user_id == uid)
    if uid is not None:
        q = q.filter(TranscriptionTask.user_id == uid)
    return q


def _agent_episode_payload(task):
    published = _episode_published_date(task.episode_published)
    return {
        'id': task.id,
        'title': task.episode_title,
        'publisher': task.podcast_name,
        'published_at': published.isoformat() if published else (
            task.episode_published if task.episode_published
            and not str(task.episode_published).lower().startswith('unknown')
            else None
        ),
        'transcript_status': agent_transcript_status(task),
        'language': task.language,
        'audio_duration_seconds': task.audio_duration,
        'artwork_url': task.artwork_url,
        'rss_url': task.rss_url,
        'task_status': task.status,
        'started_at': task.started_at.isoformat() if task.started_at else None,
        'completed_at': task.completed_at.isoformat() if task.completed_at else None,
        'error_message': task.error_message if task.status == 'error' else None,
    }


def _segments_to_srt(segments_json, fallback_text=''):
    """Build SubRip text from stored Whisper segments. Cheap: no re-encode."""
    if not segments_json:
        return None
    try:
        segments = json.loads(segments_json)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not segments:
        return None
    lines = []
    for i, seg in enumerate(segments, 1):
        lines.append(f"{i}")
        lines.append(
            f"{format_timestamp(seg['start'])} --> {format_timestamp(seg['end'])}"
        )
        lines.append((seg.get('text') or '').strip() or fallback_text)
        lines.append('')
    return '\n'.join(lines)


@app.route('/api/v1/episodes')
@require_agent_api_key
def agent_list_episodes():
    """Search transcribed episodes by publisher/show and calendar date.

    Query params:
      publisher / show  – case-insensitive substring on podcast_name
      date              – YYYY-MM-DD, interpreted in Europe/Oslo against
                          episode_published
      limit             – max rows (default 50, hard cap 100)

    At least one of publisher/show or date is required so a bare GET cannot
    dump the whole library.
    """
    publisher = (request.args.get('publisher') or request.args.get('show') or '').strip()
    date_raw = (request.args.get('date') or '').strip()
    if not publisher and not date_raw:
        return jsonify({
            'error': 'Provide publisher (or show) and/or date=YYYY-MM-DD',
        }), 400

    target_date = None
    if date_raw:
        target_date = _parse_agent_date(date_raw)
        if target_date is None:
            return jsonify({'error': 'date must be ISO YYYY-MM-DD'}), 400

    try:
        limit = min(int(request.args.get('limit', AGENT_EPISODE_LIMIT)), 100)
    except (TypeError, ValueError):
        limit = AGENT_EPISODE_LIMIT
    limit = max(1, limit)

    q = _agent_task_query()
    if publisher:
        # lower()+LIKE: SQLite's bare LIKE only folds ASCII, and ilike is the
        # same under this dialect. Escape %/_ so the filter is literal.
        needle = f'%{_like_contains(publisher.lower())}%'
        q = q.filter(
            sa_func.lower(TranscriptionTask.podcast_name).like(needle, escape='\\')
        )

    # Pull a bounded window newest-first, then apply the Oslo date filter in
    # Python -- episode_published is a free-form string, not a typed column.
    candidates = (q.order_by(TranscriptionTask.started_at.desc())
                  .limit(500).all())
    episodes = []
    for task in candidates:
        if target_date is not None:
            pub = _episode_published_date(task.episode_published)
            if pub != target_date:
                continue
        episodes.append(_agent_episode_payload(task))
        if len(episodes) >= limit:
            break

    return jsonify({
        'episodes': episodes,
        'count': len(episodes),
        'filters': {
            'publisher': publisher or None,
            'date': target_date.isoformat() if target_date else None,
            'timezone': 'Europe/Oslo',
        },
    })


@app.route('/api/v1/episodes/<task_id>')
@require_agent_api_key
def agent_get_episode(task_id):
    """Episode metadata + transcript_status for one transcription task id."""
    task = _agent_task_query().filter(TranscriptionTask.id == task_id).first()
    if not task:
        return jsonify({'error': 'Episode not found'}), 404
    return jsonify(_agent_episode_payload(task))


@app.route('/api/v1/episodes/<task_id>/transcript')
@require_agent_api_key
def agent_get_transcript(task_id):
    """Return transcript text when ready; otherwise transcript_status only.

    Never 500s on a missing/pending transcript -- agents need a stable
    contract. Optional ?format=srt when segments_json is present (cheap).
    """
    task = _agent_task_query().filter(TranscriptionTask.id == task_id).first()
    if not task:
        return jsonify({'error': 'Episode not found', 'transcript_status': 'none'}), 404

    status = agent_transcript_status(task)
    fmt = (request.args.get('format') or 'txt').strip().lower()
    if fmt not in ('txt', 'srt', 'text', 'plain'):
        return jsonify({'error': 'format must be txt or srt',
                        'transcript_status': status}), 400

    payload = {
        'id': task.id,
        'title': task.episode_title,
        'publisher': task.podcast_name,
        'transcript_status': status,
        'format': 'srt' if fmt == 'srt' else 'txt',
    }

    if status != 'ready':
        # Clear status, no body text, no 500 -- CoS can poll or tell Sindre.
        return jsonify(payload), 200

    if fmt == 'srt':
        srt = _segments_to_srt(task.segments_json, task.transcript_text or '')
        if srt is None:
            payload['error'] = 'SRT unavailable (no segment timestamps stored)'
            payload['transcript_status'] = 'ready'
            # Still ready for plain text; surface that rather than pretending
            # SRT failed the whole transcript.
            payload['text'] = None
            return jsonify(payload), 200
        payload['text'] = srt
        return jsonify(payload)

    payload['text'] = task.transcript_text or ''
    raw = request.args.get('raw')
    if raw in ('1', 'true', 'yes'):
        return Response(payload['text'], mimetype='text/plain; charset=utf-8')
    return jsonify(payload)


def _agent_json_body():
    """JSON object from the request, or {} when absent / not JSON."""
    data = request.get_json(silent=True)
    if isinstance(data, dict):
        return data
    return {}


def _agent_request_value(*keys):
    """First non-empty value among JSON body keys, then form, then query args."""
    body = _agent_json_body()
    for key in keys:
        for source in (body, request.form, request.args):
            raw = source.get(key)
            if raw is None:
                continue
            value = str(raw).strip()
            if value:
                return value
    return ''


def _catalog_episode_payload(ep, rss_url):
    """Resolved catalog episode (no TranscriptionTask yet)."""
    published = _episode_published_date(ep.get('published'))
    return {
        'title': ep.get('title'),
        'publisher': ep.get('podcast_name'),
        'published_at': published.isoformat() if published else (
            ep.get('published') if ep.get('published')
            and not str(ep.get('published')).lower().startswith('unknown')
            else None
        ),
        'audio_url': ep.get('audio_url'),
        'rss_url': rss_url,
        'duration_min': ep.get('duration_min'),
        'artwork_url': ep.get('artwork'),
        'transcript_status': 'none',
    }


def _itunes_shows_matching(publisher):
    """iTunes shows with a public feed matching publisher (exact, then substring).

    Same case-insensitive substring spirit as GET /api/v1/episodes; exact
    normalised title wins so «Spårtsklubben» does not lose to a longer name.
    """
    try:
        results = _itunes_search(publisher, 'podcast')
    except requests.RequestException as e:
        raise RuntimeError(f'Podcast directory unreachable: {e}') from e

    want_exact = _normalize_title(publisher)
    want_sub = publisher.casefold()
    exact, partial = [], []
    for item in results:
        feed = item.get('feedUrl')
        name = item.get('collectionName') or ''
        if not feed or not name:
            continue
        if _normalize_title(name) == want_exact:
            exact.append(item)
        elif want_sub in name.casefold():
            partial.append(item)
    return exact + partial


def _episodes_on_date(episodes, target_date):
    """Feed episodes whose Europe/Oslo calendar date matches target_date."""
    hits = []
    for ep in episodes or []:
        pub = _episode_published_date(ep.get('published'))
        if pub == target_date:
            hits.append(ep)
    return hits


def resolve_catalog_episode(publisher=None, target_date=None, url=None):
    """Find a public RSS/iTunes episode without needing a TranscriptionTask.

    Returns ``(catalog_payload, None)`` or ``(None, error_message)``.
    """
    publisher = (publisher or '').strip()
    url = (url or '').strip()

    if not publisher and not url:
        return None, 'Provide publisher (or show) and date=YYYY-MM-DD, and/or url.'

    # --- URL paths: Spotify, Apple show, direct audio, RSS feed ---
    if url:
        kind, spotify_id = parse_spotify_url(url)
        if kind:
            try:
                outcome = resolve_spotify_url(url)
            except requests.RequestException:
                return None, "Couldn't reach the podcast directory. Try again in a moment."
            results = outcome.get('results') or []
            err = outcome.get('error')
            if err and not results:
                return None, err
            if not results:
                return None, 'Episode not found for that Spotify link.'
            hit = results[0]
            if hit.get('type') == 'show':
                if target_date is None:
                    return None, (
                        'That Spotify link is a show. Pass date=YYYY-MM-DD to pick '
                        'an episode, or use an episode link.'
                    )
                feed_url = hit.get('feed_url')
                if not feed_url:
                    return None, err or 'No public RSS feed for that show.'
                episodes, feed_err = get_episodes_from_rss(feed_url)
                if feed_err or not episodes:
                    return None, feed_err or 'No episodes found in that feed.'
                hits = _episodes_on_date(episodes, target_date)
                if not hits:
                    return None, (
                        f'No episode published on {target_date.isoformat()} '
                        f'(Europe/Oslo) in that feed.'
                    )
                return _catalog_episode_payload(hits[0], feed_url), None
            # Episode result from Spotify resolver.
            return {
                'title': hit.get('name'),
                'publisher': hit.get('artist'),
                'published_at': hit.get('released') or None,
                'audio_url': hit.get('audio_url'),
                'rss_url': hit.get('feed_url') or '',
                'duration_min': hit.get('duration_min'),
                'artwork_url': hit.get('artwork'),
                'transcript_status': 'none',
            }, None

        if 'podcasts.apple.com' in url.lower() or '/id' in url:
            rss_url, apple_err = convert_apple_podcasts_url_to_rss(url)
            if not rss_url:
                return None, apple_err or 'Could not resolve Apple Podcasts URL.'
            if target_date is None and not publisher:
                return None, (
                    'Apple Podcasts show URL needs date=YYYY-MM-DD to pick an episode.'
                )
            episodes, feed_err = get_episodes_from_rss(rss_url)
            if feed_err or not episodes:
                return None, feed_err or 'No episodes found in that feed.'
            if target_date is not None:
                hits = _episodes_on_date(episodes, target_date)
                if not hits:
                    return None, (
                        f'No episode published on {target_date.isoformat()} '
                        f'(Europe/Oslo) in that feed.'
                    )
                return _catalog_episode_payload(hits[0], rss_url), None
            return _catalog_episode_payload(episodes[0], rss_url), None

        # Direct audio URL (UI episode-search path).
        path = urlparse(url).path.lower()
        if any(path.endswith(ext) for ext in (
                '.mp3', '.m4a', '.wav', '.aac', '.ogg', '.mp4', '.mpeg')):
            if not _is_fetchable_url(url):
                return None, 'That audio URL cannot be fetched.'
            published = target_date.isoformat() if target_date else None
            return {
                'title': publisher or 'Episode',
                'publisher': publisher or None,
                'published_at': published,
                'audio_url': url,
                'rss_url': '',
                'duration_min': None,
                'artwork_url': None,
                'transcript_status': 'none',
            }, None

        # Treat as RSS feed URL when fetchable.
        if _is_fetchable_url(url):
            if target_date is None:
                return None, 'RSS feed URL needs date=YYYY-MM-DD to pick an episode.'
            episodes, feed_err = get_episodes_from_rss(url)
            if feed_err or not episodes:
                return None, feed_err or 'No episodes found in that feed.'
            hits = _episodes_on_date(episodes, target_date)
            if not hits:
                return None, (
                    f'No episode published on {target_date.isoformat()} '
                    f'(Europe/Oslo) in that feed.'
                )
            return _catalog_episode_payload(hits[0], url), None
        return None, 'URL is not a supported Spotify, Apple, audio, or RSS link.'

    # --- publisher + date via iTunes directory + public RSS ---
    if not publisher:
        return None, 'Provide publisher (or show) with date=YYYY-MM-DD.'
    if target_date is None:
        return None, 'date must be ISO YYYY-MM-DD (Europe/Oslo).'

    try:
        shows = _itunes_shows_matching(publisher)
    except RuntimeError as e:
        return None, str(e)
    if not shows:
        return None, (
            f'No public podcast feed found for "{publisher}". '
            'Spotify-exclusive shows have no RSS Podskrift can fetch.'
        )

    last_feed_err = None
    for show in shows[:5]:
        feed_url = show.get('feedUrl')
        if not feed_url or not _is_fetchable_url(feed_url):
            continue
        episodes, feed_err = get_episodes_from_rss(feed_url)
        if feed_err or not episodes:
            last_feed_err = feed_err
            continue
        hits = _episodes_on_date(episodes, target_date)
        if hits:
            # Prefer the feed's own title; fall back to iTunes collection name.
            ep = dict(hits[0])
            if not ep.get('podcast_name'):
                ep['podcast_name'] = show.get('collectionName')
            return _catalog_episode_payload(ep, feed_url), None

    if last_feed_err:
        return None, last_feed_err
    return None, (
        f'No episode of "{publisher}" published on {target_date.isoformat()} '
        f'(Europe/Oslo).'
    )


def _find_existing_agent_task(user_id, catalog):
    """Reuse a recent ready/pending task for the same audio or show+date.

    Avoids burning trial minutes when CoS retries the same episode.
    """
    publisher = (catalog.get('publisher') or '').strip()
    published_at = catalog.get('published_at')
    target = _parse_agent_date(published_at) if published_at else None

    q = (TranscriptionTask.query
         .filter(TranscriptionTask.user_id == user_id)
         .order_by(TranscriptionTask.started_at.desc())
         .limit(100))
    for task in q.all():
        status = agent_transcript_status(task)
        if status not in ('ready', 'pending'):
            continue
        # Match on audio URL when we have it stored... we don't store audio_url
        # on the task. Match publisher + published date, or title + publisher.
        if publisher and target is not None:
            if (task.podcast_name
                    and publisher.casefold() in (task.podcast_name or '').casefold()
                    and _episode_published_date(task.episode_published) == target):
                return task
        if (catalog.get('title') and task.episode_title
                and _normalize_title(task.episode_title)
                == _normalize_title(catalog['title'])
                and publisher
                and task.podcast_name
                and _normalize_title(task.podcast_name)
                == _normalize_title(publisher)):
            return task
    return None


def _catalog_to_enqueue_meta(catalog):
    return {
        'title': catalog.get('title') or 'Episode',
        'audio_url': catalog.get('audio_url'),
        'podcast_name': catalog.get('publisher'),
        'artwork': catalog.get('artwork_url'),
        'published': catalog.get('published_at'),
        'duration_min': _positive_float_or_none(catalog.get('duration_min')),
    }


@app.route('/api/v1/resolve', methods=['POST', 'GET'])
@require_agent_api_key
def agent_resolve_episode():
    """Resolve a catalog episode by publisher+date and/or URL.

    Does not require an existing TranscriptionTask. Clear 404 when the show
    has no public RSS or no episode on that Europe/Oslo date.
    """
    publisher = _agent_request_value('publisher', 'show')
    date_raw = _agent_request_value('date')
    url = _agent_request_value('url', 'episode_url', 'audio_url')

    target_date = None
    if date_raw:
        target_date = _parse_agent_date(date_raw)
        if target_date is None:
            return jsonify({'error': 'date must be ISO YYYY-MM-DD'}), 400

    catalog, err = resolve_catalog_episode(
        publisher=publisher or None,
        target_date=target_date,
        url=url or None,
    )
    if err:
        # 404 for not-found / no feed; 400 for bad input already returned above.
        status = 400 if err.startswith('Provide ') or 'needs date' in err else 404
        return jsonify({'error': err, 'episode': None}), status
    return jsonify({'episode': catalog})


@app.route('/api/v1/transcriptions', methods=['POST'])
@require_agent_api_key
def agent_start_transcription():
    """Resolve (if needed) and start Whisper for the authenticated API user.

    Body/query: same as /api/v1/resolve (publisher+date and/or url), or the
    fields returned by resolve (audio_url, title, publisher, ...). Reuses the
    UI enqueue path -- same trial pool, no separate agent billing. Customer
    keys enqueue as the key owner; the CoS AGENT_API_KEY enqueues as
    AGENT_API_USER_ID.
    """
    if not _api_write_rate_limit_ok():
        return jsonify({
            'error': 'Too many transcription starts; try again shortly.',
        }), 429

    user, err_payload, err_status = _api_write_user()
    if err_payload:
        return jsonify(err_payload), err_status

    if _agent_in_flight_count(user.id) >= AGENT_MAX_IN_FLIGHT:
        return jsonify({
            'error': (
                f'Already {AGENT_MAX_IN_FLIGHT} transcriptions in flight for '
                'this account. Poll an existing job or wait for one to finish.'
            ),
        }), 429

    publisher = _agent_request_value('publisher', 'show', 'podcast_name')
    date_raw = _agent_request_value('date', 'published', 'published_at')
    url = _agent_request_value('url', 'episode_url', 'audio_url')
    language = _agent_request_value('language')

    # Prefer an already-resolved audio_url from the client when present.
    body = _agent_json_body()
    direct_audio = (body.get('audio_url') or request.form.get('audio_url') or '').strip()
    if direct_audio and _is_fetchable_url(direct_audio) and (
            body.get('title') or body.get('episode_title')
            or request.form.get('title') or request.form.get('episode_title')):
        catalog = {
            'title': (body.get('title') or body.get('episode_title')
                      or request.form.get('title')
                      or request.form.get('episode_title') or 'Episode'),
            'publisher': publisher or body.get('publisher') or body.get('podcast_name'),
            'published_at': (body.get('published_at') or body.get('published')
                             or date_raw or None),
            'audio_url': direct_audio,
            'rss_url': (body.get('rss_url') or request.form.get('rss_url') or ''),
            'duration_min': _positive_float_or_none(
                body.get('duration_min') or request.form.get('duration_min')),
            'artwork_url': (body.get('artwork_url') or body.get('artwork')
                            or request.form.get('artwork_url')
                            or request.form.get('artwork')),
            'transcript_status': 'none',
        }
    else:
        target_date = None
        if date_raw:
            target_date = _parse_agent_date(date_raw)
            if target_date is None and not direct_audio:
                return jsonify({'error': 'date must be ISO YYYY-MM-DD'}), 400
        catalog, resolve_err = resolve_catalog_episode(
            publisher=publisher or None,
            target_date=target_date,
            url=url or None,
        )
        if resolve_err:
            status = 400 if resolve_err.startswith('Provide ') or 'needs date' in resolve_err else 404
            return jsonify({'error': resolve_err}), status

    existing = _find_existing_agent_task(user.id, catalog)
    if existing is not None:
        payload = _agent_episode_payload(existing)
        payload['reused'] = True
        return jsonify(payload)

    meta = _catalog_to_enqueue_meta(catalog)
    result, status = enqueue_transcription(
        user, meta, rss_url=catalog.get('rss_url') or None, language=language,
        source='api')
    if status != 200:
        # Map missing-key to 403 for agents (ops misconfig), keep 402 for trial.
        if status == 400 and 'API key' in (result.get('error') or ''):
            return jsonify(result), 403
        return jsonify(result), status

    task = db.session.get(TranscriptionTask, result['task_id'])
    payload = _agent_episode_payload(task) if task else {
        'id': result['task_id'],
        'transcript_status': 'pending',
    }
    payload['reused'] = False
    return jsonify(payload), 201


def faq_entries():
    """Answered on the page, in the schema and in llms.txt.

    These are the questions people actually put to a search box or an assistant
    -- "hvordan transkribere en podcast" is the query, not "what is Podskrift".

    Built at call time, not as a constant, so the trial length always matches
    NEW_USER_TRIAL_SECONDS (what new accounts get). Existing NULL-limit
    accounts still use TRIAL_MINUTES via trial_status(); marketing copy
    advertises the new-signup grant.
    """
    minutes = advertised_trial_minutes() or 0
    hourly = f'${openai_whisper_cost_usd(60):.2f}'
    cost_90 = f'${openai_whisper_cost_usd(90):.2f}'
    # trial_available() is the predicate the code actually enforces. Copy that
    # promises free minutes while the kill switch is on is a promise the app
    # then refuses at /start_transcription.
    if trial_available() and minutes > 0:
        hours_bit = (f' (about {minutes // 60} hours)' if minutes >= 120 else '')
        pack_bit = (
            f' Or buy a one-time pack: ${CREDIT_PACK_AMOUNT_CENTS / 100:.0f} for '
            f'{CREDIT_PACK_MINUTES} minutes ({CREDIT_PACK_MINUTES // 60} hours), '
            f'VAT included, paid via Stripe — no subscription.'
            if stripe_checkout_enabled() else ''
        )
        free = (f'New accounts get {minutes} minutes of audio free'
                f'{hours_bit}. Longer episodes still start free — you get the '
                f'first {minutes} minutes as a preview, then buy minutes or add '
                f'your own OpenAI API key to finish (a 90-minute episode costs '
                f'about {cost_90} at OpenAI\'s rate). Once the trial is used up, '
                f'the same options apply.{pack_bit}')
        need_key = ('Not to start. The free trial runs on ours, including a free '
                    'preview of the first stretch of a longer episode. Add your own '
                    'key when the trial runs out and there is no limit beyond what '
                    'you spend at OpenAI.')
    else:
        pack_bit = (
            f' Or buy a one-time pack: ${CREDIT_PACK_AMOUNT_CENTS / 100:.0f} for '
            f'{CREDIT_PACK_MINUTES} minutes ({CREDIT_PACK_MINUTES // 60} hours), '
            f'VAT included — no subscription.'
            if stripe_checkout_enabled() else ''
        )
        free = ('Podskrift itself is free. You add your own OpenAI API key and pay OpenAI '
                f'directly at their rate -- about {hourly} per hour of audio. There is no '
                f'subscription.{pack_bit}')
        need_key = ('Yes. Add it in Settings; it is stored on your account and used only '
                    'for your own transcriptions.')
    return [
        ('How do I transcribe a podcast episode to text?',
         'Paste a Spotify episode link, or search for the podcast or episode by name. '
         'Podskrift downloads the audio and transcribes it with OpenAI Whisper. You get '
         'the full text plus an .srt subtitle file. No file to upload and no feed URL to '
         'find first.'),
        ('Hvordan transkriberer jeg en norsk podcast til tekst?',
         'Lim inn en Spotify-lenke, eller søk opp podkasten eller episoden på navn. '
         'Podskrift laster ned lyden og transkriberer den med OpenAI Whisper. Du får '
         'hele teksten og en .srt-fil med teksting. Velg norsk i språkvelgeren, så '
         'slipper du at den gjetter feil.'),
        ('Which languages does it handle well?',
         f'{len(LANGUAGE_ENGLISH_NAMES)} languages, from English, Spanish and Mandarin to '
         'Norwegian, Ukrainian and Vietnamese. You can name the language rather than relying '
         'on auto-detect, which matters on short or accented audio: Whisper treats the '
         'choice as a constraint rather than a hint.'),
        ('Is it free?', free),
        ('Do I need an OpenAI API key?', need_key),
        ('What file formats do I get?',
         'Plain text (.txt) and SubRip subtitles (.srt) with timestamps.'),
        ('Can I transcribe a podcast that is not in the search index?',
         'Yes. Paste the RSS feed URL instead and pick the episode from the feed.'),
        ('Is there an HTTP API?',
         'Yes. Create a key in Settings (psk_…) and call the API to resolve an episode, '
         'start a transcription, then fetch the transcript — Bearer or X-Api-Key. '
         + (f'Same {minutes} minutes free trial as the web UI. '
            if minutes else '')
         + 'Curl examples and status codes: /docs/api.'),
    ]


@app.context_processor
def inject_language_count():
    """One number for every surface that quotes it. It was hardcoded in three
    meta tags beside a comment claiming the derived form existed so they could
    not drift."""
    return {
        'language_count': len(LANGUAGE_ENGLISH_NAMES),
        'display_language': display_language,
    }


@app.context_processor
def inject_trial_badge():
    """Remaining free/paid minutes for the needs-own-key badge on episode rows."""
    nav = _nav_credits_context()
    return {
        'trial_badge_remaining_min': trial_badge_remaining_minutes(),
        'trial_badge_paid_min': trial_badge_paid_minutes(),
        'trial_max_episode_min': (
            TRIAL_MAX_EPISODE_SECONDS // 60 if TRIAL_MAX_EPISODE_SECONDS else None
        ),
        'whisper_cost_per_minute': WHISPER_COST_PER_MINUTE,
        'openai_hourly_cost': f'{openai_whisper_cost_usd(60):.2f}',
        'openai_90min_cost': f'{openai_whisper_cost_usd(90):.2f}',
        'stripe_buy_enabled': stripe_checkout_enabled(),
        'credit_pack_label': CREDIT_PACK_LABEL,
        'credit_pack_subline': CREDIT_PACK_SUBLINE,
        'credit_pack_payment_hint': CREDIT_PACK_PAYMENT_HINT,
        'credit_pack_minutes': CREDIT_PACK_MINUTES,
        'credit_pack_price_usd': f'{CREDIT_PACK_AMOUNT_CENTS / 100:.0f}',
        'nav_minutes_left': nav['nav_minutes_left'],
        'nav_daily_exhausted': nav['nav_daily_exhausted'],
        'nav_show_buy': nav['nav_show_buy'],
        'trial_variant': (
            user_trial_variant(current_user)
            if current_user.is_authenticated else None
        ),
        'csrf_token': generate_csrf_token,
    }


@app.context_processor
def inject_posthog():
    """Expose PostHog public config to templates. Empty key → client SDK off."""
    key = product_analytics.posthog_key()
    return {
        'posthog_key': key,
        'posthog_host': product_analytics.posthog_host() if key else '',
        'posthog_user_id': (
            str(current_user.id)
            if getattr(current_user, 'is_authenticated', False)
            else ''
        ),
        # Admin blueprint overrides to True; default False so Jinja `not`
        # is unambiguous outside /admin.
        'is_admin_page': False,
        # Nav "Admin" link — only for ADMIN_EMAILS allowlisted sessions.
        'show_admin_nav': is_admin_user(),
        'public_base_url': PUBLIC_BASE_URL,
        'task_source_via_label': task_source_via_label,
    }


@app.context_processor
def inject_changelog_popup():
    """Latest What's new ids for the dismissible returning-visitor popup."""
    entries = load_changelog_entries()
    # Cap the payload: the popup only shows a few newest items anyway.
    preview = [
        {'id': e['id'], 'date': e['date'], 'title': e['title'],
         'summary': e['summary']}
        for e in entries[:8]
    ]
    return {
        'changelog_latest_id': entries[0]['id'] if entries else '',
        'changelog_preview': preview,
    }


def _structured_data():
    """JSON-LD for the home page.

    WebApplication rather than Organization: nobody asks an assistant "what is
    Podskrift". They ask how to transcribe a Norwegian podcast, and the answer
    is a tool. FAQPage carries the same answers the page shows, so what gets
    quoted is what a visitor actually reads.
    """
    import json as _json
    home = public_url('index')
    data = {
        '@context': 'https://schema.org',
        '@graph': [
            {
                '@type': 'WebApplication',
                '@id': home + '#app',
                'name': 'Podskrift',
                'url': home,
                'applicationCategory': 'MultimediaApplication',
                'operatingSystem': 'Any (web browser)',
                'description': (
                    f'Transcribes podcast episodes to text using OpenAI Whisper, in '
                    f'{len(LANGUAGE_ENGLISH_NAMES)} languages. Search any podcast or '
                    'episode by name, pick the episode, and get plain text and timestamped '
                    'subtitles. Name the language rather than relying on auto-detect, which '
                    'matters on short or accented audio.'
                ),
                # From the ordered list, not the set: iterating a set gave two
                # gunicorn workers two different JSON-LD bodies for one URL.
                'inLanguage': [code for code, _ in SUPPORTED_LANGUAGES if code],
                'featureList': [
                    'Search podcasts and individual episodes by name',
                    'Transcribe to plain text (.txt)',
                    'Transcribe to SubRip subtitles (.srt) with timestamps',
                    'Choose the spoken language or auto-detect',
                    'Transcription continues after you leave the page',
                ],
                'offers': {
                    '@type': 'Offer',
                    'price': '0',
                    'priceCurrency': 'USD',
                    'description': (
                        (f'{advertised_trial_minutes()} minutes of audio free on signup. '
                         'After that, ' if advertised_trial_minutes() else '')
                        + (
                            f'buy a one-time pack (${CREDIT_PACK_AMOUNT_CENTS / 100:.0f} for '
                            f'{CREDIT_PACK_MINUTES} minutes, VAT included) via Stripe, or '
                            if stripe_checkout_enabled() else ''
                        )
                        + 'bring your own OpenAI API key and pay OpenAI directly at their '
                          'rate. No subscription.'
                    ),
                },
                'provider': {
                    '@type': 'Organization',
                    'name': 'Nettsmed',
                    'url': 'https://nettsmed.no',
                },
            },
            {
                '@type': 'FAQPage',
                '@id': home + '#faq',
                'mainEntity': [
                    {
                        '@type': 'Question',
                        'name': question,
                        'acceptedAnswer': {'@type': 'Answer', 'text': answer},
                    }
                    for question, answer in faq_entries()
                ],
            },
        ],
    }
    # json.dumps does not escape '<', so the first dynamic value to reach this
    # -- a podcast title from RSS, say -- would break out of the <script> block.
    # Everything here is a constant today; this costs nothing and stops that.
    return (_json.dumps(data, ensure_ascii=False, indent=2)
            .replace('<', '\\u003c').replace('>', '\\u003e'))


def _show_page_structured_data(show, faq, page_url):
    """PodcastSeries + FAQPage JSON-LD for a show landing page."""
    import json as _json
    graph = [
        {
            '@type': 'PodcastSeries',
            '@id': page_url + '#series',
            'name': show['name'],
            'url': page_url,
            'description': (
                show.get('description')
                or f'Transcribe episodes of {show["name"]} to text with Podskrift.'
            ),
        },
        {
            '@type': 'FAQPage',
            '@id': page_url + '#faq',
            'mainEntity': [
                {
                    '@type': 'Question',
                    'name': question,
                    'acceptedAnswer': {'@type': 'Answer', 'text': answer},
                }
                for question, answer in faq
            ],
        },
    ]
    if show.get('author'):
        graph[0]['author'] = {'@type': 'Person', 'name': show['author']}
    if show.get('artwork'):
        graph[0]['image'] = show['artwork']
    # The seed's `language` is often the Apple storefront ('us'), which is not
    # a language. Only emit codes the app itself knows as languages.
    lang = (show.get('language') or '').strip()
    if lang and lang.split('-')[0].lower() in LANGUAGE_ENGLISH_NAMES:
        graph[0]['inLanguage'] = lang
    graph.append({
        '@type': 'BreadcrumbList',
        '@id': page_url + '#breadcrumb',
        'itemListElement': [
            {'@type': 'ListItem', 'position': 1, 'name': 'Podskrift',
             'item': public_url('index')},
            {'@type': 'ListItem', 'position': 2, 'name': 'Podcasts',
             'item': public_url('podcasts_index')},
            {'@type': 'ListItem', 'position': 3, 'name': show['name'],
             'item': page_url},
        ],
    })
    data = {'@context': 'https://schema.org', '@graph': graph}
    return (_json.dumps(data, ensure_ascii=False, indent=2)
            .replace('<', '\\u003c').replace('>', '\\u003e'))


@app.route('/podcasts')
def podcasts_index():
    """Index of curated + community show landing pages, grouped by letter."""
    shows = show_pages_mod.all_shows(db.session, TranscriptionTask)
    curated_count = sum(1 for s in shows if s.get('source') == 'curated')
    community_count = len(shows) - curated_count
    return render_template(
        'podcasts_index.html',
        letter_groups=show_pages_mod.group_shows_by_letter(shows),
        show_count=len(shows),
        curated_count=curated_count,
        community_count=community_count,
    )


@app.route('/podcasts/<slug>')
def podcast_show(slug):
    """Per-show landing page: description, recent episodes, FAQ. No transcript text."""
    show = show_pages_mod.find_show(slug, db.session, TranscriptionTask)
    if not show:
        abort(404)
    # Community feeds still go through the SSRF gate before we fetch.
    if not _is_fetchable_url(show['feed_url']):
        episodes, feed_meta = [], {'from_cache': False, 'stale': False, 'error': 'feed_blocked'}
    else:
        episodes, feed_meta = show_pages_mod.fetch_show_episodes(
            show,
            get_episodes_from_rss=get_episodes_from_rss,
            is_fetchable_url=_is_fetchable_url,
        )
    trial_minutes = advertised_trial_minutes() or 0
    faq = show_pages_mod.show_faq_entries(
        show['name'],
        trial_minutes=trial_minutes,
        credit_pack_price=f'{CREDIT_PACK_AMOUNT_CENTS / 100:.0f}',
        credit_pack_minutes=CREDIT_PACK_MINUTES,
        language_count=len(LANGUAGE_ENGLISH_NAMES),
    )
    page_url = public_url('podcast_show', slug=show['slug'])
    # Analytics: coarse referrer bucket only (no full URL / PII).
    distinct = (
        str(current_user.id)
        if getattr(current_user, 'is_authenticated', False)
        else _analytics_anon_id()
    )
    product_analytics.capture(
        'show_page_viewed',
        distinct,
        {
            'show_slug': show['slug'],
            'referrer_source': show_pages_mod.referrer_source(
                request.referrer or '',
                request.args.get('utm_source', ''),
            ),
            'show_source': show.get('source') or 'curated',
            '$process_person_profile': False,
        },
    )
    return render_template(
        'podcast_show.html',
        show=show,
        episodes=episodes,
        feed_meta=feed_meta,
        faq=faq,
        trial_minutes=trial_minutes,
        structured_data=_show_page_structured_data(show, faq, page_url),
    )


CONTENT_SIGNAL = 'Content-Signal: search=yes, ai-input=yes, ai-train=yes'


@app.route('/robots.txt')
def robots_txt():
    """Explicit crawler policy.

    There was no robots.txt at all, which leaves every crawler guessing. The
    assistant referrals are a real channel here -- roughly 25 visits a month
    arrive from ChatGPT with nothing on the site written for them -- so the
    bots behind that channel are allowed by name rather than by omission.
    """
    disallow = ['Disallow: ' + path for path in (
        '/settings', '/history', '/transcription/', '/download/',
        '/api/', '/oauth/', '/status/', '/active-jobs', '/cancel/', '/t/', '/admin',
    )]
    lines = [
        '# Podskrift -- podcast transcription',
        '# Full policy and a plain-language summary of the site: /llms.txt',
        '',
        'User-agent: *',
        'Allow: /',
        # Content Signals (contentsignals.org): the explicit statement of what
        # the Allow lines already imply -- search, AI answers and AI training
        # are all welcome on the public pages.
        CONTENT_SIGNAL,
        # No blank lines inside a group: some parsers end the group there.
        '# Nothing here is useful without a session, and some of it is personal.',
    ] + disallow + [
        '',
        '# Assistants that send real traffic, allowed explicitly.',
    ]
    for agent in ('GPTBot', 'OAI-SearchBot', 'ChatGPT-User', 'ClaudeBot', 'Claude-Web',
                  'anthropic-ai', 'PerplexityBot', 'Perplexity-User', 'Google-Extended',
                  'Applebot-Extended', 'CCBot'):
        # RFC 9309: a crawler obeys ONLY its most specific matching group and
        # ignores `User-agent: *` entirely. A named group containing just
        # `Allow: /` therefore told exactly the bots this file exists for that
        # /history and /download/ were fair game -- strictly worse than not
        # naming them. Every group repeats the rules.
        lines += [f'User-agent: {agent}', 'Allow: /', CONTENT_SIGNAL] + disallow + ['']
    lines.append(f'Sitemap: {public_url('sitemap_xml')}')
    return Response('\n'.join(lines) + '\n', mimetype='text/plain')


@app.route('/llms.txt')
def llms_txt():
    """A plain-Markdown description of the site for language models.

    The homepage is a search box: it says almost nothing about what the tool
    does, which languages it is good at, or what it costs. An assistant asked
    "how do I transcribe a Norwegian podcast" has to infer all of that. This
    states it.
    """
    languages = ', '.join(
        f'{LANGUAGE_ENGLISH_NAMES[code]} ({native})'
        if LANGUAGE_ENGLISH_NAMES.get(code, native) != native else native
        for code, native in SUPPORTED_LANGUAGES if code)
    faq = '\n\n'.join(f'**{q}**\n\n{a}' for q, a in faq_entries())
    grant = advertised_trial_minutes()
    signup_blurb = (f'free account, {grant} trial minutes'
                    if grant else 'free account, bring your own OpenAI key')
    cost = (f'New accounts get {grant} minutes of audio free on '
            "Podskrift's own OpenAI key.\nAfter that you"
            if grant else 'You')
    pack = (
        f'\nOr buy a one-time pack: ${CREDIT_PACK_AMOUNT_CENTS / 100:.0f} for '
        f'{CREDIT_PACK_MINUTES} minutes ({CREDIT_PACK_MINUTES // 60} hours), VAT '
        f'included, paid via Stripe — no subscription.'
        if stripe_checkout_enabled() else ''
    )
    lang_count = len(LANGUAGE_ENGLISH_NAMES)
    body = f"""# Podskrift

> Transcribes podcast episodes to text using OpenAI Whisper, in {lang_count}
> languages. Paste a Spotify, Apple Podcasts, or RSS link — or search by show
> or episode name. No file upload required.

Podskrift (https://podskrift.com) turns podcast audio into private text.
You paste a Spotify episode link, an Apple Podcasts link, or an RSS feed URL,
or search by show/episode name. Podskrift downloads the audio and returns the
full transcript plus timestamped SubRip subtitles (.srt). Transcripts are
private to your account; public show pages list episodes but never publish
transcript text.

Made by Nettsmed (Fjellestad AS), Kristiansand, Norway.

## What it does
- Search podcast catalogues by show name or by individual episode title
- Paste Spotify, Apple Podcasts, or RSS links (and direct audio URLs)
- Transcribe an episode to plain text (.txt) and SubRip subtitles (.srt)
- Pick the spoken language explicitly, or let Whisper detect it
- Follow progress live; the job keeps running if you close the page
- Per-show landing pages at /podcasts for “transcript of [show]” queries

## Languages
{languages}

{lang_count} languages in the picker (Whisper’s commonly used set). Auto-detect
is the default, but naming the language beats it on short or accented audio —
Whisper takes the choice as a constraint rather than a hint.

## What it costs
{cost} add your own OpenAI API key and pay OpenAI directly -- roughly
USD {60 * WHISPER_COST_PER_MINUTE:.2f} per hour of audio. There is no subscription and no per-seat pricing.{pack}

## Pages
- [Home]({public_url('index')}): search, paste Spotify/Apple/RSS, pick an episode, transcribe
- [Podcasts]({public_url('podcasts_index')}): show landing pages for popular podcasts
- [Pricing]({public_url('pricing')}): free trial, credit pack, or bring your own key
- [Use in ChatGPT & Claude]({public_url('ai_landing')}): connect Podskrift MCP (`/mcp`) in ChatGPT, Claude, Cursor or Claude Code; copyable prompts for your library, marketing, research and routines
- [Guides]({public_url('guides_index')}): setup walkthroughs for ChatGPT, Claude, and the long-form overview
- [Connect Podskrift to ChatGPT]({public_url('guide_chatgpt')}): Plugins → custom MCP server → OAuth
- [Connect Podskrift to Claude]({public_url('guide_claude')}): Customize → Connectors → custom connector
- [Guide: podcast transcripts in ChatGPT and Claude]({public_url('guide_ai_transcripts')}): step-by-step connector setup, example asks, and what it costs
- [Contact]({public_url('contact')}): hello@podskrift.com — we reply fast
- [What's new]({public_url('whats_new')}): dated feature list, newest first (build in public)
- [API docs]({public_url('api_docs')}): customer HTTP API (resolve → transcribe → transcript)
- [How to find an RSS feed]({public_url('rss_help')}): for podcasts outside the search index
- [Sign up]({public_url('register')}): {signup_blurb}

## For agents
- [API docs as Markdown]({public_url('site_standards.api_docs_markdown')}): the same reference, no HTML (or send `Accept: text/markdown` to /docs/api; on / it returns this file)
- [Agent skill index]({public_url('site_standards.agent_skills_index')}): a SKILL.md for getting transcripts through the API
- [API catalog]({public_url('site_standards.api_catalog')}): RFC 9727 linkset
- [What's new feed]({public_url('site_standards.whats_new_feed')}): RSS of new features

## Frequently asked

{faq}

**Is there a podcast transcript API / get-transcript endpoint?**

Yes. Create a key in Settings (`psk_…`) and call the HTTP API: resolve → start transcription → get transcript. Docs: https://podskrift.com/docs/api

## Contact
Nettsmed -- https://nettsmed.no
"""
    return Response(body, mimetype='text/plain')


@app.route('/sitemap.xml')
def sitemap_xml():
    """Public pages worth indexing, including curated show landings."""
    from xml.sax.saxutils import escape
    pages = [public_url('index'),
             public_url('podcasts_index'),
             public_url('pricing'),
             public_url('ai_landing'),
             public_url('guides_index'),
             public_url('guide_chatgpt'),
             public_url('guide_claude'),
             public_url('guide_ai_transcripts'),
             public_url('contact'),
             public_url('whats_new'),
             public_url('api_docs'),
             public_url('rss_help'),
             public_url('register')]
    # Curated show pages only — community slugs can churn with the DB.
    for show in show_pages_mod.load_curated_shows():
        pages.append(public_url('podcast_show', slug=show['slug']))
    # No lastmod: it was emitting today's date on every fetch, which claims all
    # pages change daily. That is a discount signal, not a freshness one.
    urls = '\n'.join(f'  <url><loc>{escape(u)}</loc></url>' for u in pages)
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
           f'{urls}\n</urlset>\n')
    return Response(xml, mimetype='application/xml')


def _mcp_connector_url():
    base = PUBLIC_BASE_URL or request.url_root.rstrip('/')
    return base + '/mcp'


def ai_ask_prompt_groups():
    """Copyable example prompts for /ai and the ChatGPT/Claude guide.

    Honest framing: Podskrift returns transcripts (and lists your library);
    the chat app writes the summary, post, or document. Keep prompts short.
    """
    return [
        {
            'id': 'library',
            'title': 'Your library',
            'blurb': 'Browse what you’ve already transcribed — free to fetch again.',
            'prompts': [
                'Find my latest transcript of Hard Fork and turn it into a polished document with key points and quotes',
                'List everything I transcribed this week',
                'Search my transcripts for what was said about pricing',
            ],
        },
        {
            'id': 'marketing',
            'title': 'Content & marketing',
            'blurb': 'Podskrift supplies the transcript; your AI writes the piece.',
            'prompts': [
                'Turn this episode into a blog post',
                '5 LinkedIn posts from my latest Lex Fridman transcript',
                'Pull the 10 best quotes with timestamps',
                'Extract the sales arguments and objections for our product from this sales podcast',
            ],
        },
        {
            'id': 'research',
            'title': 'Research & learning',
            'blurb': 'Compare episodes or build notes from the transcript text.',
            'prompts': [
                'Compare what three episodes said about AI regulation',
                'Make study notes and flashcards from this episode',
            ],
        },
        {
            'id': 'routines',
            'title': 'Routines',
            'blurb': (
                'Use ChatGPT scheduled tasks or Claude schedules. '
                'New episodes use your Podskrift minutes; repeats of the same episode do not.'
            ),
            'prompts': [
                'Every Monday, get the newest episode of [show] and email me a summary',
            ],
        },
    ]


def ai_faq_entries():
    """FAQ shown on /ai and mirrored in that page's FAQPage JSON-LD."""
    return [
        (
            'What is MCP?',
            'MCP (Model Context Protocol) lets an AI app call tools on a remote '
            'server. Podskrift’s connector at /mcp can look up shows, start a '
            'transcription, and return the transcript so ChatGPT, Claude or '
            'Cursor can answer from the episode text.',
        ),
        (
            'What does it cost?',
            'Same as the website: your free trial minutes, a credit pack, or '
            'your own OpenAI key. Episodes you\'ve already transcribed cost '
            'nothing to fetch again on your account.',
        ),
        (
            'Is my data private?',
            'Transcripts stay on your Podskrift account. The AI app only sees '
            'what its tools return for your signed-in session. Revoke access '
            'anytime under Settings → Connected apps.',
        ),
        (
            'Which apps work?',
            'ChatGPT (web; plan-dependent — full MCP beta for Business/'
            'Enterprise/Edu, Pro read/fetch; not Free/Go), Claude.ai and '
            'Desktop (Free one connector, Pro, Max, Team, Enterprise), '
            'Cursor, VS Code, and Claude Code.',
        ),
    ]


def guide_ai_faq_entries():
    """Exactly the five FAQ pairs on the ChatGPT/Claude guide (and its JSON-LD)."""
    pack = (
        f'buy a ${CREDIT_PACK_AMOUNT_CENTS / 100:.0f} pack of '
        f'{CREDIT_PACK_MINUTES} minutes'
        if stripe_checkout_enabled()
        else 'buy more minutes'
    )
    return [
        (
            'Can ChatGPT or Claude summarise a podcast episode?',
            'Not from the audio alone. They need a transcript. With Podskrift '
            'connected, they can find the episode, get the transcript and '
            'summarise it in the same chat.',
        ),
        (
            'Do I need an API key?',
            'Not for ChatGPT or Claude. You log in to Podskrift when you '
            'connect. Cursor, VS Code and Claude Code can use either a login '
            'or an API key from Settings.',
        ),
        (
            'Which ChatGPT plans can add the connector?',
            'It depends on your plan and workspace. OpenAI lists full MCP '
            'support as a beta for Business, Enterprise and Edu, and '
            'read/fetch access for Pro. Free and Go don\'t have plugin '
            'extensions. If Add custom MCP server doesn\'t appear under '
            'Plugins, your account can\'t add one yet.',
        ),
        (
            'What happens when my minutes run out?',
            f'Podskrift won\'t start the job. Your AI gets the cost and a '
            f'link to {pack}, or you can add your own OpenAI key in Settings.',
        ),
        (
            'Can it transcribe Spotify-exclusive podcasts?',
            'No. Podskrift needs a public RSS feed, and Spotify exclusives '
            'don\'t have one. Most shows on Spotify are also published '
            'publicly, and those work.',
        ),
    ]


def _faq_page_structured_data(page_url, page_name, page_description, faq_pairs,
                              howto=None, prompt_groups=None):
    """WebPage + FAQPage JSON-LD (optional HowTo / prompt ItemList).

    FAQ answers must match the visible copy. Optional ``prompt_groups``
    (from ``ai_ask_prompt_groups``) add an ItemList of example asks so
    assistants can surface concrete prompts.
    """
    import json as _json
    graph = [
        {
            '@type': 'WebPage',
            '@id': page_url + '#page',
            'name': page_name,
            'url': page_url,
            'description': page_description,
            'isPartOf': {
                '@type': 'WebSite',
                'name': 'Podskrift',
                'url': public_url('index'),
            },
        },
        {
            '@type': 'FAQPage',
            '@id': page_url + '#faq',
            'mainEntity': [
                {
                    '@type': 'Question',
                    'name': question,
                    'acceptedAnswer': {'@type': 'Answer', 'text': answer},
                }
                for question, answer in faq_pairs
            ],
        },
    ]
    if howto:
        howto_node = {
            '@type': 'HowTo',
            '@id': page_url + '#howto',
            'name': howto['name'],
            'description': howto.get('description') or page_description,
            'step': [
                {
                    '@type': 'HowToStep',
                    'position': i,
                    'name': step['name'],
                    'text': step['text'],
                    **({'image': step['image']} if step.get('image') else {}),
                }
                for i, step in enumerate(howto['steps'], start=1)
            ],
        }
        graph.insert(1, howto_node)
    if prompt_groups:
        items = []
        pos = 1
        for group in prompt_groups:
            for prompt in group.get('prompts') or ():
                items.append({
                    '@type': 'ListItem',
                    'position': pos,
                    'name': prompt,
                })
                pos += 1
        if items:
            graph.append({
                '@type': 'ItemList',
                '@id': page_url + '#ask-prompts',
                'name': 'What you can ask',
                'description': (
                    'Example prompts for ChatGPT or Claude with Podskrift. '
                    'Podskrift returns transcripts; the AI writes summaries '
                    'and documents.'
                ),
                'numberOfItems': len(items),
                'itemListElement': items,
            })
    data = {
        '@context': 'https://schema.org',
        '@graph': graph,
    }
    return (_json.dumps(data, ensure_ascii=False, indent=2)
            .replace('<', '\\u003c').replace('>', '\\u003e'))


_GUIDE_EXAMPLE_PROMPTS = (
    'Summarise the latest episode of Hard Fork',
    'Get the last three episodes of Lenny\'s Podcast and list every book they mention',
    'Find the exact sentence where the guest talks about pricing',
    'Transcribe this episode and translate the summary into Norwegian',
)


def _guide_shot_stems(subdir):
    """Return {stem: True} for PNG/WebP pairs under static/guides/<subdir>/."""
    root = os.path.join(app.static_folder, 'guides', subdir)
    found = {}
    if not os.path.isdir(root):
        return found
    for name in os.listdir(root):
        if name.endswith('.webp') or name.endswith('.png'):
            stem, _ = os.path.splitext(name)
            found[stem] = True
    return found


def _guide_static_url(subdir, stem):
    """Absolute URL for a guide screenshot (webp preferred for JSON-LD)."""
    folder = os.path.join(app.static_folder, 'guides', subdir)
    for ext in ('.webp', '.png'):
        if os.path.isfile(os.path.join(folder, stem + ext)):
            return public_url('static', filename=f'guides/{subdir}/{stem}{ext}')
    return None


def guide_chatgpt_faq_entries():
    """FAQ on /guides/chatgpt (mirrored in FAQPage JSON-LD)."""
    return [
        (
            'Which ChatGPT plans can add Podskrift?',
            'Full MCP is a beta for Business, Enterprise and Edu. Pro can '
            'connect with read/fetch tools. Free and Go do not have plugin '
            'extensions. Sources: OpenAI Help on developer mode and MCP apps, '
            'and the Connect an MCP server docs.',
        ),
        (
            'Do I need an API key?',
            'No. You sign in to Podskrift with OAuth when you create the '
            'plugin. ChatGPT never sees your OpenAI key.',
        ),
        (
            'What does it cost?',
            'Uses your Podskrift minutes. Episodes you\'ve already transcribed '
            'are free to fetch again. ChatGPT\'s own reply is part of your '
            'ChatGPT plan.',
        ),
        (
            'How do I disconnect?',
            'On Podskrift go to Settings → Connected apps and revoke access. '
            'You can also remove the plugin under ChatGPT Customize → Plugins.',
        ),
    ]


def guide_claude_faq_entries():
    """FAQ on /guides/claude (mirrored in FAQPage JSON-LD)."""
    return [
        (
            'Which Claude plans work?',
            'Free (one custom connector), Pro, Max, Team and Enterprise, plus '
            'Claude Desktop on the same account. On Team/Enterprise an Owner '
            'adds the connector under Organization settings → Connectors first. '
            'Source: Anthropic\'s custom connectors help article.',
        ),
        (
            'Do I need an API key?',
            'No. You sign in to Podskrift with OAuth when you Connect. Claude '
            'never sees your OpenAI key.',
        ),
        (
            'What does it cost?',
            'Uses your Podskrift minutes. Episodes you\'ve already transcribed '
            'are free to fetch again. Claude\'s own reply is part of your '
            'Claude plan.',
        ),
        (
            'How do I disconnect?',
            'On Podskrift go to Settings → Connected apps and revoke access. '
            'You can also remove the connector under Claude Customize → '
            'Connectors.',
        ),
    ]


@app.route('/ai')
def ai_landing():
    """Public landing: connect Podskrift MCP in ChatGPT, Claude, Cursor, etc."""
    mcp_url = _mcp_connector_url()
    prompts = ai_ask_prompt_groups()
    return render_template(
        'ai.html',
        mcp_connector_url=mcp_url,
        ai_faq=ai_faq_entries(),
        ask_prompt_groups=prompts,
        structured_data=_faq_page_structured_data(
            public_url('ai_landing'),
            'Use Podskrift in ChatGPT and Claude',
            'Connect Podskrift to ChatGPT, Claude, Cursor or Claude Code '
            'via MCP. Ask your AI to summarise episodes, search your '
            'transcript library, or draft posts from the transcript text.',
            ai_faq_entries(),
            prompt_groups=prompts,
        ),
        trial_minutes=advertised_trial_minutes(),
    )


@app.route('/guides')
def guides_index():
    """Hub for setup guides (ChatGPT, Claude, and the long-form overview)."""
    return render_template('guides/index.html')


@app.route('/guides/chatgpt')
def guide_chatgpt():
    """Setup guide: connect Podskrift to ChatGPT via custom MCP plugin."""
    mcp_url = _mcp_connector_url()
    faq = guide_chatgpt_faq_entries()
    shots = _guide_shot_stems('chatgpt')
    page_url = public_url('guide_chatgpt')
    howto = {
        'name': 'Connect Podskrift to ChatGPT',
        'description': (
            'Add Podskrift as a custom MCP server in ChatGPT Plugins, sign in, '
            'and ask ChatGPT about any podcast episode.'
        ),
        'steps': [
            {
                'name': 'Open Customize, then Plugins',
                'text': (
                    'In ChatGPT on the web, open Customize in the sidebar and '
                    'choose Plugins, or go to chatgpt.com/plugins.'
                ),
                'image': _guide_static_url('chatgpt', '01-customize-plugins'),
            },
            {
                'name': 'Add a custom MCP server',
                'text': 'Click Add, then choose Add custom MCP server.',
                'image': _guide_static_url('chatgpt', '02-add-custom-mcp-server'),
            },
            {
                'name': 'Fill in Podskrift and create the plugin',
                'text': (
                    f'Name it Podskrift, set Server URL to {mcp_url}, '
                    'Authentication to OAuth, tick I understand and want to '
                    'continue, then click Create as a plugin.'
                ),
                'image': _guide_static_url('chatgpt', '03-create-as-plugin'),
            },
            {
                'name': 'Sign in to Podskrift and Allow',
                'text': (
                    'Log in to Podskrift when prompted and click Allow so '
                    'ChatGPT can use your minutes.'
                ),
            },
            {
                'name': 'Try a prompt',
                'text': 'In a new chat, ask: Summarise the latest episode of Hard Fork.',
            },
        ],
    }
    for step in howto['steps']:
        if not step.get('image'):
            step.pop('image', None)
    return render_template(
        'guides/chatgpt.html',
        mcp_connector_url=mcp_url,
        guide_faq=faq,
        example_prompts=_GUIDE_EXAMPLE_PROMPTS,
        shots=shots,
        structured_data=_faq_page_structured_data(
            page_url,
            'Connect Podskrift to ChatGPT',
            'Add Podskrift as a custom MCP server in ChatGPT Plugins. '
            'Name it Podskrift, URL https://podskrift.com/mcp, Authentication OAuth.',
            faq,
            howto=howto,
        ),
    )


@app.route('/guides/claude')
def guide_claude():
    """Setup guide: connect Podskrift to Claude via custom connector."""
    mcp_url = _mcp_connector_url()
    faq = guide_claude_faq_entries()
    shots = _guide_shot_stems('claude')
    page_url = public_url('guide_claude')
    howto = {
        'name': 'Connect Podskrift to Claude',
        'description': (
            'Add Podskrift as a custom connector in Claude, sign in, enable it '
            'in a chat, and ask about any podcast episode.'
        ),
        'steps': [
            {
                'name': 'Open Customize → Connectors',
                'text': 'In Claude, open Customize, then Connectors.',
                'image': _guide_static_url('claude', '01-customize-connectors'),
            },
            {
                'name': 'Add a custom connector',
                'text': 'Click + Add, then Add custom connector.',
                'image': _guide_static_url('claude', '02-add-custom-connector'),
            },
            {
                'name': 'Name it Podskrift and paste the URL',
                'text': (
                    f'Name it Podskrift, paste {mcp_url}, click Continue, '
                    'review the detected OAuth settings and finish adding it.'
                ),
                'image': _guide_static_url('claude', '03-connector-form'),
            },
            {
                'name': 'Connect, sign in, and Allow',
                'text': 'Click Connect, log in to Podskrift, and click Allow.',
                'image': _guide_static_url('claude', '04-connect-allow'),
            },
            {
                'name': 'Enable it in a chat',
                'text': (
                    'Open the tools menu in a chat, turn on Podskrift, then ask '
                    'for an episode.'
                ),
                'image': _guide_static_url('claude', '05-enable-in-chat'),
            },
        ],
    }
    # Drop image keys that resolved to None so JSON-LD stays clean.
    for step in howto['steps']:
        if not step.get('image'):
            step.pop('image', None)
    return render_template(
        'guides/claude.html',
        mcp_connector_url=mcp_url,
        guide_faq=faq,
        example_prompts=_GUIDE_EXAMPLE_PROMPTS,
        shots=shots,
        structured_data=_faq_page_structured_data(
            page_url,
            'Connect Podskrift to Claude',
            'Add Podskrift as a custom connector in Claude. Name it Podskrift, '
            'URL https://podskrift.com/mcp — then ask Claude about any episode.',
            faq,
            howto=howto,
        ),
    )


@app.route('/guides/podcast-transcripts-in-chatgpt-and-claude')
def guide_ai_transcripts():
    """Long-form guide: podcast transcripts in ChatGPT and Claude via MCP."""
    mcp_url = _mcp_connector_url()
    faq = guide_ai_faq_entries()
    prompts = ai_ask_prompt_groups()
    return render_template(
        'guide_ai_transcripts.html',
        mcp_connector_url=mcp_url,
        guide_faq=faq,
        ask_prompt_groups=prompts,
        structured_data=_faq_page_structured_data(
            public_url('guide_ai_transcripts'),
            'Podcast Transcripts in ChatGPT and Claude',
            'Get podcast transcripts in ChatGPT and Claude: connect Podskrift '
            'at podskrift.com/mcp, ask for any episode, search your library, '
            'and let the chat summarise or draft from the transcript.',
            faq,
            prompt_groups=prompts,
        ),
        trial_minutes=advertised_trial_minutes(),
        stripe_configured=stripe_checkout_enabled(),
    )


@app.route('/contact')
def contact():
    """Public contact page — hello@podskrift.com is the support address."""
    return render_template('contact.html')


@app.route('/pricing')
def pricing():
    """Public pricing page: free trial, one-time pack, or bring your own key."""
    # Must be a real bool — Jinja `a and b` returns b, so piping openai_api_key
    # through |tojson would render the raw key into the page (and PostHog).
    has_own_key = bool(
        current_user.is_authenticated and current_user.openai_api_key)
    return render_template(
        'pricing.html',
        trial_minutes=advertised_trial_minutes(),
        stripe_configured=stripe_checkout_enabled(),
        has_own_key=has_own_key,
    )


@app.route('/whats-new')
def whats_new():
    """Public build-in-public changelog — curated entries from changelog.json."""
    return render_template(
        'whats_new.html',
        entries=load_changelog_entries(),
    )


@app.route('/rss-help')
def rss_help():
    return render_template('rss_help.html')


@app.route('/privacy')
def privacy():
    """Short privacy note — analytics/replay disclosure for EU PostHog."""
    return render_template('privacy.html')


@app.route('/terms')
def terms():
    """Short terms + refund policy for the prepaid credit pack."""
    return render_template('terms.html')


CUSTOMER_API_DOC_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'docs', 'customer-api.md')

CHANGELOG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'changelog.json')


def load_changelog_entries():
    """User-facing What's new entries from changelog.json, newest first.

    Returns a list of dicts with id, date (YYYY-MM-DD), date_display, title,
    summary. Entries with ``"hidden": true`` are skipped (for features that
    are merged but not yet switched on in production). Missing or invalid
    files yield an empty list so a deploy without the data file still serves
    the rest of the site.
    """
    try:
        with open(CHANGELOG_PATH, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        app.logger.warning('Could not load changelog.json: %s', exc)
        return []
    entries = data.get('entries') if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return []
    out = []
    for raw in entries:
        if not isinstance(raw, dict):
            continue
        if raw.get('hidden') is True:
            continue
        entry_id = (raw.get('id') or '').strip()
        title = (raw.get('title') or '').strip()
        summary = (raw.get('summary') or '').strip()
        date_raw = (raw.get('date') or '').strip()
        if not entry_id or not title or not summary or not date_raw:
            continue
        try:
            date_display = datetime.strptime(date_raw, '%Y-%m-%d').strftime('%d %b %Y')
        except ValueError:
            date_display = date_raw
        out.append({
            'id': entry_id,
            'date': date_raw,
            'date_display': date_display,
            'title': title,
            'summary': summary,
        })
    return out


def load_customer_api_markdown():
    """Source for /docs/api. Public HTML must never ship AGENT_API_KEY copy."""
    with open(CUSTOMER_API_DOC_PATH, encoding='utf-8') as f:
        text = f.read()
    # MCP connect docs are gated on MCP_ENABLED (default off).
    text = mcp_server_mod.filter_mcp_docs_section(
        text, enabled=mcp_server_mod.mcp_enabled())
    # OAuth connector steps are nested inside that section; also require
    # MCP_OAUTH_ENABLED so the page stays quiet until OAuth is flipped on.
    text = oauth_server_mod.filter_mcp_oauth_docs_section(
        text, enabled=(
            mcp_server_mod.mcp_enabled()
            and oauth_server_mod.mcp_oauth_enabled()
        ))
    # Defence in depth: the public page must not document the host CoS secret,
    # even if someone reintroduces that line in the markdown.
    kept = []
    for line in text.splitlines():
        if 'AGENT_API_KEY' in line:
            continue
        kept.append(line)
    return '\n'.join(kept).strip() + '\n'


def _md_inline(text):
    """Escape, then apply a tiny subset of Markdown inline markup."""
    out = html_lib.escape(text)
    out = re.sub(r'`([^`]+)`', r'<code>\1</code>', out)
    out = re.sub(r'\*\*([^*]+)\*\*', r'<strong>\1</strong>', out)
    out = re.sub(
        r'\[([^\]]+)\]\((https?://[^)\s]+)\)',
        r'<a href="\2" rel="noopener noreferrer">\1</a>',
        out,
    )
    return out


def markdown_to_safe_html(source):
    """Render the customer-api.md subset to HTML. No third-party Markdown lib.

    Handles ATX headings, fenced code, tables, paragraphs, and the inline
    forms in `_md_inline`. Anything else stays escaped text.
    """
    lines = source.splitlines()
    parts = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith('```'):
            lang = html_lib.escape(line[3:].strip())
            i += 1
            body = []
            while i < len(lines) and not lines[i].startswith('```'):
                body.append(lines[i])
                i += 1
            if i < len(lines):
                i += 1  # closing fence
            code = html_lib.escape('\n'.join(body))
            cls = f' class="language-{lang}"' if lang else ''
            parts.append(f'<pre><code{cls}>{code}</code></pre>')
            continue
        if line.startswith('|'):
            rows = []
            while i < len(lines) and lines[i].startswith('|'):
                rows.append(lines[i])
                i += 1
            # Drop Markdown separator rows (| --- | --- |)
            data_rows = [
                r for r in rows
                if not re.match(r'^\|\s*[-:| ]+\|\s*$', r)
            ]
            if not data_rows:
                continue
            def cells(row):
                return [c.strip() for c in row.strip('|').split('|')]
            header = cells(data_rows[0])
            parts.append('<table><thead><tr>')
            for cell in header:
                parts.append(f'<th scope="col">{_md_inline(cell)}</th>')
            parts.append('</tr></thead><tbody>')
            for row in data_rows[1:]:
                parts.append('<tr>')
                for cell in cells(row):
                    parts.append(f'<td>{_md_inline(cell)}</td>')
                parts.append('</tr>')
            parts.append('</tbody></table>')
            continue
        heading = re.match(r'^(#{1,3})\s+(.*)$', line)
        if heading:
            level = len(heading.group(1))
            parts.append(f'<h{level}>{_md_inline(heading.group(2))}</h{level}>')
            i += 1
            continue
        if not line.strip():
            i += 1
            continue
        para = [line]
        i += 1
        while i < len(lines) and lines[i].strip() and not lines[i].startswith(
                ('#', '|', '```')):
            para.append(lines[i])
            i += 1
        parts.append(f'<p>{_md_inline(" ".join(para))}</p>')
    return '\n'.join(parts)


@app.route('/docs/api')
def api_docs():
    """Public customer API docs — HTML from docs/customer-api.md."""
    body_html = markdown_to_safe_html(load_customer_api_markdown())
    return render_template(
        'api_docs.html',
        body_html=body_html,
        trial_minutes=advertised_trial_minutes(),
    )


@app.route('/design/components')
def design_components_gallery():
    """Dev-only component gallery. Visible when DEBUG or the viewer is admin."""
    if not (app.debug or is_admin_user()):
        abort(404)
    response = make_response(render_template('design_components.html'))
    response.headers['X-Robots-Tag'] = 'noindex, nofollow'
    return response


# Remote MCP (Streamable HTTP). Route always exists; returns 404 when the flag
# is off so clients and probes get a stable path once MCP_ENABLED is flipped.
mcp_server_mod.register_mcp(app)
# OAuth 2.1 AS + PRM well-known (404 until MCP_OAUTH_ENABLED=1).
oauth_server_mod.register_oauth(app)


init_site_standards(
    app,
    public_base_url=PUBLIC_BASE_URL,
    changelog_loader=load_changelog_entries,
    api_markdown_loader=load_customer_api_markdown,
    posthog_host=product_analytics.posthog_host,
)


@app.route('/health')
def health():
    """Liveness probe JSON. Includes today's free-trial budget status.

    `trial_available` is True when today's shared daily budget still has room.
    `trial_daily_used` / `trial_daily_limit` are minute integers (not secrets).
    """
    used = trial_daily_used_seconds() if trial_available() else 0
    limit = TRIAL_DAILY_SECONDS if trial_available() else 0
    return jsonify({
        'ok': True,
        'trial_available': trial_daily_budget_available(),
        'trial_daily_used': used // 60,
        'trial_daily_limit': limit // 60,
    })


@app.route('/convert-apple-url', methods=['POST'])
def convert_apple_url():
    try:
        data = request.get_json()
        apple_url = data.get('apple_url', '').strip()
        if not apple_url:
            return jsonify({'success': False, 'error': 'No URL provided'})
        if 'podcasts.apple.com' not in apple_url:
            return jsonify({'success': False, 'error': 'Not an Apple Podcasts URL'})

        rss_url, error = convert_apple_podcasts_url_to_rss(apple_url)
        if rss_url:
            return jsonify({'success': True, 'rss_url': rss_url})
        return jsonify({'success': False, 'error': error or 'Failed to convert URL'})
    except Exception as e:
        return jsonify({'success': False, 'error': f'Server error: {e}'})


def _best_artwork(item):
    """Pick the largest artwork iTunes offers.

    Episode results (entity=podcastEpisode) never carry artworkUrl100 -- they use
    60/160/600 -- so reading only the 100 key left every episode row without an image.
    """
    for key in ('artworkUrl600', 'artworkUrl160', 'artworkUrl100', 'artworkUrl60'):
        if item.get(key):
            return item[key]
    return ''


@app.route('/search-podcasts', methods=['GET'])
def search_podcasts():
    """Search iTunes for podcast shows, or for individual episodes.

    `type=episode` uses entity=podcastEpisode, which returns episodeUrl -- the
    direct audio file. That lets someone search an episode topic and go straight
    to transcribing it, instead of finding the show first and paging its feed.
    """
    query = request.args.get('q', '').strip()
    search_type = request.args.get('type', 'show')
    if not query or len(query) < 2:
        return jsonify({'results': []})

    is_episode = search_type == 'episode'
    params = {'term': query, 'media': 'podcast', 'limit': 25}
    if is_episode:
        params['entity'] = 'podcastEpisode'

    try:
        resp = requests.get('https://itunes.apple.com/search', params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return jsonify({'results': [], 'error': str(e)})

    results = []
    for item in data.get('results', []):
        if is_episode:
            if item.get('episodeUrl'):
                results.append(_itunes_episode_result(item))
        elif item.get('feedUrl'):
            results.append(_itunes_show_result(item))

    return jsonify({'results': results})


def _itunes_episode_result(item):
    """An iTunes podcastEpisode hit, in the shape the search box renders."""
    duration_min = None
    if item.get('trackTimeMillis'):
        duration_min = round(item['trackTimeMillis'] / 60000, 1)
    apple_url = (
        safe_public_http_url(item.get('trackViewUrl') or '')
        or (
            canonical_apple_podcasts_url(
                f"https://podcasts.apple.com/podcast/id{item.get('collectionId')}"
                + (f"?i={item.get('trackId')}" if item.get('trackId') else '')
            )
            if item.get('collectionId') else None
        )
    )
    return {
        'type': 'episode',
        'name': item.get('trackName', ''),
        'artist': item.get('collectionName', ''),
        'artwork': _best_artwork(item),
        'audio_url': item.get('episodeUrl'),
        'feed_url': item.get('feedUrl', ''),
        'released': (item.get('releaseDate') or '')[:10],
        'duration_min': duration_min,
        'estimated_cost': (
            round(duration_min * WHISPER_COST_PER_MINUTE, 3) if duration_min else None
        ),
        'origin': 'itunes_episode',
        'apple_url': apple_url or '',
    }


def _itunes_show_result(item):
    """An iTunes podcast (show) hit, in the shape the search box renders."""
    apple_url = (
        safe_public_http_url(item.get('collectionViewUrl') or '')
        or (
            canonical_apple_podcasts_url(
                f"https://podcasts.apple.com/podcast/id{item.get('collectionId')}"
            )
            if item.get('collectionId') else None
        )
    )
    return {
        'type': 'show',
        'name': item.get('collectionName', ''),
        'artist': item.get('artistName', ''),
        'artwork': _best_artwork(item),
        'feed_url': item.get('feedUrl'),
        'genre': item.get('primaryGenreName', ''),
        'apple_url': apple_url or '',
    }


# ---------------------------------------------------------------------------
# Spotify links  (TSK-20440)
# ---------------------------------------------------------------------------

#: open.spotify.com/episode/<id>, /intl-no/show/<id>, /embed/..., spotify:episode:<id>.
#: Only the first 22 base62 characters of the id are used to build a request,
#: so glued junk after the id (e.g. an si= param pasted without '?') is ignored
#: and nothing the user typed decides which host the server talks to.
SPOTIFY_URL_RE = re.compile(
    r'(?:open\.spotify\.com/(?:intl-[a-z]{2}(?:-[a-z]{2})?/)?(?:embed/)?|spotify:)'
    r'(episode|show)[/:]([A-Za-z0-9]{22})'
)
#: Pasted links sometimes lose a slash or letters ("open.spotify.comsode/<id>",
#: "sode/<id>"). Still route a bare episode|show/<22-char-id> — and the common
#: mangled "sode/<id>" form of episode — to the resolver, never as name search.
#: group1=episode|show (needs a non-alnum boundary so "myepisode/…" is ignored),
#: group2=sode (may sit inside a mangled host like "comsode"), group3=id.
SPOTIFY_LOOSE_ID_RE = re.compile(
    r'(?:(?:^|[^A-Za-z0-9])(episode|show)|(sode))/([A-Za-z0-9]{22})',
    re.I,
)
#: spotify:show:<id> on an episode embed's relatedEntityUri.
SPOTIFY_SHOW_URI_RE = re.compile(r'^spotify:show:([A-Za-z0-9]{22})$')
#: Embed subtitle is sometimes the generic label "Podcast" / "Podcasts".
_GENERIC_SPOTIFY_SHOW_NAMES = frozenset({'podcast', 'podcasts'})

SPOTIFY_NO_FEED_HINT = (
    "If the show has one, paste the feed URL, or try searching for the show by name."
)

#: One short pause before retrying a transient directory failure.
SPOTIFY_DIRECTORY_RETRY_PAUSE_SEC = 0.4
#: Metadata fetch (embed / oEmbed): keep timeouts short so one retry still
#: finishes well under ~12s worst case (4 attempts × 2.5s + pauses).
SPOTIFY_META_TIMEOUT_SEC = 2.5
SPOTIFY_META_RETRY_PAUSE_SEC = 0.4
_SPOTIFY_META_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})

_SPOTIFY_HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; Podskrift/1.0)'}
_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)
_SHOW_SUFFIX_RE = re.compile(r'\s*[\(\[\{][^\)\]\}]*[\)\]\}]\s*$')


def parse_spotify_url(raw):
    """Return (kind, id) for a Spotify episode/show link, or (None, None).

    IDs shorter than 22 characters are rejected. Trailing base62 junk after a
    full 22-character id is ignored; the truncated id is only useful when
    Spotify metadata for it resolves.
    """
    text = raw or ''
    match = SPOTIFY_URL_RE.search(text)
    if match:
        return match.group(1), match.group(2)
    loose = SPOTIFY_LOOSE_ID_RE.search(text)
    if not loose:
        return None, None
    kind = (loose.group(1) or 'episode').lower()
    return kind, loose.group(3)


def _is_generic_spotify_show_name(name):
    """True when the embed subtitle is empty or a useless generic label."""
    stripped = (name or '').strip()
    if not stripped:
        return True
    return _normalize_title(stripped) in _GENERIC_SPOTIFY_SHOW_NAMES


def _normalize_title(text):
    """Case-, width- and punctuation-insensitive form for matching titles."""
    text = unicodedata.normalize('NFKC', text or '').casefold()
    return re.sub(r'[\W_]+', ' ', text).strip()


def _show_name_variants(show_name):
    """Exact name first, then without trailing parenthetical/bracketed suffixes."""
    variants, seen = [], set()

    def add(name):
        name = (name or '').strip()
        key = _normalize_title(name)
        if not name or not key or key in seen:
            return
        seen.add(key)
        variants.append(name)

    add(show_name)
    stripped = (show_name or '').strip()
    while True:
        nxt = _SHOW_SUFFIX_RE.sub('', stripped).strip()
        if nxt == stripped:
            break
        stripped = nxt
        add(stripped)
    return variants


def _empty_spotify_meta():
    return {
        'title': '', 'show': '', 'related_show_id': None,
        'playability_reason': None, 'is_playable': None, 'has_video': None,
    }


def _spotify_http_get(url, params=None, timeout=SPOTIFY_META_TIMEOUT_SEC):
    """GET with one retry on timeout / 429 / 5xx. Returns (resp, error_detail).

    error_detail is a short non-sensitive token (status_503, Timeout, …) for
    logs and the resolve JSON; None when the response is usable (2xx/3xx/4xx
    other than 429 — caller decides whether the body is useful).
    """
    last_detail = None
    for attempt in range(2):
        try:
            resp = requests.get(url, params=params, headers=_SPOTIFY_HEADERS,
                                timeout=timeout)
        except requests.Timeout:
            last_detail = 'Timeout'
        except requests.RequestException as exc:
            last_detail = type(exc).__name__
            # Non-timeout transport errors are unlikely to heal in 400ms.
            return None, last_detail
        else:
            if resp.status_code in _SPOTIFY_META_RETRY_STATUSES:
                last_detail = f'status_{resp.status_code}'
            else:
                return resp, (
                    f'status_{resp.status_code}' if resp.status_code >= 400 else None)
        if attempt == 0 and last_detail:
            time.sleep(SPOTIFY_META_RETRY_PAUSE_SEC)
    return None, last_detail


def _spotify_embed_metadata(kind, spotify_id):
    """Episode title and show name from Spotify's public embed player.

    The Web API needs an app registration; the embed player does not, and its
    __NEXT_DATA__ carries both names. It is not a documented API, so any shape
    change lands here as None and the caller falls back to oEmbed.

    Returns (meta_dict_or_None, error_detail_or_None).
    """
    resp, detail = _spotify_http_get(
        f'https://open.spotify.com/embed/{kind}/{spotify_id}')
    if resp is None:
        return None, detail
    if resp.status_code >= 400:
        return None, detail or f'status_{resp.status_code}'
    try:
        entity = json.loads(_NEXT_DATA_RE.search(resp.text).group(1))[
            'props']['pageProps']['state']['data']['entity']
    except (AttributeError, KeyError, TypeError, ValueError):
        return None, 'embed_parse'
    name = entity.get('name') or entity.get('title') or ''
    meta = _empty_spotify_meta()
    meta['playability_reason'] = entity.get('playabilityReason')
    meta['is_playable'] = entity.get('isPlayable')
    meta['has_video'] = entity.get('hasVideo')
    related = entity.get('relatedEntityUri') or ''
    related_match = SPOTIFY_SHOW_URI_RE.match(related)
    if related_match:
        meta['related_show_id'] = related_match.group(1)
    if entity.get('type') == 'episode':
        # A show embed renders its latest episode: the show is the subtitle.
        meta['title'] = name if kind == 'episode' else ''
        meta['show'] = entity.get('subtitle') or ''
        return meta, None
    meta['show'] = name
    return meta, None


def _spotify_oembed_metadata(kind, spotify_id):
    """oEmbed is documented but only carries one title: the episode's or the show's.

    Returns (meta_dict_or_None, error_detail_or_None).
    """
    resp, detail = _spotify_http_get(
        'https://open.spotify.com/oembed',
        params={'url': f'https://open.spotify.com/{kind}/{spotify_id}'})
    if resp is None:
        return None, detail
    if resp.status_code >= 400:
        return None, detail or f'status_{resp.status_code}'
    try:
        title = (resp.json().get('title') or '').strip()
    except (ValueError, AttributeError):
        return None, 'oembed_parse'
    if not title:
        return None, 'oembed_empty'
    meta = _empty_spotify_meta()
    if kind == 'episode':
        meta['title'] = title
    else:
        meta['show'] = title
    return meta, None


def _spotify_resolve_show_name(show_id):
    """Real show title from the show's embed, then oEmbed. Empty when unknown."""
    meta, _ = _spotify_embed_metadata('show', show_id)
    if meta:
        name = (meta.get('show') or '').strip()
        if name and not _is_generic_spotify_show_name(name):
            return name
    oem, _ = _spotify_oembed_metadata('show', show_id)
    if oem:
        return (oem.get('show') or '').strip()
    return ''


def fetch_spotify_metadata(kind, spotify_id):
    """{'title', 'show', playability…} for a Spotify link, or None when empty.

    Returns (meta_or_None, error_detail_or_None). error_detail is set when the
    link could not be read, for logs and the resolve JSON.
    """
    meta, detail = _spotify_embed_metadata(kind, spotify_id)
    if meta:
        related_show_id = meta.get('related_show_id')
        # Prefer the show page's name whenever relatedEntityUri gives a show id
        # (episode subtitle is sometimes the generic label "Podcast ").
        if related_show_id:
            resolved = _spotify_resolve_show_name(related_show_id)
            if resolved:
                meta['show'] = resolved
        if meta['show'] or meta['title']:
            return meta, None
    oem, oem_detail = _spotify_oembed_metadata(kind, spotify_id)
    if oem and (oem['show'] or oem['title']):
        return oem, None
    return None, detail or oem_detail or 'unreadable'


def _itunes_search(term, entity):
    """Raw iTunes results. Raises requests.RequestException -- the caller must
    tell "directory unreachable" apart from "not in the directory"."""
    resp = requests.get('https://itunes.apple.com/search', timeout=10, params={
        'term': term, 'media': 'podcast', 'entity': entity, 'limit': 25})
    resp.raise_for_status()
    return resp.json().get('results', [])


def _public_shows_named(show_name):
    """iTunes shows with a feed whose name matches the Spotify show exactly."""
    want = _normalize_title(show_name)
    if not want:
        return []
    return [item for item in _itunes_search(show_name, 'podcast')
            if item.get('feedUrl') and _normalize_title(item.get('collectionName')) == want]


def _public_shows_named_loose(show_name):
    """Shows matching a stripped variant of the name (parens/brackets removed).

    Only used when exact match finds nothing; callers must confirm the episode
    is in the candidate's feed before accepting the show (false positives are
    otherwise easy — e.g. two shows that share a short base name).
    """
    variants = _show_name_variants(show_name)
    if len(variants) <= 1:
        return []
    found, seen_feeds = [], set()
    for variant in variants[1:]:
        want = _normalize_title(variant)
        for item in _itunes_search(variant, 'podcast'):
            feed = item.get('feedUrl')
            if not feed or feed in seen_feeds:
                continue
            if _normalize_title(item.get('collectionName')) != want:
                continue
            seen_feeds.add(feed)
            found.append(item)
    return found


def _match_episode(episodes, title):
    """The feed episode with this title: exact first, then containment.

    Containment covers feeds that prefix a number ("#212 - Title") but only for
    titles long enough that a substring hit is not a coincidence.
    """
    want = _normalize_title(title)
    if not want:
        return None
    normalized = [(_normalize_title(ep['title']), ep) for ep in episodes]
    for have, ep in normalized:
        if have == want:
            return ep
    if len(want) >= 12:
        for have, ep in normalized:
            if want in have:
                return ep
    return None


#: /resolve-spotify is public, so a feed is read into memory only up to this.
#: Large back catalogues run to a few MB; anything past this is not a feed we want.
SPOTIFY_FEED_MAX_BYTES = 15 * 1024 * 1024

#: Closed RSS/Atom entry markers — used to stop reading once we have enough
#: newest items for the alert poller without holding a whole mega-feed.
_FEED_ITEM_CLOSE_RE = re.compile(br'</(item|entry)\s*>', re.I)


def _fetch_feed_capped(feed_url, max_bytes=SPOTIFY_FEED_MAX_BYTES,
                       early_stop_items=None, timeout=15):
    """Feed bytes, or None when the feed is unreachable or too big.

    A dead feed returns None instead of raising, so the resolver still gets
    to try the next show and the iTunes episode search. Redirects are followed
    by hand and every hop revalidated, as in download_audio: the route is
    public, and a public feed could otherwise 302 to a private address.

    When ``early_stop_items`` is set (alert poller), streaming stops after that
    many ``</item>`` / ``</entry>`` closes — newest-first feeds only need the
    head. Oversized feeds without an early-stop still return None.
    """
    current = feed_url
    seen = set()
    redirects = 0
    try:
        with requests.Session() as session:
            while True:
                if not _is_fetchable_url(current):
                    return None
                finger = current.split('#', 1)[0]
                if finger in seen:
                    return None
                seen.add(finger)
                with session.get(current, headers=_SPOTIFY_HEADERS, timeout=timeout,
                                 stream=True, allow_redirects=False) as resp:
                    if resp.is_redirect or resp.is_permanent_redirect:
                        location = resp.headers.get('location')
                        if not location:
                            return None
                        if redirects >= MAX_REDIRECTS:
                            return None
                        redirects += 1
                        current = urljoin(current, location)
                        continue
                    resp.raise_for_status()
                    chunks, size, items_seen = [], 0, 0
                    for chunk in resp.iter_content(64 * 1024):
                        next_size = size + len(chunk)
                        if next_size > max_bytes:
                            # Alert poller: keep a usable prefix if we already
                            # streamed some bytes; Spotify resolve: refuse.
                            if not early_stop_items or not chunks:
                                return None
                            break
                        chunks.append(chunk)
                        size = next_size
                        if early_stop_items:
                            items_seen += len(_FEED_ITEM_CLOSE_RE.findall(chunk))
                            if items_seen >= early_stop_items:
                                break
                    return b''.join(chunks) if chunks else None
    except requests.RequestException:
        return None
    return None


def _episode_from_feed(feed_url, title):
    """Find the episode in the show's public RSS feed, as a search-box result."""
    if not _is_fetchable_url(feed_url):
        return None
    body = _fetch_feed_capped(feed_url)
    if body is None:
        return None
    episodes, _ = get_episodes_from_rss(body)
    ep = _match_episode(episodes or [], title)
    if not ep:
        return None
    return {
        'type': 'episode',
        'name': ep['title'],
        'artist': ep['podcast_name'],
        'artwork': ep['artwork'],
        'audio_url': ep['audio_url'],
        'feed_url': feed_url,
        'released': ep['published'],
        'duration_min': ep['duration_min'],
        'estimated_cost': ep['estimated_cost'],
        'episode_link': ep.get('episode_link') or '',
        'show_link': ep.get('show_link') or '',
    }


def _episode_from_itunes(title, show_name):
    """Fallback when the feed lookup misses: the same episode search the Episodes
    tab runs, accepted only on an exact title (and show, when known) match."""
    want_title, want_show = _normalize_title(title), _normalize_title(show_name)
    for item in _itunes_search(title, 'podcastEpisode'):
        if not item.get('episodeUrl'):
            continue
        if _normalize_title(item.get('trackName')) != want_title:
            continue
        if want_show and _normalize_title(item.get('collectionName')) != want_show:
            continue
        return _itunes_episode_result(item)
    return None


def _no_feed_message(name):
    label = name or 'this show'
    return (
        f'We couldn\'t find a public RSS feed for "{label}". '
        f'{SPOTIFY_NO_FEED_HINT}'
    )


def _paid_episode_message(show_name):
    base = (
        'This episode is for paying Spotify subscribers only, so Podskrift '
        "can't fetch its audio to transcribe."
    )
    if show_name:
        return (
            f'{base} The show "{show_name}" is below — open it to pick a '
            'public episode if the feed has any.'
        )
    return base


def _spotify_resolve_outcome(results, error=None, error_kind=None, show_name=None,
                             error_detail=None):
    return {
        'results': results,
        'error': error,
        'error_kind': error_kind,
        'show_name': show_name or '',
        'error_detail': error_detail or '',
    }


def resolve_spotify_url(raw):
    """Map a Spotify link onto the public feed Podskrift can fetch.

    Returns a dict: results, error, error_kind, show_name, error_detail. Raises
    requests.RequestException when a directory lookup itself fails.
    """
    kind, spotify_id = parse_spotify_url(raw)
    if not kind:
        return _spotify_resolve_outcome(
            [], "That doesn't look like a Spotify episode or show link.",
            'unreadable_link')
    meta, meta_detail = fetch_spotify_metadata(kind, spotify_id)
    if not meta:
        return _spotify_resolve_outcome(
            [], "Couldn't read that Spotify link. Check that it's a public episode or show.",
            'unreadable_link', error_detail=meta_detail)

    show_name = meta.get('show') or ''
    title = meta.get('title') or ''
    paid = (meta.get('playability_reason') or '').upper() == 'PAYMENT_REQUIRED'

    shows = _public_shows_named(show_name) if show_name else []
    if kind == 'show':
        if shows:
            return _spotify_resolve_outcome([_itunes_show_result(shows[0])],
                                            show_name=show_name)
        return _spotify_resolve_outcome(
            [], _no_feed_message(show_name), 'no_feed', show_name)

    if paid:
        # Still surface the show when we can find its public feed.
        if shows:
            return _spotify_resolve_outcome(
                [_itunes_show_result(shows[0])],
                _paid_episode_message(show_name), 'paid_episode', show_name)
        return _spotify_resolve_outcome(
            [], _paid_episode_message(show_name), 'paid_episode', show_name)

    spotify_episode_url = (
        f'https://open.spotify.com/episode/{spotify_id}' if kind == 'episode' else ''
    )

    def _with_spotify(hit, itunes_show=None):
        out = dict(hit, origin='spotify')
        if spotify_episode_url:
            out['spotify_url'] = spotify_episode_url
        if not out.get('apple_url') and itunes_show:
            apple = (
                safe_public_http_url(itunes_show.get('collectionViewUrl') or '')
                or (
                    canonical_apple_podcasts_url(
                        f"https://podcasts.apple.com/podcast/id"
                        f"{itunes_show.get('collectionId')}"
                    )
                    if itunes_show.get('collectionId') else None
                )
            )
            if apple:
                out['apple_url'] = apple
        return out

    for show in shows[:2]:
        hit = _episode_from_feed(show['feedUrl'], title)
        if hit:
            return _spotify_resolve_outcome(
                [_with_spotify(hit, show)], show_name=show_name)

    # Exact name missed: try stripped variants, but only accept a candidate
    # when the episode title is actually in that show's feed.
    if show_name and title and not shows:
        for show in _public_shows_named_loose(show_name)[:3]:
            hit = _episode_from_feed(show['feedUrl'], title)
            if hit:
                return _spotify_resolve_outcome(
                    [_with_spotify(hit, show)], show_name=show_name)

    hit = _episode_from_itunes(title, show_name)
    if hit:
        # Prefer spotify even when the match came via iTunes as a lookup aid.
        itunes_show = shows[0] if shows else None
        return _spotify_resolve_outcome(
            [_with_spotify(hit, itunes_show)], show_name=show_name)
    if shows:
        return _spotify_resolve_outcome(
            [_itunes_show_result(shows[0])],
            (f'Found "{show_name}", but this episode isn\'t in its public feed. '
             'Open the show below to pick from the episodes that are.'),
            'episode_not_in_feed', show_name)
    name = show_name or title
    return _spotify_resolve_outcome(
        [], _no_feed_message(name), 'no_feed', show_name or name)


def _log_spotify_resolve_failure(spotify_id, kind, error_kind, error_detail=None):
    if not error_kind:
        return
    app.logger.info(
        'spotify resolve failed id=%s type=%s error_kind=%s detail=%s',
        spotify_id or '-', kind or '-', error_kind, error_detail or '-')


@app.route('/resolve-spotify', methods=['GET'])
def resolve_spotify():
    """Turn a pasted Spotify episode/show link into a search-box result."""
    raw = request.args.get('url', '').strip()
    kind, spotify_id = parse_spotify_url(raw)
    outcome = None
    try:
        outcome = resolve_spotify_url(raw)
    except requests.RequestException:
        try:
            time.sleep(SPOTIFY_DIRECTORY_RETRY_PAUSE_SEC)
            outcome = resolve_spotify_url(raw)
        except requests.RequestException:
            outcome = _spotify_resolve_outcome(
                [], "Couldn't reach the podcast directory. Try again in a moment.",
                'directory_unreachable', error_detail='directory_unreachable')
    _log_spotify_resolve_failure(
        spotify_id, kind, outcome.get('error_kind'), outcome.get('error_detail'))
    return jsonify({
        'results': outcome['results'],
        'error': outcome['error'],
        'error_kind': outcome['error_kind'],
        'show_name': outcome.get('show_name') or '',
        'error_detail': outcome.get('error_detail') or '',
    })


# ---------------------------------------------------------------------------
# Apple Podcasts links
# ---------------------------------------------------------------------------

#: Any country store: /us/podcast/…/id123, /cn/podcast/…/id123?i=456, …
APPLE_SHOW_ID_RE = re.compile(r'/id(\d+)', re.I)


def parse_apple_podcasts_url(raw):
    """Return (show_id, episode_id) from a podcasts.apple.com URL.

    ``episode_id`` is None for show-only links. Both are digit strings when
    present. Returns (None, None) when the URL is not an Apple Podcasts link
    with a show id.
    """
    text = (raw or '').strip()
    if not text or 'podcasts.apple.com' not in text.lower():
        return None, None
    show_match = APPLE_SHOW_ID_RE.search(text)
    if not show_match:
        return None, None
    show_id = show_match.group(1)
    episode_id = None
    try:
        parsed = urlparse(text if '://' in text else 'https://' + text)
        values = parse_qs(parsed.query).get('i') or []
        if values and re.fullmatch(r'\d+', values[0] or ''):
            episode_id = values[0]
    except ValueError:
        pass
    return show_id, episode_id


def _itunes_lookup(itunes_id, *, entity=None, limit=None):
    """Raw iTunes lookup results. Raises requests.RequestException."""
    params = {'id': itunes_id}
    if entity:
        params['entity'] = entity
    if limit is not None:
        params['limit'] = limit
    resp = requests.get('https://itunes.apple.com/lookup', timeout=10, params=params)
    resp.raise_for_status()
    return resp.json().get('results', [])


def _apple_resolve_outcome(results, error=None, error_kind=None):
    return {
        'results': results,
        'error': error,
        'error_kind': error_kind,
    }


def _apple_show_from_lookup(items):
    """The podcast/show row from an iTunes lookup payload, or None."""
    for item in items:
        if item.get('kind') == 'podcast' and item.get('feedUrl'):
            return item
        # Some payloads omit kind and only set wrapperType=track + feedUrl.
        if item.get('feedUrl') and item.get('wrapperType') == 'track' and (
                item.get('kind') in (None, 'podcast')):
            return item
    for item in items:
        if item.get('feedUrl') and not item.get('episodeUrl'):
            return item
    return None


def _apple_episode_from_lookup(items, episode_id):
    """Match trackId to the pasted ``i=`` value among podcastEpisode rows."""
    want = str(episode_id)
    for item in items:
        if str(item.get('trackId') or '') != want:
            continue
        if item.get('wrapperType') == 'podcastEpisode' or item.get('kind') == 'podcast-episode':
            return item
        # Defensive: episode rows always carry episodeUrl; the show row does not.
        if item.get('episodeUrl'):
            return item
    return None


def _apple_episode_result(item, show_item=None, *, pasted_url=None):
    """Episode search-box row; copy feedUrl from the show when the episode omits it."""
    if not item.get('feedUrl') and show_item and show_item.get('feedUrl'):
        item = dict(item, feedUrl=show_item['feedUrl'])
    # Pasted Apple Podcasts links are a distinct origin from iTunes name search.
    out = dict(_itunes_episode_result(item), origin='apple')
    # Prefer the exact pasted episode URL when it parses cleanly.
    pasted = canonical_apple_podcasts_url(pasted_url or '')
    if pasted:
        out['apple_url'] = pasted
    return out


def resolve_apple_url(raw):
    """Map a podcasts.apple.com show/episode link to a search-box result.

    Returns a dict: results, error, error_kind. Raises
    requests.RequestException when the iTunes lookup itself fails.
    """
    show_id, episode_id = parse_apple_podcasts_url(raw)
    if not show_id:
        return _apple_resolve_outcome(
            [], "That doesn't look like an Apple Podcasts link.",
            'unreadable_link')

    if episode_id:
        items = _itunes_lookup(show_id, entity='podcastEpisode', limit=200)
        show_item = _apple_show_from_lookup(items)
        episode_item = _apple_episode_from_lookup(items, episode_id)
        if episode_item and episode_item.get('episodeUrl'):
            ep = _apple_episode_result(
                episode_item, show_item, pasted_url=raw)
            audio = ep.get('audio_url') or ''
            if audio and not _is_fetchable_url(audio):
                # Keep the show when audio is on a private host; never fetch it.
                if show_item and show_item.get('feedUrl') and _is_fetchable_url(
                        show_item['feedUrl']):
                    return _apple_resolve_outcome(
                        [_itunes_show_result(show_item)],
                        "That episode's audio can't be fetched. "
                        'The show is below — open it to pick another episode.',
                        'episode_not_fetchable')
                return _apple_resolve_outcome(
                    [], "That episode's audio can't be fetched.",
                    'episode_not_fetchable')
            feed = ep.get('feed_url') or ''
            if feed and not _is_fetchable_url(feed):
                ep['feed_url'] = ''
            return _apple_resolve_outcome([ep])

        if show_item and show_item.get('feedUrl') and _is_fetchable_url(
                show_item['feedUrl']):
            return _apple_resolve_outcome(
                [_itunes_show_result(show_item)],
                "Couldn't find that episode in Apple's directory. "
                'The show is below — open it to pick an episode.',
                'episode_not_found')
        if show_item:
            return _apple_resolve_outcome(
                [], 'We couldn\'t find a public RSS feed for that show.',
                'no_feed')
        return _apple_resolve_outcome(
            [], "Couldn't find that Apple Podcasts show.",
            'not_found')

    items = _itunes_lookup(show_id)
    show_item = _apple_show_from_lookup(items)
    if show_item and show_item.get('feedUrl') and _is_fetchable_url(
            show_item['feedUrl']):
        return _apple_resolve_outcome([_itunes_show_result(show_item)])
    if show_item:
        return _apple_resolve_outcome(
            [], 'We couldn\'t find a public RSS feed for that show.',
            'no_feed')
    return _apple_resolve_outcome(
        [], "Couldn't find that Apple Podcasts show.",
        'not_found')


def _log_apple_resolve_failure(show_id, episode_id, error_kind):
    if not error_kind:
        return
    app.logger.info(
        'apple resolve failed show_id=%s episode_id=%s error_kind=%s',
        show_id or '-', episode_id or '-', error_kind)


@app.route('/resolve-apple', methods=['GET'])
def resolve_apple():
    """Turn a pasted Apple Podcasts show/episode link into a search-box result."""
    raw = request.args.get('url', '').strip()
    show_id, episode_id = parse_apple_podcasts_url(raw)
    outcome = None
    try:
        outcome = resolve_apple_url(raw)
    except requests.RequestException:
        try:
            time.sleep(SPOTIFY_DIRECTORY_RETRY_PAUSE_SEC)
            outcome = resolve_apple_url(raw)
        except requests.RequestException:
            outcome = _apple_resolve_outcome(
                [], "Couldn't reach the podcast directory. Try again in a moment.",
                'directory_unreachable')
    _log_apple_resolve_failure(show_id, episode_id, outcome.get('error_kind'))
    return jsonify({
        'results': outcome['results'],
        'error': outcome['error'],
        'error_kind': outcome['error_kind'],
    })


# ---------------------------------------------------------------------------
# Init
# ---------------------------------------------------------------------------

def _live_columns(table):
    """Column names of ``table`` as seen by the session's own connection.

    Uses ``PRAGMA table_info`` through ``db.session`` -- the same connection
    that runs the ALTER TABLEs -- instead of ``sa_inspect(db.engine)``, which
    checks out a *different* pooled connection. With WAL and several pooled
    SQLite connections, that other connection can report a stale schema, so
    a missing column looked present and was silently skipped (PODSKRIFT-6
    follow-up). Returns an empty set when the table does not exist.
    ``table`` is always a hard-coded name, never user input.
    """
    rows = db.session.execute(text(f'PRAGMA table_info({table})')).fetchall()
    db.session.commit()
    return {row[1] for row in rows}


def apply_column_migrations():
    """Add columns missing from an existing database. Safe to run concurrently.

    This module is imported by every gunicorn worker, and prod runs two of
    them, so both race the same ALTER TABLE at boot. The inspector snapshot
    says the column is missing for both; the loser's ALTER then fails with
    "duplicate column name", which killed the worker -- and gunicorn treats a
    worker that fails to boot as fatal and shuts the master down. The 2026-09-09
    deploy survived only because systemd restarted the unit.

    Losing that race means the other worker did our work, so it is a success.
    Any other OperationalError is a real schema problem and still raises.

    Returns the columns this process actually added.
    """
    from sqlalchemy.exc import OperationalError

    added = []
    tables = (
        ('transcription_tasks', TASK_COLUMN_MIGRATIONS),
        ('users', USER_COLUMN_MIGRATIONS),
        ('credit_purchases', CREDIT_PURCHASE_COLUMN_MIGRATIONS),
        ('transcript_shares', TRANSCRIPT_SHARE_COLUMN_MIGRATIONS),
        ('saved_feeds', SAVED_FEED_COLUMN_MIGRATIONS),
    )
    for table, migrations in tables:
        existing = _live_columns(table)
        if not existing:
            # Table not created yet.
            continue
        for column, ddl_type in migrations.items():
            if column in existing:
                continue
            try:
                db.session.execute(text(
                    f'ALTER TABLE {table} ADD COLUMN {column} {ddl_type}'
                ))
                db.session.commit()
                added.append(f'{table}.{column}')
            except OperationalError as exc:
                db.session.rollback()
                if 'duplicate column name' not in str(exc).lower():
                    raise
                app.logger.info(
                    '%s.%s was added by another worker; continuing', table, column)
    return added


def raise_60_minute_trial_cohort():
    """Lift accounts that signed up under the 60-minute grant to the current one.

    Idempotent and safe when two gunicorn workers race: one conditional UPDATE
    that only matches rows still at exactly 60 minutes inside the PR #54 signup
    window. After it runs no row matches, so a second worker or a later boot
    updates nothing. ``trial_seconds_used`` is untouched, and trial_reserve()
    reads the limit inside its own UPDATE, so a job in flight is unaffected.
    NULL (legacy 180) and hand-set limits are never touched. Returns rowcount.
    """
    if NEW_USER_TRIAL_SECONDS <= TRIAL_60_COHORT_SECONDS:
        return 0
    if 'trial_seconds_limit' not in _live_columns('users'):
        return 0
    result = db.session.execute(text(
        'UPDATE users SET trial_seconds_limit = :new_limit '
        'WHERE trial_seconds_limit = :old_limit '
        'AND created_at >= :created_from AND created_at < :created_before'
    ), {
        'new_limit': NEW_USER_TRIAL_SECONDS,
        'old_limit': TRIAL_60_COHORT_SECONDS,
        'created_from': TRIAL_60_COHORT_CREATED_FROM,
        'created_before': TRIAL_60_COHORT_CREATED_BEFORE,
    })
    db.session.commit()
    raised = result.rowcount or 0
    if raised:
        app.logger.info('Raised %d 60-minute trial accounts to %d minutes',
                        raised, NEW_USER_TRIAL_SECONDS // 60)
    return raised


def ensure_credit_purchases_table():
    """Create credit_purchases if missing. Safe when two gunicorn workers race.

    ``db.create_all()`` uses check-then-create, so two workers that both see the
    table as absent can both run CREATE and the loser dies on "already exists".
    ``CREATE TABLE IF NOT EXISTS`` makes that race a no-op.

    Also applies CREDIT_PURCHASE_COLUMN_MIGRATIONS and the payment_intent index
    so an existing table from the first Stripe ship picks up the hardening
    columns without a separate migration framework.
    """
    from sqlalchemy.exc import OperationalError

    try:
        db.session.execute(text("""
            CREATE TABLE IF NOT EXISTS credit_purchases (
                id INTEGER NOT NULL PRIMARY KEY,
                user_id INTEGER,
                stripe_session_id VARCHAR(255) NOT NULL UNIQUE,
                stripe_event_id VARCHAR(255),
                stripe_payment_intent_id VARCHAR(255),
                amount_cents INTEGER NOT NULL,
                amount_subtotal_cents INTEGER,
                amount_tax_cents INTEGER,
                amount_total_cents INTEGER,
                currency VARCHAR(16) NOT NULL,
                customer_country VARCHAR(2),
                minutes INTEGER NOT NULL,
                status VARCHAR(20) NOT NULL DEFAULT 'credited',
                seconds_clawed_back INTEGER NOT NULL DEFAULT 0,
                amount_refunded_cents INTEGER NOT NULL DEFAULT 0,
                refunded_at DATETIME,
                created_at DATETIME,
                FOREIGN KEY(user_id) REFERENCES users (id)
            )
        """))
        db.session.execute(text(
            'CREATE INDEX IF NOT EXISTS ix_credit_purchases_user_id '
            'ON credit_purchases (user_id)'
        ))
        # The payment_intent index is created below, only after missing
        # columns are ALTERed in: a table from the first Stripe ship lacks
        # stripe_payment_intent_id, and indexing it here crashed boot.
        db.session.commit()
    except OperationalError as exc:
        db.session.rollback()
        msg = str(exc).lower()
        if 'already exists' not in msg:
            raise
        app.logger.info(
            'credit_purchases was created by another worker; continuing')

    # Additive column upgrades for tables created by the first Stripe ship.
    # Read on the session's connection, the one that runs the ALTERs below.
    existing = _live_columns('credit_purchases')
    if not existing:
        return
    for column, ddl_type in CREDIT_PURCHASE_COLUMN_MIGRATIONS.items():
        if column in existing:
            continue
        try:
            db.session.execute(text(
                f'ALTER TABLE credit_purchases ADD COLUMN {column} {ddl_type}'
            ))
            db.session.commit()
        except OperationalError as exc:
            db.session.rollback()
            if 'duplicate column name' not in str(exc).lower():
                raise
            app.logger.info(
                'credit_purchases.%s was added by another worker; continuing',
                column)
    try:
        db.session.execute(text(
            'CREATE INDEX IF NOT EXISTS ix_credit_purchases_stripe_payment_intent_id '
            'ON credit_purchases (stripe_payment_intent_id)'
        ))
        db.session.commit()
    except OperationalError:
        db.session.rollback()
        app.logger.exception(
            'Could not create ix_credit_purchases_stripe_payment_intent_id')


def ensure_transcript_shares_table():
    """Create transcript_shares if missing. Safe when two gunicorn workers race.

    Indexes that reference a column are created only AFTER that column is
    known to exist (CREATE TABLE includes it, or an ALTER has added it). A
    past outage came from indexing a column before the ALTER landed.
    """
    from sqlalchemy.exc import OperationalError

    try:
        db.session.execute(text("""
            CREATE TABLE IF NOT EXISTS transcript_shares (
                id INTEGER NOT NULL PRIMARY KEY,
                token VARCHAR(64) NOT NULL UNIQUE,
                task_id VARCHAR(36) NOT NULL UNIQUE,
                user_id INTEGER NOT NULL,
                created_at DATETIME,
                revoked_at DATETIME,
                FOREIGN KEY(task_id) REFERENCES transcription_tasks (id),
                FOREIGN KEY(user_id) REFERENCES users (id)
            )
        """))
        db.session.commit()
    except OperationalError as exc:
        db.session.rollback()
        msg = str(exc).lower()
        if 'already exists' not in msg:
            raise
        app.logger.info(
            'transcript_shares was created by another worker; continuing')

    # Additive column upgrades first — never index a column that might be
    # missing on a table created by an earlier ship of this feature.
    existing = _live_columns('transcript_shares')
    if not existing:
        return
    for column, ddl_type in TRANSCRIPT_SHARE_COLUMN_MIGRATIONS.items():
        if column in existing:
            continue
        try:
            db.session.execute(text(
                f'ALTER TABLE transcript_shares ADD COLUMN {column} {ddl_type}'
            ))
            db.session.commit()
        except OperationalError as exc:
            db.session.rollback()
            if 'duplicate column name' not in str(exc).lower():
                raise
            app.logger.info(
                'transcript_shares.%s was added by another worker; continuing',
                column)

    # Re-read after ALTERs so indexes only target columns that exist now.
    existing = _live_columns('transcript_shares')
    index_specs = []
    if 'token' in existing:
        index_specs.append(
            ('ix_transcript_shares_token',
             'CREATE UNIQUE INDEX IF NOT EXISTS ix_transcript_shares_token '
             'ON transcript_shares (token)')
        )
    if 'task_id' in existing:
        index_specs.append(
            ('ix_transcript_shares_task_id',
             'CREATE UNIQUE INDEX IF NOT EXISTS ix_transcript_shares_task_id '
             'ON transcript_shares (task_id)')
        )
    if 'user_id' in existing:
        index_specs.append(
            ('ix_transcript_shares_user_id',
             'CREATE INDEX IF NOT EXISTS ix_transcript_shares_user_id '
             'ON transcript_shares (user_id)')
        )
    for name, ddl in index_specs:
        try:
            db.session.execute(text(ddl))
            db.session.commit()
        except OperationalError:
            db.session.rollback()
            app.logger.exception('Could not create %s', name)


def ensure_email_sent_log_table():
    """Create email_sent_log if missing. Index only after the table exists.

    Same IF NOT EXISTS pattern as credit_purchases: two gunicorn workers race
    boot, and check-then-create killed a worker before. Indexes are created
    after CREATE so a half-applied migration cannot index a missing column.
    """
    from sqlalchemy.exc import OperationalError

    try:
        db.session.execute(text("""
            CREATE TABLE IF NOT EXISTS email_sent_log (
                id INTEGER NOT NULL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                kind VARCHAR(64) NOT NULL,
                idempotency_key VARCHAR(255) NOT NULL UNIQUE,
                created_at DATETIME,
                FOREIGN KEY(user_id) REFERENCES users (id)
            )
        """))
        db.session.commit()
    except OperationalError as exc:
        db.session.rollback()
        msg = str(exc).lower()
        if 'already exists' not in msg:
            raise
        app.logger.info(
            'email_sent_log was created by another worker; continuing')

    existing = _live_columns('email_sent_log')
    if not existing:
        return
    # Index AFTER columns exist (past outage: index-before-column on credit_purchases).
    try:
        db.session.execute(text(
            'CREATE INDEX IF NOT EXISTS ix_email_sent_log_user_id '
            'ON email_sent_log (user_id)'
        ))
        db.session.commit()
    except OperationalError:
        db.session.rollback()
        app.logger.exception('Could not create ix_email_sent_log_user_id')


def ensure_summary_email_tables():
    """Create summary-email job + budget tables. Index only after columns exist."""
    from sqlalchemy.exc import OperationalError

    try:
        db.session.execute(text("""
            CREATE TABLE IF NOT EXISTS summary_email_jobs (
                id INTEGER NOT NULL PRIMARY KEY,
                idempotency_key VARCHAR(255) NOT NULL UNIQUE,
                rss_url VARCHAR(1024) NOT NULL,
                audio_url VARCHAR(1024) NOT NULL,
                episode_guid VARCHAR(1024) NOT NULL,
                episode_title VARCHAR(512),
                podcast_name VARCHAR(512),
                duration_seconds FLOAT,
                status VARCHAR(32) NOT NULL DEFAULT 'queued',
                task_id VARCHAR(36),
                skip_reason VARCHAR(128),
                error_message TEXT,
                budget_seconds INTEGER,
                budget_day VARCHAR(10),
                created_at DATETIME,
                updated_at DATETIME
            )
        """))
        db.session.execute(text("""
            CREATE TABLE IF NOT EXISTS summary_email_budget_days (
                day VARCHAR(10) NOT NULL PRIMARY KEY,
                seconds_used INTEGER NOT NULL DEFAULT 0
            )
        """))
        db.session.commit()
    except OperationalError as exc:
        db.session.rollback()
        msg = str(exc).lower()
        if 'already exists' not in msg:
            raise
        app.logger.info(
            'summary_email tables created by another worker; continuing')

    for table, index_sql in (
        ('summary_email_jobs',
         'CREATE INDEX IF NOT EXISTS ix_summary_email_jobs_status '
         'ON summary_email_jobs (status)'),
        ('summary_email_jobs',
         'CREATE INDEX IF NOT EXISTS ix_summary_email_jobs_audio_url '
         'ON summary_email_jobs (audio_url)'),
    ):
        existing = _live_columns(table)
        if not existing:
            continue
        try:
            db.session.execute(text(index_sql))
            db.session.commit()
        except OperationalError:
            db.session.rollback()
            app.logger.exception('Could not create summary_email index')


def ensure_trial_budget_days_table():
    """Create the shared free-trial daily budget ledger if missing.

    Additive only — PK on Europe/Oslo day string. Same IF NOT EXISTS race
    pattern as summary_email_budget_days.
    """
    from sqlalchemy.exc import OperationalError

    try:
        db.session.execute(text("""
            CREATE TABLE IF NOT EXISTS trial_budget_days (
                day VARCHAR(10) NOT NULL PRIMARY KEY,
                seconds_used INTEGER NOT NULL DEFAULT 0
            )
        """))
        db.session.commit()
    except OperationalError as exc:
        db.session.rollback()
        msg = str(exc).lower()
        if 'already exists' not in msg:
            raise
        app.logger.info(
            'trial_budget_days created by another worker; continuing')


def ensure_password_reset_tokens_table():
    """Create password_reset_tokens if missing. Index only after columns exist.

    Same IF NOT EXISTS pattern as transcript_shares: two gunicorn workers race
    boot, and indexes that mention a column are created only after that column
    is known to exist (CREATE TABLE includes it, or an ALTER has added it).
    """
    from sqlalchemy.exc import OperationalError

    try:
        db.session.execute(text("""
            CREATE TABLE IF NOT EXISTS password_reset_tokens (
                id INTEGER NOT NULL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                token_hash VARCHAR(64) NOT NULL UNIQUE,
                expires_at DATETIME NOT NULL,
                used_at DATETIME,
                created_at DATETIME,
                FOREIGN KEY(user_id) REFERENCES users (id)
            )
        """))
        db.session.commit()
    except OperationalError as exc:
        db.session.rollback()
        msg = str(exc).lower()
        if 'already exists' not in msg:
            raise
        app.logger.info(
            'password_reset_tokens was created by another worker; continuing')

    existing = _live_columns('password_reset_tokens')
    if not existing:
        return
    for column, ddl_type in PASSWORD_RESET_TOKEN_COLUMN_MIGRATIONS.items():
        if column in existing:
            continue
        try:
            db.session.execute(text(
                f'ALTER TABLE password_reset_tokens ADD COLUMN {column} {ddl_type}'
            ))
            db.session.commit()
        except OperationalError as exc:
            db.session.rollback()
            if 'duplicate column name' not in str(exc).lower():
                raise
            app.logger.info(
                'password_reset_tokens.%s was added by another worker; continuing',
                column)

    existing = _live_columns('password_reset_tokens')
    index_specs = []
    if 'token_hash' in existing:
        index_specs.append(
            ('ix_password_reset_tokens_token_hash',
             'CREATE UNIQUE INDEX IF NOT EXISTS '
             'ix_password_reset_tokens_token_hash '
             'ON password_reset_tokens (token_hash)')
        )
    if 'user_id' in existing:
        index_specs.append(
            ('ix_password_reset_tokens_user_id',
             'CREATE INDEX IF NOT EXISTS ix_password_reset_tokens_user_id '
             'ON password_reset_tokens (user_id)')
        )
    for name, ddl in index_specs:
        try:
            db.session.execute(text(ddl))
            db.session.commit()
        except OperationalError:
            db.session.rollback()
            app.logger.exception('Could not create %s', name)


with app.app_context():
    # create_all is check-then-create. Two gunicorn workers (and the suite's
    # cross-process reservation test) can both see email_sent_log as absent
    # and the loser dies on "already exists" — same hole credit_purchases had
    # before ensure_* used IF NOT EXISTS. Tolerate that race; the ensure_*
    # helpers below still make the Stripe / email tables idempotent.
    from sqlalchemy.exc import OperationalError as _BootOpError
    try:
        db.create_all()
    except _BootOpError as exc:
        db.session.rollback()
        if 'already exists' not in str(exc).lower():
            raise
        app.logger.info(
            'db.create_all raced another worker (%s); continuing',
            str(exc).split('\n')[0][:120],
        )
    ensure_credit_purchases_table()
    ensure_transcript_shares_table()
    ensure_email_sent_log_table()
    ensure_summary_email_tables()
    ensure_trial_budget_days_table()
    ensure_password_reset_tokens_table()
    oauth_server_mod.ensure_oauth_tables()

    apply_column_migrations()
    raise_60_minute_trial_cohort()
    # Admin dashboard indexes: only after columns exist (same rule as
    # credit_purchases payment_intent index). Additive IF NOT EXISTS.
    ensure_admin_indexes(db)
    if STRIPE_MANAGED_PAYMENTS and STRIPE_AUTOMATIC_TAX:
        app.logger.warning(
            'STRIPE_MANAGED_PAYMENTS and STRIPE_AUTOMATIC_TAX are both on; '
            'Managed Payments wins and automatic_tax is not sent')
    if STRIPE_AUTOMATIC_TAX and not STRIPE_PRICE_ID:
        app.logger.warning(
            'STRIPE_AUTOMATIC_TAX is on but STRIPE_PRICE_ID is unset; '
            'prefer a Dashboard Price with tax_behavior set explicitly')
    inspector = sa_inspect(db.engine)

    # Transcription runs in a daemon thread, so a deploy restart leaves tasks
    # mid-flight. Re-queue each orphaned task once (reservation kept); a second
    # failure after resume gets a clear server_restart message. Live jobs owned
    # by another worker keep heartbeating and are skipped. Two gunicorn workers
    # race the same claim; resume_attempts is the conditional UPDATE.
    install_shutdown_handlers()
    if _SERVER_STARTED_AT_ENV:
        # Real server boot (gunicorn.conf.py): orphan = heartbeat older than
        # this server generation, so a lone respawned worker never steals a
        # live job from its sibling.
        resume_interrupted_tasks()
    else:
        # Any other importer (ops scripts, one-offs) must never claim or
        # spawn transcription work: its own start time says nothing about
        # whether gunicorn's jobs are alive. Keep the old, threshold-based
        # stale sweep only.
        _sweep_stale_tasks(source='boot')

    settle_stranded_charges()

    # Backstop when nobody is polling: a hung mid-chunk job with a closed tab
    # used to sit until the next deploy. Interval is coarse; the hang budget
    # on Whisper is what bounds how long a live call may stay quiet.
    start_stale_watchdog()

    # One-time migration: move old transcriptions table to transcription_tasks
    if 'transcriptions' in inspector.get_table_names():
        rows = db.session.execute(text(
            'SELECT id, user_id, episode_title, rss_url, language, transcript_text, created_at '
            'FROM transcriptions'
        )).fetchall()
        for row in rows:
            existing = db.session.get(TranscriptionTask, str(row[0]))
            if not existing:
                created = row[6]
                if isinstance(created, str):
                    try:
                        created = datetime.fromisoformat(created)
                    except (ValueError, TypeError):
                        created = datetime.now(timezone.utc)
                task = TranscriptionTask(
                    id=str(uuid.uuid4()),
                    user_id=row[1],
                    episode_title=row[2],
                    rss_url=row[3],
                    language=row[4],
                    transcript_text=row[5],
                    status='completed',
                    progress=100,
                    started_at=created,
                    completed_at=created,
                )
                db.session.add(task)
        db.session.commit()
        db.session.execute(text('DROP TABLE transcriptions'))
        db.session.commit()

if __name__ == '__main__':
    print("=" * 50)
    print("PODCAST TRANSCRIBER WEB APP")
    print("=" * 50)
    print(f"OpenAI API Key (global): {'Yes' if GLOBAL_OPENAI_KEY else 'No'}")
    if trial_available():
        daily_bit = (
            f"{TRIAL_DAILY_SECONDS // 60} min/day Oslo "
            f"(~${TRIAL_DAILY_SECONDS / 60 * WHISPER_COST_PER_MINUTE:.2f}/day)"
        )
        life_bit = (
            f", lifetime safety {TRIAL_GLOBAL_SECONDS // 60} min"
            if TRIAL_GLOBAL_SECONDS > 0 else ', lifetime safety off'
        )
        print(f"Trial: {NEW_USER_TRIAL_SECONDS // 60} min/new account "
              f"(NULL limit → {TRIAL_DEFAULT_SECONDS // 60}), "
              f"{daily_bit}{life_bit}")
    else:
        print("Trial: off (no global key, or TRIAL_ENABLED=0)")
    print(f"Environment: {os.getenv('FLASK_ENV', 'development')}")
    print("=" * 50)

    host = '0.0.0.0' if os.getenv('FLASK_ENV') == 'production' else '127.0.0.1'
    debug = os.getenv('FLASK_ENV') != 'production'
    app.run(debug=debug, host=host, port=5002)
