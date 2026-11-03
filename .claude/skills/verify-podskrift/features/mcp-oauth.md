# MCP + OAuth

## Sub-features

- Remote MCP at `POST /mcp` (off unless `MCP_ENABLED=1` — on in verify)
- Auth: customer `psk_…`, CoS agent key, or OAuth access `poa_…` when `MCP_OAUTH_ENABLED`
- Unauthenticated → `401` + `WWW-Authenticate: Bearer … resource_metadata="…"`
- Discovery: `/.well-known/oauth-protected-resource`, authorization server metadata
- DCR at `/oauth/register`; authorize + token; refresh rotation
- Tools reuse the same enqueue/trial path as the web UI
- Docs at `/docs/api` (and customer markdown)

## How to get to it (user POV)

1. Create a `psk_…` key under Settings (or connect ChatGPT/Claude via OAuth).
2. Point the MCP client at `https://podskrift.com/mcp` (locally: verify base URL).
3. Call tools (list episodes, start transcription, fetch transcript).

## Driving it with verify-podskrift

```bash
bin/verify-podskrift up && bin/verify-podskrift seed && bin/verify-podskrift login
bin/verify-podskrift check
# doctor portion asserts MCP 401 + resource_metadata
# check also runs tools/list with the seeded psk_ and ping with OAuth token
bin/verify-podskrift shot mcp-docs
```

State file (gitignored durable dir) holds `api_key` and `oauth_access_token` —
never printed. Agents use those for HTTP; they never type a real password.

## Gotchas

- Without `MCP_ENABLED`, `/mcp` is 404 — verify forces it on.
- OAuth resource must match `PUBLIC_BASE_URL/mcp` (verify sets loopback origin).
- Tool calls still meter the user; stub STT keeps cost at zero.
- Do not log bearer tokens; scrubber redacts `psk_` / `poa_` shapes on stdout.
