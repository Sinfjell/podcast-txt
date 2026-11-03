# Signup + free trial

## Sub-features

- `/register` (and `/signup` → 301) with email + password (≥8 chars)
- Disposable-email and per-IP registration rate limit
- Trial grant: `NEW_USER_TRIAL_MINUTES` (default 120) or `TRIAL_SPLIT_VARIANTS` / `TRIAL_AB_*`
- `trial_variant` stamped on the user; PostHog `user_signed_up` / `trial_ab_assigned`
- Device cookie + IP hash for trial-abuse denial (`trial_denied_reason`)
- Login wall at `/login`; session via Flask-Login

## How to get to it (user POV)

1. Open `/register` (anonymous).
2. Submit email + password → redirected to home (or `?next=`).
3. Flash shows free minutes (or denial note).
4. Header shows remaining trial/paid minutes when logged in.

## Driving it with verify-podskrift

```bash
bin/verify-podskrift up
bin/verify-podskrift seed          # creates verify-user@podskrift.test with trial
bin/verify-podskrift login         # storage state; password never printed
bin/verify-podskrift shot signup   # anonymous register page
bin/verify-podskrift doctor
```

Inspect seed result: `users_seeded`, `roles` (no PII). Variant assignment is
deterministic from user id when split/AB env is on (verify defaults:
`TRIAL_SPLIT_ENABLED=1`, `TRIAL_AB_ENABLED=0`).

## Gotchas

- Empty `MAILGUN_API_KEY` means forgot-password is a no-op — expected in verify.
- Do not quote a single trial length in UI when split/AB is on (marketing uses min arm).
- Registration slot is reserved for the request; failed validation releases it.
- Never seed by copying `data/podcast.db` (PII).
