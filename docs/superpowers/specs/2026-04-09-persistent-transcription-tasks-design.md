# Persistent Transcription Tasks

## Problem

Transcription tasks are stored in-memory (Python dicts). When the Flask process restarts, all in-progress and completed tasks are lost, causing "Task not found" errors.

## Decisions

- **Require login** to transcribe (no more guest/BYOK mode)
- **Store all task state in SQLite** — replace both `transcription_status` and `transcription_results` dicts
- **Generate txt/srt on-the-fly** from DB — no files on disk except temporary audio during processing
- **Migrate existing `Transcription` data** to the new model (SRT unavailable for old entries)
- **Full session persistence** — users can close browser and return to see status/results

## New Model: `TranscriptionTask`

Replaces both the in-memory dicts and the `Transcription` model.

| Column | Type | Notes |
|--------|------|-------|
| id | String(36), PK | UUID |
| user_id | Integer, FK(users.id) | Required |
| episode_title | String(512) | |
| rss_url | String(1024) | Nullable |
| status | String(20) | pending, downloading, transcribing, completed, error |
| progress | Integer | 0-100 |
| download_progress | Integer | 0-100 |
| error_message | Text | Nullable |
| transcript_text | Text | Nullable, filled on completion |
| segments_json | Text | Nullable, JSON array of {start, end, text} for SRT generation |
| language | String(10) | Nullable |
| transcription_time | Float | Nullable, seconds |
| started_at | DateTime | |
| completed_at | DateTime | Nullable |

## Changed Routes

- `POST /start_transcription` — requires login, creates `TranscriptionTask` row, starts thread
- `GET /status/<task_id>` — reads from DB instead of dict
- `GET /transcription/<task_id>` — reads from DB, works after restart
- `GET /download/<task_id>/<type>` — generates txt/srt on-the-fly from `transcript_text` / `segments_json`
- `GET /history` — queries `TranscriptionTask` where status=completed

## Removed

- `transcription_status` dict
- `transcription_results` dict
- `Transcription` model (replaced by `TranscriptionTask`)
- Guest transcription (BYOK without login)

## Migration

Existing `Transcription` rows are migrated to `TranscriptionTask` with status=completed. They lack `segments_json`, so SRT download is unavailable for old entries; txt works fine.
