# Transcribe (Spotify / RSS / upload URL)

## Sub-features

- Home paste UI: Spotify / Apple / RSS / direct audio URL
- `/parse_rss` episode picker; `/start_transcription` (session)
- Customer/agent API: `POST /api/v1/transcriptions` + poll `/api/v1/episodes/<id>`
- Trial reserve → download → ffmpeg chunk → STT → reconcile/refund
- Partial preview when trial covers only the start of a long episode
- Status page `/transcription/<task_id>` and `/status/<task_id>`
- Opt-out list blocks some shows (`podcast_optout`)

## How to get to it (user POV)

1. Log in (trial minutes remaining).
2. Paste a show/episode URL on `/` → pick episode → Start.
3. Wait on the status/result page; download txt/srt when complete.
4. Agents: resolve then `POST /api/v1/transcriptions` with `psk_…`.

## Driving it with verify-podskrift

Default proves the **job lifecycle against stub STT** (no OpenAI network):

```bash
bin/verify-podskrift up                 # installs whisper stub + loopback audio
bin/verify-podskrift seed && bin/verify-podskrift login
bin/verify-podskrift transcribe          # starts job on fixture MP3, polls to completed
bin/verify-podskrift transcribe --fail   # stub failure path → status error
bin/verify-podskrift shot transcribe
```

Fixture audio: `http://127.0.0.1:<stub_port>/audio.mp3` (SSRF guard relaxed only
for that stub port while `PODSKRIFT_VERIFY=1`).

Full (explicit): `up --stt full` with `OPENAI_API_KEY` in the **process** env,
audio &lt;30 s, refused when `CI`/`GITHUB_ACTIONS` is set. Max one call.

## Gotchas

- `_is_fetchable_url` blocks private IPs — only the verify server monkeypatch allows the stub.
- Billing uses measured duration, never client `duration_min`.
- Shadow STT (`STT_SHADOW_RATE`, Scribe shadow, …) is forced off in verify — do not “fix” shadow here.
- Spotify/RSS resolution needs network; stub path uses direct `audio_url` instead.
