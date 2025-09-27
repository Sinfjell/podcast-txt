# Web App Instructions

## Setup (run once)

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Run the Web App

```bash
source venv/bin/activate
python3 app.py
```

## Usage

1. Open your browser and go to: http://127.0.0.1:5001
2. Enter the RSS feed URL
3. Select an episode from the list
4. Click "Start Transcription"
5. Wait for completion
6. Download the transcript and subtitle files

## Example RSS Feeds

```bash
# Test with this feed
https://anchor.fm/s/fa9dcbb4/podcast/rss

# Or any other podcast RSS feed
https://feeds.npr.org/510289/podcast.xml
```
