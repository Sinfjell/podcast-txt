Mandate: finish the Podskrift visual redesign on the branch `feat/redesign-blekk` and open a pull request against `main`. Do NOT merge and do NOT push to `main`: a push to main deploys to production. Work autonomously; Sindre is away. Finished = PR open, tests green, every template checked in a browser in light and dark at 1280 and 390 wide.

## Read first
- `CLAUDE.md` (invariants: do not change any Python logic for money, capacity, counters or data; this is a templates/CSS change)
- `DESIGN.md` and `design-reference.html` in the repo root: the design contract
- `docs/design/brand-book.md`, `docs/design/tokens.json`
- `docs/design/prototype/*.dc.html`: approved prototypes (inline-styled; read for exact sizes, spacing and structure). `Forside` = home page, `Episoder` = episode selection, `Transkripsjon` = transcription page, `Mobilmeny` = mobile menu, `Komponenter` = component sheet. The prototypes were drawn against an older version of the app: where prototype copy or structure differs from what `main` has today, keep today's content and features and apply the new look.

## Already done on this branch (verify, do not redo)
Foundation: self-hosted Schibsted Grotesk + Newsreader in `static/fonts/`, design tokens for light and dark (`prefers-color-scheme`, light default, no toggle) in `templates/base.html`, shared components restyled, mobile menu restyled, token rename across templates, new `static/favicon.svg`.

## Remaining work
1. **Page-level pass on every template** in `templates/` (25 files). Group them: (a) `index.html`, `pricing.html`, `_buy_modal.html`, `billing_success.html`; (b) `episode_selection.html`, `podcast_show.html`, `podcasts_index.html`, `feeds.html`, `history.html`, `rss_help.html`, `go_transcribe.html`; (c) `transcription.html`, `shared_transcript.html`; (d) `login.html`, `register.html`, `settings.html`, `api_docs.html`, `whats_new.html`, `privacy.html`, `terms.html`, `error.html`, `error_500.html`, the two email-unsubscribe pages. For each: remove page-local styles that fight the system (pill radii, shadows, gradients, monospace timestamps, left-border accent cards, hardcoded colours, white text on accent fills), bring headings/spacing to the type scale in DESIGN.md, and make sure there is at most one accent-filled button per screen.
2. **Home page** (`index.html`): hero as in `Forside.dc.html`: display headline, search with underline tabs, and beside it the "sound becomes text" card (bar wave + three accent lines + two transcript lines in Newsreader) with the animation described in DESIGN.md `## Motion / Texture`, disabled under `prefers-reduced-motion`. Keep all existing search behaviour, ids and JavaScript. Front the credit pack ("pay for minutes, not months": free trial, credit pack, own OpenAI key) using the real prices and limits the app already renders (never hardcode a price or minute count the templates currently compute).
3. **Transcript text** (`transcription.html`, `shared_transcript.html`): Newsreader 20px/1.55 (18px under 640px), timestamps in Schibsted Grotesk 13px/700 in `--accent` with `font-variant-numeric: tabular-nums`.
4. **PNG icons and og-image** still show the old dark look. Regenerate `og-image.png`, `icon-192.png`, `icon-512.png`, `icon-maskable-512.png`, `apple-touch-icon.png`, `favicon.ico` from the new mark if you can do it reproducibly (script committed under `ops/`); otherwise leave them and say so in the PR.
5. Keep every existing class name, id, data-attribute and aria attribute that tests or JavaScript use. Never delete, skip or loosen a test; if an assertion pins a value you legitimately changed, update it and list it in the PR.

## Verify (all required, paste output in the PR body)
- `python -m pytest test_app.py -q` green (needs `ffmpeg`/`ffprobe`; install them if missing).
- `grep -rn -E "\-\-(bg|text|border|border-light|accent-bright|accent-bg|accent-border|error|error-bg|warning|warning-bg)\b" templates/` returns nothing; no `border-radius` of 999px/50px/9999px on buttons, badges or tabs; no `font-family` with monospace in templates.
- Run the app locally with a temp database (never `data/podcast.db`), log in with a test user you create, and take Playwright screenshots of every template at 1280 and 390 wide, in light and dark (`colorScheme`). Look at each screenshot yourself: text contrast, no white-on-bright-green, no clipped or overlapping text, mobile menu opens and closes, no horizontal scroll at 390. Fix what you find and re-shoot. Attach a contact sheet or list of the screenshots to the PR.
- Contrast: body text and controls meet WCAG AA in both themes (DESIGN.md has the table).
- `git diff --stat origin/main` contains only templates, static assets, DESIGN.md/design docs, tests, and the files that serve theme-color/manifest. No changes to transcription, billing or database logic.

## Deliver
- Conventional Commits, one PR titled `feat(ui): redesign with light/dark design system`. Add a `## [Unreleased]` entry in the changelog if the repo has one.
- PR body: what changed, the verification output, screenshots, every test assertion you changed, anything you could not verify, and a note that a database backup is NOT needed (no migration).
- The diff is larger than 5 files, so run `/code-review` on it before opening the PR and fix merge-blocking findings.
- Stop at the open PR. Do not merge, do not deploy, do not touch the server.

## State handed over (verified locally before dispatch)
- Branch has three commits on top of main: the foundation, the design docs, and a merge of `origin/main` (includes #65 summaries, #66, #68). Full suite: 698 passed with `test_ops_backup.py::test_umask_077_yields_mode_600` deselected; that test fails on macOS only (`stat -c`) and should pass on Linux. `validate.sh` for DESIGN.md is green with one explained warning.
- If the branch is not on GitHub yet, push it (`git push -u origin feat/redesign-blekk`) before opening the PR. If `main` has moved again, merge it in first.

## Known leftovers from the foundation task (do these)
- `templates/transcription.html:600` sets `borderLeft = '2px solid var(--border)'` (came in with the #65 merge): old token AND a left-border accent. Restyle to the system. Check every template that #65 added or changed for old token names and old styling.
- The token grep in the brief has a false positive on `--warning-tint`; use `\-\-(bg|text|border|border-light|accent-bright|accent-bg|accent-border|error|error-bg|warning|warning-bg)([^-a-z]|$)`.
- `.btn-ghost` currently renders as the bordered secondary button (about 50 usages rely on that) and `.btn-text` is the true text-only variant. Migrate each `.btn-ghost` usage to `.btn-secondary` or `.btn-text` by intent, then remove `.btn-ghost`.
- `.btn-primary` is now accent-filled. Audit every usage: a screen must not end up with several green buttons; demote the non-primary ones to secondary.
- Desktop nav "Sign up" is ink-filled (`--ink` background, `--paper` text), as in the approved prototype. DESIGN.md and design-reference.html currently describe it as an outline button: update those two files to allow exactly this one ink-filled nav call to action.
- `border-radius: 50%` on status dots is fine; on avatars/cover art use `--radius` or `--radius-sm`. Remove monospace in `api_docs.html` and `settings.html` (API keys and code samples may keep a monospace stack ONLY inside `<code>`/`<pre>`; say so in DESIGN.md if you keep it). Replace the old 12.5/13.5px inline font sizes with the type scale.
- `--radius-sm` is now 4px: inputs and buttons that used it must move to `--radius` (8px).
