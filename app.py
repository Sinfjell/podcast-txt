#!/usr/bin/env python3
"""
Podcast Transcriber Web App with OpenAI API

A Flask web application for transcribing podcast episodes from RSS feeds using OpenAI's Whisper API.
Supports user accounts, saved RSS feeds, and self-serve API keys.
"""

import os
import ssl
import time
import threading
from datetime import datetime, timedelta, timezone
import certifi
import requests
import feedparser

# Fix CA bundle path for Python 3.14+ where certifi may ship without the PEM
if not os.path.exists(certifi.where()):
    _sys_ca = ssl.get_default_verify_paths().cafile
    if _sys_ca and os.path.exists(_sys_ca):
        os.environ.setdefault('REQUESTS_CA_BUNDLE', _sys_ca)
        os.environ.setdefault('SSL_CERT_FILE', _sys_ca)
from flask import Flask, render_template, request, jsonify, send_file, flash, redirect, url_for
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from urllib.parse import urlparse
import uuid
from dotenv import load_dotenv
from openai import OpenAI
from pydub import AudioSegment

from models import db, User, SavedFeed, TranscriptionTask, TASK_COLUMN_MIGRATIONS

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'change-me-in-production')

# Database
db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
os.makedirs(db_path, exist_ok=True)
app.config['SQLALCHEMY_DATABASE_URI'] = f"sqlite:///{os.path.join(db_path, 'podcast.db')}"
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db.init_app(app)

# Login manager
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'
login_manager.login_message_category = 'info'


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


# Global fallback OpenAI key
GLOBAL_OPENAI_KEY = os.getenv('OPENAI_API_KEY')

# Whisper pricing, used for the cost estimates shown in the UI
WHISPER_COST_PER_MINUTE = 0.006

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
SUPPORTED_LANGUAGES = [
    ('', 'Auto-detect'),
    ('no', 'Norsk'),
    ('en', 'English'),
    ('sv', 'Svenska'),
    ('da', 'Dansk'),
    ('de', 'Deutsch'),
    ('fr', 'Fran\u00e7ais'),
    ('es', 'Espa\u00f1ol'),
    ('nl', 'Nederlands'),
]
VALID_LANGUAGE_CODES = {code for code, _ in SUPPORTED_LANGUAGES}


def get_openai_client(user=None):
    """Get OpenAI client using user's key or global fallback."""
    key = None
    if user and hasattr(user, 'get_openai_key'):
        key = user.get_openai_key()
    if not key:
        key = GLOBAL_OPENAI_KEY
    if not key:
        return None
    return OpenAI(api_key=key)


# ---------------------------------------------------------------------------
# Audio helpers
# ---------------------------------------------------------------------------

#: A task with no progress write for this long is treated as abandoned.
STALE_TASK_SECONDS = 15 * 60


def _update_task(task_id, **kwargs):
    """Update a TranscriptionTask row. Must be called within an app context."""
    task = db.session.get(TranscriptionTask, task_id)
    if task:
        for k, v in kwargs.items():
            setattr(task, k, v)
        task.heartbeat_at = datetime.now(timezone.utc)
        db.session.commit()


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

    try:
        response = requests.get(url, stream=True, headers=headers, timeout=30)
        response.raise_for_status()
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 403:
            alt_headers = {'User-Agent': 'podcast-downloader/1.0', 'Accept': '*/*'}
            try:
                response = requests.get(url, stream=True, headers=alt_headers, timeout=30)
                response.raise_for_status()
            except requests.exceptions.HTTPError as e2:
                raise Exception(
                    f"Access denied ({e2.response.status_code}) for audio file. "
                    "This podcast may restrict direct downloads."
                )
        else:
            raise Exception(f"HTTP error {e.response.status_code}")
    except requests.exceptions.RequestException as e:
        raise Exception(f"Failed to download audio: {e}")

    total_size = int(response.headers.get('content-length', 0) or 0)
    downloaded = 0
    last_db_update = 0.0

    # Report bytes even when the CDN omits content-length -- without this the bar
    # sat at 0% for the whole download on every feed that doesn't send the header.
    _update_task(task_id, bytes_downloaded=0, bytes_total=total_size)

    with open(filename, 'wb') as f:
        for chunk in response.iter_content(chunk_size=8192):
            if not chunk:
                continue
            f.write(chunk)
            downloaded += len(chunk)
            now = time.time()
            if now - last_db_update >= 1:
                fields = {'bytes_downloaded': downloaded}
                if total_size > 0:
                    fields['download_progress'] = int((downloaded / total_size) * 100)
                _update_task(task_id, **fields)
                last_db_update = now

    _update_task(
        task_id,
        bytes_downloaded=downloaded,
        bytes_total=total_size or downloaded,
        download_progress=100,
    )
    return filename


def get_audio_duration(audio_file):
    """Get audio duration in seconds."""
    try:
        import librosa
        return librosa.get_duration(path=audio_file)
    except Exception:
        file_size_mb = os.path.getsize(audio_file) / (1024 * 1024)
        return file_size_mb * 60


def split_audio_if_needed(audio_file, max_size_mb=24):
    """Split audio into chunks if it exceeds OpenAI's 25MB limit."""
    file_size_mb = os.path.getsize(audio_file) / (1024 * 1024)
    if file_size_mb <= max_size_mb:
        return [audio_file]

    num_chunks = int((file_size_mb / max_size_mb) + 1)
    try:
        audio = AudioSegment.from_file(audio_file)
        total_duration_ms = len(audio)
        chunk_duration_ms = total_duration_ms // num_chunks
        base_name = os.path.splitext(audio_file)[0]
        chunk_files = []

        for i in range(num_chunks):
            start = i * chunk_duration_ms
            end = min((i + 1) * chunk_duration_ms, total_duration_ms)
            chunk = audio[start:end]
            chunk_file = f"{base_name}_chunk_{i + 1}.mp3"
            chunk.export(chunk_file, format="mp3")
            chunk_files.append(chunk_file)

        os.remove(audio_file)
        return chunk_files
    except Exception:
        return [audio_file]


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

    _update_task(
        task_id,
        status='splitting',
        phase='splitting',
        phase_started_at=datetime.now(timezone.utc),
        progress=PHASE_SPANS['splitting'][0],
    )
    audio_chunks = split_audio_if_needed(audio_file, max_size_mb=24)

    task = db.session.get(TranscriptionTask, task_id)
    # The RSS feed's itunes:duration beats anything we can measure locally --
    # get_audio_duration() falls back to size*60 and multiplies chunk 0 by the
    # chunk count, both of which skew the ETA badly.
    audio_duration = task.audio_duration if task and task.audio_duration else None
    if not audio_duration:
        audio_duration = get_audio_duration(audio_chunks[0])
        if len(audio_chunks) > 1:
            audio_duration *= len(audio_chunks)

    _update_task(
        task_id,
        status='transcribing',
        phase='transcribing',
        chunk_total=len(audio_chunks),
        chunk_index=0,
        audio_duration=audio_duration,
        progress=PHASE_SPANS['transcribing'][0],
        phase_started_at=datetime.now(timezone.utc),
    )

    upload_start = time.time()
    all_segments = []
    full_text = ""

    for i, chunk_file in enumerate(audio_chunks):
        _update_task(
            task_id,
            status=f'transcribing chunk {i + 1}/{len(audio_chunks)}',
            chunk_index=i,
            phase_started_at=datetime.now(timezone.utc),
            progress=_transcribe_checkpoint(i, len(audio_chunks)),
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

    full_text = full_text.strip()
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


def compute_live_progress(task):
    """Interpolate a task's progress between its last two checkpoints.

    Returns (percent, eta_seconds_or_None). The stored `progress` column is
    treated as a floor so the bar can never travel backwards between polls.
    """
    stored = task.progress or 0
    if task.status in ('completed', 'error'):
        return (100 if task.status == 'completed' else stored), None

    phase_elapsed = _seconds_since(task.phase_started_at)

    if task.phase == 'transcribing' and task.chunk_total:
        lo, hi = PHASE_SPANS['transcribing']
        # Expected wall-clock seconds for the chunk currently in flight
        per_chunk = (task.audio_duration or 0) / task.chunk_total / WHISPER_REALTIME_FACTOR
        per_chunk = max(per_chunk, 8.0)
        # Cap at 0.97 so a slow chunk never claims to be finished
        within = min(0.97, phase_elapsed / per_chunk)
        done = ((task.chunk_index or 0) + within) / task.chunk_total
        percent = lo + done * (hi - lo)
        eta = max(0.0, per_chunk * task.chunk_total * (1 - done))
        return max(stored, int(percent)), eta

    if task.phase == 'downloading' and task.bytes_total:
        lo, hi = PHASE_SPANS['downloading']
        frac = min(1.0, (task.bytes_downloaded or 0) / task.bytes_total)
        rate = (task.bytes_downloaded or 0) / phase_elapsed if phase_elapsed > 1 else 0
        eta = ((task.bytes_total - (task.bytes_downloaded or 0)) / rate) if rate > 0 else None
        return max(stored, int(lo + frac * (hi - lo))), eta

    if task.phase == 'splitting':
        lo, hi = PHASE_SPANS['splitting']
        # No measurable signal here; creep toward the top of the band over ~30s
        return max(stored, int(lo + min(0.9, phase_elapsed / 30) * (hi - lo))), None

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

    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        password2 = request.form.get('password2', '')

        if not email or not password:
            flash('Email and password are required.', 'error')
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

        login_user(user)
        flash('Account created! Add your OpenAI API key in Settings to use your own quota.', 'success')
        return redirect(url_for('index'))

    return render_template('register.html')


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
        current_user.openai_api_key = api_key if api_key else None
        db.session.commit()
        flash('Settings saved.', 'success')
        return redirect(url_for('settings'))

    return render_template('settings.html')


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


def _user_has_api_key():
    """Check if current user has an API key available (own key or global fallback)."""
    if current_user.is_authenticated and current_user.openai_api_key:
        return True
    return bool(GLOBAL_OPENAI_KEY)


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
    return render_template('index.html', saved_feeds=saved_feeds)


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
            'duration_min': _float_or_none(request.form.get('duration_min')),
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
            'duration_min': episode.get('duration_min'),
        }

    openai_client = get_openai_client(current_user)
    if not openai_client:
        return jsonify({
            'error': 'No OpenAI API key configured. Add your key in Settings.'
        }), 400

    task_id = str(uuid.uuid4())
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
    )
    db.session.add(task)
    db.session.commit()

    parsed_url = urlparse(meta['audio_url'])
    audio_filename = f"temp_audio_{task_id}" + (os.path.splitext(parsed_url.path)[1] or '.mp3')
    source_url = meta['audio_url']

    def transcribe_thread():
        with app.app_context():
            try:
                download_audio(source_url, audio_filename, task_id)
                transcribe_audio(audio_filename, task_id, openai_client, language=language)
            except Exception as e:
                _update_task(task_id, status='error', phase='error', error_message=str(e))
            finally:
                if os.path.exists(audio_filename):
                    try:
                        os.remove(audio_filename)
                    except OSError:
                        pass

    thread = threading.Thread(target=transcribe_thread)
    thread.daemon = True
    thread.start()

    return jsonify({'task_id': task_id})


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


def _float_or_none(raw):
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


@app.route('/status/<task_id>')
@login_required
def get_status(task_id):
    task = db.session.get(TranscriptionTask, task_id)
    if not task or task.user_id != current_user.id:
        return jsonify({'error': 'Task not found'}), 404

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

    if task.status == 'error':
        result['error'] = task.error_message or 'Unknown error'

    # Partial text so the page fills in as chunks land, rather than staying empty
    if task.transcript_text and task.status != 'completed':
        result['partial_text'] = task.transcript_text

    if task.status == 'completed':
        result['download_txt'] = url_for('download_file', task_id=task_id, file_type='txt')
        result['download_srt'] = url_for('download_file', task_id=task_id, file_type='srt')
        result['transcript_text'] = task.transcript_text or ''
        if task.transcription_time:
            result['actual_transcription_time'] = f"{task.transcription_time:.1f} seconds"
        if task.language:
            result['language'] = task.language

    return jsonify(result)


@app.route('/download/<task_id>/<file_type>')
@login_required
def download_file(task_id, file_type):
    import json as _json
    from io import BytesIO

    task = db.session.get(TranscriptionTask, task_id)
    if not task or task.user_id != current_user.id or task.status != 'completed':
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
        (t.audio_duration / 60) * 0.006
        for t in tasks if t.audio_duration
    )
    return render_template('history.html', transcriptions=tasks, total_cost=total_cost)


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

with app.app_context():
    db.create_all()

    # Add columns that may be missing on existing databases
    from sqlalchemy import inspect, text
    inspector = inspect(db.engine)
    existing_cols = {c['name'] for c in inspector.get_columns('transcription_tasks')}
    for column, ddl_type in TASK_COLUMN_MIGRATIONS.items():
        if column not in existing_cols:
            db.session.execute(text(
                f'ALTER TABLE transcription_tasks ADD COLUMN {column} {ddl_type}'
            ))
    db.session.commit()

    # Transcription runs in a daemon thread, so a deploy or crash leaves tasks
    # stuck in a running state forever. Fail those at boot -- but only ones that
    # have gone quiet: this module is imported by every gunicorn worker, and a
    # worker respawning mid-life must not kill jobs another worker is running.
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=STALE_TASK_SECONDS)
    orphaned = [
        t for t in TranscriptionTask.query.filter(
            ~TranscriptionTask.status.in_(['completed', 'error'])
        ).all()
        if _seconds_since(t.heartbeat_at or t.started_at) > STALE_TASK_SECONDS
    ]
    for task in orphaned:
        task.status = 'error'
        task.phase = 'error'
        task.error_message = (
            'Transcription was interrupted and stopped making progress. Please try again.'
        )
    if orphaned:
        db.session.commit()

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
    print(f"Environment: {os.getenv('FLASK_ENV', 'development')}")
    print("=" * 50)

    host = '0.0.0.0' if os.getenv('FLASK_ENV') == 'production' else '127.0.0.1'
    debug = os.getenv('FLASK_ENV') != 'production'
    app.run(debug=debug, host=host, port=5002)
