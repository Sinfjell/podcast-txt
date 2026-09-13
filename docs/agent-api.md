# Agent API (TSK-20496 read + TSK-20498 write)

HTTP API for Chief-of-Staff / Grok agents: find an episode by publisher + date
(or URL), start Whisper transcription on the same trial path as the UI, poll
until ready, and fetch transcript text. No UI scraping.

Base URL (prod): `https://podskrift.com`

Audience: **Sindre / CoS only**. One shared key, scoped to one account. No
customer API keys, no multi-tenant agent billing, no webhooks, no MCP.

## Auth

Set a shared secret on the host:

```bash
# systemd Environment= or Plesk env — never commit the value
AGENT_API_KEY=<long random secret>
# Required for writes (and recommended for reads on a multi-tenant DB)
AGENT_API_USER_ID=<numeric users.id of Sindre’s account>
```

**Where CoS stores the key**

| Place | What to put there |
| --- | --- |
| Prod host env / systemd | `AGENT_API_KEY` + `AGENT_API_USER_ID` |
| 1Password | Item e.g. `Podskrift AGENT_API_KEY` — CoS reads via 1Password CLI/SDK or an injected env, **not** from chat |
| CoS / Grok tool config | Reference the 1Password item or an env var name; **never paste the secret into Slack, Notion, or a PR body** |

Send the key on every request:

```http
Authorization: Bearer $AGENT_API_KEY
```

or:

```http
X-Api-Key: $AGENT_API_KEY
```

Missing or wrong key → `401`. Unset `AGENT_API_KEY` on the server → every call `401` (fail closed).

Writes without `AGENT_API_USER_ID` → `403`. Transcription minutes come from that
user’s normal trial / own-key pool (same as the web UI). There is no separate
agent billing.

## End-to-end (resolve → start → poll → transcript)

Acceptance example: **Spårtsklubben** + **2026-09-10**, even when no
`TranscriptionTask` exists yet.

```bash
export AGENT_API_KEY=…   # from 1Password / host env — not from chat

# 1) Resolve from the public catalog / RSS (no prior transcription needed)
curl -sS -H "Authorization: Bearer $AGENT_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"publisher":"Spårtsklubben","date":"2026-09-10"}' \
  https://podskrift.com/api/v1/resolve

# 2) Start Whisper (same trial path as the UI; scoped to AGENT_API_USER_ID)
START=$(curl -sS -H "Authorization: Bearer $AGENT_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"publisher":"Spårtsklubben","date":"2026-09-10"}' \
  https://podskrift.com/api/v1/transcriptions)
echo "$START"
ID=$(python3 -c "import json,sys; print(json.load(sys.stdin)['id'])" <<<"$START")

# 3) Poll until transcript_status is ready or failed
curl -sS -H "Authorization: Bearer $AGENT_API_KEY" \
  "https://podskrift.com/api/v1/episodes/$ID"

# 4) Fetch text when ready
curl -sS -H "Authorization: Bearer $AGENT_API_KEY" \
  "https://podskrift.com/api/v1/episodes/$ID/transcript"
```

`POST /api/v1/transcriptions` also accepts an episode `url` (Spotify episode,
Apple show + date, direct `.mp3`, or RSS feed + date). Starting the same
show+date again while a job is pending/ready returns that task with
`"reused": true` instead of double-charging.

## Endpoints

### `POST /api/v1/resolve` (also `GET` with query params)

Resolve a **catalog** episode without requiring an existing transcription.

| Param | Meaning |
| --- | --- |
| `publisher` / `show` | Show title (case-insensitive substring; exact match preferred). Norwegian letters like `å` are fine. |
| `date` | `YYYY-MM-DD`, Europe/Oslo calendar date against the feed’s publish time |
| `url` | Optional: Spotify / Apple / audio / RSS instead of (or with) publisher |

```bash
curl -sS -H "Authorization: Bearer $AGENT_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"publisher":"Spårtsklubben","date":"2026-09-10"}' \
  https://podskrift.com/api/v1/resolve
```

Not found / no public RSS → `404` with a clear `error` string (not 500).

### `POST /api/v1/transcriptions`

Resolve (if needed) and enqueue Whisper as `AGENT_API_USER_ID`.

```bash
curl -sS -H "Authorization: Bearer $AGENT_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"publisher":"Spårtsklubben","date":"2026-09-10"}' \
  https://podskrift.com/api/v1/transcriptions
```

| Status | Meaning |
| --- | --- |
| `201` | Job created (`transcript_status`: `pending`) |
| `200` | Existing pending/ready task reused |
| `401` | Missing/wrong API key |
| `402` | Trial exhausted or episode past trial per-episode cap |
| `403` | `AGENT_API_USER_ID` missing/invalid, or no OpenAI key available for that user |
| `404` | Episode/show not found in catalog |
| `429` | Agent rate limit or too many in-flight jobs for the account |

### `GET /api/v1/episodes`

Search **already transcribed** episodes (task rows). Provide `publisher` (alias
`show`) and/or `date=YYYY-MM-DD` (Europe/Oslo against `episode_published`).

```bash
curl -sS -H "Authorization: Bearer $AGENT_API_KEY" \
  "https://podskrift.com/api/v1/episodes?publisher=Forklaring&date=2026-09-12"
```

Empty match → `{"episodes":[],"count":0,...}` (not an error). This does **not**
search the public catalog — use `/api/v1/resolve` for that.

### `GET /api/v1/episodes/<id>`

Metadata for one episode (transcription task id), including
`transcript_status`: `none` | `pending` | `ready` | `failed`. Poll this after
start.

```bash
curl -sS -H "Authorization: Bearer $AGENT_API_KEY" \
  "https://podskrift.com/api/v1/episodes/<id>"
```

### `GET /api/v1/episodes/<id>/transcript`

When `transcript_status` is `ready`, JSON includes `text` (full plain transcript).
When not ready, same JSON shape with `transcript_status` set and **no** `text` —
HTTP 200, never 500.

Optional: `?format=srt` if segment timestamps were stored; `?raw=1` for
`text/plain` body when ready.

```bash
curl -sS -H "Authorization: Bearer $AGENT_API_KEY" \
  "https://podskrift.com/api/v1/episodes/$ID/transcript"
```

## Deploy notes

1. Generate a key: `openssl rand -hex 32`
2. Store in 1Password; set `AGENT_API_KEY` and `AGENT_API_USER_ID` on the
   Plesk/systemd unit for podskrift.com
3. Restart gunicorn / the app service
4. Smoke-test: curl without header → 401; resolve Spårtsklubben + date → episode
   metadata; start → poll → transcript

Optional tuning (defaults are fine):

| Variable | Default | Meaning |
| --- | --- | --- |
| `AGENT_WRITE_MAX_PER_WINDOW` | `10` | Max agent starts per process per window |
| `AGENT_WRITE_WINDOW_SECONDS` | `60` | Rate-limit window |
| `AGENT_MAX_IN_FLIGHT` | `2` | Max pending jobs for the scoped user |

Out of scope: webhooks, MCP server, multi-tenant agent keys, Credits/Paddle.
