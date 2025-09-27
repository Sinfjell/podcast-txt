# Terminal Commands

## Setup (run once)

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Usage

```bash
source venv/bin/activate
python3 podcast_transcriber.py "https://your-rss-feed-url.com/rss" [episode_number]
```

## Examples

```bash
# Most recent episode (default)
source venv/bin/activate
python3 podcast_transcriber.py "https://anchor.fm/s/fa9dcbb4/podcast/rss"

# Second most recent episode
source venv/bin/activate
python3 podcast_transcriber.py "https://anchor.fm/s/fa9dcbb4/podcast/rss" 1

# Third most recent episode
source venv/bin/activate
python3 podcast_transcriber.py "https://anchor.fm/s/fa9dcbb4/podcast/rss" 2
```

## How to choose episode

The script will show you available episodes when you run it:
- 0 = most recent episode
- 1 = second most recent
- 2 = third most recent
- etc.