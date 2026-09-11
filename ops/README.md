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

## Error reporting (Sentry)

Project `nettsmed/podskrift` on sentry.io (EU region). Errors only -- no
tracing, no session replay. Reported: uncaught request exceptions, `ERROR` log
records, transcription tasks that end in `error`, and tasks the stale sweep
fails (fingerprint `stale-task`, level warning). OpenAI failures group by status
and key source, so a burst of 429s on the trial key is one issue, apart from
BYOK users with a bad key. The issue alert emails on **new** issues only.

The DSN lives only in the server's `.env` (the unit's `EnvironmentFile`), never
in git. Unset, reporting is off -- which is how dev and the test suite run.

```bash
# once, as root, from the app directory
.venv/bin/pip install -r requirements.txt
printf 'SENTRY_DSN=<dsn from Sentry: Settings > Projects > podskrift > Client Keys>\nSENTRY_ENVIRONMENT=production\n' >> .env
systemctl restart podskrift
sudo -u podskrift .venv/bin/python ops/sentry-check.py   # sends one test exception
```

`observability.py` owns the scrubbing, and it is the part that matters: OpenAI
echoes the submitted key in a 401 (users have pasted passwords there), and
private feeds carry their token in the audio URL. Request bodies, local
variables and PII are never collected, OpenAI's own error message is dropped,
and key-shaped strings, the rest of OpenAI's echo line and the query string
after any URL or path (requests quotes bare paths) are redacted from every event. The
test check event above must arrive with its fake key as `[redacted]`.

If sentry-sdk is missing from the venv the app still boots and logs an error
that reporting is off -- a deploy that skips `pip install` degrades, not dies.

## Notion daily metrics

Weekday cron upserts one row into the Podskrift daily metrics Notion database
(signups, completions, trial burn, etc.). Read-only against SQLite; Python 3
stdlib only.

### Install (once, on the host)

```bash
cd /var/www/vhosts/podskrift.nettsmed.dev/app
cp ops/env.metrics.example ops/.env.metrics
# paste the Notion integration token; leave the database id as-is unless it moves
chmod 600 ops/.env.metrics
mkdir -p data/logs
```

Never commit `ops/.env.metrics` — it holds `NOTION_TOKEN`.

### Cron (Europe/Oslo, weekdays)

```cron
50 7 * * 1-5  cd /var/www/vhosts/podskrift.nettsmed.dev/app && TZ=Europe/Oslo ./ops/notion-daily-metrics.sh >> data/logs/notion-daily-metrics.log 2>&1
```

If the crontab cannot set `TZ` per line, set `CRON_TZ=Europe/Oslo` at the top
of the crontab instead. Default metric day is yesterday in that timezone.

### Dry-run

```bash
DRY_RUN=1 ./ops/notion-daily-metrics.sh
DRY_RUN=1 ./ops/notion-daily-metrics.sh --day 2026-09-10
```

Prints the JSON payload and skips the Notion write. Useful after a deploy
before enabling the cron.
