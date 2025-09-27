#!/usr/bin/env python3
"""
Podcast Transcriber Web App with OpenAI API

A Flask web application for transcribing podcast episodes from RSS feeds using OpenAI's Whisper API.
"""

import os
import sys
import time
import threading
import requests
import feedparser
from flask import Flask, render_template, request, jsonify, send_file, flash, redirect, url_for
from urllib.parse import urlparse
import uuid
import json
from dotenv import load_dotenv
import openai
from openai import OpenAI
import subprocess
import tempfile
from pydub import AudioSegment

# Load environment variables
load_dotenv()

app = Flask(__name__)
app.secret_key = 'your-secret-key-here'

# Global variables for transcription status
transcription_status = {}
transcription_results = {}

# OpenAI configuration
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
if not OPENAI_API_KEY:
    print("Warning: OPENAI_API_KEY not found in .env file")

client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

def download_audio(url, filename, task_id):
    """Download audio file from URL with progress reporting."""
    response = requests.get(url, stream=True)
    response.raise_for_status()
    
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
                    
                    # Calculate ETA
                    if progress > 0:
                        eta_seconds = (elapsed / progress) * (100 - progress)
                        eta_minutes = eta_seconds / 60
                        transcription_status[task_id]['eta'] = f"{eta_minutes:.1f} min"
                    
                    transcription_status[task_id]['download_progress'] = progress
                    transcription_status[task_id]['download_speed'] = f"{(downloaded / 1024 / 1024 / elapsed):.1f} MB/s" if elapsed > 0 else "0 MB/s"
    
    return filename

def get_audio_duration(audio_file):
    """Get audio file duration in seconds."""
    try:
        import librosa
        duration = librosa.get_duration(path=audio_file)
        return duration
    except:
        # Fallback: estimate based on file size (rough estimate)
        file_size_mb = os.path.getsize(audio_file) / (1024 * 1024)
        # Rough estimate: 1MB ≈ 1 minute of audio
        return file_size_mb * 60


def split_audio_if_needed(audio_file, max_size_mb=24):
    """Split audio file into chunks if it exceeds the maximum size limit for OpenAI API."""
    file_size_mb = os.path.getsize(audio_file) / (1024 * 1024)
    print(f"DEBUG: Checking file {audio_file}, size: {file_size_mb:.1f}MB, limit: {max_size_mb}MB")
    
    if file_size_mb <= max_size_mb:
        print(f"DEBUG: File is within limit, no splitting needed")
        return [audio_file]
    
    print(f"Audio file is {file_size_mb:.1f}MB, splitting into chunks to fit OpenAI's {max_size_mb}MB limit...")
    
    # Calculate how many chunks we need
    num_chunks = int((file_size_mb / max_size_mb) + 1)
    print(f"Will split into {num_chunks} chunks")
    
    try:
        print(f"DEBUG: Loading audio file with pydub: {audio_file}")
        # Load audio file
        audio = AudioSegment.from_file(audio_file)
        print(f"DEBUG: Audio loaded successfully, duration: {len(audio)/1000:.1f} seconds")
        
        # Calculate chunk duration
        total_duration_ms = len(audio)
        chunk_duration_ms = total_duration_ms // num_chunks
        
        print(f"Total duration: {total_duration_ms/1000:.1f}s, chunk duration: {chunk_duration_ms/1000:.1f}s")
        
        # Split audio into chunks
        base_name = os.path.splitext(audio_file)[0]
        chunk_files = []
        
        for i in range(num_chunks):
            start_time = i * chunk_duration_ms
            end_time = min((i + 1) * chunk_duration_ms, total_duration_ms)
            
            # Extract chunk
            chunk = audio[start_time:end_time]
            chunk_file = f"{base_name}_chunk_{i+1}.mp3"
            
            # Export chunk
            chunk.export(chunk_file, format="mp3")
            
            chunk_size_mb = os.path.getsize(chunk_file) / (1024 * 1024)
            print(f"Chunk {i+1}: {chunk_file}, size: {chunk_size_mb:.1f}MB, duration: {(end_time-start_time)/1000:.1f}s")
            
            chunk_files.append(chunk_file)
        
        # Remove original file
        os.remove(audio_file)
        print(f"Successfully split into {len(chunk_files)} chunks")
        
        return chunk_files
            
    except Exception as e:
        print(f"Error splitting audio: {e}")
        # If splitting fails, return original file
        return [audio_file]

def transcribe_audio_openai(audio_file, output_txt, output_srt, task_id):
    """Transcribe audio using OpenAI Whisper API - 10x faster!"""
    try:
        if not client:
            raise Exception("OpenAI API key not configured. Please check your .env file.")
        
        # Split audio if needed to fit OpenAI's 25MB limit
        print(f"DEBUG: Before splitting - audio_file: {audio_file}")
        transcription_status[task_id]['status'] = 'splitting'
        transcription_status[task_id]['progress'] = 5
        transcription_status[task_id]['debug'] = f"Before splitting: {audio_file}"
        
        audio_chunks = split_audio_if_needed(audio_file, max_size_mb=24)
        print(f"DEBUG: After splitting - got {len(audio_chunks)} chunks")
        transcription_status[task_id]['debug'] = f"Split into {len(audio_chunks)} chunks"
        
        # Get audio duration for time estimation (use first chunk if multiple)
        audio_duration = get_audio_duration(audio_chunks[0])
        if len(audio_chunks) > 1:
            # Estimate total duration based on first chunk
            audio_duration = audio_duration * len(audio_chunks)
        
        transcription_status[task_id]['audio_duration'] = audio_duration
        estimated_transcription_time = max(30, audio_duration * 0.1)  # Estimate: 10% of audio duration, min 30 seconds
        
        # Update status
        transcription_status[task_id]['status'] = 'transcribing'
        transcription_status[task_id]['progress'] = 10
        transcription_status[task_id]['eta'] = f"{estimated_transcription_time/60:.1f} min"
        
        upload_start = time.time()
        
        # Transcribe all chunks
        all_segments = []
        full_text = ""
        
        for i, chunk_file in enumerate(audio_chunks):
            print(f"Transcribing chunk {i+1}/{len(audio_chunks)}: {chunk_file}")
            
            # Update progress
            progress = 10 + (i / len(audio_chunks)) * 60  # 10-70% for transcription
            transcription_status[task_id]['progress'] = progress
            transcription_status[task_id]['status'] = f'transcribing chunk {i+1}/{len(audio_chunks)}'
            
            chunk_size_mb = os.path.getsize(chunk_file) / (1024 * 1024)
            print(f"DEBUG: Transcribing chunk {i+1}, size: {chunk_size_mb:.1f}MB")
            
            # Upload and transcribe chunk with OpenAI
            with open(chunk_file, 'rb') as f:
                chunk_transcript = client.audio.transcriptions.create(
                    model="whisper-1",
                    file=f,
                    language="no",  # Norwegian
                    response_format="verbose_json",
                    timestamp_granularities=["segment"]
                )
            
            # Add chunk text to full text
            full_text += chunk_transcript.text + " "
            
            # Adjust timestamps for segments (add offset based on chunk position)
            if hasattr(chunk_transcript, 'segments') and chunk_transcript.segments:
                chunk_duration = audio_duration / len(audio_chunks)
                time_offset = i * chunk_duration
                
                for segment in chunk_transcript.segments:
                    # Create a new segment dict with adjusted timestamps
                    adjusted_segment = {
                        'start': segment.start + time_offset,
                        'end': segment.end + time_offset,
                        'text': segment.text
                    }
                    all_segments.append(adjusted_segment)
            
            # Clean up chunk file
            os.remove(chunk_file)
            
        # Create combined transcript object
        class CombinedTranscript:
            def __init__(self, text, segments, language):
                self.text = text.strip()
                self.segments = segments
                self.language = language
                self.language_probability = 1.0
        
        transcript = CombinedTranscript(full_text, all_segments, "no")
        
        transcription_status[task_id]['status'] = 'processing'
        transcription_status[task_id]['progress'] = 50
        transcription_status[task_id]['language'] = transcript.language
        transcription_status[task_id]['language_probability'] = getattr(transcript, 'language_probability', 1.0)
        
        # Write full transcript
        with open(output_txt, 'w', encoding='utf-8') as f:
            f.write(transcript.text)
        
        transcription_status[task_id]['progress'] = 75
        
        # Write SRT subtitle file
        with open(output_srt, 'w', encoding='utf-8') as f:
            if hasattr(transcript, 'segments') and transcript.segments:
                for i, segment in enumerate(transcript.segments, 1):
                    start_time_srt = format_timestamp(segment['start'])
                    end_time_srt = format_timestamp(segment['end'])
                    f.write(f"{i}\n")
                    f.write(f"{start_time_srt} --> {end_time_srt}\n")
                    f.write(f"{segment['text'].strip()}\n\n")
            else:
                # Fallback: create a single subtitle entry
                f.write("1\n")
                f.write("00:00:00,000 --> 00:00:01,000\n")
                f.write(transcript.text)
        
        transcription_status[task_id]['status'] = 'completed'
        transcription_status[task_id]['progress'] = 100
        
        # Store results
        transcription_results[task_id] = {
            'txt_file': output_txt,
            'srt_file': output_srt,
            'episode_title': transcription_status[task_id].get('episode_title', 'Unknown'),
            'language': transcript.language,
            'language_probability': getattr(transcript, 'language_probability', 1.0),
            'transcription_time': time.time() - upload_start
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
        'download_progress': 0,
        'eta': 'Calculating...',
        'start_time': time.time()
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
            download_audio(episode['audio_url'], audio_filename, task_id)
            
            # Transcribe
            transcribe_audio_openai(audio_filename, txt_filename, srt_filename, task_id)
            
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
    
    # Calculate elapsed time
    if 'start_time' in status:
        elapsed = time.time() - status['start_time']
        status['elapsed_time'] = f"{elapsed/60:.1f} min"
    
    # Add download link if completed
    if status['status'] == 'completed' and task_id in transcription_results:
        status['download_txt'] = url_for('download_file', task_id=task_id, file_type='txt')
        status['download_srt'] = url_for('download_file', task_id=task_id, file_type='srt')
        if 'transcription_time' in transcription_results[task_id]:
            status['actual_transcription_time'] = f"{transcription_results[task_id]['transcription_time']:.1f} seconds"
    
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

@app.route('/rss-help')
def rss_help():
    """Display help page for finding RSS URLs."""
    return render_template('rss_help.html')

if __name__ == '__main__':
    print("=" * 50)
    print("STARTING FLASK APP WITH COMPRESSION DEBUG")
    print("=" * 50)
    app.run(debug=True, host='127.0.0.1', port=5002)
