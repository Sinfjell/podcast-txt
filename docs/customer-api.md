# Podcast transcript API

Podskrift’s podcast transcription API lets agents and scripts get a transcript over HTTP — the same path as the web UI. Resolve an episode by publisher/show and date (or URL), start Whisper, poll until ready, then fetch the plain-text transcript. New accounts get 120 free trial minutes on our OpenAI key; after that, add your own. Create a `psk_…` key in Settings.

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

<!-- mcp-section -->
## MCP (ChatGPT / Claude / Cursor)

Connect Podskrift as a remote MCP server so an agent can search shows, list
episodes, and fetch transcripts with the same trial / paid minutes / BYOK rules
as this HTTP API.

**Endpoint:** `https://podskrift.com/mcp` (Streamable HTTP, JSON-RPC 2.0)

**Auth:** the same Settings API key — `Authorization: Bearer psk_…`

**Tools**

| Tool | What it does |
| --- | --- |
| `search_podcasts` | Find shows by name (Apple Podcasts directory) |
| `list_episodes` | Recent episodes for a show name or feed URL |
| `list_my_transcripts` | List *your* existing transcripts (History); filter/search/paginate |
| `get_my_transcript` | Fetch one past transcript by `task_id` (never charges; supports paging) |
| `get_transcript` | Return text if ready, or start Whisper and return a `job_id` |
| `get_transcript_status` | Poll a `job_id` until `ready` / `failed` (waits briefly server-side) |

When the user asks about past transcripts (“what have I transcribed?”), use
`list_my_transcripts` / `get_my_transcript`. Re-fetching an episode you already
transcribed via `get_transcript` is free (`cost_minutes: 0`).

`get_transcript` reports `cost_minutes` and remaining balance before/after. If
the episode is longer than your remaining minutes, it refuses and includes
`pricing_url` (`/pricing`) instead of starting a job. While a job is in
progress, status responses include `progress_pct` / `eta_seconds` and tell the
model to call `get_transcript_status` again in 30–60 seconds (a transcript-ready
email is sent when done; progress also appears on `/history`).

**Cursor example** (`~/.cursor/mcp.json` or project config):

```json
{
  "mcpServers": {
    "podskrift": {
      "url": "https://podskrift.com/mcp",
      "headers": {
        "Authorization": "Bearer psk_…"
      }
    }
  }
}
```

Replace `psk_…` with your key from Settings. Long episodes can take several
minutes — call `get_transcript_status` again yourself every 30–60 seconds until
`transcript_status` is `ready` (do not ask the user to remind you).

<!-- mcp-oauth-section -->
### OAuth for ChatGPT and Claude.ai

ChatGPT connectors and Claude.ai custom connectors use OAuth 2.1 (MCP
authorization spec) instead of pasting an API key. Podskrift runs its own
authorization server on the same host — no third-party IdP.

**MCP URL:** `https://podskrift.com/mcp`

**Discovery:** clients fetch
`https://podskrift.com/.well-known/oauth-protected-resource`
(and authorization-server metadata at
`/.well-known/oauth-authorization-server`). Unauthenticated `/mcp` calls
return `401` with a `WWW-Authenticate` header pointing at that metadata —
enough for URL-only configs such as Cursor / VS Code
`{"url":"https://podskrift.com/mcp"}` to start OAuth (DCR or CIMD) without a
pre-pasted API key.

The authorization server advertises PKCE `S256`, `offline_access` (refresh
tokens), Dynamic Client Registration, and
`client_id_metadata_document_supported` (Client ID Metadata Documents).

#### ChatGPT

Verified against OpenAI’s
[connect an MCP server](https://developers.openai.com/plugins/deploy/connect-chatgpt)
and [developer mode and MCP apps](https://help.openai.com/en/articles/12584461).

1. Go to [chatgpt.com/plugins](https://chatgpt.com/plugins), select **+**, then
   **Add custom MCP server**.
2. Name it Podskrift and set the URL to `https://podskrift.com/mcp`.
3. Choose **OAuth**, accept the risk warning, and select **Create as a plugin**.
4. Complete Podskrift’s consent page — log in if needed, then **Allow**.
5. In a chat, type `@Podskrift` or ask for an episode.

**Availability:** full MCP is a beta for Business, Enterprise and Edu. Pro can
connect with read/fetch permissions. Free and Go don’t have plugin extensions.
Web only (not mobile). Business/Enterprise workspaces may need an admin to
allow custom MCP servers. On some personal accounts the option only appears
after **Developer mode** under Settings → Security and login. Revoke from
ChatGPT’s plugin settings or Podskrift **Settings → Connected apps**.

#### Claude.ai

Verified against Anthropic’s
[custom connectors (remote MCP)](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp)
and [authentication for connectors](https://claude.com/docs/connectors/building/authentication).

1. Go to **Customize → Connectors**, click **+ Add**, then **Add custom
   connector**.
2. Name it Podskrift and set the MCP server URL to `https://podskrift.com/mcp`.
3. Keep sign-in (OAuth), click **Add**, then **Connect**. Complete Podskrift’s
   consent screen.
4. On Team / Enterprise, an Owner adds the connector under Organization
   settings → Connectors first; members then Connect.

Works on Claude Free (one custom connector), Pro, Max, Team and Enterprise,
and in Claude Desktop on the same account. OAuth client registration (DCR)
works with Podskrift; Claude’s hosted callback is
`https://claude.ai/api/mcp/auth_callback`. Revoke from Claude’s connector
settings or Podskrift **Settings → Connected apps**.

OAuth access tokens last one hour; refresh tokens rotate and last 30 days.
Transcription still uses your trial / paid minutes / BYOK — same metering as
the website and the `psk_…` API key path. Episodes you’ve already transcribed
cost nothing to fetch again on your account.

A shorter setup page lives at
[podskrift.com/ai](https://podskrift.com/ai); the full walkthrough is
[podcast transcripts in ChatGPT and Claude](https://podskrift.com/guides/podcast-transcripts-in-chatgpt-and-claude).
<!-- /mcp-oauth-section -->
<!-- /mcp-section -->
