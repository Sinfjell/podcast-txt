#!/usr/bin/env bash
# Nightly SQLite backup for Podskrift.
#
# Uses `.backup`, not `cp`: the database runs in WAL mode, so copying the file
# alone can capture a torn state and miss committed pages still in the -wal file.
set -euo pipefail

APP_DIR="${APP_DIR:-/var/www/vhosts/podskrift.nettsmed.dev/app}"
DB="$APP_DIR/data/podcast.db"
DEST="${BACKUP_DIR:-$APP_DIR/data/backups}"
KEEP_DAYS="${KEEP_DAYS:-14}"

[ -f "$DB" ] || { echo "no database at $DB" >&2; exit 1; }
mkdir -p "$DEST"

STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="$DEST/podcast-$STAMP.db"

sqlite3 "$DB" ".backup '$OUT'"

# Fail loudly rather than keeping a corrupt backup
if ! sqlite3 "$OUT" 'PRAGMA integrity_check;' | grep -qx 'ok'; then
    echo "integrity check FAILED for $OUT" >&2
    rm -f "$OUT"
    exit 1
fi

# integrity_check opens the copy, which leaves -wal/-shm sidecars next to it
rm -f "$OUT-wal" "$OUT-shm"

gzip -f "$OUT"
find "$DEST" -name 'podcast-*.db.gz' -mtime "+$KEEP_DAYS" -delete

echo "backup ok: $OUT.gz ($(du -h "$OUT.gz" | cut -f1)), $(ls -1 "$DEST"/*.gz | wc -l) kept"
