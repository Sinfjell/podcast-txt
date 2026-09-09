# Podcast Transcriber Web App

A Flask web application that downloads podcast episodes from RSS feeds and transcribes them using OpenAI's Whisper API. Features an intuitive web interface with real-time progress tracking and support for large audio files through intelligent splitting.

## 🐳 Docker Support

This app now includes complete Docker support for easy deployment and local development. See [Docker Setup](#docker-setup) section below.

## Features

- **🌐 Web Interface**: Easy-to-use Flask web app with real-time progress tracking
- **📡 RSS Feed Support**: Automatically parses podcast RSS feeds and lists episodes
- **🍎 Apple Podcasts Integration**: Convert Apple Podcasts URLs to RSS feeds automatically
- **🎵 Audio Processing**: Handles large audio files by splitting them into manageable chunks
- **📝 High-Quality Transcription**: Uses OpenAI's Whisper API for accurate speech-to-text
- **📄 Multiple Output Formats**: Generates both full transcripts and SRT subtitle files
- **🔍 RSS Help Guide**: Built-in help system for finding RSS feeds from various platforms
- **⚡ Real-time Updates**: Live progress tracking with download speed and ETA estimates

## Requirements

- Python 3.7+
- OpenAI API key
- Virtual environment (recommended)

## 🚀 Quick Start with Docker (Recommended)

### Option 1: One-Click Setup
```bash
git clone <repository-url>
cd podcast_txt
./run.sh
```

### Option 2: Manual Docker Setup
```bash
git clone <repository-url>
cd podcast_txt

# Copy environment template
cp .env.example .env

# Edit .env file and add your OpenAI API key
# OPENAI_API_KEY=your_api_key_here

# Run with Docker Compose
docker-compose up --build
```

The app will be available at **http://localhost:5002**

## 📋 Manual Installation (Alternative)

If you prefer to run without Docker:

1. **Clone the repository**
   ```bash
   git clone <repository-url>
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

   For development, install the test dependencies too:
   ```bash
   pip install -r requirements-dev.txt
   ```

4. **Set up OpenAI API key**
   ```bash
   # Create .env file
   echo "OPENAI_API_KEY=your_api_key_here" > .env
   ```

## Running the tests

```bash
pytest test_app.py
```

The suite points `DATABASE_URL` at a temporary file before importing the app, so
it never touches `data/podcast.db`.

## Docker Setup

### Environment Variables
Create a `.env` file with:
```env
# Required
OPENAI_API_KEY=sk-your-openai-api-key-here

# Optional
SECRET_KEY=your-secret-key-change-in-production
FLASK_ENV=development
```

### Docker Commands
```bash
# Start the app
docker-compose up

# Start in background
docker-compose up -d

# Stop the app
docker-compose down

# View logs
docker-compose logs -f

# Rebuild after code changes
docker-compose up --build
```

## Usage

### Start the Web App (Manual Installation)

```bash
# Activate virtual environment
source venv/bin/activate

# Start the Flask app
python3 app.py
```

The app will be available at: **http://127.0.0.1:5002** (manual) or **http://localhost:5002** (Docker)

### Using the Web Interface

1. **Enter RSS Feed URL**: Paste your podcast's RSS feed URL
2. **Convert Apple Podcasts URLs**: Use the built-in converter for Apple Podcasts links
3. **Select Episode**: Choose from the list of available episodes
4. **Start Transcription**: Click "Start Transcription" and watch real-time progress
5. **Download Results**: Get both transcript (.txt) and subtitle (.srt) files

### Finding RSS Feeds

The app includes a comprehensive help guide for finding RSS feeds from:
- **Apple Podcasts**: Automatic URL conversion to RSS
- **Spotify**: Instructions to find original RSS feeds
- **Google Podcasts**: Guidance for locating RSS feeds
- **Direct RSS**: How to find RSS feeds from podcast websites

## How It Works

### Audio Processing
- **Smart Splitting**: Large files (>25MB) are automatically split into chunks
- **Sequential Processing**: Each chunk is transcribed separately
- **Seamless Assembly**: Results are combined with proper timestamps

### Error Handling
- **Download Protection**: Handles 403 errors from hosting services like Buzzsprout
- **Retry Logic**: Multiple header strategies for different podcast hosts
- **Clear Error Messages**: Helpful feedback when downloads fail

## API Endpoints

- `GET /` - Main application interface
- `POST /parse_rss` - Parse RSS feed and list episodes
- `POST /start_transcription` - Start transcription process
- `GET /transcription/<task_id>` - View transcription results
- `GET /status/<task_id>` - Get real-time transcription status
- `GET /download/<task_id>/<format>` - Download transcript files
- `GET /rss-help` - RSS feed help guide
- `POST /convert-apple-url` - Convert Apple Podcasts URL to RSS

## Configuration

### Environment Variables
- `OPENAI_API_KEY` — the **global trial key**. Optional, and it costs you money:
  every account with no key of its own transcribes on it. Leave it unset and the
  trial is off; users must add their own key. See "Free trial" below.
- `SECRET_KEY` — Flask session key. Set it in production.
- `DATABASE_URL` — SQLAlchemy URL. Defaults to `sqlite:///data/podcast.db`.

### Free trial

Set `OPENAI_API_KEY` and keyless accounts get a metered allowance on it. Audio
seconds are reserved before any request reaches Whisper, and charged on audio
**we measure with ffprobe** — never on the duration the feed or the client
claims, which a caller controls.

| Variable | Default | What it bounds |
| --- | --- | --- |
| `TRIAL_ENABLED` | `1` | Kill switch. `0` stops handing out the key. |
| `TRIAL_MINUTES` | `60` | Free audio minutes per account (~$0.36 each). |
| `TRIAL_GLOBAL_MINUTES` | `600` | **Lifetime** minutes across all accounts (~$3.60). |
| `TRIAL_MAX_EPISODE_MINUTES` | `180` | Longest single episode the trial accepts. |
| `TRIAL_UNKNOWN_ESTIMATE_MINUTES` | `30` | Reserved when a feed states no duration. |

`TRIAL_GLOBAL_MINUTES` is a **lifetime ceiling, not a monthly budget** — nothing
resets it on a schedule. It counts minutes actually *spent*: a job that fails
before transcribing gives its reservation back, so the ceiling tracks the bill
rather than the attempts. That is deliberate — the failure mode is "the trial
stops working", never "the bill kept growing". At the defaults, ten accounts
using their full grant exhaust it.
Raising the env var re-opens it.

`TRIAL_MINUTES` is the default only. A per-account override lives in
`users.trial_seconds_limit`; `NULL` means "use the default", so raising the env
var lifts everyone who has no individual grant. There is no UI for it — set it
with SQL:

```sql
UPDATE users SET trial_seconds_limit = 7200 WHERE email = 'someone@example.com';
```

Run `ops/trial-usage.sh` to see what the trial has actually cost.

### Capacity limits

The trial ceiling caps what a surge can **cost**. These cap what it can **break**:
each in-flight job holds up to 500 MB on disk, and splitting decodes the whole
episode to raw PCM in memory (an hour of 44.1 kHz stereo is ~635 MB). Production
is a shared Plesk host with 50+ other services on it, so exhausting its memory
takes other sites down too.

| Variable | Default | What it bounds |
| --- | --- | --- |
| `MAX_CONCURRENT_TRANSCRIPTIONS` | `2` | Jobs at once **per gunicorn worker**. |
| `MIN_FREE_DISK_MB` | `2048` | Refuse to start below this much free space. |

`MAX_CONCURRENT_TRANSCRIPTIONS` is per worker — a `threading.Semaphore` cannot
span processes — so with `--workers 2` the real ceiling is 4 concurrent jobs:
~2 GB of disk against 24 GB free, ~2.5 GB of decode against 4.5 GB available.
Over the limit, requests get a 503 telling the user to try again shortly; they
are not queued, because an unbounded queue is the same outage arriving later.

### Also set a hard budget at OpenAI

The ceilings above are enforced by this app. Set a monthly spend limit on the
OpenAI account as well — that one still holds if this code has a bug.

### File Size Limits
- **OpenAI Limit**: 25MB per audio file
- **Auto-Splitting**: Files larger than 24MB are split automatically
- **Chunk Size**: Optimal chunk duration for best transcription quality

## Troubleshooting

### Common Issues

1. **403 Forbidden Error**
   - Some podcast hosts (like Buzzsprout) restrict direct downloads
   - Try a different episode or contact the podcast creator

2. **OpenAI API Errors**
   - Ensure your API key is valid and has sufficient credits
   - Check the `.env` file contains your API key

3. **Large File Processing**
   - Files are automatically split for OpenAI's 25MB limit
   - Processing time scales with audio length

### Performance Tips

- **First Run**: May take longer as dependencies are downloaded
- **Long Episodes**: Transcription time is typically 1/10th of audio duration
- **Memory Usage**: Flask app is lightweight, most processing is done by OpenAI

## Dependencies

- `flask` - Web framework
- `requests` - HTTP requests
- `feedparser` - RSS feed parsing
- `openai` - OpenAI Whisper API
- `pydub` - Audio processing and splitting
- `python-dotenv` - Environment variable management

## License

This project is provided as-is for educational and personal use.

## Support

For issues or questions:
1. Check that your RSS feed URL is valid and contains audio files
2. Ensure your OpenAI API key is correctly configured
3. Verify all dependencies are installed correctly
4. Check the built-in RSS help guide for finding feeds

## 📝 Changelog

### v2.0.0 - Docker Support & Production Ready (Current)
- 🐳 **Added complete Docker support** with Dockerfile and docker-compose.yml
- 🚀 **One-click setup** with `./run.sh` script
- 🔧 **Production-ready configuration** with proper environment handling
- 📦 **Multi-stage Docker build** for optimized image size
- 🏥 **Health checks** for container monitoring
- 🔒 **Secure environment handling** with .env file support
- 📚 **Comprehensive documentation** with Docker setup instructions
- 🧹 **Cleaned up debug code** for production deployment
- ⚡ **Improved error handling** and logging

### v1.0.0 - Core Features
- ✅ Added Apple Podcasts URL to RSS converter
- ✅ Improved audio download error handling
- ✅ Enhanced RSS help guide with platform-specific instructions
- ✅ Added real-time progress tracking
- ✅ Implemented audio file splitting for large files
- ✅ Fixed 403 Forbidden errors with better headers

## 🚀 Deployment Options

The Docker setup works on multiple platforms:

- **Local Development**: `docker-compose up --build`
- **Production Hosting**: Railway, Render, DigitalOcean, AWS ECS
- **Cloud Platforms**: Google Cloud Run, Azure Container Instances
- **Self-hosted**: Any Docker-compatible server

## 🔧 Development

For development with live code reloading:
```bash
# Create development override
echo 'version: "3.8"
services:
  podcast-transcriber:
    volumes:
      - .:/app
    environment:
      - FLASK_ENV=development' > docker-compose.dev.yml

# Run with development config
docker-compose -f docker-compose.yml -f docker-compose.dev.yml up --build
```