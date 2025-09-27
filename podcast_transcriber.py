#!/usr/bin/env python3
"""
Podcast RSS Feed Transcriber

Downloads the most recent episode from a podcast RSS feed and transcribes it
using faster-whisper with automatic language detection (preferring Norwegian).
"""

import sys
import os
import requests
import feedparser
from faster_whisper import WhisperModel
from urllib.parse import urlparse
import time


def download_audio(url, filename):
    """Download audio file from URL with progress reporting."""
    print(f"Downloading audio from: {url}")
    
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
                    print(f"\rDownload progress: {progress:.1f}%", end='', flush=True)
    
    print(f"\nDownload complete: {filename}")
    return filename


def transcribe_audio(audio_file, output_txt, output_srt):
    """Transcribe audio using faster-whisper and save as text and SRT."""
    print("Loading Whisper model (large-v3)...")
    model = WhisperModel("large-v3", compute_type="int8")
    
    print("Starting transcription...")
    start_time = time.time()
    
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
    
    print(f"Detected language: {info.language} (probability: {info.language_probability:.2f})")
    
    # Write full transcript
    print("Writing transcript to episode.txt...")
    with open(output_txt, 'w', encoding='utf-8') as f:
        for segment in segments:
            f.write(segment.text + " ")
    
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
    print("Writing subtitles to episode.srt...")
    with open(output_srt, 'w', encoding='utf-8') as f:
        for i, segment in enumerate(segments, 1):
            start_time_srt = format_timestamp(segment.start)
            end_time_srt = format_timestamp(segment.end)
            f.write(f"{i}\n")
            f.write(f"{start_time_srt} --> {end_time_srt}\n")
            f.write(f"{segment.text.strip()}\n\n")
    
    elapsed_time = time.time() - start_time
    print(f"Transcription completed in {elapsed_time:.1f} seconds")


def format_timestamp(seconds):
    """Format timestamp for SRT format (HH:MM:SS,mmm)."""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millisecs = int((seconds % 1) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millisecs:03d}"


def get_audio_url_from_rss(rss_url, episode_index=0):
    """Parse RSS feed and get a specific episode's audio URL."""
    print(f"Parsing RSS feed: {rss_url}")
    
    feed = feedparser.parse(rss_url)
    
    if not feed.entries:
        raise ValueError("No episodes found in RSS feed")
    
    # Show available episodes
    print(f"\nAvailable episodes (showing first 10):")
    for i, entry in enumerate(feed.entries[:10]):
        title = entry.title[:60] + "..." if len(entry.title) > 60 else entry.title
        published = entry.get('published', 'Unknown date')
        print(f"  {i}: {title} ({published})")
    
    if len(feed.entries) > 10:
        print(f"  ... and {len(feed.entries) - 10} more episodes")
    
    # Get the specified episode
    if episode_index >= len(feed.entries):
        raise ValueError(f"Episode index {episode_index} not found. Available: 0-{len(feed.entries)-1}")
    
    episode = feed.entries[episode_index]
    
    print(f"\nSelected episode {episode_index}: {episode.title}")
    print(f"Published: {episode.get('published', 'Unknown date')}")
    
    # Find the audio enclosure
    audio_url = None
    if hasattr(episode, 'enclosures'):
        for enclosure in episode.enclosures:
            if enclosure.type.startswith('audio/'):
                audio_url = enclosure.href
                break
    
    if not audio_url:
        raise ValueError("No audio file found in the selected episode")
    
    print(f"Audio URL: {audio_url}")
    return audio_url, episode.title


def main():
    # Parse command line arguments
    if len(sys.argv) < 2:
        print("Usage: python3 podcast_transcriber.py <rss_url> [episode_index]")
        print("Example: python3 podcast_transcriber.py 'https://anchor.fm/s/fa9dcbb4/podcast/rss' 0")
        print("         (0 = most recent, 1 = second most recent, etc.)")
        sys.exit(1)
    
    rss_url = sys.argv[1]
    episode_index = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    
    try:
        # Get audio URL from RSS feed
        audio_url, episode_title = get_audio_url_from_rss(rss_url, episode_index)
        
        # Generate filenames
        parsed_url = urlparse(audio_url)
        audio_filename = "episode_audio" + os.path.splitext(parsed_url.path)[1]
        txt_filename = "episode.txt"
        srt_filename = "episode.srt"
        
        # Download audio
        download_audio(audio_url, audio_filename)
        
        # Transcribe audio
        transcribe_audio(audio_filename, txt_filename, srt_filename)
        
        # Print results
        print("\n" + "="*50)
        print("TRANSCRIPTION COMPLETE")
        print("="*50)
        print(f"Episode: {episode_title}")
        print(f"Audio file: {audio_filename}")
        print(f"Transcript: {txt_filename}")
        print(f"Subtitles: {srt_filename}")
        print("="*50)
        
        # Clean up audio file (optional)
        if os.path.exists(audio_filename):
            os.remove(audio_filename)
            print(f"Cleaned up temporary audio file: {audio_filename}")
        
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
