# Customer API

Generate a key in **Settings → API key**. One key per account. Jobs and trial
minutes belong to you. Public copy of this page: https://podskrift.com/docs/api
(`templates/api_docs.html` — keep the two in step).

Never use or share the host `AGENT_API_KEY` — that is internal CoS only.

```bash
export PODSKRIFT_API_KEY='psk_…'   # from Settings — never commit

# Resolve an episode
curl -sS -H "Authorization: Bearer $PODSKRIFT_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"publisher":"Spårtsklubben","date":"2026-09-10"}' \
  https://podskrift.com/api/v1/resolve

# Start transcription (same trial path as the UI)
START=$(curl -sS -H "Authorization: Bearer $PODSKRIFT_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"publisher":"Spårtsklubben","date":"2026-09-10"}' \
  https://podskrift.com/api/v1/transcriptions)
ID=$(python3 -c "import json,sys; print(json.load(sys.stdin)['id'])" <<<"$START")

# Poll
curl -sS -H "Authorization: Bearer $PODSKRIFT_API_KEY" \
  "https://podskrift.com/api/v1/episodes/$ID"

# Transcript when ready
curl -sS -H "Authorization: Bearer $PODSKRIFT_API_KEY" \
  "https://podskrift.com/api/v1/episodes/$ID/transcript"
```

Also accepts `X-Api-Key: $PODSKRIFT_API_KEY`.

| Status | Meaning |
| --- | --- |
| `401` | Missing / wrong / revoked key |
| `402` | Trial exhausted |
| `404` | Not found (or another account’s job) |

Endpoints: `POST/GET /api/v1/resolve`, `POST /api/v1/transcriptions`,
`GET /api/v1/episodes`, `GET /api/v1/episodes/<id>`,
`GET /api/v1/episodes/<id>/transcript`.

Revoke or Regenerate in Settings — old keys stop working immediately.
