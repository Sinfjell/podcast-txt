# Podskrift (podcast-txt)

Flask + SQLite podcast transcription service. Production: podskrift.com, on
Hetzner/Plesk under systemd, `gunicorn --workers 2 --threads 4 --timeout 300`.

## Invariants

Changes touching any of these need `/code-review` **and** `review-against-plan`
before merging, regardless of how many files the diff has.

### Money — the trial spends our OpenAI key
- **Billing is charged on audio we measured, never on audio the client claimed.**
  `duration_min` (direct path) and `itunes:duration` (RSS path) are both
  attacker-controlled. Charge on `probe_audio_duration()`, falling back to
  `estimate_audio_duration()`; never on `task.audio_duration`.
- **Nothing reaches `audio.transcriptions.create` before the allowance is
  reconciled.** `trial_reconcile_task()` runs in `transcribe_audio()` before
  splitting; everything after it bills OpenAI.
- **Reservations must be atomic across processes.** Two gunicorn workers, so a
  `threading.Lock` guards nothing. `trial_reserve()` is one conditional UPDATE
  and must stay one statement.
- **Both caps hold:** per-account (`users.trial_seconds_limit`) and the global
  lifetime ceiling (`TRIAL_GLOBAL_MINUTES`).
- **Refunds return only what was not spent.** Chunks already sent to Whisper are
  billed to us whatever happens next; `trial_refund_task()` is pro-rata on chunk
  progress and idempotent via a conditional UPDATE on the task.
- **A user with their own key is never metered** — `trial_seconds_charged` stays
  NULL.

### Counters and concurrency
- Rate limits and allowances reserve under a single atomic step and release in a
  `finally`. A check-then-record split has shipped as a live hole here twice.

### Data
- `data/podcast.db` holds real user accounts. Tests must never touch it — the
  suite points `DATABASE_URL` at a temp file *before* importing `app`.
- Schema changes go in `TASK_COLUMN_MIGRATIONS` / `USER_COLUMN_MIGRATIONS`,
  applied by the startup `ALTER TABLE` block. There is no migration framework.
- Take a backup before any deploy that migrates: `ops/backup-db.sh`.

### Secrets
- OpenAI's 401 body echoes the submitted key. Never surface a raw OpenAI error
  to the user or the logs — `describe_openai_error()` exists for this. A user
  once pasted their password into the key field and it landed in the database.

## Testing

```bash
python -m pytest test_app.py -q
```

Needs a venv with `requirements.txt` + `requirements-dev.txt`. On Python 3.13+
also install `audioop-lts` — `pydub` imports the `audioop` module the stdlib
dropped. Production runs Python 3.12.

Money-path changes are expected to come with a mutation check: revert the fix,
confirm the suite goes red. A test that stays green without the fix is not a test.
