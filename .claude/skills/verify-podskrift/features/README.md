# Feature Map — Podskrift

Index of user-facing surfaces the verify harness can drive. Each file has four
H2s: Sub-features, How to get to it, Driving it with verify-podskrift, Gotchas.

| Feature | File | Primary commands |
|---|---|---|
| Signup + free trial (incl. A/B variant) | [signup-trial.md](signup-trial.md) | `seed`, `login`, `shot signup` |
| Transcribe (Spotify / RSS / upload URL) | [transcribe.md](transcribe.md) | `transcribe`, `shot transcribe` |
| Paywall / buy modal | [paywall.md](paywall.md) | `login --user empty`, `shot paywall` |
| MCP + OAuth | [mcp-oauth.md](mcp-oauth.md) | `check`, `login` |
| Admin dashboard | [admin.md](admin.md) | `login --user admin`, `shot admin` |

Start here on a cold agent: `up` → `doctor` → `seed` → drive one row → `down`.
