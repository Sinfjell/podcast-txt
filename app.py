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
import sqlite3
import shutil
import subprocess
import ssl
import time
import threading
import unicodedata
import wave
from datetime import date, datetime, timezone
from functools import wraps
from zoneinfo import ZoneInfo
import certifi
import requests
import feedparser

# Fix CA bundle path for Python 3.14+ where certifi may ship without the PEM
if not os.path.exists(certifi.where()):
    _sys_ca = ssl.get_default_verify_paths().cafile
    if _sys_ca and os.path.exists(_sys_ca):
        os.environ.setdefault('REQUESTS_CA_BUNDLE', _sys_ca)
        os.environ.setdefault('SSL_CERT_FILE', _sys_ca)
from flask import (Flask, render_template, request, jsonify, send_file, flash,
                   redirect, url_for, Response, g, session, has_request_context)
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from urllib.parse import urljoin, urlparse, unquote
import uuid
from dotenv import load_dotenv
from openai import OpenAI, APIConnectionError, APITimeoutError

from sqlalchemy import (event as sa_event, func as sa_func, inspect as sa_inspect, text,
                        update as sa_update, or_ as sa_or_)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError as SaIntegrityError

from models import (db, User, SavedFeed, TranscriptionTask, CreditPurchase,
                    TASK_COLUMN_MIGRATIONS, USER_COLUMN_MIGRATIONS,
                    CREDIT_PURCHASE_COLUMN_MIGRATIONS)
from observability import init_sentry, report_stale_task, report_task_failure
import analytics as product_analytics

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
    return db.session.get(User, int(user_id))


#: Canonical public origin. url_for(_external=True) builds from the request,
#: which behind Plesk's nginx is http:// on an https site -- so the canonical
#: link, the sitemap and the JSON-LD @id all pointed at URLs that 301 away.
#: Set explicitly rather than trusting X-Forwarded-*: those headers are only as
#: trustworthy as the proxy stripping them, and this needs no such assumption.
PUBLIC_BASE_URL = (os.getenv('PUBLIC_BASE_URL') or '').rstrip('/')


def public_url(endpoint, **values):
    """Absolute URL for `endpoint`, on the configured public origin."""
    path = url_for(endpoint, **values)
    if PUBLIC_BASE_URL:
        return PUBLIC_BASE_URL + path
    return url_for(endpoint, _external=True, **values)


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


def openai_whisper_cost_usd(minutes):
    """Rough USD cost at OpenAI's Whisper rate for `minutes` of audio."""
    return round(float(minutes) * WHISPER_COST_PER_MINUTE, 2)


#: Session key for an episode an anonymous visitor picked before signing up.
#: Cleared after resume (success or failure) so a stale stash cannot fire later.
PENDING_TRANSCRIPTION_KEY = 'pending_transcription'
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


def episode_needs_own_key(duration_min, remaining_minutes, paid_minutes=0):
    """True when a platform user cannot cover this episode on free+paid minutes.

    No estimate → no badge. Own-key users pass remaining_minutes=None.
    Episodes over the free per-episode cap need paid minutes (or BYOK) covering
    the full length — free trial alone cannot take them.
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
        return estimate > paid_sec
    return estimate > free_sec + paid_sec


def trial_badge_remaining_minutes():
    """Free trial minutes left for the needs-own-key badge, or None to hide it.

    Anonymous visitors see the new-account grant. Logged-in trial users see
    what they have left. Own-key users get None (badge hidden). Paid minutes
    are exposed separately via trial_badge_paid_minutes().
    """
    if current_user.is_authenticated and current_user.openai_api_key:
        return None
    if not trial_available() and not (
            current_user.is_authenticated and paid_balance_seconds(current_user) > 0):
        return None
    if current_user.is_authenticated:
        if trial_available():
            return trial_status(current_user)[2] // 60
        return 0
    if trial_available():
        return TRIAL_DEFAULT_SECONDS // 60
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
# up before _fail_if_stale() presumes the task dead. Sized against the 900s floor
# rather than the (larger) per-task window, so it holds for every task: 420 x 2
# attempts = 840s. Note httpx reads a bare float as a per-operation timeout, not
# a wall-clock total, so this is a close approximation and not a hard ceiling --
# retry backoff eats a few seconds of the margin. A steadily-progressing upload
# never trips it: 24 MB (the chunk cap) over 420s is only 57 KB/s.
WHISPER_TIMEOUT_SECONDS = 420.0
WHISPER_MAX_RETRIES = 1

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


def build_openai_client(key):
    """Wrap a raw key in a configured OpenAI client, or None if there is no key."""
    if not key:
        return None
    return OpenAI(
        api_key=key,
        timeout=WHISPER_TIMEOUT_SECONDS,
        max_retries=WHISPER_MAX_RETRIES,
    )


# ---------------------------------------------------------------------------
# Trial metering
# ---------------------------------------------------------------------------
#
# A user with their own OpenAI key spends their own quota and is never metered.
# Everyone else transcribes on OUR key, which is real money -- so every second
# of audio is reserved against a per-account allowance before any request
# reaches Whisper, and a global ceiling caps what the whole service can spend
# no matter how many accounts exist.


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


#: Free audio minutes granted to an account with no key of its own.
TRIAL_DEFAULT_SECONDS = _env_minutes('TRIAL_MINUTES', 180) * 60
#: Hard ceiling on trial minutes across ALL accounts. Without this, the per-user
#: cap bounds nothing -- signups are free, so N accounts cost N x the grant.
#: Lifetime (not daily/monthly): nothing resets it on a schedule. Counts minutes
#: actually spent; a refused/failed-before-Whisper job releases its reservation.
TRIAL_GLOBAL_SECONDS = _env_minutes('TRIAL_GLOBAL_MINUTES', 1800) * 60
#: What to reserve when the feed publishes no itunes:duration. Reconciled
#: against the real duration after download, before a single Whisper call.
TRIAL_UNKNOWN_ESTIMATE_SECONDS = _env_minutes('TRIAL_UNKNOWN_ESTIMATE_MINUTES', 30) * 60
#: Longest single episode the trial will take on. Default tracks TRIAL_MINUTES
#: so a new account's grant can cover one max-length episode; set the env var
#: explicitly if you want them different.
TRIAL_MAX_EPISODE_SECONDS = _env_minutes(
    'TRIAL_MAX_EPISODE_MINUTES', TRIAL_DEFAULT_SECONDS // 60) * 60
#: Kill switch. Set TRIAL_ENABLED=0 to stop handing out our key entirely.
TRIAL_ENABLED = os.getenv('TRIAL_ENABLED', '1').strip().lower() not in ('0', 'false', 'no', 'off')

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
CREDIT_PACK_LABEL = 'Buy 5 hours for $5'
#: Shown under Buy buttons — do not fold into CREDIT_PACK_LABEL (SKU/analytics).
CREDIT_PACK_SUBLINE = 'One-time · 300 min · VAT incl.'
CREDIT_PACK_SKU = 'minutes_300_usd500_v1'
CREDIT_PACK_TAX_BEHAVIOR = STRIPE_TAX_BEHAVIOR if STRIPE_TAX_BEHAVIOR in (
    'inclusive', 'exclusive') else 'inclusive'
#: Soft nudge on the home banner when free+paid remaining is under this.
LOW_BALANCE_MINUTES = 30

if stripe is not None and STRIPE_API_VERSION:
    stripe.api_version = STRIPE_API_VERSION


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


class TaskAbandoned(Exception):
    """Raised when a task was failed out from under the worker still running it."""


def trial_available():
    """Is there a trial to hand out at all?"""
    return bool(TRIAL_ENABLED and GLOBAL_OPENAI_KEY and TRIAL_DEFAULT_SECONDS > 0)


def trial_status(user):
    """(limit, used, remaining) trial seconds for `user`."""
    limit = user.trial_seconds_limit
    if limit is None:
        limit = TRIAL_DEFAULT_SECONDS
    used = user.trial_seconds_used or 0
    return limit, used, max(0, limit - used)


def trial_refusal_scope(user, needed_seconds):
    """Which cap refused a reservation: 'user' if the account is short, else 'global'."""
    _, _, remaining = trial_status(user)
    return 'user' if remaining < needed_seconds else 'global'


def trial_global_used_seconds():
    """Trial seconds spent across every account."""
    return int(db.session.execute(text(
        'SELECT COALESCE(SUM(trial_seconds_used), 0) FROM users'
    )).scalar() or 0)


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


def trial_reserve(user_id, seconds):
    """Atomically reserve `seconds` of allowance. True only if granted.

    Deliberately one statement. Podskrift runs two gunicorn workers, so a
    threading.Lock would guard one process and let the other one through;
    SQLite serialises the write, and both the per-user cap and the global
    ceiling are evaluated inside it. Two parallel starts therefore cannot
    both be told there is room that only one of them can have.
    """
    seconds = int(math.ceil(seconds))
    if seconds <= 0:
        return True
    result = db.session.execute(text("""
        UPDATE users
           SET trial_seconds_used = COALESCE(trial_seconds_used, 0) + :n
         WHERE id = :uid
           AND COALESCE(trial_seconds_used, 0) + :n
               <= COALESCE(trial_seconds_limit, :default_limit)
           AND (SELECT COALESCE(SUM(trial_seconds_used), 0) FROM users) + :n
               <= :global_limit
    """), {'n': seconds, 'uid': user_id,
           'default_limit': TRIAL_DEFAULT_SECONDS,
           'global_limit': TRIAL_GLOBAL_SECONDS})
    db.session.commit()
    return result.rowcount == 1


def trial_release(user_id, seconds):
    """Hand back reserved seconds that were never spent."""
    seconds = int(seconds)
    if seconds <= 0:
        return
    db.session.execute(text("""
        UPDATE users
           SET trial_seconds_used = MAX(0, COALESCE(trial_seconds_used, 0) - :n)
         WHERE id = :uid
    """), {'n': seconds, 'uid': user_id})
    db.session.commit()


def paid_reserve(user_id, seconds):
    """Atomically debit paid credit-pack seconds. True only if granted.

    Not subject to the free-trial global ceiling — the user already paid.
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


def platform_reserve(user_id, seconds, *, paid_only=False):
    """Reserve platform minutes: free trial first, then paid.

    Returns (trial_seconds, paid_seconds) on success, or None if the full
    amount cannot be covered. `paid_only` skips the free trial (used for
    episodes over the free per-episode cap — those must not burn free minutes).

    Paid minutes are never limited by TRIAL_GLOBAL_SECONDS.
    """
    seconds = int(math.ceil(seconds))
    if seconds <= 0:
        return (0, 0)

    if paid_only:
        return (0, seconds) if paid_reserve(user_id, seconds) else None

    if trial_reserve(user_id, seconds):
        return (seconds, 0)

    user = db.session.get(User, user_id)
    if user is None:
        return None
    _, _, trial_rem = trial_status(user)
    global_rem = max(0, TRIAL_GLOBAL_SECONDS - trial_global_used_seconds())
    trial_take = min(seconds, trial_rem, global_rem)
    if trial_take > 0 and not trial_reserve(user_id, trial_take):
        trial_take = 0
    paid_need = seconds - trial_take
    if paid_need > 0 and not paid_reserve(user_id, paid_need):
        if trial_take:
            trial_release(user_id, trial_take)
        return None
    return (trial_take, paid_need)


def platform_release(user_id, trial_seconds, paid_seconds):
    """Hand back a platform reservation that never reached Whisper spend."""
    trial_release(user_id, trial_seconds or 0)
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


def trial_refund_task(task):
    """Refund the part of a failed task we did not actually spend.

    Chunks already sent to Whisper were billed to us whatever happens to the
    task afterwards, so refunding the whole reservation would hand back money
    that is gone -- and the stale sweeper fires on tasks whose worker is often
    several chunks in. Refund the unstarted remainder instead, pro-rata on
    chunk progress. Safe to call repeatedly: the conditional UPDATE on the
    task is what decides which caller may move the balance.

    Spend is applied to free trial first, then paid (matching charge order).
    Unspent minutes are released to the same buckets.
    """
    # Read the row rather than trusting the caller's copy. /status hands us an
    # object loaded at the top of the request; if the worker reconciled the
    # charge in between, a claim against the stale value silently matches
    # nothing and the user forfeits the allowance with no path to get it back.
    row = db.session.execute(text(
        'SELECT user_id, trial_seconds_charged, paid_seconds_charged, '
        'chunk_total, chunk_index, trial_settled '
        'FROM transcription_tasks WHERE id = :tid'
    ), {'tid': task.id}).first()
    if row is None:
        return 0
    user_id, trial_charged, paid_charged, chunk_total, chunk_index, settled = row
    trial_charged = int(trial_charged or 0)
    paid_charged = int(paid_charged or 0)
    total_charged = trial_charged + paid_charged
    if settled or total_charged <= 0:
        return 0

    if (chunk_total or 0) > 0 and chunk_index is not None:
        # chunk_index is written immediately BEFORE that chunk is uploaded, so
        # index k means k+1 chunks have been sent and billed to us. Rounding
        # toward charging is deliberate: refunding a chunk that did reach
        # Whisper is exactly how a swept single-chunk episode -- every episode
        # under 24 MB, so the common case -- came out free.
        started = min(chunk_total, max(0, chunk_index) + 1)
        spent = int(total_charged * started / chunk_total)
    else:
        spent = 0  # nothing reached Whisper yet

    spent_trial = min(trial_charged, spent)
    spent_paid = spent - spent_trial

    # Settling is the claim, and it also pins the amounts we read.
    if not _claim_task_platform_charges(
            task.id, trial_charged, paid_charged, spent_trial, spent_paid,
            settle=True):
        return 0
    refund_trial = trial_charged - spent_trial
    refund_paid = paid_charged - spent_paid
    platform_release(user_id, refund_trial, refund_paid)
    return refund_trial + refund_paid


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
    spend. Raises TrialExhausted when the real length will not fit.

    Charge order: free trial first, then paid. Episodes over the free
    per-episode cap must be covered by paid minutes (or BYOK) — they do not
    burn free trial, and paid is not subject to the global free-trial ceiling.
    """
    task = db.session.get(TranscriptionTask, task_id)
    if not task or task.trial_settled:
        return
    trial_reserved = int(task.trial_seconds_charged or 0)
    paid_reserved = int(task.paid_seconds_charged or 0)
    if trial_reserved <= 0 and paid_reserved <= 0:
        # NULL/0 on both is an own-key task, nothing metered. Settled means the
        # sweeper got here first -- re-opening the charge would bill the user
        # for an episode that goes on to send nothing.
        return
    total_reserved = trial_reserved + paid_reserved
    actual = int(math.ceil(max(0.0, actual_seconds or 0.0)))
    user_id = task.user_id
    over_free_cap = bool(
        TRIAL_MAX_EPISODE_SECONDS and actual > TRIAL_MAX_EPISODE_SECONDS)

    if over_free_cap and paid_reserved <= 0 and trial_reserved > 0:
        # Started as a free-trial job; real audio exceeds the free per-episode
        # cap. Paid credits can still save it if they cover the full length;
        # otherwise refuse without mutating the reservation — the worker's
        # refund settles, matching the pre-credits behaviour.
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
        if paid_balance_seconds(owner) < actual:
            raise TrialExhausted(refuse_msg, scope='episode_length')
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
        trial_release(user_id, trial_reserved)
        _capture_minutes_exhausted_if_depleted(
            user_id, 'reconcile', before_trial, before_paid)
        return

    if actual > total_reserved:
        extra = actual - total_reserved
        before_trial, before_paid = _platform_remaining_seconds(user_id)
        split = platform_reserve(user_id, extra, paid_only=over_free_cap)
        if split is None:
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
            platform_release(user_id, extra_trial, extra_paid)
        else:
            _capture_minutes_exhausted_if_depleted(
                user_id, 'reconcile', before_trial, before_paid)
    elif actual < total_reserved:
        # Shrink paid first (LIFO), then trial — reverse of charge order.
        shrink = total_reserved - actual
        new_paid = max(0, paid_reserved - shrink)
        shrink_paid = paid_reserved - new_paid
        shrink_trial = shrink - shrink_paid
        new_trial = trial_reserved - shrink_trial
        if _claim_task_platform_charges(
                task_id, trial_reserved, paid_reserved, new_trial, new_paid):
            platform_release(user_id, shrink_trial, shrink_paid)


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


def is_disposable_email(email):
    return email.rsplit('@', 1)[-1].lower() in DISPOSABLE_EMAIL_DOMAINS


# ---------------------------------------------------------------------------
# API key handling
# ---------------------------------------------------------------------------

def _is_openai_error(exc):
    """True for exceptions raised by the OpenAI SDK, which must never be shown raw."""
    return type(exc).__module__.split('.')[0] == 'openai'


def describe_openai_error(exc, context='transcription'):
    """Turn an OpenAI SDK exception into something a human can act on.

    Users were shown the raw error JSON, which is both unreadable and unsafe:
    OpenAI echoes the submitted key back in 401s, and people paste passwords
    into that field, so the raw text put a third party's password in our
    database. Never surface the provider's message verbatim.
    """
    status = getattr(exc, 'status_code', None)
    if status == 401:
        return ('OpenAI rejected this key. It may have been deleted, or copied '
                'incompletely. Create a new one at platform.openai.com/api-keys '
                'and paste the whole thing.')
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
        return ('OpenAI refused the job: your OpenAI account has no credit left. '
                'Add credit at platform.openai.com/account/billing — it can take a '
                'minute to activate — then retry this episode. Nothing was charged '
                'by Podskrift.')
    if status == 403:
        return ('Your OpenAI key is not allowed to use the Whisper API. Check its '
                'permissions at platform.openai.com.')
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
#: _stale_after_seconds().
STALE_TASK_SECONDS = 15 * 60


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
                timeout=30, allow_redirects=False,
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

    try:
        response = _fetch(headers)
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else None
        if status == 403:
            try:
                response = _fetch({'User-Agent': 'podcast-downloader/1.0', 'Accept': '*/*'})
            except requests.exceptions.HTTPError as e2:
                status2 = e2.response.status_code if e2.response is not None else 'unknown'
                raise Exception(
                    f"Access denied ({status2}) for audio file. "
                    "This podcast may restrict direct downloads."
                )
            except requests.exceptions.RequestException as e2:
                raise Exception(f"Failed to download audio: {e2}")
        else:
            raise Exception(f"HTTP error {status}" if status else f"HTTP error: {e}")
    except requests.exceptions.RequestException as e:
        raise Exception(f"Failed to download audio: {e}")

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
#: floor or a live job gets swept and the user is told the server restarted.
#: ffmpeg runs at roughly 100x realtime here, so this is generous.
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


def _ffmpeg_error(message, stderr=b''):
    """Log ffmpeg's own words, hand the user something they can act on."""
    detail = (stderr or b'').decode('utf-8', 'replace').strip()
    if detail:
        app.logger.error('ffmpeg failed: %s', detail[:2000])
    return RuntimeError(message)


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
        raise _ffmpeg_error('This episode took too long to process. Please try a shorter one.')

    parts = sorted(glob.glob(produced_glob))
    if result.returncode != 0 or not parts:
        _cleanup_glob(produced_glob)
        raise _ffmpeg_error(
            'This audio file could not be processed. It may be corrupt or in an '
            'unsupported format.', result.stderr)

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

    # Last point at which refusing is still free: everything below this line
    # bills OpenAI.
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


def _transcribe_chunks(audio_chunks, remaining, task_id, openai_client, language,
                       audio_duration):
    """Send each chunk to Whisper, publishing partial text as it goes.

    Returns (full_text, segments). `remaining` is mutated as chunks are consumed
    so the caller can clean up whatever is left if this raises.
    """
    all_segments = []
    full_text = ""

    for i, chunk_file in enumerate(audio_chunks):
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
            status=f'transcribing chunk {i + 1}/{len(audio_chunks)}',
            chunk_index=i,
            phase_started_at=datetime.now(timezone.utc),
            progress=_transcribe_checkpoint(i, len(audio_chunks)),
        ):
            raise TaskAbandoned(
                'Task was marked failed between chunks; stopping so it cannot '
                'keep billing against an allowance already refunded.'
            )

        with open(chunk_file, 'rb') as f:
            create_kwargs = {
                'model': "whisper-1",
                'file': f,
                'response_format': "verbose_json",
                'timestamp_granularities': ["segment"],
            }
            # Omitting `language` entirely is what makes Whisper auto-detect;
            # passing None or '' is rejected by the API.
            if language:
                create_kwargs['language'] = language
            chunk_transcript = openai_client.audio.transcriptions.create(**create_kwargs)

        full_text += chunk_transcript.text + " "

        if hasattr(chunk_transcript, 'segments') and chunk_transcript.segments:
            chunk_dur = audio_duration / len(audio_chunks)
            offset = i * chunk_dur
            for seg in chunk_transcript.segments:
                all_segments.append({
                    'start': seg.start + offset,
                    'end': seg.end + offset,
                    'text': seg.text
                })

        detected = getattr(chunk_transcript, 'language', None)
        # Publish the text we have so far so the page can show it streaming in
        # instead of an empty box. History only lists completed tasks, so a
        # partial write here is never user-visible as a finished transcript.
        _update_task(
            task_id,
            transcript_text=full_text.strip(),
            progress=_transcribe_checkpoint(i + 1, len(audio_chunks)),
            language=language or detected or None,
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
            resp = requests.get(
                rss_url,
                timeout=timeout,
                headers={
                    'User-Agent': (
                        'Mozilla/5.0 (compatible; Podskrift/1.0; +https://podskrift.com)'
                    ),
                    'Accept': 'application/rss+xml, application/xml, text/xml, */*',
                },
            )
            resp.raise_for_status()
            feed = feedparser.parse(resp.content)
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
        trial_minutes=(TRIAL_DEFAULT_SECONDS // 60 if trial_available() else None),
        pending=pending,
        **extra,
    )


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

    if request.method != 'POST':
        return _register_template()

    email = request.form.get('email', '').strip().lower()
    password = request.form.get('password', '')

    if not email or not password:
        flash('Email and password are required.', 'error')
        return _register_template()

    ip = _client_ip()
    slot = register_reserve_slot(ip)
    if slot is None:
        flash('Too many accounts created from this address. Try again later.', 'error')
        return _register_template()

    # The slot is held for the rest of this request and released unless an
    # account is actually created, so validation failures cost the user nothing
    # while a parallel burst still cannot exceed the limit.
    created = False
    try:
        if is_disposable_email(email):
            flash('Please register with a real email address.', 'error')
            return _register_template()

        if '@' not in email or '.' not in email.rsplit('@', 1)[-1]:
            flash('Please enter a valid email address.', 'error')
            return _register_template()

        if len(password) < 8:
            flash('Password must be at least 8 characters.', 'error')
            return _register_template()

        if User.query.filter_by(email=email).first():
            flash('An account with this email already exists.', 'error')
            return _register_template()

        user = User(email=email)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        created = True

        login_user(user)
        product_analytics.capture('user_signed_up', user.id)
        if session.get(PENDING_TRANSCRIPTION_KEY):
            flash('Account created — starting your transcript.', 'success')
        else:
            minutes = TRIAL_DEFAULT_SECONDS // 60
            flash(
                f'Account created — you have {minutes} free minutes. '
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

    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')

        user = User.query.filter_by(email=email).first()
        if user and user.check_password(password):
            login_user(user, remember=True)
            return _redirect_after_auth()

        flash('Invalid email or password.', 'error')

    return render_template('login.html', next=_auth_next_arg())


def _stash_pending_from_request():
    """Pull episode fields off the request into the session. Returns an error or None."""
    language = request.form.get('language', '')
    if language not in VALID_LANGUAGE_CODES:
        language = ''

    audio_url = (request.form.get('audio_url') or '').strip()
    rss_url = (request.form.get('rss_url') or '').strip() or None
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

    language = pending.get('language') or ''
    rss_url = pending.get('rss_url')
    episode_index = pending.get('episode_index')

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
    """Session CSRF token for billing Buy POSTs.

    Call only from templates that render a Buy form for a logged-in user.
    Minting here writes the session and would otherwise give anonymous
    homepage visitors a cookie for nothing.
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
        '$set': {'has_purchased': True},
    }
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
    return render_template(
        'settings.html',
        trial=trial_ctx,
        new_api_key=new_api_key,
        openai_key_hint=_openai_key_hint(current_user),
        buy_source=buy_source,
        no_billing_warning=no_billing_warning,
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
    ph_sid = (request.form.get('ph_sid') or '').strip()[:128]
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

    params = {
        'mode': 'payment',
        'line_items': [line_item],
        'success_url': success_url,
        'cancel_url': cancel_abs,
        'client_reference_id': str(current_user.id),
        'customer_email': current_user.email,
        'customer_creation': 'always',
        'billing_address_collection': 'required',
'metadata': meta,
        'payment_intent_data': {
            'metadata': pi_meta,
        },
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
        checkout_session = client.v1.checkout.sessions.create(
            params=params,
            options={'idempotency_key': f'checkout-{current_user.id}-{uuid.uuid4()}'},
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
    }
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


# ---------------------------------------------------------------------------
# Saved feeds
# ---------------------------------------------------------------------------

@app.route('/feeds')
@login_required
def feeds():
    user_feeds = SavedFeed.query.filter_by(user_id=current_user.id).order_by(SavedFeed.created_at.desc()).all()
    return render_template('feeds.html', feeds=user_feeds)


@app.route('/feeds/add', methods=['POST'])
@login_required
def add_feed():
    name = request.form.get('name', '').strip()
    rss_url = request.form.get('rss_url', '').strip()

    if not name or not rss_url:
        flash('Name and RSS URL are required.', 'error')
        return redirect(url_for('feeds'))

    existing = SavedFeed.query.filter_by(user_id=current_user.id, rss_url=rss_url).first()
    if existing:
        flash('This feed is already saved.', 'info')
        return redirect(url_for('feeds'))

    feed = SavedFeed(user_id=current_user.id, name=name, rss_url=rss_url)
    db.session.add(feed)
    db.session.commit()
    flash(f'Feed "{name}" saved.', 'success')
    return redirect(url_for('feeds'))


@app.route('/feeds/delete/<int:feed_id>', methods=['POST'])
@login_required
def delete_feed(feed_id):
    feed = SavedFeed.query.filter_by(id=feed_id, user_id=current_user.id).first_or_404()
    db.session.delete(feed)
    db.session.commit()
    flash('Feed removed.', 'success')
    return redirect(url_for('feeds'))


@app.route('/feeds/use/<int:feed_id>')
@login_required
def use_feed(feed_id):
    feed = SavedFeed.query.filter_by(id=feed_id, user_id=current_user.id).first_or_404()
    episodes, error = get_episodes_from_rss(feed.rss_url)
    if error:
        flash(error, 'error')
        return redirect(url_for('feeds'))

    _annotate_episodes_for_trial(episodes)
    episodes_to_show = episodes[:10]
    has_more = len(episodes) > 10
    return render_template(
        'episode_selection.html',
        episodes=episodes_to_show,
        all_episodes=episodes,
        rss_url=feed.rss_url,
        feed_name=feed.name,
        has_more=has_more,
        needs_api_key=_episode_paywall_needed(),
        show_openai_cost=_show_openai_cost_estimates(),
        podcast_name=episodes[0].get('podcast_name') or feed.name,
        artwork=episodes[0].get('artwork') or '',
        languages=language_choices(),
    )


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
    exhausted = (remaining <= 0 and paid_seconds <= 0)
    total_minutes = remaining_minutes + paid_minutes
    low_balance = (
        not on_own_key
        and not exhausted
        and total_minutes < LOW_BALANCE_MINUTES
    )
    return {
        'limit_minutes': limit // 60,
        'used_minutes': used // 60,
        'remaining_minutes': remaining_minutes,
        'paid_minutes': paid_minutes,
        'total_minutes': total_minutes,
        'exhausted': exhausted,
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
            'nav_show_buy': False,
        }
    on_own_key = bool(current_user.openai_api_key)
    if on_own_key:
        return {
            'nav_minutes_left': None,
            'nav_show_buy': False,
        }
    paid_minutes = paid_balance_seconds(current_user) // 60
    if trial_available():
        remaining_minutes = trial_status(current_user)[2] // 60
    else:
        remaining_minutes = 0
    # Always show a pill for metered users so "0 min left" is still obvious.
    return {
        'nav_minutes_left': remaining_minutes + paid_minutes,
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


def _minutes_limit_actions(user_id, location='enqueue', reason=None):
    """CTA payload for out-of-minutes messages: Buy (if configured) + add key.

    Analytics for paywall/offer are browser-side (paywall_shown / offer_shown).
    """
    actions = []
    if stripe_checkout_enabled():
        actions.append({
            'label': CREDIT_PACK_LABEL,
            'url': url_for('billing_checkout'),
            'method': 'POST',
        })
    actions.append({
        'label': 'Add OpenAI key →',
        'url': settings_openai_url(),
        'method': 'GET',
    })
    # Keep the legacy single-action fields pointing at the primary CTA.
    primary = actions[0]
    return {
        'actions': actions,
        'action_url': primary['url'] if primary['method'] == 'GET' else settings_credits_url(),
        'action_label': primary['label'],
        'buy_available': stripe_checkout_enabled(),
        'buy_label': CREDIT_PACK_LABEL if stripe_checkout_enabled() else None,
        'buy_url': url_for('billing_checkout') if stripe_checkout_enabled() else None,
        'paywall_reason': reason,
        'paywall_location': location,
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
    saved_feeds = []
    first_run = False
    if current_user.is_authenticated:
        saved_feeds = SavedFeed.query.filter_by(
            user_id=current_user.id
        ).order_by(SavedFeed.created_at.desc()).limit(5).all()
        # No transcription rows yet → lead with "start your first transcript"
        # instead of a Buy banner (activation before purchase).
        first_run = (
            TranscriptionTask.query.filter_by(user_id=current_user.id)
            .limit(1).first() is None
        )
    return render_template('index.html', saved_feeds=saved_feeds,
                           languages=language_choices(),
                           trial=_trial_context(),
                           first_run=first_run,
                           faq=faq_entries(),
                           trial_minutes=(TRIAL_DEFAULT_SECONDS // 60
                                          if trial_available() else None),
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
    )


def _capture_trial_limit_hit(user_id, scope, stage, source,
                             estimate_min=None, remaining_min=None):
    """The buying signal: a trial user wanted more than the free allowance gives.

    scope: episode_length | user | global. stage: start (refused before the job
    existed) or reconcile (the real audio turned out longer than the feed said).
    """
    props = {'scope': scope, 'stage': stage, 'source': source}
    if estimate_min is not None:
        props['estimate_min'] = int(estimate_min)
    if remaining_min is not None:
        props['remaining_min'] = int(remaining_min)
    product_analytics.capture('trial_limit_hit', user_id, props)


def enqueue_transcription(user, meta, rss_url=None, language='', source='web'):
    """Start Whisper for one episode on behalf of `user`.

    Shared by the UI form and the agent write API so trial reservation,
    admission control, and the worker thread stay one code path. Returns
    ``(payload_dict, http_status)``. On success payload is
    ``{'task_id': ...}``; on refusal it carries ``error``.

    `meta` keys: title, audio_url, podcast_name, artwork, published, duration_min.
    `source` is 'web' or 'api'; it only labels the analytics events.
    """
    if language not in VALID_LANGUAGE_CODES:
        language = ''

    audio_url = (meta.get('audio_url') or '').strip()
    if not audio_url or not _is_fetchable_url(audio_url):
        return {'error': 'That audio URL cannot be fetched.'}, 400

    # Snapshot before the worker thread: callers may pass flask_login's
    # current_user LocalProxy, which is None outside a request context.
    # Evaluating .id inside the thread is what turned completed jobs into
    # status=error with "'NoneType' object has no attribute 'id'".
    user_id = user.id
    api_key, key_source = resolve_openai_key(user)
    if not api_key:
        return {
            'error': 'No OpenAI API key configured. Add your key in Settings.'
        }, 400
    openai_client = build_openai_client(api_key)
    # Every analytics event for this job carries the same two labels.
    ph_props = {'key_source': key_source, 'source': source}

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
        if key_source == 'trial':
            # Floored at a minute: a zero reservation would quietly make the
            # task look unmetered (NULL/0 charges).
            estimate = trial_estimate_seconds(meta.get('duration_min'))
            estimate_min = estimate // 60
            over_free_cap = bool(
                TRIAL_MAX_EPISODE_SECONDS and estimate > TRIAL_MAX_EPISODE_SECONDS)
            before_trial, before_paid = _platform_remaining_seconds(user_id)
            if over_free_cap:
                # Free trial must not cover over-long episodes; paid credits can.
                split = platform_reserve(user_id, estimate, paid_only=True)
                if split is None:
                    _, _, remaining = trial_status(user)
                    _capture_trial_limit_hit(
                        user_id, 'episode_length', 'start', source,
                        estimate_min=estimate_min, remaining_min=remaining // 60)
                    cost = openai_whisper_cost_usd(estimate_min)
                    payload = {
                        'error': (
                            f'This episode is {estimate_min} minutes — too long for the '
                            f'free trial (max {TRIAL_MAX_EPISODE_SECONDS // 60}). '
                            f'{CREDIT_PACK_LABEL if stripe_checkout_enabled() else "Buy more minutes"}, '
                            f'or add your own OpenAI API key (about ${cost} at '
                            f'OpenAI\'s rate).'
                        ),
                    }
                    payload.update(_minutes_limit_actions(
                        user_id, 'enqueue_episode_length', reason='episode_too_long'))
                    return payload, 402
                trial_charge, paid_charge = split
                _capture_minutes_exhausted_if_depleted(
                    user_id, source, before_trial, before_paid)
            else:
                split = platform_reserve(user_id, estimate)
                if split is None:
                    _, _, remaining = trial_status(user)
                    scope = trial_refusal_scope(user, estimate)
                    # If free trial still has room but paid+trial couldn't cover,
                    # the global ceiling ate the free path — keep that scope.
                    if remaining >= estimate and paid_balance_seconds(user) <= 0:
                        scope = 'global'
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
                        message = (
                            f'This episode is about {estimate_min} minutes — longer than '
                            f'the {remaining // 60} free minutes you have left{paid_bit}. '
                            f'Pick a shorter episode, or {buy_bit}'
                            f'add your own OpenAI API key '
                            f'(about ${cost} for this one, billed by OpenAI).'
                        )
                        if remaining <= 0 and paid_left <= 0:
                            paywall_reason = (
                                'paid_exhausted' if before_paid > 0 and before_trial <= 0
                                else 'trial_exhausted')
                        else:
                            paywall_reason = 'low_balance'
                    else:
                        # The user still has room; the service as a whole does not.
                        # Saying "you have 60 minutes left" here would contradict itself.
                        paywall_reason = 'global_cap'
                        if stripe_checkout_enabled():
                            message = (
                                'Podskrift has handed out all the free minutes it has '
                                f'budgeted. {CREDIT_PACK_LABEL}, or add your own OpenAI '
                                'API key in Settings to keep transcribing.'
                            )
                        else:
                            message = (
                                'Podskrift has handed out all the free minutes it has '
                                'budgeted. Add your own OpenAI API key in Settings to '
                                'keep transcribing.'
                            )
                    payload = {'error': message}
                    payload.update(_minutes_limit_actions(
                        user_id, 'enqueue', reason=paywall_reason))
                    return payload, 402
                trial_charge, paid_charge = split
                _capture_minutes_exhausted_if_depleted(
                    user_id, source, before_trial, before_paid)

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
                # Feed duration is the best ETA source we have, and it is available
                # before a single byte is downloaded.
                audio_duration=(meta['duration_min'] * 60) if meta.get('duration_min') else None,
                language=language or None,
                trial_seconds_charged=trial_charge,
                paid_seconds_charged=paid_charge,
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
                    except Exception as e:
                        _update_task(task_id, status='error', phase='error',
                                     error_message=describe_openai_error(e)
                                     if _is_openai_error(e) else str(e))
                        # A job that never produced a transcript must not consume the
                        # trial allowance it reserved.
                        failed = db.session.get(TranscriptionTask, task_id)
                        if failed:
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
                            elif _is_openai_error(e):
                                reason = product_analytics.openai_fail_reason(e)
                            else:
                                reason = 'other'
                            product_analytics.capture(
                                'transcript_failed',
                                failed.user_id,
                                {**ph_props, 'reason': reason},
                            )
                        # After the refund, and it cannot raise: reporting must
                        # never cost a user the allowance they are owed.
                        report_task_failure(e, task_id=task_id, key_source=key_source)
                    else:
                        # Outside the except that marks the job failed: analytics
                        # must never be able to turn a completed transcript into
                        # status=error (and must not trigger a trial refund).
                        # user_id is a plain int snapshotted before the thread;
                        # capture() also swallows, but keep a local guard so a
                        # broken monkeypatch / SDK cannot fail the job either.
                        try:
                            product_analytics.capture(
                                'transcript_completed', user_id, ph_props)
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
    language = request.form.get('language', '')
    if language not in VALID_LANGUAGE_CODES:
        language = ''

    audio_url = request.form.get('audio_url')
    rss_url = request.form.get('rss_url')

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


def _stale_after_seconds(task):
    """How long this particular task may stay quiet before it is presumed dead.

    The heartbeat is written per chunk, so the window has to clear the slowest
    plausible single chunk. A 24 MB chunk of 64 kbps audio is ~50 minutes long,
    and if Whisper degrades to ~1.5x realtime that one chunk runs for over half
    an hour -- a flat 15-minute window would kill a job that is very much alive.
    """
    if task.chunk_total and task.audio_duration:
        per_chunk = task.audio_duration / task.chunk_total / WHISPER_REALTIME_FACTOR
        return max(STALE_TASK_SECONDS, per_chunk * 8)
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


def _fail_if_stale(task):
    """Fail a task whose worker has stopped writing progress.

    The boot sweep alone is not enough: a task orphaned by a restart has a
    heartbeat only seconds old, so the sweep on that same boot skips it and
    nothing runs again afterwards. Checking here means the page polling the
    task is what notices, which is exactly where the user is waiting.
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
    # transcript into "the server restarted, please try again" while keeping the
    # full charge, which invites a paid re-run. The live job bar polls this from
    # every open tab, so it fires roughly 15x more often than it used to.
    claimed = db.session.execute(text("""
        UPDATE transcription_tasks
           SET status = 'error', phase = 'error', error_message = :message
         WHERE id = :tid AND status NOT IN ('completed', 'error', 'cancelled')
    """), {
        'tid': task.id,
        'message': ('Transcription stopped making progress, most likely because '
                    'the server restarted. Please try again.'),
    }).rowcount == 1
    db.session.commit()
    db.session.expire(task)
    if not claimed:
        return False
    trial_refund_task(task)
    report_stale_task(task.id, last_status, quiet, source='poll')
    return True


@app.route('/status/<task_id>')
@login_required
def get_status(task_id):
    task = db.session.get(TranscriptionTask, task_id)
    if not task or task.user_id != current_user.id:
        return jsonify({'error': 'Task not found'}), 404

    _fail_if_stale(task)
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
        # Boolean only — the result page uses this to offer Follow when the
        # async related-episodes fetch times out or fails.
        'has_rss': bool((task.rss_url or '').strip()),
    }

    if task.status == 'cancelled':
        result['cancelled'] = True
    elif task.status == 'error':
        err = task.error_message or 'Unknown error'
        result['error'] = err
        if _is_no_billing_error(err):
            # Boolean only — never echo provider text or key material.
            result['no_billing'] = True
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
                    'duration_min': (
                        str(duration_min) if duration_min is not None else ''),
                    'language': task.language or '',
                }
            if stripe_checkout_enabled():
                result['buy_available'] = True
                result['buy_label'] = CREDIT_PACK_LABEL
        elif _is_minutes_limit_error(err):
            result['minutes_error'] = True
            reason = 'trial_exhausted'
            low = err.lower()
            if 'too long for the free trial' in low:
                reason = 'episode_too_long'
            elif 'handed out all the free minutes' in low:
                reason = 'global_cap'
            elif 'paid minutes you have left' in low:
                reason = 'paid_exhausted'
            elif 'minutes you have left' in low:
                reason = 'low_balance'
            result.update(_minutes_limit_actions(
                task.user_id, 'reconcile_status', reason=reason))

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
            result['language'] = task.language

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

    safe_title = task.episode_title.replace(' ', '_')

    if file_type == 'txt':
        content = (task.transcript_text or '').encode('utf-8')
        return send_file(
            BytesIO(content),
            as_attachment=True,
            download_name=f"{safe_title}.txt",
            mimetype='text/plain',
        )
    elif file_type == 'srt':
        if task.segments_json:
            segments = _json.loads(task.segments_json)
            lines = []
            for i, seg in enumerate(segments, 1):
                lines.append(f"{i}")
                lines.append(f"{format_timestamp(seg['start'])} --> {format_timestamp(seg['end'])}")
                lines.append(seg['text'].strip())
                lines.append('')
            content = '\n'.join(lines).encode('utf-8')
        else:
            content = f"1\n00:00:00,000 --> 00:00:01,000\n{task.transcript_text or ''}".encode('utf-8')
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
    return render_template('transcription.html', task_id=task_id)


def _task_owned_or_404(task_id):
    """Return the caller's TranscriptionTask, or a (json, status) error pair."""
    task = db.session.get(TranscriptionTask, task_id)
    if not task or task.user_id != current_user.id:
        return None, (jsonify({'error': 'Task not found'}), 404)
    return task, None


def _feed_already_saved(user_id, rss_url):
    if not rss_url:
        return False
    return SavedFeed.query.filter_by(user_id=user_id, rss_url=rss_url).first() is not None


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
    if not rss_url:
        return jsonify({
            'has_feed': False,
            'podcast_name': podcast_name,
            'following': False,
            'episodes': [],
        })

    following = _feed_already_saved(current_user.id, rss_url)
    episodes, error = get_episodes_from_rss(
        rss_url, timeout=RELATED_EPISODES_TIMEOUT)
    if error or not episodes:
        return jsonify({
            'has_feed': True,
            'podcast_name': podcast_name,
            'following': following,
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
        'following': following,
        'episodes': others,
    })


@app.route('/transcription/<task_id>/follow', methods=['POST'])
@login_required
def follow_task_podcast(task_id):
    """One-click save of the task's RSS feed into Saved Feeds (My Feeds)."""
    task, err = _task_owned_or_404(task_id)
    if err:
        return err

    rss_url = (task.rss_url or '').strip()
    if not rss_url:
        return jsonify({'error': 'No feed URL on this transcript'}), 400

    name = (task.podcast_name or '').strip() or 'Podcast'
    existing = SavedFeed.query.filter_by(
        user_id=current_user.id, rss_url=rss_url).first()
    if existing:
        return jsonify({'following': True, 'already': True, 'feed_id': existing.id})

    feed = SavedFeed(user_id=current_user.id, name=name, rss_url=rss_url)
    db.session.add(feed)
    db.session.commit()
    return jsonify({'following': True, 'already': False, 'feed_id': feed.id})


@app.route('/history')
@login_required
def history():
    query = ' '.join(request.args.get('q', '').split())[:TRANSCRIPT_QUERY_MAX]
    if query:
        return render_template('history.html', query=query,
                               matches=search_transcripts(current_user.id, query),
                               transcriptions=[], total_cost=0,
                               cost_per_minute=WHISPER_COST_PER_MINUTE)
    tasks = TranscriptionTask.query.filter_by(
        user_id=current_user.id, status='completed'
    ).order_by(TranscriptionTask.completed_at.desc()).limit(50).all()
    total_cost = sum(
        (t.audio_duration / 60) * WHISPER_COST_PER_MINUTE
        for t in tasks if t.audio_duration
    )
    return render_template('history.html', transcriptions=tasks, query='',
                           total_cost=total_cost,
                           cost_per_minute=WHISPER_COST_PER_MINUTE)


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
    """User row for API writes (customer key owner or CoS agent scope)."""
    kind = getattr(g, 'api_auth_kind', None)
    if kind == 'customer':
        uid = getattr(g, 'api_user_id', None)
        user = db.session.get(User, uid) if uid is not None else None
        if user is None:
            # Key was revoked or account deleted between auth and write.
            return None, {'error': 'Unauthorized'}, 401
        return user, None, None
    return _agent_write_user()


def _api_write_rate_limit_ok():
    """True if this process still has room for another API write.

    CoS agent shares one bucket; each customer is limited separately so one
    account cannot starve another inside the same gunicorn worker.
    """
    now = time.time()
    kind = getattr(g, 'api_auth_kind', None)
    if kind == 'customer':
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
                results, err = resolve_spotify_url(url)
            except requests.RequestException:
                return None, "Couldn't reach the podcast directory. Try again in a moment."
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
    TRIAL_DEFAULT_SECONDS. A first version hardcoded 60 minutes and would have
    kept saying so after the grant changed.
    """
    minutes = TRIAL_DEFAULT_SECONDS // 60
    hourly = f'${openai_whisper_cost_usd(60):.2f}'
    cost_90 = f'${openai_whisper_cost_usd(90):.2f}'
    # trial_available() is the predicate the code actually enforces. Copy that
    # promises free minutes while the kill switch is on is a promise the app
    # then refuses at /start_transcription.
    if trial_available():
        hours_bit = (f' (about {minutes // 60} hours)' if minutes >= 120 else '')
        pack_bit = (
            f' Or buy a one-time pack: ${CREDIT_PACK_AMOUNT_CENTS / 100:.0f} for '
            f'{CREDIT_PACK_MINUTES} minutes ({CREDIT_PACK_MINUTES // 60} hours), '
            f'VAT included, paid via Stripe — no subscription.'
            if stripe_checkout_enabled() else ''
        )
        free = (f'The free trial covers {minutes} minutes of audio in total'
                f'{hours_bit}. For longer episodes, or once the '
                f'trial is used up, add your own OpenAI API key: a 90-minute '
                f'episode costs about {cost_90} at OpenAI\'s rate.{pack_bit}')
        need_key = ('Not to start. The free trial runs on ours. Add your own key when the '
                    'trial runs out and there is no limit beyond what you spend at OpenAI.')
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
            if trial_available() else '')
         + 'Curl examples and status codes: /docs/api.'),
    ]


@app.context_processor
def inject_language_count():
    """One number for every surface that quotes it. It was hardcoded in three
    meta tags beside a comment claiming the derived form existed so they could
    not drift."""
    return {'language_count': len(LANGUAGE_ENGLISH_NAMES)}


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
        'credit_pack_minutes': CREDIT_PACK_MINUTES,
        'credit_pack_price_usd': f'{CREDIT_PACK_AMOUNT_CENTS / 100:.0f}',
        'nav_minutes_left': nav['nav_minutes_left'],
        'nav_show_buy': nav['nav_show_buy'],
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
                        (f'{TRIAL_DEFAULT_SECONDS // 60} minutes of audio free on signup. '
                         'After that, ' if trial_available() else '')
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


@app.route('/robots.txt')
def robots_txt():
    """Explicit crawler policy.

    There was no robots.txt at all, which leaves every crawler guessing. The
    assistant referrals are a real channel here -- roughly 25 visits a month
    arrive from ChatGPT with nothing on the site written for them -- so the
    bots behind that channel are allowed by name rather than by omission.
    """
    disallow = ['Disallow: ' + path for path in (
        '/settings', '/history', '/feeds', '/transcription/', '/download/',
        '/api/', '/status/', '/active-jobs', '/cancel/',
    )]
    lines = [
        '# Podskrift -- podcast transcription',
        '# Full policy and a plain-language summary of the site: /llms.txt',
        '',
        'User-agent: *',
        'Allow: /',
        '',
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
        lines += [f'User-agent: {agent}', 'Allow: /'] + disallow + ['']
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
    signup_blurb = (f'free account, {TRIAL_DEFAULT_SECONDS // 60} trial minutes'
                    if trial_available() else 'free account, bring your own OpenAI key')
    cost = (f'New accounts get {TRIAL_DEFAULT_SECONDS // 60} minutes of audio free on '
            "Podskrift's own OpenAI key.\nAfter that you"
            if trial_available() else 'You')
    pack = (
        f'\nOr buy a one-time pack: ${CREDIT_PACK_AMOUNT_CENTS / 100:.0f} for '
        f'{CREDIT_PACK_MINUTES} minutes ({CREDIT_PACK_MINUTES // 60} hours), VAT '
        f'included, paid via Stripe — no subscription.'
        if stripe_checkout_enabled() else ''
    )
    body = f"""# Podskrift

> Transcribes podcast episodes to text using OpenAI Whisper, in {len(LANGUAGE_ENGLISH_NAMES)}
> languages. Works with any podcast in any of them -- search by show or episode
> name, no file upload and no feed URL needed.

Podskrift is a free web tool. You search for a podcast or an individual episode
by name, pick the episode, and it downloads the audio and returns the full
transcript plus timestamped subtitles. There is no file to upload and no RSS
feed to track down first -- though you can paste a feed URL if the podcast is
not in the search index.

Made by Nettsmed (Fjellestad AS), Kristiansand, Norway.

## What it does
- Search podcast catalogues by show name or by individual episode title
- Transcribe an episode to plain text (.txt) and SubRip subtitles (.srt)
- Pick the spoken language explicitly, or let Whisper detect it
- Follow progress live; the job keeps running if you close the page

## Languages
{languages}

Auto-detect is the default, but naming the language beats it on short or
accented audio -- Whisper takes the choice as a constraint rather than a hint.

## What it costs
{cost} add your own OpenAI API key and pay OpenAI directly -- roughly
USD {60 * WHISPER_COST_PER_MINUTE:.2f} per hour of audio. There is no subscription and no per-seat pricing.{pack}

## Pages
- [Home]({public_url('index')}): search, pick an episode, transcribe
- [Pricing]({public_url('pricing')}): free trial, credit pack, or bring your own key
- [API docs]({public_url('api_docs')}): customer HTTP API (resolve → transcribe → transcript)
- [How to find an RSS feed]({public_url('rss_help')}): for podcasts outside the search index
- [Sign up]({public_url('register')}): {signup_blurb}

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
    """The three pages worth indexing. Everything else needs a session."""
    from xml.sax.saxutils import escape
    pages = [public_url('index'),
             public_url('pricing'),
             public_url('api_docs'),
             public_url('rss_help'),
             public_url('register')]
    # No lastmod: it was emitting today's date on every fetch, which claims all
    # pages change daily. That is a discount signal, not a freshness one.
    urls = '\n'.join(f'  <url><loc>{escape(u)}</loc></url>' for u in pages)
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
           f'{urls}\n</urlset>\n')
    return Response(xml, mimetype='application/xml')


@app.route('/pricing')
def pricing():
    """Public pricing page: free trial, one-time pack, or bring your own key."""
    # Must be a real bool — Jinja `a and b` returns b, so piping openai_api_key
    # through |tojson would render the raw key into the page (and PostHog).
    has_own_key = bool(
        current_user.is_authenticated and current_user.openai_api_key)
    return render_template(
        'pricing.html',
        trial_minutes=(TRIAL_DEFAULT_SECONDS // 60 if trial_available() else None),
        stripe_configured=stripe_checkout_enabled(),
        has_own_key=has_own_key,
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


def load_customer_api_markdown():
    """Source for /docs/api. Public HTML must never ship AGENT_API_KEY copy."""
    with open(CUSTOMER_API_DOC_PATH, encoding='utf-8') as f:
        text = f.read()
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
                parts.append(f'<th>{_md_inline(cell)}</th>')
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
        trial_minutes=(TRIAL_DEFAULT_SECONDS // 60 if trial_available() else None),
    )


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
    }


def _itunes_show_result(item):
    """An iTunes podcast (show) hit, in the shape the search box renders."""
    return {
        'type': 'show',
        'name': item.get('collectionName', ''),
        'artist': item.get('artistName', ''),
        'artwork': _best_artwork(item),
        'feed_url': item.get('feedUrl'),
        'genre': item.get('primaryGenreName', ''),
    }


# ---------------------------------------------------------------------------
# Spotify links  (TSK-20440)
# ---------------------------------------------------------------------------

#: open.spotify.com/episode/<id>, /intl-no/show/<id>, /embed/..., spotify:episode:<id>.
#: Only the 22-character id is ever used to build a request, so nothing the
#: user typed decides which host the server talks to.
SPOTIFY_URL_RE = re.compile(
    r'(?:open\.spotify\.com/(?:intl-[a-z]{2}(?:-[a-z]{2})?/)?(?:embed/)?|spotify:)'
    r'(episode|show)[/:]([A-Za-z0-9]{22})(?![A-Za-z0-9])'
)

SPOTIFY_NO_FEED_HINT = (
    "Podskrift can only transcribe podcasts that publish a public RSS feed. "
    "Spotify-exclusive shows don't, so their audio can't be fetched."
)

_SPOTIFY_HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; Podskrift/1.0)'}
_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)


def parse_spotify_url(raw):
    """Return (kind, id) for a Spotify episode/show link, or (None, None)."""
    match = SPOTIFY_URL_RE.search(raw or '')
    return (match.group(1), match.group(2)) if match else (None, None)


def _normalize_title(text):
    """Case-, width- and punctuation-insensitive form for matching titles."""
    text = unicodedata.normalize('NFKC', text or '').casefold()
    return re.sub(r'[\W_]+', ' ', text).strip()


def _spotify_embed_metadata(kind, spotify_id):
    """Episode title and show name from Spotify's public embed player.

    The Web API needs an app registration; the embed player does not, and its
    __NEXT_DATA__ carries both names. It is not a documented API, so any shape
    change lands here as None and the caller falls back to oEmbed.
    """
    try:
        resp = requests.get(f'https://open.spotify.com/embed/{kind}/{spotify_id}',
                            headers=_SPOTIFY_HEADERS, timeout=10)
        resp.raise_for_status()
        entity = json.loads(_NEXT_DATA_RE.search(resp.text).group(1))[
            'props']['pageProps']['state']['data']['entity']
    except (requests.RequestException, AttributeError, KeyError, TypeError, ValueError):
        return None
    name = entity.get('name') or entity.get('title') or ''
    if entity.get('type') == 'episode':
        # A show embed renders its latest episode: the show is the subtitle.
        show = entity.get('subtitle') or ''
        return {'title': name if kind == 'episode' else '', 'show': show}
    return {'title': '', 'show': name}


def _spotify_oembed_metadata(kind, spotify_id):
    """oEmbed is documented but only carries one title: the episode's or the show's."""
    try:
        resp = requests.get('https://open.spotify.com/oembed',
                            params={'url': f'https://open.spotify.com/{kind}/{spotify_id}'},
                            headers=_SPOTIFY_HEADERS, timeout=10)
        resp.raise_for_status()
        title = (resp.json().get('title') or '').strip()
    except (requests.RequestException, ValueError, AttributeError):
        return None
    if not title:
        return None
    return {'title': title, 'show': ''} if kind == 'episode' else {'title': '', 'show': title}


def fetch_spotify_metadata(kind, spotify_id):
    """{'title', 'show'} for a Spotify link, or None when Spotify tells us nothing."""
    meta = _spotify_embed_metadata(kind, spotify_id)
    if meta and (meta['show'] or meta['title']):
        return meta
    return _spotify_oembed_metadata(kind, spotify_id)


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
    return [item for item in _itunes_search(show_name, 'podcast')
            if item.get('feedUrl') and _normalize_title(item.get('collectionName')) == want]


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


def _fetch_feed_capped(feed_url, max_bytes=SPOTIFY_FEED_MAX_BYTES):
    """Feed bytes, or None when the feed is unreachable or too big.

    A dead feed returns None instead of raising, so the resolver still gets
    to try the next show and the iTunes episode search. Redirects are followed
    by hand and every hop revalidated, as in download_audio: the route is
    public, and a public feed could otherwise 302 to a private address.
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
                with session.get(current, headers=_SPOTIFY_HEADERS, timeout=15,
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
                    chunks, size = [], 0
                    for chunk in resp.iter_content(64 * 1024):
                        size += len(chunk)
                        if size > max_bytes:
                            return None
                        chunks.append(chunk)
                    return b''.join(chunks)
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


def resolve_spotify_url(raw):
    """Map a Spotify link onto the public feed Podskrift can fetch.

    Returns (results, error) in /search-podcasts' result shape. Raises
    requests.RequestException when a directory lookup itself fails.
    """
    kind, spotify_id = parse_spotify_url(raw)
    if not kind:
        return [], "That doesn't look like a Spotify episode or show link."
    meta = fetch_spotify_metadata(kind, spotify_id)
    if not meta:
        return [], "Couldn't read that Spotify link. Check that it's a public episode or show."
    shows = _public_shows_named(meta['show']) if meta['show'] else []
    if kind == 'show':
        if shows:
            return [_itunes_show_result(shows[0])], None
        return [], f'"{meta["show"]}" has no public podcast feed. {SPOTIFY_NO_FEED_HINT}'

    for show in shows[:2]:
        hit = _episode_from_feed(show['feedUrl'], meta['title'])
        if hit:
            return [hit], None
    hit = _episode_from_itunes(meta['title'], meta['show'])
    if hit:
        return [hit], None
    if shows:
        return [_itunes_show_result(shows[0])], (
            f'Found "{meta["show"]}", but this episode isn\'t in its public feed -- it may be '
            'Spotify-exclusive. Open the show below to pick from the episodes that are.')
    name = meta['show'] or meta['title']
    return [], f'"{name}" has no public podcast feed. {SPOTIFY_NO_FEED_HINT}'


@app.route('/resolve-spotify', methods=['GET'])
def resolve_spotify():
    """Turn a pasted Spotify episode/show link into a search-box result."""
    try:
        results, error = resolve_spotify_url(request.args.get('url', '').strip())
    except requests.RequestException:
        results, error = [], "Couldn't reach the podcast directory. Try again in a moment."
    return jsonify({'results': results, 'error': error})


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


with app.app_context():
    db.create_all()
    ensure_credit_purchases_table()

    apply_column_migrations()
    if STRIPE_MANAGED_PAYMENTS and STRIPE_AUTOMATIC_TAX:
        app.logger.warning(
            'STRIPE_MANAGED_PAYMENTS and STRIPE_AUTOMATIC_TAX are both on; '
            'Managed Payments wins and automatic_tax is not sent')
    if STRIPE_AUTOMATIC_TAX and not STRIPE_PRICE_ID:
        app.logger.warning(
            'STRIPE_AUTOMATIC_TAX is on but STRIPE_PRICE_ID is unset; '
            'prefer a Dashboard Price with tax_behavior set explicitly')
    inspector = sa_inspect(db.engine)

    # Transcription runs in a daemon thread, so a deploy or crash leaves tasks
    # stuck in a running state forever. Fail those at boot -- but only ones that
    # have gone quiet: this module is imported by every gunicorn worker, and a
    # worker respawning mid-life must not kill jobs another worker is running.
    orphaned = [
        t for t in TranscriptionTask.query.filter(
            ~TranscriptionTask.status.in_(['completed', *TERMINAL_STATUSES])
        ).all()
        if _seconds_since(t.heartbeat_at or t.started_at) > _stale_after_seconds(t)
    ]
    for task in orphaned:
        # Both gunicorn workers run this sweep, so a stuck task can be reported
        # twice. The shared fingerprint keeps that to one issue.
        report_stale_task(task.id, task.status,
                          _seconds_since(task.heartbeat_at or task.started_at), source='boot')
        task.status = 'error'
        task.phase = 'error'
        task.error_message = (
            'Transcription was interrupted and stopped making progress. Please try again.'
        )
    if orphaned:
        db.session.commit()
        for task in orphaned:
            trial_refund_task(task)

    settle_stranded_charges()

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
        print(f"Trial: {TRIAL_DEFAULT_SECONDS // 60} min/account, "
              f"{TRIAL_GLOBAL_SECONDS // 60} min total ceiling "
              f"(~${TRIAL_GLOBAL_SECONDS / 60 * WHISPER_COST_PER_MINUTE:.2f} max spend)")
    else:
        print("Trial: off (no global key, or TRIAL_ENABLED=0)")
    print(f"Environment: {os.getenv('FLASK_ENV', 'development')}")
    print("=" * 50)

    host = '0.0.0.0' if os.getenv('FLASK_ENV') == 'production' else '127.0.0.1'
    debug = os.getenv('FLASK_ENV') != 'production'
    app.run(debug=debug, host=host, port=5002)
