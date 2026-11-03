# Trial A/B: 60 vs 30 free minutes

Off-by-default experiment. Merging the code changes nothing until
`TRIAL_AB_ENABLED=1` is set in the environment.

## Arms

| Variant (`users.trial_variant`) | Grant | Notes |
| --- | --- | --- |
| `control` | 60 minutes | Baseline grant while the experiment runs. |
| `trial30` | 30 minutes | Treatment. |

Assignment is deterministic 50/50 at signup from
`sha256("podskrift-trial-ab-30-vs-60:{user_id}")`. Existing users (NULL
`trial_variant`, or rows stamped before enablement) are never rewritten.

When the flag is on, `advertised_trial_minutes()` returns `None` so
logged-out marketing never quotes 30 or 60 — templates use neutral
“start free / try it free” copy. After signup/login, UI shows the
account’s stamped allowance. When the flag is off, signup behaviour and
marketing copy are unchanged (existing `TRIAL_SPLIT_ENABLED` /
`NEW_USER_TRIAL_MINUTES` path).

## Env

```bash
TRIAL_AB_ENABLED=0                 # default — no behaviour change
# TRIAL_AB_CONTROL_MINUTES=60
# TRIAL_AB_TRIAL30_MINUTES=30
```

When enabling in production, prefer also setting `TRIAL_SPLIT_ENABLED=0` so
only one experiment assigns new signups (AB already takes precedence in code).

## Analytics

- Person + event property `trial_variant` on `user_signed_up`,
  `transcript_started` / `transcript_completed`, client `paywall_shown`,
  `checkout_started`, `purchase_completed`.
- Event `trial_ab_assigned` at signup (same distinct_id = user id) with
  `trial_variant` and `trial_granted_min`.

## Decision rule

- **Primary metric:** share of new signups who make a purchase within 7 days
  of signup.
- **Guardrails:**
  - share of signups with a finished transcript
  - contribution margin per signup (net revenue minus STT cost of trial minutes)
- **Minimum sample:** 300 signups per arm **or** 21 days, whichever comes
  later.
- **Ship `trial30` if** its purchase rate is higher **and** the
  finished-transcript rate drops by less than 5 percentage points **and**
  CM per signup is not worse; otherwise keep 60 (`control`).

## Expected calendar length

At ~20 signups/day, 300 per arm (600 total) takes about **30 days**. The
21-day floor is therefore not binding at that rate — plan on roughly a
month of traffic after enablement before a decision, and longer if daily
signups are softer.
