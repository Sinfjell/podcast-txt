# Customer HTTP API

Per-account API access for Podskrift. Same endpoints as the internal agent
API; auth is a key you generate in **Settings**, not the shared CoS
`AGENT_API_KEY`.

Base URL (prod): `https://podskrift.com`

## Auth

1. Sign in → **Settings** → **Generate API key**
2. Copy the key once (`psk_…`). It is never shown again.
3. Send it on every request:

```http
Authorization: Bearer $PODSKRIFT_API_KEY
```

or:

```http
X-Api-Key: $PODSKRIFT_API_KEY
```

Missing / wrong / revoked key → `401`. Jobs and trial minutes belong to the
key’s owner. Exhausted trial → `402` (never `500`).

Revoke or rotate in Settings; the old key stops working immediately.

## End-to-end (resolve → start → poll → transcript)

```bash
export PODSKRIFT_API_KEY='psk_…'   # from Settings — never commit

# 1) Resolve from the public catalog / RSS
curl -sS -H "Authorization: Bearer $PODSKRIFT_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"publisher":"Spårtsklubben","date":"2026-09-10"}' \
  https://podskrift.com/api/v1/resolve

# 2) Start Whisper (same trial path as the UI)
START=$(curl -sS -H "Authorization: Bearer $PODSKRIFT_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"publisher":"Spårtsklubben","date":"2026-09-10"}' \
  https://podskrift.com/api/v1/transcriptions)
echo "$START"
ID=$(python3 -c "import json,sys; print(json.load(sys.stdin)['id'])" <<<"$START")

# 3) Poll until transcript_status is ready or failed
curl -sS -H "Authorization: Bearer $PODSKRIFT_API_KEY" \
  "https://podskrift.com/api/v1/episodes/$ID"

# 4) Fetch text when ready
curl -sS -H "Authorization: Bearer $PODSKRIFT_API_KEY" \
  "https://podskrift.com/api/v1/episodes/$ID/transcript"
```

`POST /api/v1/transcriptions` also accepts an episode `url` (Spotify episode,
Apple show + date, direct `.mp3`, or RSS feed + date). Starting the same
show+date again while a job is pending/ready returns that task with
`"reused": true` instead of double-charging.

## Endpoints

| Method | Path | Notes |
| --- | --- | --- |
| `POST` / `GET` | `/api/v1/resolve` | Catalog lookup by `publisher`+`date` and/or `url` |
| `POST` | `/api/v1/transcriptions` | Resolve (if needed) and enqueue Whisper |
| `GET` | `/api/v1/episodes` | Search **your** transcribed episodes (`publisher` / `date`) |
| `GET` | `/api/v1/episodes/<id>` | Poll `transcript_status` |
| `GET` | `/api/v1/episodes/<id>/transcript` | Full text when `ready` |

### Status codes (writes)

| Status | Meaning |
| --- | --- |
| `201` | Job created (`transcript_status`: `pending`) |
| `200` | Existing pending/ready task reused |
| `401` | Missing / wrong / revoked API key |
| `402` | Trial exhausted or episode past trial per-episode cap |
| `403` | No OpenAI key available for the account (ops / BYOK) |
| `404` | Episode/show not found in catalog |
| `429` | Rate limit or too many in-flight jobs |

You only ever see your own jobs. Another account’s episode id returns `404`.

## Out of scope (v1)

Credits purchase / Paddle, webhooks, MCP, multi-key per user, public marketplace
listing. The shared host `AGENT_API_KEY` is **not** a customer credential.
