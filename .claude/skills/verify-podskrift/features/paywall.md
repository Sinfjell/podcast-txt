# Paywall / buy modal

## Sub-features

- Buy modal (`templates/_buy_modal.html`, `[data-open-buy-modal]`)
- Empty-balance / insufficient-trial → HTTP 402 from enqueue with paywall payload
- Remaining-minutes offer (price for what’s left) on result / lifecycle email
- `POST /billing/checkout` → Stripe Checkout (test mode only in verify)
- Volume pricing slider when `VOLUME_PRICING_ENABLED=1` (off by default in verify)
- Partial-preview paywall after a truncated trial transcript

## How to get to it (user POV)

1. Log in with no trial left and no paid balance (or start an episode longer than remaining minutes).
2. Start transcription → paywall / Buy CTA.
3. Or open Buy from the header (`data-open-buy-modal`).
4. Checkout success returns to `/billing/success` and may resume a stashed episode.

## Driving it with verify-podskrift

```bash
bin/verify-podskrift up && bin/verify-podskrift seed
bin/verify-podskrift login --user empty
bin/verify-podskrift shot paywall        # opens buy modal when the control exists
```

Stripe: verify allows `sk_test_` / `rk_test_` already in the process env so checkout
can be proven; live keys are refused. Without test keys, Buy stays hidden (same as
prod when Stripe is unset) — screenshot still captures the empty-balance chrome.

## Gotchas

- Buy is hidden unless **both** secret key and webhook secret are set (app rule).
- Never put `sk_live_` / `rk_live_` in verify env.
- 402 enqueue stashes pending episode for post-checkout resume.
- PostHog paywall events are off in verify (blank `POSTHOG_KEY`).
