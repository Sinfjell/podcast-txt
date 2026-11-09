#!/usr/bin/env bash
# After a root-owned `git pull` on the Plesk host, tracked files often land as
# root:root mode 640. The app user (podskrift) then cannot read them — e.g.
# data/show_pages.json → load_curated_shows() returns [] → every /podcasts/*
# 404s and drops out of the sitemap.
#
# Own and chmod only paths in the git index (plus parent directory *inodes*).
# Never recurse into data/: untracked runtime files (podcast.db, backups, .env)
# keep whatever tight modes they already have.
#
# Usage (as root, from the app checkout):
#   APP_USER=podskrift APP_GROUP=psaserv ops/fix-tracked-perms.sh
set -euo pipefail

APP_USER="${APP_USER:-podskrift}"
APP_GROUP="${APP_GROUP:-psaserv}"

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "fix-tracked-perms: not inside a git work tree" >&2
  exit 1
fi

# Tracked files → app user; readable (644) or executable (755).
git ls-files -z | xargs -0 -r chown "${APP_USER}:${APP_GROUP}"
git ls-files -z | while IFS= read -r -d '' f; do
  if [ -x "$f" ]; then
    chmod 755 "$f"
  else
    chmod 644 "$f"
  fi
done

# Parent dirs of tracked files (directory inode only; never recursive).
# Includes ancestors so a new nested path is traversable.
mapfile -d '' -t tracked < <(git ls-files -z)
declare -A dirs=()
for f in "${tracked[@]}"; do
  d=$(dirname -- "$f")
  while [ "$d" != '.' ] && [ "$d" != '/' ]; do
    dirs["$d"]=1
    d=$(dirname -- "$d")
  done
done
if ((${#dirs[@]} > 0)); then
  printf '%s\0' "${!dirs[@]}" | xargs -0 -r chown "${APP_USER}:${APP_GROUP}"
  printf '%s\0' "${!dirs[@]}" | xargs -0 -r chmod 755
fi
