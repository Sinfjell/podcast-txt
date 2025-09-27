# Podcast Transcriber

A Python script that downloads the most recent episode from a podcast RSS feed and transcribes it using faster-whisper with automatic language detection (preferring Norwegian).

## Features

- **RSS Feed Parsing**: Automatically parses podcast RSS feeds
- **Audio Download**: Downloads the most recent episode with progress reporting
- **Speech-to-Text**: Uses faster-whisper with "large-v3" model for high-quality transcription
- **Language Detection**: Automatically detects language, prefers Norwegian ("no")
- **Multiple Output Formats**: Generates both full transcript and time-coded subtitles
- **Efficient Processing**: Uses `compute_type="int8"` for optimal local performance

## Requirements

- Python 3.7+
- Virtual environment (recommended)

## Installation

1. **Clone or download this repository**
   ```bash
   cd podcast_txt
   ```

2. **Create and activate virtual environment**
   ```bash
   python3 -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

## Usage

### Basic Usage

```bash
# Activate virtual environment
source venv/bin/activate

# Run with default RSS feed (test feed)
python3 podcast_transcriber.py
```

### With Custom RSS Feed

```bash
# Run with your own RSS feed URL
python3 podcast_transcriber.py "https://your-podcast-rss-feed.com/rss"
```

### Examples

```bash
# NPR podcast
python3 podcast_transcriber.py "https://feeds.npr.org/510289/podcast.xml"

# Any other podcast RSS feed
python3 podcast_transcriber.py "https://example.com/podcast/feed.xml"
```

## Output Files

The script generates two files:

- **`episode.txt`** - Full transcript of the podcast episode
- **`episode.srt`** - Time-coded subtitle file in SRT format

## Process Flow

1. **RSS Parsing**: Parses the provided RSS feed URL
2. **Episode Selection**: Selects the most recent episode (first `<item>`)
3. **Audio Download**: Downloads the audio file with progress reporting
4. **Transcription**: Runs speech-to-text using faster-whisper
5. **File Generation**: Creates transcript and subtitle files
6. **Cleanup**: Removes temporary audio file

## Configuration

The script uses these default settings:
- **Model**: `large-v3` (high-quality transcription)
- **Compute Type**: `int8` (efficient local processing)
- **Language**: Auto-detection with Norwegian preference
- **Beam Size**: 5
- **Temperature**: 0.0 (deterministic output)

## Troubleshooting

### Common Issues

1. **ModuleNotFoundError**: Make sure virtual environment is activated
   ```bash
   source venv/bin/activate
   ```

2. **No audio file found**: Check if the RSS feed contains audio enclosures
   - The feed must have `<enclosure>` tags with `type="audio/*"`

3. **Transcription takes long time**: This is normal for long episodes
   - The script shows progress during transcription
   - Consider using a smaller model for faster processing

### Performance Tips

- **First run**: The script downloads the Whisper model (~3GB) on first use
- **Long episodes**: Transcription time depends on audio length (typically 1/10th of audio duration)
- **Memory usage**: Large-v3 model requires ~6GB RAM

## Dependencies

- `feedparser` - RSS feed parsing
- `requests` - HTTP requests for audio download
- `faster-whisper` - Speech-to-text transcription

## License

This script is provided as-is for educational and personal use.

## Support

For issues or questions:
1. Check that your RSS feed URL is valid and contains audio files
2. Ensure you have sufficient disk space and RAM
3. Verify all dependencies are installed correctly
