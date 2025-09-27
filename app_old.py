#!/usr/bin/env python3
"""
Podcast Transcriber Web App

A Flask web application for transcribing podcast episodes from RSS feeds.
"""

import os
import sys
import time
import threading
import requests
import feedparser
from flask import Flask, render_template, request, jsonify, send_file, flash, redirect, url_for
from faster_whisper import WhisperModel
from urllib.parse import urlparse
import uuid
import json

app = Flask(__name__)
app.secret_key = 'your-secret-key-here'

# Global variables for transcription status
transcription_status = {}
transcription_results = {}

def download_audio(url, filename):
    """Download audio file from URL with progress reporting."""
    response = requests.get(url, stream=True)
    response.raise_for_status()
    
    total_size = int(response.headers.get('content-length', 0))
    downloaded = 0
    
    with open(filename, 'wb') as f:
        for chunk in response.iter_content(chunk_size=8192):
            if chunk:
                f.write(chunk)
                downloaded += len(chunk)
                if total_size > 0:
                    progress = (downloaded / total_size) * 100
                    # Update progress in global status
                    if filename in transcription_status:
                        transcription_status[filename]['download_progress'] = progress
    
    return filename

def transcribe_audio(audio_file, output_txt, output_srt, task_id):
    """Transcribe audio using faster-whisper and save as text and SRT."""
    try:
        # Update status
        transcription_status[task_id]['status'] = 'loading_model'
        transcription_status[task_id]['progress'] = 0
        
        # Load Whisper model
        model = WhisperModel("large-v3", compute_type="int8")
        
        transcription_status[task_id]['status'] = 'transcribing'
        transcription_status[task_id]['progress'] = 10
        
        # Transcribe with automatic language detection, but prefer Norwegian
        segments, info = model.transcribe(
            audio_file,
            language="no",  # Force Norwegian if possible
            beam_size=5,
            best_of=5,
            temperature=0.0,
            condition_on_previous_text=True,
            initial_prompt="Dette er en podcast-episode på norsk."
        )
        
        transcription_status[task_id]['progress'] = 50
        transcription_status[task_id]['language'] = info.language
        transcription_status[task_id]['language_probability'] = info.language_probability
        
        # Write full transcript
        with open(output_txt, 'w', encoding='utf-8') as f:
            for segment in segments:
                f.write(segment.text + " ")
        
        transcription_status[task_id]['progress'] = 75
        
        # Reset segments iterator for SRT generation
        segments, _ = model.transcribe(
            audio_file,
            language="no",
            beam_size=5,
            best_of=5,
            temperature=0.0,
            condition_on_previous_text=True,
            initial_prompt="Dette er en podcast-episode på norsk."
        )
        
        # Write SRT subtitle file
        with open(output_srt, 'w', encoding='utf-8') as f:
            for i, segment in enumerate(segments, 1):
                start_time_srt = format_timestamp(segment.start)
                end_time_srt = format_timestamp(segment.end)
                f.write(f"{i}\n")
                f.write(f"{start_time_srt} --> {end_time_srt}\n")
                f.write(f"{segment.text.strip()}\n\n")
        
        transcription_status[task_id]['status'] = 'completed'
        transcription_status[task_id]['progress'] = 100
        
        # Store results
        transcription_results[task_id] = {
            'txt_file': output_txt,
            'srt_file': output_srt,
            'episode_title': transcription_status[task_id].get('episode_title', 'Unknown'),
            'language': info.language,
            'language_probability': info.language_probability
        }
        
        # Clean up audio file
        if os.path.exists(audio_file):
            os.remove(audio_file)
            
    except Exception as e:
        transcription_status[task_id]['status'] = 'error'
        transcription_status[task_id]['error'] = str(e)

def format_timestamp(seconds):
    """Format timestamp for SRT format (HH:MM:SS,mmm)."""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millisecs = int((seconds % 1) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millisecs:03d}"

def get_episodes_from_rss(rss_url):
    """Parse RSS feed and return list of episodes."""
    try:
        feed = feedparser.parse(rss_url)
        
        if not feed.entries:
            return None, "No episodes found in RSS feed"
        
        episodes = []
        for i, entry in enumerate(feed.entries):
            # Find audio enclosure
            audio_url = None
            if hasattr(entry, 'enclosures'):
                for enclosure in entry.enclosures:
                    if enclosure.type.startswith('audio/'):
                        audio_url = enclosure.href
                        break
            
            if audio_url:
                episodes.append({
                    'index': i,
                    'title': entry.title,
                    'published': entry.get('published', 'Unknown date'),
                    'audio_url': audio_url,
                    'description': entry.get('description', '')[:200] + '...' if len(entry.get('description', '')) > 200 else entry.get('description', '')
                })
        
        return episodes, None
        
    except Exception as e:
        return None, f"Error parsing RSS feed: {str(e)}"

@app.route('/')
def index():
    return render_template('index.html')

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
    
    # Show only first 10 episodes initially
    episodes_to_show = episodes[:10]
    has_more = len(episodes) > 10
    
    return render_template('episode_selection.html', 
                         episodes=episodes_to_show, 
                         all_episodes=episodes,
                         rss_url=rss_url, 
                         has_more=has_more)

@app.route('/start_transcription', methods=['POST'])
def start_transcription():
    rss_url = request.form.get('rss_url')
    episode_index = int(request.form.get('episode_index'))
    
    # Get episode details
    episodes, error = get_episodes_from_rss(rss_url)
    if error or episode_index >= len(episodes):
        return jsonify({'error': 'Invalid episode selection'}), 400
    
    episode = episodes[episode_index]
    
    # Generate unique task ID
    task_id = str(uuid.uuid4())
    
    # Initialize status
    transcription_status[task_id] = {
        'status': 'starting',
        'progress': 0,
        'episode_title': episode['title'],
        'download_progress': 0
    }
    
    # Generate filenames
    parsed_url = urlparse(episode['audio_url'])
    audio_filename = f"temp_audio_{task_id}" + os.path.splitext(parsed_url.path)[1]
    txt_filename = f"transcript_{task_id}.txt"
    srt_filename = f"transcript_{task_id}.srt"
    
    # Start transcription in background thread
    def transcribe_thread():
        try:
            # Download audio
            transcription_status[task_id]['status'] = 'downloading'
            download_audio(episode['audio_url'], audio_filename)
            
            # Transcribe
            transcribe_audio(audio_filename, txt_filename, srt_filename, task_id)
            
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
    
    # Add download link if completed
    if status['status'] == 'completed' and task_id in transcription_results:
        status['download_txt'] = url_for('download_file', task_id=task_id, file_type='txt')
        status['download_srt'] = url_for('download_file', task_id=task_id, file_type='srt')
    
    return jsonify(status)

@app.route('/download/<task_id>/<file_type>')
def download_file(task_id, file_type):
    if task_id not in transcription_results:
        return "File not found", 404
    
    result = transcription_results[task_id]
    
    if file_type == 'txt':
        filename = result['txt_file']
        download_name = f"{result['episode_title'].replace(' ', '_')}.txt"
    elif file_type == 'srt':
        filename = result['srt_file']
        download_name = f"{result['episode_title'].replace(' ', '_')}.srt"
    else:
        return "Invalid file type", 400
    
    if not os.path.exists(filename):
        return "File not found", 404
    
    return send_file(filename, as_attachment=True, download_name=download_name)

@app.route('/transcription/<task_id>')
def transcription_page(task_id):
    if task_id not in transcription_status:
        return "Task not found", 404
    
    return render_template('transcription.html', task_id=task_id)

if __name__ == '__main__':
    app.run(debug=True, host='127.0.0.1', port=5002)
