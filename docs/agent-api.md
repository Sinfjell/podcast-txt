# Agent read API (TSK-20496)

HTTP lookup for Chief-of-Staff / Grok agents: find an episode by publisher +
date and fetch its transcript. No UI scraping.

Base URL (prod): `https://podskrift.com`

## Auth

Set a shared secret on the host:

```bash
# systemd Environment= or Plesk env — never commit the value
AGENT_API_KEY=<long random secret>
```

Optional: scope reads to one account (recommended if the DB has other users):

```bash
AGENT_API_USER_ID=<numeric users.id>
```

**Where CoS stores the key**

| Place | What to put there |
| --- | --- |
| Prod host env / systemd | `AGENT_API_KEY` (and optional `AGENT_API_USER_ID`) |
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

## Endpoints

### `GET /api/v1/episodes`

Search transcribed episodes. Provide `publisher` (alias `show`) and/or
`date=YYYY-MM-DD` (Europe/Oslo calendar date against `episode_published`).

```bash
curl -sS -H "Authorization: Bearer $AGENT_API_KEY" \
  "https://podskrift.com/api/v1/episodes?publisher=Forklaring&date=2026-09-12"
```

Empty match → `{"episodes":[],"count":0,...}` (not an error).

### `GET /api/v1/episodes/<id>`

Metadata for one episode (transcription task id), including
`transcript_status`: `none` | `pending` | `ready` | `failed`.

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
# Full text when ready
curl -sS -H "Authorization: Bearer $AGENT_API_KEY" \
  "https://podskrift.com/api/v1/episodes/<id>/transcript"

# CoS flow: search → pick id → transcript
ID=$(curl -sS -H "Authorization: Bearer $AGENT_API_KEY" \
  "https://podskrift.com/api/v1/episodes?publisher=Forklaring&date=2026-09-12" \
  | python3 -c "import sys,json; e=json.load(sys.stdin)['episodes']; print(e[0]['id'] if e else '')")
curl -sS -H "Authorization: Bearer $AGENT_API_KEY" \
  "https://podskrift.com/api/v1/episodes/$ID/transcript"
```

## Deploy notes

1. Generate a key: `openssl rand -hex 32`
2. Store in 1Password; set `AGENT_API_KEY` on the Plesk/systemd unit for podskrift.com
3. Optionally set `AGENT_API_USER_ID` to Sindre’s (or the ops) account id
4. Restart gunicorn / the app service
5. Smoke-test with the curl examples above (expect `401` without the header)

Out of scope: webhooks, auto-transcribe on publish, MCP server.
