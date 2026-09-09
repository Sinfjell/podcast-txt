# Ops

## Database backups

Until 2026-09-09 there were none. The database is the only copy of every user's
account and transcripts.

### Install (once, as root on the host)

```bash
cd /var/www/vhosts/podskrift.nettsmed.dev/app
ln -sf $PWD/ops/podskrift-backup.service /etc/systemd/system/podskrift-backup.service
ln -sf $PWD/ops/podskrift-backup.timer   /etc/systemd/system/podskrift-backup.timer
systemctl daemon-reload
systemctl enable --now podskrift-backup.timer
systemctl start podskrift-backup.service   # prove it works now, don't wait for 03:30
systemctl status podskrift-backup.service
```

### Verify it is still running

A timer that silently stopped is the same as having no backups. Check that the
newest backup is less than two days old:

```bash
find /var/www/vhosts/podskrift.nettsmed.dev/app/data/backups \
     -name 'podcast-*.db.gz' -mtime -2 | grep -q . \
  && echo "backups OK" || echo "BACKUPS STALE"
```

### Restore

```bash
systemctl stop podskrift
cd /var/www/vhosts/podskrift.nettsmed.dev/app
cp data/podcast.db data/podcast.db.before-restore-$(date +%Y%m%d-%H%M%S)
gunzip -c data/backups/podcast-<STAMP>.db.gz > data/podcast.db
rm -f data/podcast.db-wal data/podcast.db-shm   # stale WAL from the old database
chown podskrift:psaserv data/podcast.db
sqlite3 data/podcast.db 'PRAGMA integrity_check; select count(*) from users;'
systemctl start podskrift
```

### Known gaps

- Backups live on the same volume as the database, so host loss loses both.
  An offsite copy (rsync to another host, or S3) is still to do.
- The `.gz` files contain **plaintext OpenAI API keys** and password hashes.
  `umask 077` keeps them owner-only; treat them as secrets.
- No alerting on failure. Run the staleness check above, or wire `OnFailure=`
  in the service unit to an alert unit once a mail relay exists.

## Runtime dependencies not in requirements.txt

`ffmpeg` and `ffprobe` must be on PATH. ffmpeg and ffprobe are needed to inspect and re-encode audio, and
`get_audio_duration()` shells out to ffprobe. If ffprobe is missing, duration
falls back to a size estimate silently — worse ETAs, no error.

```bash
ffmpeg -version | head -1 && ffprobe -version | head -1
```
