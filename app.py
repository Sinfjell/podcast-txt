#!/usr/bin/env python3
"""
Podcast Transcriber Web App with OpenAI API

A Flask web application for transcribing podcast episodes from RSS feeds using OpenAI's Whisper API.
Supports user accounts, saved RSS feeds, and self-serve API keys.
"""

import collections
import glob
import math
import os
import sqlite3
import shutil
import subprocess
import ssl
import time
import threading
from datetime import datetime, timezone
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
                   redirect, url_for, Response)
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from urllib.parse import urljoin, urlparse
import uuid
from dotenv import load_dotenv
from openai import OpenAI, APIConnectionError, APITimeoutError

from sqlalchemy import (event as sa_event, inspect as sa_inspect, text,
                        update as sa_update)
from sqlalchemy.engine import Engine

from models import (db, User, SavedFeed, TranscriptionTask,
                    TASK_COLUMN_MIGRATIONS, USER_COLUMN_MIGRATIONS)

load_dotenv()

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


# Whisper pricing, used for the cost estimates shown in the UI
WHISPER_COST_PER_MINUTE = 0.006

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
MAX_REDIRECTS = 5
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

#: (code, native name) -- what the picker renders.
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
TRIAL_DEFAULT_SECONDS = _env_minutes('TRIAL_MINUTES', 60) * 60
#: Hard ceiling on trial minutes across ALL accounts. Without this, the per-user
#: cap bounds nothing -- signups are free, so N accounts cost N x the grant.
TRIAL_GLOBAL_SECONDS = _env_minutes('TRIAL_GLOBAL_MINUTES', 600) * 60
#: What to reserve when the feed publishes no itunes:duration. Reconciled
#: against the real duration after download, before a single Whisper call.
TRIAL_UNKNOWN_ESTIMATE_SECONDS = _env_minutes('TRIAL_UNKNOWN_ESTIMATE_MINUTES', 30) * 60
#: Longest single episode the trial will take on, so one four-hour interview
#: cannot swallow an entire allowance in one go.
TRIAL_MAX_EPISODE_SECONDS = _env_minutes('TRIAL_MAX_EPISODE_MINUTES', 180) * 60
#: Kill switch. Set TRIAL_ENABLED=0 to stop handing out our key entirely.
TRIAL_ENABLED = os.getenv('TRIAL_ENABLED', '1').strip().lower() not in ('0', 'false', 'no', 'off')


class TrialExhausted(Exception):
    """Raised when a job would cost more trial allowance than is left."""


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


def trial_global_used_seconds():
    """Trial seconds spent across every account."""
    return int(db.session.execute(text(
        'SELECT COALESCE(SUM(trial_seconds_used), 0) FROM users'
    )).scalar() or 0)


def resolve_openai_key(user):
    """Return (key, source) where source is 'user', 'trial', or None.

    A user's own key always wins -- it costs us nothing and has no cap.
    """
    own = getattr(user, 'openai_api_key', None) if user is not None else None
    if own:
        return own, 'user'
    if trial_available():
        return GLOBAL_OPENAI_KEY, 'trial'
    return None, None


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


def trial_refund_task(task):
    """Refund the part of a failed task we did not actually spend.

    Chunks already sent to Whisper were billed to us whatever happens to the
    task afterwards, so refunding the whole reservation would hand back money
    that is gone -- and the stale sweeper fires on tasks whose worker is often
    several chunks in. Refund the unstarted remainder instead, pro-rata on
    chunk progress. Safe to call repeatedly: the conditional UPDATE on the
    task is what decides which caller may move the balance.
    """
    # Read the row rather than trusting the caller's copy. /status hands us an
    # object loaded at the top of the request; if the worker reconciled the
    # charge in between, a claim against the stale value silently matches
    # nothing and the user forfeits the allowance with no path to get it back.
    row = db.session.execute(text(
        'SELECT user_id, trial_seconds_charged, chunk_total, chunk_index, trial_settled '
        'FROM transcription_tasks WHERE id = :tid'
    ), {'tid': task.id}).first()
    if row is None:
        return 0
    user_id, charged, chunk_total, chunk_index, settled = row
    if settled or not charged or charged <= 0:
        return 0

    if (chunk_total or 0) > 0 and chunk_index is not None:
        # chunk_index is written immediately BEFORE that chunk is uploaded, so
        # index k means k+1 chunks have been sent and billed to us. Rounding
        # toward charging is deliberate: refunding a chunk that did reach
        # Whisper is exactly how a swept single-chunk episode -- every episode
        # under 24 MB, so the common case -- came out free.
        started = min(chunk_total, max(0, chunk_index) + 1)
        spent = int(charged * started / chunk_total)
    else:
        spent = 0  # nothing reached Whisper yet

    # Settling is the claim, and it also pins the amount we read: a row whose
    # charge moved under us (reconcile) or that someone else already settled
    # does not match, so only one caller ever moves the balance -- and never
    # twice, which a claim on the amount alone could not guarantee once the
    # refund became pro-rata.
    claimed = db.session.execute(text("""
        UPDATE transcription_tasks
           SET trial_seconds_charged = :spent, trial_settled = 1
         WHERE id = :tid AND trial_settled = 0 AND trial_seconds_charged = :charged
    """), {'tid': task.id, 'spent': spent, 'charged': charged}).rowcount == 1
    db.session.commit()
    if not claimed:
        return 0
    refund = charged - spent
    trial_release(user_id, refund)
    return refund


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
        TranscriptionTask.trial_seconds_charged > 0,
    ).all()
    for task in stranded:
        trial_refund_task(task)
    return len(stranded)


def trial_reconcile_task(task_id, actual_seconds):
    """Match a task's reservation to the audio we actually downloaded.

    Runs after the download but BEFORE the first Whisper call, so an episode
    that turns out longer than the feed claimed costs us bandwidth, never API
    spend. Raises TrialExhausted when the real length will not fit.
    """
    task = db.session.get(TranscriptionTask, task_id)
    if not task or task.trial_settled or not task.trial_seconds_charged:
        # NULL is an own-key task, nothing metered. Settled means the sweeper
        # got here first -- re-opening the charge would bill the user for an
        # episode that goes on to send nothing.
        return
    reserved = int(task.trial_seconds_charged)
    actual = int(math.ceil(max(0.0, actual_seconds or 0.0)))
    user_id = task.user_id

    if TRIAL_MAX_EPISODE_SECONDS and actual > TRIAL_MAX_EPISODE_SECONDS:
        raise TrialExhausted(
            f'This episode runs {actual // 60} minutes, past the '
            f'{TRIAL_MAX_EPISODE_SECONDS // 60}-minute per-episode limit of the free '
            'trial. Add your own OpenAI API key in Settings to transcribe it.'
        )

    if actual > reserved:
        extra = actual - reserved
        if not trial_reserve(user_id, extra):
            raise TrialExhausted(
                f'This episode runs {actual // 60} minutes and your free trial has '
                'less than that left. Add your own OpenAI API key in Settings to '
                'keep transcribing.'
            )
        if not _claim_task_charge(task_id, reserved, actual):
            # Someone else settled the task while we were topping up; give the
            # top-up straight back rather than leaking it against the user.
            trial_release(user_id, extra)
    elif actual < reserved:
        if _claim_task_charge(task_id, reserved, actual):
            trial_release(user_id, reserved - actual)


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
        return ('Your OpenAI API key was rejected. Check it in Settings — it should '
                'start with "sk-" and come from platform.openai.com/api-keys.')
    if status == 429:
        return ('Your OpenAI account is out of credit, or you have hit its rate limit. '
                'Add billing at platform.openai.com/account/billing, then try again.')
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


def verify_openai_key(key):
    """Check a key against OpenAI. Returns (ok, message).

    Done at save time rather than at transcription time: previously the first
    signal that a key was wrong came minutes later, after picking an episode and
    waiting through a download. 15 of 16 production failures were this.
    """
    if not looks_like_openai_key(key):
        return False, ('That does not look like an OpenAI API key. Keys start with '
                       '"sk-" and come from platform.openai.com/api-keys — it is not '
                       'your OpenAI password.')
    try:
        OpenAI(api_key=key, timeout=15.0, max_retries=0).models.list()
    except Exception as e:
        status = getattr(e, 'status_code', None)
        # A 5xx or a connection failure means we could not CHECK the key, not
        # that OpenAI rejected it. Refusing the save there would make an OpenAI
        # outage look like the user's key is broken.
        unreachable = (status is not None and 500 <= status < 600) or isinstance(
            e, (APIConnectionError, APITimeoutError))
        if unreachable:
            return True, ('Key saved, but OpenAI could not be reached to verify it. '
                          'If transcription fails, re-check the key here.')
        if status == 429:
            # The key authenticated; the account is just out of credit or rate
            # limited. Refusing the save would leave them unable to store a
            # working key at all.
            return True, ('Key saved. Note: ' +
                          describe_openai_error(e, context='verify'))
        return False, describe_openai_error(e, context='verify')
    return True, 'API key verified.'


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
                       'Chrome/91.0.4472.124 Safari/537.36',
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
        the check done on the original URL.
        """
        current = url
        for _ in range(MAX_REDIRECTS):
            if not _is_fetchable_url(current):
                raise Exception('Audio URL points somewhere that cannot be fetched.')
            resp = requests.get(
                current, stream=True, headers=request_headers,
                timeout=30, allow_redirects=False,
            )
            if resp.is_redirect or resp.is_permanent_redirect:
                location = resp.headers.get('location')
                resp.close()
                if not location:
                    raise Exception('Redirect without a target while fetching audio.')
                current = urljoin(current, location)
                continue
            try:
                resp.raise_for_status()
            except requests.exceptions.HTTPError:
                resp.close()   # streamed responses hold the connection open
                raise
            return resp
        raise Exception('Too many redirects while fetching audio.')

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


def get_episodes_from_rss(rss_url):
    """Parse RSS feed and return (episodes, error). Feed title lands on each episode."""
    try:
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

@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('index'))

    if request.method != 'POST':
        return render_template('register.html')

    email = request.form.get('email', '').strip().lower()
    password = request.form.get('password', '')
    password2 = request.form.get('password2', '')

    if not email or not password:
        flash('Email and password are required.', 'error')
        return render_template('register.html')

    ip = _client_ip()
    slot = register_reserve_slot(ip)
    if slot is None:
        flash('Too many accounts created from this address. Try again later.', 'error')
        return render_template('register.html')

    # The slot is held for the rest of this request and released unless an
    # account is actually created, so validation failures cost the user nothing
    # while a parallel burst still cannot exceed the limit.
    created = False
    try:
        if is_disposable_email(email):
            flash('Please register with a real email address.', 'error')
            return render_template('register.html')

        if '@' not in email or '.' not in email.rsplit('@', 1)[-1]:
            flash('Please enter a valid email address.', 'error')
            return render_template('register.html')

        if password != password2:
            flash('Passwords do not match.', 'error')
            return render_template('register.html')

        if len(password) < 8:
            flash('Password must be at least 8 characters.', 'error')
            return render_template('register.html')

        if User.query.filter_by(email=email).first():
            flash('An account with this email already exists.', 'error')
            return render_template('register.html')

        user = User(email=email)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        created = True

        login_user(user)
        flash('Account created! Add your OpenAI API key in Settings to use your own quota.',
              'success')
        return redirect(url_for('index'))
    finally:
        if not created:
            register_release_slot(ip, slot)


@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('index'))

    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')

        user = User.query.filter_by(email=email).first()
        if user and user.check_password(password):
            login_user(user, remember=True)
            next_page = request.args.get('next')
            return redirect(next_page or url_for('index'))

        flash('Invalid email or password.', 'error')

    return render_template('login.html')


@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('Logged out.', 'info')
    return redirect(url_for('index'))


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@app.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    if request.method == 'POST':
        api_key = request.form.get('openai_api_key', '').strip()

        if not api_key:
            current_user.openai_api_key = None
            db.session.commit()
            flash('API key removed.', 'success')
            return redirect(url_for('settings'))

        ok, message = verify_openai_key(api_key)
        caveat = ok and not message.startswith('API key verified')
        if not ok:
            # Never store a rejected key: it is frequently a password, and it
            # would otherwise sit in the database and fail again at transcribe time.
            flash(message, 'error')
            return redirect(url_for('settings'))

        current_user.openai_api_key = api_key
        db.session.commit()
        flash(message, 'warning' if caveat else 'success')
        return redirect(url_for('settings'))

    return render_template('settings.html', trial=_trial_context())


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

    episodes_to_show = episodes[:10]
    has_more = len(episodes) > 10
    return render_template(
        'episode_selection.html',
        episodes=episodes_to_show,
        all_episodes=episodes,
        rss_url=feed.rss_url,
        feed_name=feed.name,
        has_more=has_more,
        needs_api_key=not _user_has_api_key(),
        podcast_name=episodes[0].get('podcast_name') or feed.name,
        artwork=episodes[0].get('artwork') or '',
        languages=SUPPORTED_LANGUAGES,
    )


def _trial_context():
    """Trial figures for the templates, or None when there is no trial."""
    if not (trial_available() and current_user.is_authenticated):
        return None
    limit, used, remaining = trial_status(current_user)
    return {
        'limit_minutes': limit // 60,
        'used_minutes': used // 60,
        'remaining_minutes': remaining // 60,
        'exhausted': remaining <= 0,
        'on_own_key': bool(current_user.openai_api_key),
    }


def _user_has_api_key():
    """Can the current user actually start a transcription right now?

    True on their own key, or on trial allowance they still have left. An
    exhausted trial counts as no key, which is what puts the "add your key"
    prompt in front of exactly the people who need to see it.
    """
    if not current_user.is_authenticated:
        return False
    if current_user.openai_api_key:
        return True
    if trial_available():
        return trial_status(current_user)[2] > 0
    return False


# ---------------------------------------------------------------------------
# Core routes
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    saved_feeds = []
    if current_user.is_authenticated:
        saved_feeds = SavedFeed.query.filter_by(
            user_id=current_user.id
        ).order_by(SavedFeed.created_at.desc()).limit(5).all()
    return render_template('index.html', saved_feeds=saved_feeds,
                           languages=SUPPORTED_LANGUAGES,
                           trial=_trial_context(),
                           faq=faq_entries(),
                           trial_minutes=(TRIAL_DEFAULT_SECONDS // 60
                                          if trial_available() else None),
                           structured_data=_structured_data())


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

    episodes_to_show = episodes[:10]
    has_more = len(episodes) > 10
    return render_template(
        'episode_selection.html',
        episodes=episodes_to_show,
        all_episodes=episodes,
        rss_url=rss_url,
        has_more=has_more,
        needs_api_key=not _user_has_api_key(),
        podcast_name=episodes[0].get('podcast_name') or '',
        artwork=episodes[0].get('artwork') or '',
        languages=SUPPORTED_LANGUAGES,
    )


@app.route('/start_transcription', methods=['POST'])
@login_required
def start_transcription():
    """Start a transcription from either an RSS feed + index, or a direct audio URL.

    The direct form is what episode search results post, so an episode found by
    name never has to be located a second time inside its feed.
    """
    language = request.form.get('language', 'no')
    if language not in VALID_LANGUAGE_CODES:
        language = 'no'

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

    api_key, key_source = resolve_openai_key(current_user)
    if not api_key:
        return jsonify({
            'error': 'No OpenAI API key configured. Add your key in Settings.'
        }), 400
    openai_client = build_openai_client(api_key)

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
        return jsonify({'error': (
            'Podskrift is out of disk space right now. Please try again later.'
        )}), 503
    if not _transcription_slots.acquire(blocking=False):
        # Logged because this is the only way to learn the cap is being hit, or
        # whether the default is the right number, short of user complaints.
        app.logger.warning('refusing transcription: worker at its limit of %s',
                           MAX_CONCURRENT_TRANSCRIPTIONS)
        episodes = 'episode' if MAX_CONCURRENT_TRANSCRIPTIONS == 1 else 'episodes'
        return jsonify({'error': (
            f'Podskrift is already transcribing {MAX_CONCURRENT_TRANSCRIPTIONS} '
            f'{episodes} right now. Please try again in a few minutes.'
        )}), 503
    slot_held = True
    try:

        # Reserve the allowance BEFORE the job exists, so a refusal leaves nothing
        # behind. The feed's duration is only an estimate; trial_reconcile_task()
        # corrects it against the real audio before anything reaches Whisper.
        trial_charge = None
        if key_source == 'trial':
            # Floored at a minute: trial_seconds_charged == 0 means "settled", so a
            # zero reservation would quietly make the task unmetered.
            estimate = max(60, int((meta['duration_min'] or 0) * 60)
                           or TRIAL_UNKNOWN_ESTIMATE_SECONDS)
            if TRIAL_MAX_EPISODE_SECONDS and estimate > TRIAL_MAX_EPISODE_SECONDS:
                return jsonify({'error': (
                    f'This episode runs {estimate // 60} minutes, past the '
                    f'{TRIAL_MAX_EPISODE_SECONDS // 60}-minute per-episode limit of the '
                    'free trial. Add your own OpenAI API key in Settings to transcribe it.'
                )}), 402
            if not trial_reserve(current_user.id, estimate):
                _, _, remaining = trial_status(current_user)
                if remaining < estimate:
                    message = (
                        f'Your free trial has {remaining // 60} minutes left, and this '
                        f'episode needs about {estimate // 60}. Add your own OpenAI API '
                        'key in Settings to keep transcribing.'
                    )
                else:
                    # The user still has room; the service as a whole does not.
                    # Saying "you have 60 minutes left" here would contradict itself.
                    message = (
                        'Podskrift has handed out all the free minutes it has budgeted. '
                        'Add your own OpenAI API key in Settings to keep transcribing.'
                    )
                return jsonify({'error': message}), 402
            trial_charge = estimate

        task_id = str(uuid.uuid4())
        try:
            task = TranscriptionTask(
                id=task_id,
                user_id=current_user.id,
                episode_title=meta['title'],
                rss_url=rss_url,
                status='downloading',
                phase='downloading',
                phase_started_at=datetime.now(timezone.utc),
                podcast_name=meta.get('podcast_name'),
                artwork_url=meta.get('artwork'),
                episode_published=meta.get('published'),
                # Feed duration is the best ETA source we have, and it is available
                # before a single byte is downloaded.
                audio_duration=(meta['duration_min'] * 60) if meta.get('duration_min') else None,
                language=language or None,
                trial_seconds_charged=trial_charge,
            )
            db.session.add(task)
            db.session.commit()
        except Exception:
            db.session.rollback()
            if trial_charge:
                trial_release(current_user.id, trial_charge)
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
                    except Exception as e:
                        _update_task(task_id, status='error', phase='error',
                                     error_message=describe_openai_error(e)
                                     if _is_openai_error(e) else str(e))
                        # A job that never produced a transcript must not consume the
                        # trial allowance it reserved.
                        failed = db.session.get(TranscriptionTask, task_id)
                        if failed:
                            trial_refund_task(failed)
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

        return jsonify({'task_id': task_id})
    finally:
        # Handed to the worker thread on success -- slot_held goes False only
        # AFTER thread.start() returns, so a thread that fails to start (the
        # box out of threads is exactly when this matters) still releases here.
        # Returned here on every refusal and any exception too, so a rejected
        # request cannot leak a slot for the life of the process.
        if slot_held:
            _transcription_slots.release()


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
    if _seconds_since(task.heartbeat_at or task.started_at) <= _stale_after_seconds(task):
        return False
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
    }

    if task.status == 'cancelled':
        result['cancelled'] = True
    elif task.status == 'error':
        result['error'] = task.error_message or 'Unknown error'

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


@app.route('/history')
@login_required
def history():
    tasks = TranscriptionTask.query.filter_by(
        user_id=current_user.id, status='completed'
    ).order_by(TranscriptionTask.completed_at.desc()).limit(50).all()
    total_cost = sum(
        (t.audio_duration / 60) * WHISPER_COST_PER_MINUTE
        for t in tasks if t.audio_duration
    )
    return render_template('history.html', transcriptions=tasks,
                           total_cost=total_cost,
                           cost_per_minute=WHISPER_COST_PER_MINUTE)


def faq_entries():
    """Answered on the page, in the schema and in llms.txt.

    These are the questions people actually put to a search box or an assistant
    -- "hvordan transkribere en podcast" is the query, not "what is Podskrift".

    Built at call time, not as a constant, so the trial length always matches
    TRIAL_DEFAULT_SECONDS. A first version hardcoded 60 minutes and would have
    kept saying so after the grant changed.
    """
    minutes = TRIAL_DEFAULT_SECONDS // 60
    hourly = f'${60 * WHISPER_COST_PER_MINUTE:.2f}'
    # trial_available() is the predicate the code actually enforces. Copy that
    # promises free minutes while the kill switch is on is a promise the app
    # then refuses at /start_transcription.
    if trial_available():
        free = (f'New accounts get {minutes} minutes of audio free, on our OpenAI key. '
                'After that you add your own OpenAI API key and pay OpenAI directly at '
                f'their rate -- about {hourly} per hour of audio. There is no subscription.')
        need_key = ('Not to start. The free trial runs on ours. Add your own key when the '
                    'trial runs out and there is no limit beyond what you spend at OpenAI.')
    else:
        free = ('Podskrift itself is free. You add your own OpenAI API key and pay OpenAI '
                f'directly at their rate -- about {hourly} per hour of audio. There is no '
                'subscription.')
        need_key = ('Yes. Add it in Settings; it is stored on your account and used only '
                    'for your own transcriptions.')
    return [
        ('How do I transcribe a podcast episode to text?',
         'Search for the podcast or the episode by name, pick the episode, and Podskrift '
         'downloads the audio and transcribes it with OpenAI Whisper. You get the full text '
         'plus an .srt subtitle file. No file to upload and no feed URL to find first.'),
        ('Hvordan transkriberer jeg en norsk podcast til tekst?',
         'Søk opp podkasten eller episoden på navn, velg episoden, og Podskrift laster ned '
         'lyden og transkriberer den med OpenAI Whisper. Du får hele teksten og en .srt-fil '
         'med teksting. Velg norsk i språkvelgeren, så slipper du at den gjetter feil.'),
        ('Which languages does it handle well?',
         f'{len(LANGUAGE_ENGLISH_NAMES)} languages, from English, Spanish and Mandarin to '
         'Norwegian, Ukrainian and Vietnamese. You can name the language rather than relying '
         'on auto-detect, which matters on short or accented audio. It does especially well '
         'on Nordic and Central European languages, where English-first tools do worst.'),
        ('Is it free?', free),
        ('Do I need an OpenAI API key?', need_key),
        ('What file formats do I get?',
         'Plain text (.txt) and SubRip subtitles (.srt) with timestamps.'),
        ('Can I transcribe a podcast that is not in the search index?',
         'Yes. Paste the RSS feed URL instead and pick the episode from the feed.'),
    ]


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
                    'subtitles. Unusually good on the languages English-first transcription '
                    'tools handle worst.'
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
                         'After that, bring ' if trial_available() else 'Bring ')
                        + 'your own OpenAI API key and pay OpenAI directly at their rate. '
                          'No subscription.'
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
        '/status/', '/active-jobs', '/cancel/',
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
    body = f"""# Podskrift

> Transcribes podcast episodes to text using OpenAI Whisper, in {len(LANGUAGE_ENGLISH_NAMES)}
> languages. Works with any podcast, anywhere -- and is unusually good on the
> languages English-first transcription tools handle worst.

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

Auto-detect is available, but naming the language beats it on short or accented
audio. Nordic and Central European languages are where this does best relative
to English-first tools -- not the only ones it handles.

## What it costs
{cost} add your own OpenAI API key and pay OpenAI directly -- roughly
USD {60 * WHISPER_COST_PER_MINUTE:.2f} per hour of audio. There is no subscription and no per-seat pricing.

## Pages
- [Home]({public_url('index')}): search, pick an episode, transcribe
- [How to find an RSS feed]({public_url('rss_help')}): for podcasts outside the search index
- [Sign up]({public_url('register')}): {signup_blurb}

## Frequently asked

{faq}

## Contact
Nettsmed -- https://nettsmed.no
"""
    return Response(body, mimetype='text/plain')


@app.route('/sitemap.xml')
def sitemap_xml():
    """The three pages worth indexing. Everything else needs a session."""
    from xml.sax.saxutils import escape
    pages = [public_url('index'),
             public_url('rss_help'),
             public_url('register')]
    # No lastmod: it was emitting today's date on every fetch, which claims all
    # three pages change daily. That is a discount signal, not a freshness one.
    urls = '\n'.join(f'  <url><loc>{escape(u)}</loc></url>' for u in pages)
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
           f'{urls}\n</urlset>\n')
    return Response(xml, mimetype='application/xml')


@app.route('/rss-help')
def rss_help():
    return render_template('rss_help.html')


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
            audio_url = item.get('episodeUrl')
            if not audio_url:
                continue
            duration_min = None
            if item.get('trackTimeMillis'):
                duration_min = round(item['trackTimeMillis'] / 60000, 1)
            results.append({
                'type': 'episode',
                'name': item.get('trackName', ''),
                'artist': item.get('collectionName', ''),
                'artwork': _best_artwork(item),
                'audio_url': audio_url,
                'feed_url': item.get('feedUrl', ''),
                'released': (item.get('releaseDate') or '')[:10],
                'duration_min': duration_min,
                'estimated_cost': (
                    round(duration_min * WHISPER_COST_PER_MINUTE, 3) if duration_min else None
                ),
            })
        else:
            feed_url = item.get('feedUrl')
            if not feed_url:
                continue
            results.append({
                'type': 'show',
                'name': item.get('collectionName', ''),
                'artist': item.get('artistName', ''),
                'artwork': _best_artwork(item),
                'feed_url': feed_url,
                'genre': item.get('primaryGenreName', ''),
            })

    return jsonify({'results': results})


# ---------------------------------------------------------------------------
# Init
# ---------------------------------------------------------------------------

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

    inspector = sa_inspect(db.engine)
    added = []
    for table, migrations in (('transcription_tasks', TASK_COLUMN_MIGRATIONS),
                              ('users', USER_COLUMN_MIGRATIONS)):
        existing = {c['name'] for c in inspector.get_columns(table)}
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



with app.app_context():
    db.create_all()

    apply_column_migrations()
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
