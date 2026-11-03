# Admin dashboard

## Sub-features

- `/admin` blueprint — read-only metrics (users, tasks, economy)
- Gate: logged-in session **and** email in `ADMIN_EMAILS` (else plain 404)
- Europe/Oslo day boundaries for admin displays
- Stripe revenue panel (server-side; fails soft without keys)
- Notion daily-metrics link (economics live in Notion, not a Costs tab)

## How to get to it (user POV)

1. Log in as an allowlisted admin email.
2. Open `/admin` (non-admins see 404, not 403).
3. Drill into `/admin/users/<id>` or `/admin/economy` as needed.

## Driving it with verify-podskrift

```bash
bin/verify-podskrift up && bin/verify-podskrift seed
bin/verify-podskrift login --user admin   # verify-admin@podskrift.test
bin/verify-podskrift shot admin
```

Verify sets `ADMIN_EMAILS=verify-admin@podskrift.test` so the seeded admin passes
the allowlist without using a real operator inbox.

## Gotchas

- Default prod allowlist includes a real email — overridden in verify env.
- Stripe admin widgets need test keys; without them the page still renders.
- Never point admin tooling at the production SQLite file.
