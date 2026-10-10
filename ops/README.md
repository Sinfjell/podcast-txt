# Ops

## Show landing pages (seed)

Curated `/podcasts/<slug>` pages are served from committed
[`data/show_pages.json`](../data/show_pages.json). Refresh manually (not on
deploy) when you want a newer Apple Charts snapshot:

```bash
python3 ops/seed-show-pages.py
```

Fetches top podcasts for us/gb/de/nl/no/es/br/it, looks up RSS via iTunes
Lookup, writes ~150 shows. Live pages never call Apple at request time.

## Production deploy (GitHub Actions)

On every push to `main` (merge or direct), [.github/workflows/deploy.yml](../.github/workflows/deploy.yml)
SSHs into the Hetzner/Plesk host and runs:

1. `git fetch` / `checkout main` / `pull --ff-only`
2. **Deploy drain** — `ops/drain-in-flight.sh` polls
   `http://127.0.0.1:5002/internal/in-flight` until there are no
   queued/running transcriptions, or until **20 minutes** elapse (then
   proceeds anyway so the Actions job stays inside its 30-minute timeout)
3. `systemctl restart podskrift`, asserts the unit is active, prints HEAD

New jobs keep being accepted during the drain; anything still mid-flight when
the process exits is **re-queued once on boot** (same task id and trial/paid
reservation — no double charge). A second failure after that resume shows a
clear “server restarted” message instead of blaming the audio file.

Manual re-run: Actions → **Deploy production** → **Run workflow**.

Deploy runs `.venv/bin/pip install -r requirements.txt` after `git pull` and
before restart, so new pins (e.g. PyJWT) land without a manual host step.

### Gunicorn graceful shutdown

Production should use [gunicorn.conf.py](../gunicorn.conf.py) (`graceful_timeout
= 120`, `post_worker_init` chains our shutdown flag onto gunicorn’s SIGTERM
handler) and a matching systemd `TimeoutStopSec`. That lets workers finish
current HTTP requests while transcription threads stop at safe points (between
download chunks / Whisper parts) instead of mid-ffmpeg with a corrupt-file
error.

See [ops/podskrift.service.example](podskrift.service.example). Apply on the
host once if the live unit still uses bare CLI flags without `-c gunicorn.conf.py`,
then `systemctl daemon-reload`.

### GitHub secrets (Settings → Secrets and variables → Actions)

| Secret | Example / how to get it |
| --- | --- |
| `PODSKRIFT_SSH_HOST` | `37.27.191.154` |
| `PODSKRIFT_SSH_USER` | `root` (matches current ops) |
| `PODSKRIFT_SSH_KEY` | Private key whose public half is in `authorized_keys` on the host. Prefer a deploy-only ed25519 key, not a personal laptop key. |
| `PODSKRIFT_SSH_KNOWN_HOSTS` | Output of `ssh-keyscan -t ed25519,rsa 37.27.191.154` (paste the host lines only). The workflow uses `StrictHostKeyChecking=yes` — never `no`. |

A failed SSH, non-ff pull, or inactive unit fails the job red.

### Rollback

On the server, check out the previous good SHA and restart (drain first so you
do not cut a live job):

```bash
cd /var/www/vhosts/podskrift.nettsmed.dev/app
git fetch origin
git checkout <previous-good-sha>
PODSKRIFT_DRAIN_MAX_WAIT_SEC=1200 ops/drain-in-flight.sh
systemctl restart podskrift
systemctl is-active podskrift
git rev-parse --short HEAD
```

To return to tracking `main` afterward: `git checkout main && git pull --ff-only`.

### Drain without deploying

```bash
cd /var/www/vhosts/podskrift.nettsmed.dev/app
ops/drain-in-flight.sh
# or: curl -sS http://127.0.0.1:5002/internal/in-flight
```

`/internal/in-flight` answers only on loopback (`127.0.0.1` / `::1`).

## Transactional email (Mailgun EU)

Feature-flagged. Off until `EMAIL_ENABLED=1` and `MAILGUN_API_KEY` is set.
When disabled the app logs and no-ops.

Sending domain is the **root** domain `podskrift.com` (EU region, verified).
API base URL defaults to `https://api.eu.mailgun.net`. From defaults to
`Podskrift <hello@podskrift.com>`. Inbound replies to `hello@` are forwarded by
a Mailgun route — no `Reply-To` needed. Tracking CNAME `email.podskrift.com`
exists, but Mailgun click/open tracking is sent as **off**; we use our own
`utm_source=email` / `utm_campaign=…` links instead.

We do not verify email addresses today — mail goes to the registered account
email. Bounce handling is future work.

### Env vars

| Var | Required when enabled | Default | Notes |
| --- | --- | --- | --- |
| `EMAIL_ENABLED` | — | off (`0`) | Feature flag |
| `MAILGUN_API_KEY` | yes | — | Domain sending key |
| `MAILGUN_DOMAIN` | no | `podskrift.com` | Root sending domain |
| `MAILGUN_BASE_URL` | no | `https://api.eu.mailgun.net` | EU API |
| `MAIL_FROM` | no | `Podskrift <hello@podskrift.com>` | |
| `MAIL_REPLY_TO` | no | unset | Optional; leave unset |

Also needs `PUBLIC_BASE_URL` and `SECRET_KEY` (unsubscribe tokens + absolute links).

### Env (append to the app `.env`, then restart)

```bash
# once, as root, from the app directory
printf '%s\n' \
  'EMAIL_ENABLED=0' \
  'MAILGUN_API_KEY=key-...' \
  'MAILGUN_DOMAIN=podskrift.com' \
  'MAILGUN_BASE_URL=https://api.eu.mailgun.net' \
  'MAIL_FROM=Podskrift <hello@podskrift.com>' \
  >> .env
# When ready to send:
#   sed -i 's/^EMAIL_ENABLED=0/EMAIL_ENABLED=1/' .env
systemctl restart podskrift
```

## Database backups

Until 2026-09-09 there were none. The database is the only copy of every user's
account and transcripts.

### Schedule

`podskrift-backup.timer` runs at **23:45 UTC** every night
(01:45 Europe/Oslo in CEST / 00:45 in CET). That is intentionally **before**
the Plesk server backup to S3 at **02:05 Oslo**, so the offsite snapshot
includes tonight's SQLite dump rather than yesterday's (~21 h stale).

`Persistent=true` so a missed window (host down at 23:45) still runs once the
machine is back.

Local files land in `data/backups/podcast-YYYYMMDD-HHMMSS.db.gz` (mode **600**,
via `umask 077` in `ops/backup-db.sh`). Retention: 14 days.

### Offsite layer

The Plesk scheduled server backup to S3 is the offsite copy of the whole host
(including `data/backups/`). Treat it as the disaster-recovery layer; the
systemd job is the consistent SQLite snapshot that must finish first.

**Recommend a periodic test restore** (quarterly is fine): pick a recent
`.db.gz` from `data/backups/`, restore it on a scratch path (not production),
run `PRAGMA integrity_check` and `SELECT COUNT(*) FROM users`, and confirm the
counts look right. A backup that has never been restored is unproven.

### Install / update (as root on the host)

After pulling a commit that changes the units or the schedule:

```bash
cd /var/www/vhosts/podskrift.nettsmed.dev/app
cp ops/podskrift-backup.service /etc/systemd/system/podskrift-backup.service
cp ops/podskrift-backup.timer /etc/systemd/system/podskrift-backup.timer
cp ops/podskrift-backup-failed.service /etc/systemd/system/podskrift-backup-failed.service
systemctl daemon-reload
systemctl enable --now podskrift-backup.timer
systemctl restart podskrift-backup.timer
systemctl start podskrift-backup.service   # prove it works now; don't wait for 23:45
systemctl status podskrift-backup.service
systemctl list-timers podskrift-backup.timer
```

`podskrift-backup-failed.service` is only started by `OnFailure=`; do not
enable it as a timer.

### Failure alerting

If `backup-db.sh` exits non-zero (missing DB, empty file, failed integrity
check, user-count mismatch, etc.), systemd starts
`podskrift-backup-failed.service`, which runs `ops/report-backup-failure.py`.
That script loads `SENTRY_DSN` from the app `.env` the same way the app and
`ops/sentry-check.py` do, and sends one Sentry error
(`fingerprint=podskrift-backup-failed`). No new secrets in the repo.

Manual dry check of the reporter (sends a real event if the DSN is set):

```bash
cd /var/www/vhosts/podskrift.nettsmed.dev/app
sudo -u podskrift .venv/bin/python ops/report-backup-failure.py
```

The weekday Notion metrics job also flags when the newest
`data/backups/podcast-*.db.gz` is older than **26 hours** (or missing) as a
Notes warning — catches a timer that stopped without a clean failure.

### Verify it is still running

```bash
find /var/www/vhosts/podskrift.nettsmed.dev/app/data/backups \
     -name 'podcast-*.db.gz' -mmin -1560 | grep -q . \
  && echo "backups OK" || echo "BACKUPS STALE"
# -mmin -1560 ≈ 26 hours
systemctl is-active podskrift-backup.timer
journalctl -u podskrift-backup.service -n 20 --no-pager
```

### Restore

```bash
systemctl stop podskrift
cd /var/www/vhosts/podskrift.nettsmed.dev/app
cp data/podcast.db data/podcast.db.before-restore-$(date +%Y%m%d-%H%M%S)
gunzip -c data/backups/podcast-<STAMP>.db.gz > data/podcast.db
rm -f data/podcast.db-wal data/podcast.db-shm   # stale WAL from the old database
chown podskrift:psaserv data/podcast.db
chmod 600 data/podcast.db
sqlite3 data/podcast.db 'PRAGMA integrity_check; select count(*) from users;'
systemctl start podskrift
```

### Secrets in backups

The `.gz` files contain **plaintext OpenAI API keys** and password hashes.
`umask 077` / mode 600 keeps them owner-only; treat them as secrets.

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

## Product analytics (PostHog)

Project on PostHog EU Cloud (`https://eu.i.posthog.com`). Used for funnel
events (signup → settings → OpenAI key → first transcript) and session replay.
Unset `POSTHOG_KEY` means fully off: no client snippet, no server captures, no
network calls — how local and current production behave until you set it.

Privacy defaults: session replay masks all inputs; email / password / OpenAI
key fields also carry `ph-no-capture`; network request bodies are stripped.
Users are identified by internal user id only (never email/name as person
properties). Event properties never include API keys, emails, or transcript
text. See `/privacy`.

Cookie consent: the browser snippet uses PostHog `cookieless_mode: 'on_reject'`.
Before Accept and after Decline, the client calls `opt_out_capturing()` so
PostHog may count anonymous pageviews/events with no cookies or
local/session storage and no session recording (server-side daily-rotating
hash). Accept calls `opt_in_capturing()`, enables session recording, and may
`identify()` by internal user id. Choice is stored 12 months in the
first-party `podskrift_cookie_consent` cookie/localStorage (banner + footer
Cookie settings). Project must have **Cookieless server hash mode** enabled
under Project Settings → Web analytics, or cookieless events are discarded.
Recommend also enabling **Discard client IP data** so IPs are not stored.
Server-side `analytics.capture()` keys on the internal user id and never
reads tracking cookies; checkout only forwards `ph_sid` → `$session_id` when
that consent cookie is `accepted`.

```bash
# once, as root, from the app directory
.venv/bin/pip install -r requirements.txt
printf 'POSTHOG_KEY=phc_...\nPOSTHOG_HOST=https://eu.i.posthog.com\n' >> .env
systemctl restart podskrift
```

`analytics.py` owns the server SDK (soft import, like Sentry). The browser
snippet lives in `templates/base.html` and only renders when `POSTHOG_KEY` is
set.

Named events:

| Event | Where | Properties |
|---|---|---|
| `podcast_searched` | browser, before signup | `search_type`, `result_count` (never the query) |
| `user_signed_up`, `settings_viewed` | server | `user_signed_up`: `trial_granted_min`, optional `trial_variant` + `$set.trial_variant` when the split assigned one |
| `openai_key_saved` / `openai_key_validation_failed` | server | `status` / `reason` |
| `transcript_started` / `transcript_completed` | server | `key_source` (trial/user), `source` (web/api) |
| `transcript_failed` | server | the above + `reason` (`invalid_key`, `no_billing`, `own_key_invalid`, `own_key_no_credit`, `rate_limit`, `network`, `trial_exhausted`, `abandoned`, `stale`, `source_audio_missing`, `source_audio_forbidden`, `other`) |
| `trial_limit_hit` | server | `scope` (`episode_length`, `user`, `daily`, `global`), `stage` (`start`, `reconcile`), `source` |
| `trial_daily_budget_exhausted` | server | once per Oslo day (uuid dedupe); `day`, `daily_limit_min`, `daily_used_min`, `source` |

`trial_limit_hit` is the buying signal: a trial user wanted more than the free
allowance gives. `trial_daily_budget_exhausted` fires on the first refusal of
the day when the shared daily budget is empty.

## Free-trial budget env vars

| Var | Default | Notes |
| --- | --- | --- |
| `TRIAL_DAILY_MINUTES` | `2000` | Shared free-trial budget for one Europe/Oslo calendar day. Resets at Oslo midnight. Reservations count immediately (`trial_budget_days`); refunds / failed-before-Whisper jobs release that day's row. |
| `TRIAL_GLOBAL_MINUTES` | unset (= off) | Optional lifetime safety ceiling across all accounts. Leave unset in normal operation; set only if you want a hard multi-day stop beyond the daily budget. |
| `TRIAL_MINUTES` | `180` | Per-account fallback when `users.trial_seconds_limit` is NULL (legacy rows). |
| `NEW_USER_TRIAL_MINUTES` | `120` | Stamped on `trial_seconds_limit` at registration when the split is off. |
| `TRIAL_SPLIT_ENABLED` | on | When on, new signups get a deterministic hash-of-id variant from `TRIAL_SPLIT_VARIANTS` (stored on `users.trial_variant`). Existing rows keep NULL variant and their current limit. |
| `TRIAL_SPLIT_VARIANTS` | `60,120` | Comma-separated minute labels for the split (equal buckets). |
| `TRIAL_ENABLED` | on | Kill switch for handing out the platform key. |
| `CHECKOUT_EXPIRES_HOURS` | `2` | Stripe Checkout Session TTL (1–24). Shorter than Stripe’s 24h default so abandoned-checkout recovery can email the same day. |
| `CHECKOUT_RECOVERY_ENABLED` | on | Sets `after_expiration.recovery.enabled` + `consent_collection.promotions=auto` on Checkout Session create. Dashboard must also enable recovery emails and the `checkout.session.expired` webhook. |

`/health` exposes `trial_available` (today's budget still has room) plus
`trial_daily_used` / `trial_daily_limit` in minutes.

## Notion daily metrics

Weekday cron upserts one row into the Podskrift daily metrics Notion database
(signups, completions, trial burn, etc.). Read-only against SQLite; Python 3
stdlib only.

Each run also hits PostHog EU (HogQL) for a short health check: whether
productivitytech.io is sending events / `cta_clicked`, whether Podskrift sees
`utm_source=productivitytech` referrals, whether named Podskrift funnel events
have stopped or fallen sharply vs a 14-day baseline, and whether
`transcript_failed` rates (overall and per `reason`) spiked. Podskrift queries
exclude the same internal cohort as dashboard
[Podskrift — aktivering](https://eu.posthog.com/project/283916/dashboard/975121)
(`filterTestAccounts` → cohort «Internal / Test users»).

Warnings go only to the Notion `Notes` property (Norwegian lines, ⚠️-prefixed,
capped at 2000 chars). No Notes write when everything looks fine. A missing
`POSTHOG_PERSONAL_API_KEY`, timeout, or API error still upserts metrics and
writes one “helsesjekk kjørte ikke” line. The same Notes channel also flags a
missing or >26 h-old `data/backups/podcast-*.db.gz`. No email/Slack/webhooks.

Thresholds (named constants in `ops/posthog_daily_health.py`): check window =
metric day, or 3 days when the metric day is Sunday (covers Fri–Sat which the
weekday cron never rows alone); baseline = previous 14 days; minimum baseline
count before “stopped” / sharp drop; failure-rate delta vs baseline.

### Install (once, on the host)

```bash
cd /var/www/vhosts/podskrift.nettsmed.dev/app
cp ops/env.metrics.example .env.metrics
# paste the Notion integration token; leave the database id as-is unless it moves
# optional: POSTHOG_PERSONAL_API_KEY (personal key, query:read) for the health check
chmod 600 .env.metrics
```

The script reads the first file it finds: `ops/.env.metrics`, then
`$APP_DIR/.env.metrics`. Production uses `.env.metrics` in the app root
(root-owned, `600`); there is no `ops/.env.metrics` on the host. Variables
already set in the environment win over the file.

Never commit `.env.metrics` — it holds `NOTION_TOKEN` and the optional
PostHog personal key.

### Cron (root crontab, weekdays)

The host runs in UTC, so the schedule is in UTC. The script computes the
metric day in Europe/Oslo itself (default: yesterday), so the crontab needs no
`TZ`:

```cron
# Podskrift Notion daily metrics — 05:50 UTC = 07:50 Europe/Oslo in CEST (06:50 in CET); always before 08:00 digest
50 5 * * 1-5 cd /var/www/vhosts/podskrift.nettsmed.dev/app && /usr/bin/python3 ops/notion-daily-metrics.py >> /var/log/podskrift-metrics.log 2>&1
```

Log: `/var/log/podskrift-metrics.log`.

### Dry-run

```bash
cd /var/www/vhosts/podskrift.nettsmed.dev/app
DRY_RUN=1 /usr/bin/python3 ops/notion-daily-metrics.py
DRY_RUN=1 /usr/bin/python3 ops/notion-daily-metrics.py --day 2026-09-10
```

Prints the JSON metrics payload, any would-be Notes warning lines, and skips
the Notion write. Useful after a deploy before enabling the cron.
