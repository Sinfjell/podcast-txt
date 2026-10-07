# AGENTS.md

See [CLAUDE.md](CLAUDE.md). It is the single source for this repo's conventions
and its `## Invariants` section.

This file is a pointer on purpose. It was briefly a copy, and the copy went
stale within one commit — it still mandated stream-copying audio at the moment
CLAUDE.md started forbidding it. Two invariant documents is worse than one.

## Build in public (What's new)

Any PR that ships a **user-facing** feature must add an entry to
[`changelog.json`](changelog.json) (top of the `entries` list, newest first).

- Skip chores, refactors, dependency bumps, ops/analytics-only work.
- Title + one or two sentences in plain user language (English — matches the UI).
- Optional helper: `python3 ops/suggest-changelog.py` (suggests from merged PR
  titles; not CI). Full format lives under **Build in public** in CLAUDE.md.
