# Customer API

Resolve an episode, start a transcription, poll until ready, then fetch the
transcript — the same path as the web UI, over HTTP.

New accounts get the same free trial minutes as the UI (on our OpenAI key)
before you add your own. Jobs and trial minutes belong to your account.

## Authentication

Generate a key in **[Settings](https://podskrift.com/settings)** → API key
(`psk_…`). One key per account.

Send it on every request as either:

`Authorization: Bearer psk_…`

or

`X-Api-Key: psk_…`

Revoke or Regenerate in Settings — old keys stop working immediately. Never
commit a real key.

```bash
export PODSKRIFT_API_KEY='psk_…'   # from Settings — never commit
```

## POST /api/v1/resolve

Also available as `GET` with the same fields as query parameters.

Look up a public catalog episode (RSS / iTunes / Spotify / Apple / direct
audio) **without** creating a transcription job.

| Field | In | Meaning |
| --- | --- | --- |
| `publisher` / `show` | body or query | Show title (case-insensitive; exact match preferred) |
| `date` | body or query | `YYYY-MM-DD`, Europe/Oslo calendar date |
| `url` | body or query | Optional Spotify / Apple / `.mp3` / RSS instead of (or with) publisher |

```bash
curl -sS -H "Authorization: Bearer $PODSKRIFT_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"publisher":"Spårtsklubben","date":"2026-09-10"}' \
  https://podskrift.com/api/v1/resolve
```

Example response:

```json
{
  "episode": {
    "title": "Ukas iddiot …",
    "publisher": "Spårtsklubben",
    "published_at": "2026-09-10",
    "audio_url": "https://…/episode.mp3",
    "rss_url": "https://…/feed.xml",
    "duration_min": 62.0,
    "artwork_url": "https://…/art.jpg",
    "transcript_status": "none"
  }
}
```

Catalog hits have no `id` yet — that appears after you start a transcription.
Not found / no public RSS → `404` with `error` and `"episode": null`.

## POST /api/v1/transcriptions

Resolve (if needed) and enqueue Whisper for your account. Same trial path and
OpenAI key rules as the web UI.

Request fields match resolve (`publisher`+`date` and/or `url`). You may also
pass a resolved `audio_url` with `title` (and optional `publisher`,
`published_at`, `duration_min`, `rss_url`, `artwork_url`), plus optional
`language`.

```bash
curl -sS -H "Authorization: Bearer $PODSKRIFT_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"publisher":"Spårtsklubben","date":"2026-09-10"}' \
  https://podskrift.com/api/v1/transcriptions
```

Example response (`201` when a new job is created):

```json
{
  "id": "a1b2c3d4e5f6",
  "title": "Ukas iddiot …",
  "publisher": "Spårtsklubben",
  "published_at": "2026-09-10",
  "transcript_status": "pending",
  "language": null,
  "audio_duration_seconds": null,
  "artwork_url": "https://…/art.jpg",
  "rss_url": "https://…/feed.xml",
  "task_status": "downloading",
  "started_at": "2026-09-10T12:00:00",
  "completed_at": null,
  "error_message": null,
  "reused": false
}
```

Starting the same show+date again while a job is still `pending` or `ready`
returns that task with `"reused": true` and HTTP `200` (no double charge).

## GET /api/v1/episodes

Search **your** already-transcribed jobs (not the public catalog — use resolve
for that). At least one of `publisher`/`show` or `date` is required.

| Param | Meaning |
| --- | --- |
| `publisher` / `show` | Case-insensitive substring on show name |
| `date` | `YYYY-MM-DD`, Europe/Oslo against episode publish time |
| `limit` | Max rows (default 50, hard cap 100) |

```bash
curl -sS -H "Authorization: Bearer $PODSKRIFT_API_KEY" \
  "https://podskrift.com/api/v1/episodes?publisher=Forklaring&date=2026-09-12"
```

Example response:

```json
{
  "episodes": [
    {
      "id": "a1b2c3d4e5f6",
      "title": "Episode title",
      "publisher": "Forklaring",
      "published_at": "2026-09-12",
      "transcript_status": "ready",
      "language": "no",
      "audio_duration_seconds": 1800.0,
      "artwork_url": null,
      "rss_url": null,
      "task_status": "completed",
      "started_at": "2026-09-12T08:00:00",
      "completed_at": "2026-09-12T08:05:00",
      "error_message": null
    }
  ],
  "count": 1,
  "filters": {
    "publisher": "Forklaring",
    "date": "2026-09-12",
    "timezone": "Europe/Oslo"
  }
}
```

Empty match → `{"episodes":[],"count":0,…}` (not an error). Other accounts’
jobs are invisible.

## GET /api/v1/episodes/{id}

Metadata for one of your transcription jobs, including `transcript_status`:
`none` | `pending` | `ready` | `failed`. Poll this after start.

```bash
curl -sS -H "Authorization: Bearer $PODSKRIFT_API_KEY" \
  "https://podskrift.com/api/v1/episodes/$ID"
```

Example response:

```json
{
  "id": "a1b2c3d4e5f6",
  "title": "Ukas iddiot …",
  "publisher": "Spårtsklubben",
  "published_at": "2026-09-10",
  "transcript_status": "ready",
  "language": "no",
  "audio_duration_seconds": 3720.0,
  "artwork_url": "https://…/art.jpg",
  "rss_url": "https://…/feed.xml",
  "task_status": "completed",
  "started_at": "2026-09-10T12:00:00",
  "completed_at": "2026-09-10T12:08:00",
  "error_message": null
}
```

Unknown id, or another account’s job → `404`.

## GET /api/v1/episodes/{id}/transcript

When `transcript_status` is `ready`, JSON includes `text` (full plain
transcript). When not ready, the same shape with `transcript_status` set and
**no** `text` — HTTP `200`, never a server error for a pending job.

| Param | Meaning |
| --- | --- |
| `format` | `txt` (default) or `srt` when segment timestamps were stored |
| `raw` | `1` / `true` / `yes` → `text/plain` body when ready (txt only) |

```bash
curl -sS -H "Authorization: Bearer $PODSKRIFT_API_KEY" \
  "https://podskrift.com/api/v1/episodes/$ID/transcript"
```

Example response (ready):

```json
{
  "id": "a1b2c3d4e5f6",
  "title": "Ukas iddiot …",
  "publisher": "Spårtsklubben",
  "transcript_status": "ready",
  "format": "txt",
  "text": "Full transcript text…"
}
```

## Errors

| Status | Meaning |
| --- | --- |
| `400` | Bad input (missing fields, invalid `date`, unsupported `format`) |
| `401` | Missing / wrong / revoked key |
| `402` | Trial exhausted (or episode past the trial per-episode cap) |
| `403` | No OpenAI key available for the account (trial off and no key in Settings) |
| `404` | Not found (catalog miss, or another account’s job) |
| `429` | Too many transcription starts, or too many jobs already in flight |
