# Trial A/B: 60 vs 30 free minutes

**Status: concluded — ship 30 for all new signups.**

The experiment ran with `TRIAL_AB_ENABLED=1`. New signups now get
`trial_variant=trial30_all` and 30 minutes (no `.env` change needed while
AB stays on). Set `TRIAL_AB_ROLLOUT=off` only to restore the old 50/50
`control` / `trial30` split.

## Outcome

| Arm | Purchase rate (sample) | Revenue / signup |
| --- | --- | --- |
| `trial30` (30 min) | 14.7% | $0.45 |
| `control` (60 min) | 10.0% | $0.27 |

Sample is small. **Revisit if the 14-day purchase rate falls below 10%.**

## Arms (historical)

| Variant (`users.trial_variant`) | Grant | Notes |
| --- | --- | --- |
| `control` | 60 minutes | Baseline while the experiment ran. |
| `trial30` | 30 minutes | Treatment. |
| `trial30_all` | 30 minutes | Post-decision rollout label for every new signup. |

Assignment during the experiment was deterministic 50/50 at signup from
`sha256("podskrift-trial-ab-30-vs-60:{user_id}")`. Existing users (NULL
`trial_variant`, or rows stamped before enablement / before rollout) are
never rewritten — variant is signup-only.

When the flag is on, `advertised_trial_minutes()` returns `None` so
logged-out marketing never quotes 30 or 60 — templates use neutral
“start free / try it free” copy. After signup/login, UI shows the
account’s stamped allowance. Pack / pricing copy that mentions
“60 minutes” as a paid pack size is unrelated and stays as-is.

## Env

```bash
TRIAL_AB_ENABLED=0                 # kill switch — off restores TRIAL_SPLIT / NEW_USER path
TRIAL_AB_ROLLOUT=all               # default when AB is on: everyone gets trial30_all / 30 min
# TRIAL_AB_ROLLOUT=off             # restore control (60) / trial30 (30) 50/50
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
  `trial_variant` and `trial_granted_min` (30 under the rollout).

## Decision rule (historical)

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

Owner approved ending the experiment in favour of 30 for all new signups
(`trial30_all`) despite the small sample, with the revisit guard above.
