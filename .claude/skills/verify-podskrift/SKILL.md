---
name: verify-podskrift
description: >
  Local verification harness for Podskrift (Flask + SQLite podcast transcription).
  Use when changing signup/trial metering, transcription enqueue/status, paywall/buy
  modal, MCP/OAuth, or admin dashboard — or before claiming a Podskrift PR is
  verified. Drives bin/verify-podskrift (doctor/up/down/seed/login/transcribe/shot/check)
  against a durable local SQLite DB with stub STT by default. Never for SSH, Hetzner,
  deploy, prod DB, or STT shadow comparison config.
---

# verify-podskrift

Repo-local lever for [verify-loop](https://github.com/): start Podskrift safely,
seed fixture users, drive the main flows, capture evidence. Agents never type a
real password and never call a real STT provider unless `--stt full`.

Entry: `bin/verify-podskrift <command>` (JSON on stdout). Cursor resolves this
skill via `.cursor/skills/verify-podskrift` → `.claude/skills/verify-podskrift`.

## Level table

Pick the **smallest** level that hits the path you changed. Times are **measured**
on this cloud VM (2026-11-03); re-measure after big harness changes.

| Change type | Level | Steps | Measured (cloud VM 2026-11-03) | Path proven |
|---|---|---|---|---|
| Docs / DESIGN / changelog only | **none** | — | 0 s | — |
| Template copy, CSS, landing SEO | **light** | `up` → `doctor` → `shot <page>` → `down` | ~12 s (up 1.1 + doctor 0.1 + shot 2.7–5 + down ~3) | UI renders; no job path |
| Signup, trial variant, settings, MCP docs | **standard** | `up` → `seed` → `login` → `check` → `shot …` → `down` | ~18 s (+ seed 1.1 + login 1.8 + check 0.1) | Auth + MCP 401/tools + UI |
| Transcription, trial reserve/refund, paywall enqueue, STT routing (not shadow) | **standard+** | above + `transcribe` (stub) [+ `--fail`] | ~20 s (transcribe ~0.5; fail ~0.5) | Job lifecycle + stub STT |
| Real Whisper once, Stripe test checkout, long audio edge | **full** | standard+ with `--stt full` (explicit; refused in CI; audio &lt;30 s; key from process env) | +15–60 s network | One live provider call |

Commands **not** shipped (and why):

| Dropped | Why |
|---|---|
| `goto` | Covered by `shot <feature>` + Feature Map paths |
| `contract` | No multi-tenant client symbol surface (unlike LMS Pro) |
| `seed sync` from prod | Prod DB is PII — fixtures only |
| CI job | Actions minutes are scarce; `check` is local only |

## Launch

```bash
bin/verify-podskrift up                  # stub STT, port 5502–5599 per worktree
bin/verify-podskrift up --stt full       # real provider; needs OPENAI_API_KEY in env
bin/verify-podskrift up --dry-run
bin/verify-podskrift down                # SIGTERM only our PID; evidence kept
```

Ready signal: `/health` → `{"ok": true, …}`. Data under
`~/.local/share/podskrift-verify/worktrees/<id>/` (override with
`PODSKRIFT_VERIFY_HOME`). Evidence under repo `.verify/evidence/<id>/`
(gitignored). Process `TZ` forced to `UTC` (matches production `Etc/UTC`);
app code still uses `ZoneInfo('Europe/Oslo')` for trial/admin day boundaries.

## Doctor

Run first, and after anything surprising:

```bash
bin/verify-podskrift doctor
```

Checks: process/port ownership, SQLite `PRAGMA database_list` inside verify dir,
empty vendor keys, Stripe not live, `/health`, MCP unauthenticated `401` +
`WWW-Authenticate` (`resource_metadata` when OAuth on).

## Drive

```bash
bin/verify-podskrift seed                 # fixture users + psk_ + OAuth token → state
bin/verify-podskrift login                # Playwright storage state; never type passwords
bin/verify-podskrift login --user admin
bin/verify-podskrift transcribe           # API start + poll (stub STT)
bin/verify-podskrift transcribe --fail    # stub failure path
bin/verify-podskrift shot signup          # desktop+mobile × light+dark
bin/verify-podskrift shot list
bin/verify-podskrift check               # doctor + MCP tools/list (not CI)
```

Feature Map: [`features/README.md`](features/README.md). Prefer ARIA / `data-*`
hooks from the map over coordinates.

## Evidence

| What | Where |
|---|---|
| Screenshots | `.verify/evidence/<id>/shots/<feature>/*.png` |
| Transcribe report | `.verify/evidence/<id>/transcribe/<task_id>.json` |
| Storage state | `.verify/evidence/<id>/storage-*.json` |
| Server log | `.verify/logs/<id>/` |
| Secrets (password, psk_, poa_) | durable `state.json` mode 0600 — **never stdout** |

Stdout is scrubbed. Details stay in `.verify/` (gitignored).

## Cleanup

`down` stops only the PID in state (never `pkill`). Evidence directories are kept —
confirm with `ls -la .verify/evidence/...` after cleanup.

## Helpers

| Script | Role |
|---|---|
| `bin/verify-podskrift` | CLI entry |
| `verify_podskrift/server.py` | Child: fixture HTTP + Flask + STT stub |
| `verify_podskrift/stt_stub.py` | Monkeypatch Whisper; loopback fetch allowlist |
| `test_verify_podskrift.py` | Prod guards vs real loader |

## Prod isolation (non-negotiable)

- Empty `MAILGUN_*` / `POSTHOG_*` / `SENTRY_DSN` / Together / Soniox / Groq /
  ElevenLabs before app import (dotenv `override=False` → empty wins).
- `DATABASE_URL` SQLite path must sit under the verify durable dir.
- Stripe: allow `sk_test_` / `rk_test_`; refuse `sk_live_` / `rk_live_`.
- Seed from fixtures only — never copy `data/podcast.db`.
- Default STT is stub; `--stt full` hard-stops unless audio is measured
  under 30 s (unknown duration refused), at most one provider call is
  recorded in the verify state dir, process-env key is set, and not CI.
- Do not change STT shadow comparison code/config used in prod.
- No SSH, Hetzner, deploy, or new CI job.
