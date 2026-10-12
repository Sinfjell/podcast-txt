#!/usr/bin/env bash
# Make a fresh cloud agent session (Claude Code on the web, Codex, Cursor
# cloud) able to run the suite and the app straight away:
#
#   python -m pytest test_app.py test_design_system.py -q
#   PODSKRIFT_ENV=dev DATABASE_URL=sqlite:////tmp/podskrift-dev.db \
#     python -m flask --app app run --port 5002
#
# Idempotent and quiet when already done, so it is safe both as the
# environment's setup script and as the SessionStart hook in
# .claude/settings.json. Never reads or writes .env or data/podcast.db.
set -euo pipefail

cd "$(dirname "$0")/.."
REPO="$PWD"

# ffmpeg/ffprobe: several tests synthesise real audio with them.
if ! command -v ffmpeg >/dev/null || ! command -v ffprobe >/dev/null; then
  if [ "$(id -u)" = 0 ] && command -v apt-get >/dev/null; then
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends ffmpeg >/dev/null
  else
    echo "cloud-setup: ffmpeg missing and cannot apt-get install it; audio tests will fail" >&2
  fi
fi

# A repo-local venv keeps the system Python clean. Reinstall only when the
# requirement files change.
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
STAMP=.venv/.requirements.sha
WANT="$(cat requirements.txt requirements-dev.txt | sha256sum | cut -d' ' -f1)"
if [ "$(cat "$STAMP" 2>/dev/null)" != "$WANT" ]; then
  .venv/bin/pip install -q --disable-pip-version-check -r requirements-dev.txt
  echo "$WANT" > "$STAMP"
fi

# Put the venv first on PATH for every later command in a Claude Code
# session, so plain `python -m pytest` uses it.
if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  {
    echo "export VIRTUAL_ENV=\"$REPO/.venv\""
    echo "export PATH=\"$REPO/.venv/bin:\$PATH\""
  } >> "$CLAUDE_ENV_FILE"
fi

echo "cloud-setup: ready ($(.venv/bin/python --version), $(ffmpeg -version 2>/dev/null | head -n1 | cut -d' ' -f1-3))"
