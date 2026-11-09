#!/usr/bin/env bash
# After a root-owned `git pull` on the Plesk host, tracked files often land as
# root:root mode 640. The app user (podskrift) then cannot read them — e.g.
# data/show_pages.json → load_curated_shows() returns [] → every /podcasts/*
# 404s and drops out of the sitemap.
#
# Own and chmod only paths in the git index. Parent directories of those paths
# are chown'd (inode only) but never chmod'd — data/ and the app dir stay
# mode 750 with their ACLs. Untracked runtime files (podcast.db, backups,
# .env) are never listed by git ls-files, so they are left alone. Symlinks
# are chown'd with -h and never chmod'd (chmod would follow the target).
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

# Tracked files: ownership + mode from the git index (100755 → 755, else 644).
# --stage -z records are: "<mode> <sha> <stage>\t<path>\0"
while IFS= read -r -d '' rec; do
  mode=${rec%% *}
  path=${rec#*$'\t'}
  [ -n "$path" ] || continue
  # Missing path (sparse / just deleted): nothing to fix.
  [ -e "$path" ] || [ -L "$path" ] || continue
  chown -h "${APP_USER}:${APP_GROUP}" -- "$path"
  # Never chmod through a symlink (would change the target).
  if [ -L "$path" ]; then
    continue
  fi
  if [ "$mode" = "100755" ]; then
    chmod 755 -- "$path"
  else
    chmod 644 -- "$path"
  fi
done < <(git ls-files -z --stage)

# Parent dirs of tracked files: chown only (preserve mode + ACL). No chmod.
declare -A dirs=()
while IFS= read -r -d '' f; do
  d=$(dirname -- "$f")
  while [ "$d" != '.' ] && [ "$d" != '/' ]; do
    dirs["$d"]=1
    d=$(dirname -- "$d")
  done
done < <(git ls-files -z)

if ((${#dirs[@]} > 0)); then
  for d in "${!dirs[@]}"; do
    [ -e "$d" ] || [ -L "$d" ] || continue
    chown -h "${APP_USER}:${APP_GROUP}" -- "$d"
  done
fi
