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

from models import db, User, SavedFeed, Transcription

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


# In-memory transcription tracking (per-process, not persisted)
transcription_status = {}
transcription_results = {}

# Global fallback OpenAI key
GLOBAL_OPENAI_KEY = os.getenv('OPENAI_API_KEY')


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

    total_size = int(response.headers.get('content-length', 0))
    downloaded = 0
    start_time = time.time()

    with open(filename, 'wb') as f:
        for chunk in response.iter_content(chunk_size=8192):
            if chunk:
                f.write(chunk)
                downloaded += len(chunk)
                if total_size > 0:
                    progress = (downloaded / total_size) * 100
                    elapsed = time.time() - start_time
                    if progress > 0:
                        eta_seconds = (elapsed / progress) * (100 - progress)
                        transcription_status[task_id]['eta'] = f"{eta_seconds / 60:.1f} min"
                    transcription_status[task_id]['download_progress'] = progress
                    if elapsed > 0:
                        transcription_status[task_id]['download_speed'] = (
                            f"{downloaded / 1024 / 1024 / elapsed:.1f} MB/s"
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


def transcribe_audio(audio_file, output_txt, output_srt, task_id, openai_client):
    """Transcribe audio using OpenAI Whisper API."""
    try:
        if not openai_client:
            raise Exception(
                "No OpenAI API key configured. "
                "Go to Settings and add your key, or ask the admin to set a global key."
            )

        transcription_status[task_id]['status'] = 'splitting'
        transcription_status[task_id]['progress'] = 5
        audio_chunks = split_audio_if_needed(audio_file, max_size_mb=24)

        audio_duration = get_audio_duration(audio_chunks[0])
        if len(audio_chunks) > 1:
            audio_duration *= len(audio_chunks)

        transcription_status[task_id]['audio_duration'] = audio_duration
        estimated_time = max(30, audio_duration * 0.1)

        transcription_status[task_id]['status'] = 'transcribing'
        transcription_status[task_id]['progress'] = 10
        transcription_status[task_id]['eta'] = f"{estimated_time / 60:.1f} min"

        upload_start = time.time()
        all_segments = []
        full_text = ""

        for i, chunk_file in enumerate(audio_chunks):
            progress = 10 + (i / len(audio_chunks)) * 60
            transcription_status[task_id]['progress'] = progress
            transcription_status[task_id]['status'] = (
                f'transcribing chunk {i + 1}/{len(audio_chunks)}'
            )

            with open(chunk_file, 'rb') as f:
                chunk_transcript = openai_client.audio.transcriptions.create(
                    model="whisper-1",
                    file=f,
                    language="no",
                    response_format="verbose_json",
                    timestamp_granularities=["segment"]
                )

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

            os.remove(chunk_file)

        transcription_status[task_id]['status'] = 'processing'
        transcription_status[task_id]['progress'] = 50

        full_text = full_text.strip()

        with open(output_txt, 'w', encoding='utf-8') as f:
            f.write(full_text)

        transcription_status[task_id]['progress'] = 75

        with open(output_srt, 'w', encoding='utf-8') as f:
            if all_segments:
                for i, seg in enumerate(all_segments, 1):
                    f.write(f"{i}\n")
                    f.write(f"{format_timestamp(seg['start'])} --> {format_timestamp(seg['end'])}\n")
                    f.write(f"{seg['text'].strip()}\n\n")
            else:
                f.write("1\n00:00:00,000 --> 00:00:01,000\n")
                f.write(full_text)

        transcription_status[task_id]['status'] = 'completed'
        transcription_status[task_id]['progress'] = 100

        transcription_results[task_id] = {
            'txt_file': output_txt,
            'srt_file': output_srt,
            'episode_title': transcription_status[task_id].get('episode_title', 'Unknown'),
            'language': 'no',
            'language_probability': 1.0,
            'transcription_time': time.time() - upload_start,
            'transcript_text': full_text,
        }

        if os.path.exists(audio_file):
            os.remove(audio_file)

    except Exception as e:
        transcription_status[task_id]['status'] = 'error'
        transcription_status[task_id]['error'] = str(e)


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


def get_episodes_from_rss(rss_url):
    """Parse RSS feed and return episode list."""
    try:
        feed = feedparser.parse(rss_url)
        if not feed.entries:
            return None, "No episodes found in RSS feed"

        episodes = []
        for i, entry in enumerate(feed.entries):
            audio_url = None
            if hasattr(entry, 'enclosures'):
                for enc in entry.enclosures:
                    if enc.type.startswith('audio/'):
                        audio_url = enc.href
                        break

            if audio_url:
                desc = entry.get('description', '')
                episodes.append({
                    'index': i,
                    'title': entry.title,
                    'published': entry.get('published', 'Unknown date'),
                    'audio_url': audio_url,
                    'description': desc[:200] + '...' if len(desc) > 200 else desc
                })

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
    )


@app.route('/start_transcription', methods=['POST'])
def start_transcription():
    rss_url = request.form.get('rss_url')
    episode_index = int(request.form.get('episode_index'))

    episodes, error = get_episodes_from_rss(rss_url)
    if error or episode_index >= len(episodes):
        return jsonify({'error': 'Invalid episode selection'}), 400

    episode = episodes[episode_index]

    # Determine which OpenAI client to use:
    # 1. Logged-in user's saved key
    # 2. Inline key from form (guest / BYOK mode)
    # 3. Global fallback
    inline_key = request.form.get('openai_key', '').strip()
    user = current_user if current_user.is_authenticated else None

    if inline_key:
        openai_client = OpenAI(api_key=inline_key)
    else:
        openai_client = get_openai_client(user)

    if not openai_client:
        return jsonify({
            'error': 'No OpenAI API key provided. Paste your key to start transcribing.'
        }), 400

    task_id = str(uuid.uuid4())
    user_id = current_user.id if current_user.is_authenticated else None

    transcription_status[task_id] = {
        'status': 'starting',
        'progress': 0,
        'episode_title': episode['title'],
        'download_progress': 0,
        'eta': 'Calculating...',
        'start_time': time.time(),
        'user_id': user_id,
        'rss_url': rss_url,
    }

    parsed_url = urlparse(episode['audio_url'])
    audio_filename = f"temp_audio_{task_id}" + os.path.splitext(parsed_url.path)[1]
    txt_filename = f"transcript_{task_id}.txt"
    srt_filename = f"transcript_{task_id}.srt"

    def transcribe_thread():
        try:
            transcription_status[task_id]['status'] = 'downloading'
            download_audio(episode['audio_url'], audio_filename, task_id)
            transcribe_audio(audio_filename, txt_filename, srt_filename, task_id, openai_client)

            # Save to DB if user is logged in
            if user_id:
                with app.app_context():
                    result = transcription_results.get(task_id)
                    if result:
                        t = Transcription(
                            user_id=user_id,
                            episode_title=episode['title'],
                            rss_url=rss_url,
                            language=result.get('language', 'no'),
                            transcript_text=result.get('transcript_text', ''),
                        )
                        db.session.add(t)
                        db.session.commit()
        except Exception as e:
            transcription_status[task_id]['status'] = 'error'
            transcription_status[task_id]['error'] = str(e)

    thread = threading.Thread(target=transcribe_thread)
    thread.daemon = True
    thread.start()

    return jsonify({'task_id': task_id})


@app.route('/status/<task_id>')
def get_status(task_id):
    if task_id not in transcription_status:
        return jsonify({'error': 'Task not found'}), 404

    status = transcription_status[task_id].copy()
    status.pop('user_id', None)
    status.pop('rss_url', None)

    if 'start_time' in status:
        elapsed = time.time() - status['start_time']
        status['elapsed_time'] = f"{elapsed / 60:.1f} min"

    if status['status'] == 'completed' and task_id in transcription_results:
        status['download_txt'] = url_for('download_file', task_id=task_id, file_type='txt')
        status['download_srt'] = url_for('download_file', task_id=task_id, file_type='srt')
        result = transcription_results[task_id]
        if 'transcription_time' in result:
            status['actual_transcription_time'] = f"{result['transcription_time']:.1f} seconds"

    return jsonify(status)


@app.route('/download/<task_id>/<file_type>')
def download_file(task_id, file_type):
    if task_id not in transcription_results:
        return "File not found", 404

    result = transcription_results[task_id]

    if file_type == 'txt':
        filename = result['txt_file']
        dl_name = f"{result['episode_title'].replace(' ', '_')}.txt"
    elif file_type == 'srt':
        filename = result['srt_file']
        dl_name = f"{result['episode_title'].replace(' ', '_')}.srt"
    else:
        return "Invalid file type", 400

    if not os.path.exists(filename):
        return "File not found", 404

    return send_file(filename, as_attachment=True, download_name=dl_name)


@app.route('/transcription/<task_id>')
def transcription_page(task_id):
    if task_id not in transcription_status:
        return "Task not found", 404
    return render_template('transcription.html', task_id=task_id)


@app.route('/history')
@login_required
def history():
    transcriptions = Transcription.query.filter_by(
        user_id=current_user.id
    ).order_by(Transcription.created_at.desc()).limit(50).all()
    return render_template('history.html', transcriptions=transcriptions)


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


@app.route('/search-podcasts', methods=['GET'])
def search_podcasts():
    """Search for podcasts by name using iTunes Search API."""
    query = request.args.get('q', '').strip()
    if not query or len(query) < 2:
        return jsonify({'results': []})

    try:
        resp = requests.get(
            'https://itunes.apple.com/search',
            params={'term': query, 'media': 'podcast', 'limit': 10},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()

        results = []
        for item in data.get('results', []):
            feed_url = item.get('feedUrl')
            if not feed_url:
                continue
            results.append({
                'name': item.get('collectionName', ''),
                'artist': item.get('artistName', ''),
                'artwork': item.get('artworkUrl100', ''),
                'feed_url': feed_url,
                'genre': item.get('primaryGenreName', ''),
            })

        return jsonify({'results': results})
    except Exception as e:
        return jsonify({'results': [], 'error': str(e)})


# ---------------------------------------------------------------------------
# Init
# ---------------------------------------------------------------------------

with app.app_context():
    db.create_all()

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
