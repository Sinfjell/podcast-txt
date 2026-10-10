---
version: alpha
name: Podskrift
description: Podskrift turns a podcast episode into text. The text is the product, so the interface looks like a page - white paper, black ink, and green only where something happens.
colors:
  # Surfaces and lines (5)
  paper: "#FFFFFF"              # Page background
  surface: "#F4F6F5"            # Cards, job card, transcript sample, disabled button fill; sits on paper
  surface-hover: "#EAEEEC"      # Row hover on paper/surface (added outside tokens.json by decision)
  line: "#DCE2DF"               # Dividers and card borders; decorative only, never the only border of a control
  line-strong: "#0E1512"        # Borders of inputs, selects and secondary buttons
  # Text (2)
  ink: "#0E1512"                # Text and logo bars
  muted: "#56635D"              # Supporting text
  # Accent family (3)
  accent: "#0A7A55"             # Primary button fill, links, timestamps, active tab, progress fill
  accent-hover: "#075C40"       # Hover of accent fills and links
  on-accent: "#FFFFFF"          # Text on accent fills
  # Alert tints (6)
  accent-tint: "#E3F3EC"        # Info alert background
  on-accent-tint: "#064E36"     # Text and links on accent-tint
  warning-tint: "#FFF1D6"       # Warning alert background (trial used up)
  on-warning-tint: "#6B3F00"    # Text and links on warning-tint
  danger-tint: "#FDE7E4"        # Error alert background
  on-danger-tint: "#8A1C12"     # Text on danger-tint
  # Semantic (1)
  danger: "#B42318"             # Error text, invalid field border, cancel button text and border
typography:
  display:
    fontFamily: Schibsted Grotesk
    fontSize: 4rem
    fontWeight: 800
    lineHeight: 1.02
    letterSpacing: "-0.035em"
  title:
    fontFamily: Schibsted Grotesk
    fontSize: 2.75rem
    fontWeight: 800
    lineHeight: 1.05
    letterSpacing: "-0.03em"
  heading:
    fontFamily: Schibsted Grotesk
    fontSize: 1.5rem
    fontWeight: 700
    lineHeight: 1.2
    letterSpacing: "-0.015em"
  body:
    fontFamily: Schibsted Grotesk
    fontSize: 1.0625rem
    fontWeight: 400
    lineHeight: 1.55
  control:
    fontFamily: Schibsted Grotesk
    fontSize: 1rem
    fontWeight: 700
    lineHeight: 1.2
  small:
    fontFamily: Schibsted Grotesk
    fontSize: 0.875rem
    fontWeight: 400
    lineHeight: 1.5
  timestamp:
    fontFamily: Schibsted Grotesk
    fontSize: 0.8125rem
    fontWeight: 700
    lineHeight: 1.2
    fontFeature: '"tnum"'
  label:
    fontFamily: Schibsted Grotesk
    fontSize: 0.75rem
    fontWeight: 700
    lineHeight: 1.2
    letterSpacing: "0.08em"
  transcript:
    fontFamily: Newsreader
    fontSize: 1.25rem
    fontWeight: 400
    lineHeight: 1.55
spacing:
  space-1: 0.25rem    # 4px  - title to meta line
  space-2: 0.5rem     # 8px  - label to field
  space-3: 0.75rem    # 12px - between buttons, compact rows
  space-4: 1rem       # 16px - row padding, gap inside cards
  space-6: 1.5rem     # 24px - card padding, between tabs
  space-9: 2.25rem    # 36px - between component groups
  space-12: 3rem      # 48px - hero padding, column gap
  space-24: 6rem      # 96px - between page sections
rounded:
  sm: 0.25rem         # 4px - progress bar, logo bars, cover thumbnails
  md: 0.5rem          # 8px - buttons, inputs, cards, alerts; no pills
  lg: 0.75rem         # 12px - transcript window and hero command bar only
components:
  button-primary:
    backgroundColor: "{colors.accent}"
    textColor: "{colors.on-accent}"
    typography: "{typography.control}"
    rounded: "{rounded.md}"
    padding: 0 1.5rem
    height: 3rem
  button-primary-hover:
    backgroundColor: "{colors.accent-hover}"
    textColor: "{colors.on-accent}"
  button-nav-cta:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.paper}"
    typography: "{typography.control}"
    rounded: "{rounded.md}"
    padding: 0 1rem
    height: 2.75rem
  button-secondary:
    backgroundColor: "{colors.paper}"
    textColor: "{colors.ink}"
    typography: "{typography.control}"
    rounded: "{rounded.md}"
    padding: 0 1.5rem
    height: 3rem
  button-secondary-hover:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
  button-tertiary:
    backgroundColor: "{colors.paper}"
    textColor: "{colors.accent}"
    typography: "{typography.control}"
    rounded: "{rounded.md}"
    padding: 0 1rem
    height: 3rem
  button-danger:
    backgroundColor: "{colors.paper}"
    textColor: "{colors.danger}"
    typography: "{typography.control}"
    rounded: "{rounded.md}"
    padding: 0 1rem
    height: 2.75rem
  button-disabled:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.muted}"
    typography: "{typography.control}"
    rounded: "{rounded.md}"
    padding: 0 1.5rem
    height: 3rem
  link-default:
    textColor: "{colors.accent}"
    typography: "{typography.body}"
  link-default-hover:
    textColor: "{colors.accent-hover}"
  input-default:
    backgroundColor: "{colors.paper}"
    textColor: "{colors.ink}"
    typography: "{typography.control}"
    rounded: "{rounded.md}"
    padding: 0 1rem
    height: 3rem
  input-search:
    backgroundColor: "{colors.paper}"
    textColor: "{colors.ink}"
    typography: "{typography.body}"
    rounded: "{rounded.md}"
    padding: 0 1rem
    height: 3.5rem
  heading-hero:
    typography: "{typography.display}"
    textColor: "{colors.ink}"
  tab-active:
    textColor: "{colors.ink}"
    typography: "{typography.control}"
    height: 2.75rem
  tab-inactive:
    textColor: "{colors.muted}"
    typography: "{typography.control}"
    height: 2.75rem
  episode-row:
    backgroundColor: "{colors.paper}"
    textColor: "{colors.ink}"
    typography: "{typography.body}"
    padding: 1rem 0
  episode-row-hover:
    backgroundColor: "{colors.surface-hover}"
  job-card:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.md}"
    padding: 1.5rem
  transcript-line:
    textColor: "{colors.ink}"
    typography: "{typography.transcript}"
  transcript-timestamp:
    textColor: "{colors.accent}"
    typography: "{typography.timestamp}"
  credit-pack:
    backgroundColor: "{colors.paper}"
    textColor: "{colors.ink}"
    rounded: "{rounded.md}"
    padding: 1.5rem
  alert-info:
    backgroundColor: "{colors.accent-tint}"
    textColor: "{colors.on-accent-tint}"
    rounded: "{rounded.md}"
    padding: 0.75rem 1rem
  alert-warning:
    backgroundColor: "{colors.warning-tint}"
    textColor: "{colors.on-warning-tint}"
    rounded: "{rounded.md}"
    padding: 0.75rem 1rem
  alert-danger:
    backgroundColor: "{colors.danger-tint}"
    textColor: "{colors.on-danger-tint}"
    rounded: "{rounded.md}"
    padding: 0.75rem 1rem
---

# Podskrift Design System

> Canonical design system for Podskrift (Flask + Jinja, CSS in `templates/base.html`). Tokens are locked; they come from the approved "Podskrift redesign" canvas and its token file. `design-reference.html` next to this file is the runnable pair.

## Overview

Podskrift turns a podcast episode into text. The text is the product, so the interface looks like a page: white paper, black ink, and green only where something happens. A visitor should read the screen as a document with one clear action on it, not as an app chrome with decorations.

Voice: interface copy is English, plain and short, sentence case, no exclamation marks, no emoji. Say what happens ("It keeps going if you close the page."). Name limits and prices in the first sentence ("First 60 minutes free", "300 minutes for $5", "No subscription"). Error text says what went wrong and what to do next.

The design rejects, by name:

- Cream, beige or off-white page backgrounds. Paper is `#FFFFFF`; the only step up is `surface`.
- Italic titles, numbered section headings, monospace type (outside `<code>`/`<pre>`), pill buttons, shadows and left-border accent cards. Gradients and blur exist exactly twice, both named in Elevation & Depth.
- Green as decoration. If a green thing does not start, mark or advance an action, it is wrong.

It embraces:

- One accent-filled button per screen; every other button is an outline or plain text. The one exception is the desktop nav call to action, which is ink-filled (see Components).
- Separating areas with 1px `line` borders and one step of `surface`, never with elevation.
- Newsreader reserved for the transcript, so the reading text is visibly different from the interface around it.
- One motif: sound bars turning into lines of text. On the home hero it runs at full width (the signal wave) and moves.

## Colors

Two themes, light first. Light is the default; dark follows the visitor's system setting (see Dark Mode). The palette is a deliberately small set of roles, grouped as surfaces, text, accent family, alert tints and one semantic colour.

### Surfaces and lines

- **Paper (`#FFFFFF`):** the page.
- **Surface (`#F4F6F5`):** cards, the running-job card, the transcript sample, the disabled-button fill. Always sits on paper.
- **Surface-hover (`#EAEEEC`):** hover fill of episode rows and similar list rows. Not part of the original token file; added by decision.
- **Line (`#DCE2DF`):** dividers and card borders on paper or surface. Decorative only, never the only border of a control (1.31:1 on paper).
- **Line-strong (`#0E1512`):** borders of inputs, selects and secondary buttons. Equals ink in light, a mid green-grey in dark so the border keeps 3:1.

### Text

- **Ink (`#0E1512`):** text and logo bars on paper or surface.
- **Muted (`#56635D`):** supporting text on paper, surface and surface-hover.

### Accent family

- **Accent (`#0A7A55`):** primary button fill, links, timestamps, active tab underline, progress fill, focus ring, the three text-line bars of the logo.
- **Accent-hover (`#075C40`):** hover of accent fills and links.
- **On-accent (`#FFFFFF`):** text on accent fills. In dark this is dark text on bright green, which is why white must never be hardcoded.

### Alert tints

Each tint is paired with a matching text colour. Use the pair as a unit, with an icon.

- **Accent-tint (`#E3F3EC`) + on-accent-tint (`#064E36`):** info, for example "Free trial: 46 minutes left".
- **Warning-tint (`#FFF1D6`) + on-warning-tint (`#6B3F00`):** warnings, for example "Your free trial is used up".
- **Danger-tint (`#FDE7E4`) + on-danger-tint (`#8A1C12`):** errors, for example "We could not fetch this feed".

### Semantic

- **Danger (`#B42318`):** error text, invalid field border, and the cancel action (text and border). Nothing else.

## Typography

Two families, both self-hosted woff2 in `static/fonts/` (never Google Fonts at runtime): **Schibsted Grotesk** for the whole interface at weights 400, 500, 700, 800; **Newsreader** (weights 400, 500) for transcript text only.

| Role | Family | Size / line-height | Weight | Letter-spacing | Use |
|---|---|---|---|---|---|
| `display` | Schibsted Grotesk | 64px / 1.02 | 800 | -0.035em | Home hero h1 (fluid down to 40px) |
| `title` | Schibsted Grotesk | 44px / 1.05 | 800 | -0.03em | Page h1 (fluid down to 30px) |
| `heading` | Schibsted Grotesk | 24px / 1.2 | 700 | -0.015em | Section h2 |
| `body` | Schibsted Grotesk | 17px / 1.55 | 400 | none | Running text, row titles (700) |
| `control` | Schibsted Grotesk | 16px / 1.2 | 700 | none | Buttons, tabs, inputs |
| `small` | Schibsted Grotesk | 14px / 1.5 | 400 | none | Supporting text, field hints |
| `timestamp` | Schibsted Grotesk | 13px / 1.2 | 700 | none | Transcript timestamps, `tabular-nums`, accent |
| `label` | Schibsted Grotesk | 12px / 1.2 | 700 | 0.08em | Small caps-free labels, sentence case |
| `transcript` | Newsreader | 20px / 1.55 | 400 | none | Transcript text only |

Rules: titles are set tight and upright, never italic. Timestamps use `font-variant-numeric: tabular-nums` and `accent`; monospace is allowed only inside `<code>` and `<pre>` (API samples, keys); everywhere else there is none. Font sizes are rem in tokens (px shown above for reference at a 16px root). Fluid hero sizing lives in CSS Variables, not in the token value.

## Layout

Page content sits in one width token, `--width-page` (1120px), centred with 24px side padding; reading and form pages use `--width-prose` (880px for episode lists, 760px for transcripts and FAQ). Section rhythm: `space-24` (96px) between page sections, `space-12` (48px) for hero padding and column gaps, `space-9` (36px) between component groups.

Proximity rule: tight values inside a component, larger between components.

| Relationship | Step | Example |
|---|---|---|
| Title to its meta line | `space-1` (4px) | Episode title, then date and duration |
| Label to field | `space-2` (8px) | "Spoken language" over the select |
| Between buttons, compact rows | `space-3` (12px) | Search field and its button |
| Row padding, gap inside cards | `space-4` (16px) | Episode row, card contents |
| Card padding, between tabs | `space-6` (24px) | Job card, credit pack |
| Between component groups | `space-9` (36px) | Tabs group and results |
| Hero padding, column gap | `space-12` (48px) | Hero text and sample |
| Between page sections | `space-24` (96px) | How it works and Pricing |

Forms are one column; the field width matches the expected input; hint text sits above the field. Controls are at least 44px tall; primary inputs and buttons are 48 to 56px.

## Elevation & Depth

Hierarchy comes from three things: a 1px `line` border, one step of `surface` against `paper`, and type weight. A card on `paper` takes `surface` or a `line` border, never both. No `box-shadow`, `text-shadow` or `drop-shadow` anywhere. The focus ring is a 3px `accent` outline with 2px offset; the hero command bar widens it to a 4px `accent-tint` outline.

Two layered effects are sanctioned (decided 2026-10-10), each pinned to one rule in `static/components.css` and enforced by `test_sanctioned_effects_stay_in_their_one_rule`:

| Effect | Where | Token |
|---|---|---|
| `backdrop-filter: blur(14px)` | `.nav-bar::before`, the sticky site header | `--header-glass` (paper at 72% / 66%) |
| `radial-gradient` glow | `.ds-cmd__box::before`, behind the hero command bar | `--glow` (accent at 16% / 20%) |

The signal wave canvas draws the same `--glow` around its scan line. No other gradient, blur or glow is allowed.

## Shapes

`radius-md` (8px) on buttons, inputs, cards and alerts. `radius-sm` (4px) on the progress bar and cover thumbnails. `radius-lg` (12px) on the transcript window and the hero command bar only. Logo bars are fully rounded by their own geometry. There are no pill buttons, no pill badges and no other radius. Borders are 1px; the invalid input border and the active-tab underline are the only thicker strokes (2px and 3px).

## Components

Reference markup for every component is in `design-reference.html`. **Runnable Jinja macros** live in `templates/components/macros.html` with styles in `static/components.css` (tokens stay in `templates/base.html`). The live gallery is `/design/components` (DEBUG or admin only, noindex).

- **Buttons.** Primary: `accent` fill, `on-accent` text, 48px tall (56px beside the hero search field), hover `accent-hover`. Secondary: `paper` fill, 1px `line-strong` border, `ink` text. Tertiary / ghost: plain `accent` text (hover: `surface` fill). Danger (cancel only): `paper` fill, 1px `danger` border and text. Disabled: `surface` fill, `muted` text, no border. All share `radius-md` and `control` type. Hover always pins `color` as well as background. **One accent-filled button per screen.**
- **Site header.** Sticky, 64px, glass (`--header-glass` + blur on `::before`). The 1px `line` hairline appears only after the page scrolls (`data-scrolled`, set by `components.js`). Links are `muted` 15px/500, `ink` with a `surface` fill on hover. Logged out, the product links sit centred between the brand and the account actions. The logo bars bounce once on hover.
- **Nav call to action.** The desktop nav "Start free →" link (was "Sign up") is ink-filled (`ink` background, `paper` text, 44px tall, `radius-md`, hover pins both colours). It is the ONE allowed non-accent filled button and does not count against the one-accent-button rule. In the mobile menu panel the same link is the accent button (`accent` fill, `on-accent` text).
- **Tabs and search.** Underline tabs: 44px tall, 3px bottom border, `accent` on the active tab and transparent on the others; active text is `ink`, inactive `muted`, state exposed with `aria-selected` / `aria-pressed`. The search field is 56px tall, `line-strong` border, with its primary button beside it.
- **Form fields.** Label above (`small`, weight 700), hint above the field in `small`/`muted`, field 48px tall with a 1px `line-strong` border. Invalid state: 2px `danger` border, and above the field an error icon plus the message in `danger` text, linked with `aria-invalid` and `aria-describedby`. Required and optional are both marked.
- **Checklist row.** Pending (empty circle), active (accent ring), done (calm `accent` fill + check in `on-accent`), error (`danger` ring + icon). Title in body/700; detail in small/`muted`.
- **Snippet / secret.** Code samples and shown-once keys use monospace only inside `<code>`/`<pre>` (or the secret `code` value), with a secondary Copy button — never monospace on the Copy label itself.
- **Episode row.** Title (`body`, 700) over a meta line (`small`, `muted`, `tabular-nums`), a 1px `line` top border, 16px vertical padding, `surface-hover` on hover. Right side: one action, either a primary "Transcribe" button or a "View transcript" link. Only one row on a screen may carry the accent button.
- **Job card.** `surface` fill, `radius-md`, 24px padding. Status line, 8px progress bar (`line` track, `accent` fill, `radius-sm`, `role="progressbar"`), the reassurance "It keeps going if you close the page.", and a danger Cancel button.
- **Transcript block.** Header row with Copy, .txt and .srt as secondary buttons, then lines: a 48px timestamp column (`timestamp`, `accent`) and the text in Newsreader 20px. Newsreader appears nowhere else.
- **Hero (home).** Centred column over the signal wave: `announce()` link, display h1 with the last phrase in `muted`, price line, lede, the command bar, an inline proof list, `works_with()`, then `transcript_window()`. Built from the macros below; the page adds nothing but spacing. **On phones (≤640px) the hero is the h1, the price line, the command bar and one proof item**: the announce link, lede, other proof items and `works_with()` are hidden with CSS (still in the DOM). A phone hero that needs scrolling to reach the input has too much text.
- **Command bar.** `command_bar()`: one 64px field with a search icon, an optional segmented scope switch (`.ds-seg` with `.tab` buttons) and the action inside it. `line-strong` border, `radius-lg`, the sanctioned glow behind it. It carries the screen's one primary button (pass `button_class='btn-primary'` so the lint counts it). `examples` are typed into the placeholder until the visitor touches the bar. On phones the field takes the first row and switch + button the second.
- **Announce link.** `announce()`: a `line`-bordered link with an `accent-tint` "New" badge and an arrow that nudges right on hover. `short` replaces the label under 640px. One per page, only for something new.
- **Works with.** `works_with()`: the names of the places Podskrift works, as text in `muted` 700, never logos. Names with a setup guide link to it.
- **Signal wave.** `signal_wave()`: a full-bleed canvas behind a `data-ds-signal-root`. Bars in `line`, a scan line in `accent` that turns them into short text strokes. The band sits just above the `data-ds-signal-anchor` element. Paused off screen and in background tabs; one still frame under reduced motion. Decorative (`aria-hidden`).
- **Transcript window.** `transcript_window()`: the product illustration. `radius-lg`, `line` border, a `surface` title bar (cover mark, episode title and meta, a status chip, Copy/.txt/.srt), a 3px `accent` playhead track, then timestamped lines in the transcript face. The current line gets a `surface` fill and `ink` text; the rest are `muted`. Fictional content, always captioned "Illustrated example".
- **MCP chat illustration.** A static, readable example conversation, implemented in `templates/components/mcp_chat_example.html`. Use a `surface` window with an 8px radius and a `line` header divider; the user message sits on `paper`. Show an example episode, “Give me the transcript of that.”, a Podskrift transcript-ready check and timestamped sample text. Use Schibsted Grotesk for chat UI and Newsreader only for the transcript excerpt. Accent marks the completed retrieval and timestamps. Always caption it “Illustrated example”; use fictional transcript text and no fake input, send button, app branding or live status semantics. Keep the same content in `design-reference.html`.
- **Credit pack.** `paper` fill, 1px `line-strong` border, figure ("300 minutes") in a heading weight, price on the button.
- **Alerts / notices.** Tint fill with its `on-…` text, `radius-md`, 12px/16px padding, always an icon plus text (colour is never the only signal). Links inside inherit the `on-…` colour and are bold.
- **Status chip.** Optional `accent-tint` / `warning-tint` chip at `radius-md` (8px). **No pills** (`9999px` / `999px` are banned).

## How to build a page

1. **Compose from macros.** Import from `templates/components/macros.html` (`button`, `link_button`, `section`/`card`, `page_header`, `tabs`/`tab_panel`, `checklist`/`checklist_row`, `snippet`, `secret_field`, `notice`, `empty_state`, `form_field`, `chip`, `proof_list`, and for heroes `announce`, `command_bar`, `works_with`, `signal_wave`, `transcript_window`). Prefer existing class names in `base.html` (`.btn`, `.form-input`, `.card`) when a macro is not needed yet — do not invent parallel styles.
2. **Never hand-roll component CSS.** No page-local `<style>` block in a new template, no `style=` attributes, no hard-coded hex or `rgb()`/`hsl()` colours, no new colours or fonts, no `box-shadow`, gradients, or pill radii. Put shared rules in `static/components.css` using the tokens already defined in `base.html`. Layout spacing uses `--space-*`.
3. **One primary button.** Exactly one `btn-primary` / accent-filled control per screen (the desktop nav Sign up CTA does not count). Demote everything else to secondary, ghost, or a link-button.
4. **Behaviour ships with the library.** `static/components.js` (loaded from `base.html`) drives tabs (click, arrow keys, Home/End) and copy buttons. Do not write per-page handlers for them. `notice()` escapes its message; pass a link with `{% call notice(variant=...) %}`.
5. **Check the gallery.** Open `/design/components` (app.debug or an ADMIN user) and match states there before shipping. The gallery is noindex.
6. **Light and dark.** Follow `prefers-color-scheme`. PRs that change UI must include light + dark screenshots (desktop and ~390px where layout matters). Home and pricing are the visual baselines — do not change their look unless the task says so.
7. **CI guardrails.** `test_design_system.py` fails the build on `<style>` blocks or inline styles in new templates, hex outside the token section, forbidden elevation/shape/type rules, and extra primary buttons. Legacy `style=` counts are ratcheted down only.

Anti-pattern (do not repeat): the rejected `/connect` draft used pill CTAs, monospace on a Copy control, ad-hoc grey boxes, and inconsistent tabs — none of that is in this system.

## Motion / Texture

```yaml
motion:
  intensity: moderate   # was minimal until 2026-10-10
  ease-out: cubic-bezier(0.16, 1, 0.3, 1)   # --ease-out
  dur-1: 0.18s          # --dur-1: hovers, nudges, hairlines
  dur-2: 0.6s           # --dur-2: entrances, the playhead highlight
```

There is no background texture or grain. The single decorative motif is **sound bars turning into text lines**: a row of bars (the audio) followed by accent lines (the text). It is the 3-bars-plus-3-lines logo mark, the running-job card while a transcription runs, and, at full width on the home hero, the signal wave.

What moves, and nothing else:

| Part | Animation | Timing |
|---|---|---|
| Signal wave (home hero) | Bars breathe; a scan line sweeps left to right and the bars behind it become short accent text strokes that fade out; a `--glow` rides the scan line | 9s sweep, linear, canvas |
| Transcript window | Rises 16px and fades in once; the playhead and the highlighted line step through the timestamps | `--dur-2` entrance; 2.6s per line |
| Command bar placeholder | Types example links and show names, deletes, repeats; stops for good on first focus or tap | about 55ms per character |
| Hover micro-interactions | Arrows nudge 2-3px right; logo bars bounce once; header hairline fades in on scroll | `--dur-1`, `--ease-out` |
| Progress bar | Width eases | 0.4s |

Everything above is removed under `prefers-reduced-motion: reduce`: the wave draws one still frame, the playhead stays on the first line, the placeholder stays put.

## Accessibility & UU

WCAG 2.1 AA is the floor. Ratios below are computed from the tokens for every text and ground pair named in the token usage notes. "Large" in Status means the pair is only used for UI strokes or large type and needs 3:1.

### Contrast, light theme

| Forgrunn | Bakgrunn | Ratio | Status |
|---|---|---|---|
| `ink` | `paper` | 18.51:1 | AAA |
| `ink` | `surface` | 17.05:1 | AAA |
| `ink` | `surface-hover` | 15.81:1 | AAA |
| `muted` | `paper` | 6.29:1 | AA |
| `muted` | `surface` | 5.80:1 | AA |
| `muted` | `surface-hover` | 5.37:1 | AA |
| `accent` | `paper` | 5.35:1 | AA |
| `accent` | `surface` | 4.93:1 | AA |
| `accent` | `surface-hover` | 4.57:1 | AA |
| `accent-hover` | `paper` | 8.03:1 | AAA |
| `accent-hover` | `surface` | 7.40:1 | AAA |
| `on-accent` | `accent` | 5.35:1 | AA |
| `on-accent` | `accent-hover` | 8.03:1 | AAA |
| `on-accent-tint` | `accent-tint` | 8.51:1 | AAA |
| `on-warning-tint` | `warning-tint` | 8.05:1 | AAA |
| `on-danger-tint` | `danger-tint` | 7.86:1 | AAA |
| `danger` | `paper` | 6.57:1 | AA |
| `danger` | `surface` | 6.06:1 | AA |
| `line-strong` | `paper` | 18.51:1 | AAA, control border |
| `line-strong` | `surface` | 17.05:1 | AAA, control border |
| `line` | `paper` | 1.31:1 | decorative, never the only border of a control |

The tightest text pair is `accent` on `surface-hover` (4.57:1): a timestamp or link inside a hovered row. It passes AA but has no headroom, so never put accent text on a darker ground than `surface-hover`.

### Rules

- Text sizes are rem; the `control` size is 16px so iOS does not zoom inputs.
- Skip link first, `<main id="main">`, one `<h1>` per page, `<nav aria-label>`.
- `:focus-visible` everywhere: 3px `accent` outline, 2px offset. Never `outline: none` without a replacement.
- Colour is never the only signal: alerts and errors carry an icon and text; the active tab has a 3px underline as well as weight.
- Targets are at least 44px tall. Form fields have visible labels; invalid fields use `aria-invalid="true"` and `aria-describedby`. Progress uses `role="progressbar"` with `aria-valuenow`, status text is `aria-live="polite"`.
- Animation respects `prefers-reduced-motion`; the hero motif is decorative (`role="img"` with a label, or `aria-hidden`).

## Do's and Don'ts

### Don't

1. **No cream, beige or off-white page backgrounds.** The page is `paper` (`#FFFFFF`); the only step up is `surface` (`#F4F6F5`). Warm tints such as `#FAF7F2` make the product read as a lifestyle blog.
2. **No italic titles.** `font-style: italic` is never set on h1, h2 or h3. Titles are upright Schibsted Grotesk 800 with `letter-spacing: -0.035em` (display) or `-0.03em` (title). Italics belong to nothing here.
3. **No numbered section headings.** Do not write "01 Search", "Step 2" or "1." in front of an h2 or h3. Sections are named by what they are ("How it works", "Pricing").
4. **No monospace outside `<code>` and `<pre>`.** A monospace stack is allowed only inside `<code>`/`<pre>` (API samples, keys, the only monospace in the product); never on timestamps, labels or UI text. Timestamps are Schibsted Grotesk 700 with `font-variant-numeric: tabular-nums` in `accent`.
5. **No pill buttons or pill badges.** `border-radius: 9999px` and `border-radius: 999px` are banned. Every control, card and alert uses `radius-md` (8px); only the progress bar and thumbnails use `radius-sm` (4px).
6. **No gradients.** No `linear-gradient`, `radial-gradient` or mesh backgrounds, and none on `accent` fills. Use the flat token. The single exception is the `--glow` behind the hero command bar (see Elevation & Depth); do not reuse it elsewhere.
7. **No shadows.** No `box-shadow`, `drop-shadow` or `text-shadow`. Separate areas with a 1px `line` border or a step of `surface`.
8. **No left-border accent cards.** `border-left: 4px solid` on an alert or card is banned. Alerts are a tint fill with an icon and text.
9. **No emoji.** Not in copy, buttons, empty states or alerts. Icons are inline SVG with a 2px stroke, round caps, in `ink` or `accent`.
10. **One accent-filled button per screen.** The desktop nav call to action is the sole non-accent filled button (ink fill, paper text); in the mobile menu panel it becomes the accent button. A second `background: var(--accent)` button on the same screen is wrong; demote it to a `line-strong` outline or plain text. Cancel is `danger` outline, never filled.
11. **Newsreader only for transcript text.** `font-family: Newsreader` appears on `.transcript` text and nowhere else, not in headings, hero copy, buttons or alerts.
12. **Never hardcode white text on accent.** Use `color: var(--on-accent)`; `#FFFFFF` on `accent` becomes 1.92:1 in dark where `accent` is `#34D399`.
13. **No Google Fonts at runtime.** `fonts.googleapis.com` and `fonts.gstatic.com` are not requested; both families are served from `static/fonts/`.

### Do

- Use `accent` only where something happens: the one primary button, links, timestamps, the active tab, progress fill, the focus ring.
- State the limit or price in the first sentence of any pricing or trial copy.
- Give every error an icon, a plain message of what went wrong, and the next step.
- Pin `color` on every button `:hover` rule so a background change never flips the text.
- Draw the sound-bars-to-text motif in the hero (the signal wave) and the running-job card, and nowhere else.

## Brand Assets

### Logo

| Asset | Ground | Use |
|---|---|---|
| Mark (3 ink bars + 3 accent lines) | Paper, surface, either theme | Header, footer, favicon source. Bars use `ink`, lines use `accent`, so the mark follows the theme |
| Wordmark "podskrift" | Paper, surface, either theme | Next to the mark. Schibsted Grotesk 800, lower case, `letter-spacing: -0.035em`, `ink` |

The mark is 30x28 and defined inline (see `design-reference.html`); bars are `radius-sm`-sized pills by their own rx. Exclusion zone: at least half the mark width on all sides.

### Favicon and icons

`static/favicon.svg` (source), `static/favicon.ico` (32x32), `static/apple-touch-icon.png` (180x180), `static/icon-192.png`, `static/icon-512.png`, `static/icon-maskable-512.png`, `static/og-image.png` (1200x630). **Open item:** the PNG icons and `og-image.png` predate this redesign and still need to be regenerated from the new mark and palette; the favicon SVG should be checked against the mark at the same time.

### Decorative element

The sound-bars-turning-into-text strip (see Motion / Texture). Bars `ink`, lines `accent`, on a `surface` panel. No other decoration.

### Logo do's and don'ts

- Do not recolour the bars or lines outside the theme tokens.
- Do not set the wordmark in capitals, italic or another weight.
- Do not place the mark on `accent` or on a photograph.

## Dark Mode

Dark is an active part of this system, not an afterthought. It follows the visitor's system setting through `prefers-color-scheme`; light is the default when no preference is expressed, and there is no manual toggle (decided; see Endringer).

### Token-overstyringer

| Token | Lys | Mørk |
|---|---|---|
| `paper` | #FFFFFF | #0A0F0D |
| `surface` | #F4F6F5 | #121916 |
| `surface-hover` | #EAEEEC | #18211D |
| `line` | #DCE2DF | #24302B |
| `line-strong` | #0E1512 | #5C6F66 |
| `ink` | #0E1512 | #EEF2F0 |
| `muted` | #56635D | #9AA8A1 |
| `accent` | #0A7A55 | #34D399 |
| `accent-hover` | #075C40 | #6EE7B7 |
| `on-accent` | #FFFFFF | #06241A |
| `accent-tint` | #E3F3EC | #0F2A20 |
| `on-accent-tint` | #064E36 | #A7F3D0 |
| `warning-tint` | #FFF1D6 | #2A2008 |
| `on-warning-tint` | #6B3F00 | #FCD34D |
| `danger` | #B42318 | #F87171 |
| `danger-tint` | #FDE7E4 | #2A0F0C |
| `on-danger-tint` | #8A1C12 | #FCA5A5 |

Notes: the page is a deep green-black, not `#000000`. `accent` is lightened so links and timestamps keep contrast, which flips `on-accent` to dark text. `line-strong` stops matching `ink` and becomes a mid green-grey so control borders keep 3:1. Shadows do not exist in either theme, so nothing disappears.

### Kontrast - mørk palett (WCAG 2.1 AA)

| Forgrunn | Bakgrunn | Ratio | Status |
|---|---|---|---|
| `ink` | `paper` | 17.11:1 | AAA |
| `ink` | `surface` | 15.81:1 | AAA |
| `ink` | `surface-hover` | 14.59:1 | AAA |
| `muted` | `paper` | 7.81:1 | AAA |
| `muted` | `surface` | 7.22:1 | AAA |
| `muted` | `surface-hover` | 6.66:1 | AA |
| `accent` | `paper` | 10.05:1 | AAA |
| `accent` | `surface` | 9.29:1 | AAA |
| `accent` | `surface-hover` | 8.57:1 | AAA |
| `accent-hover` | `paper` | 12.67:1 | AAA |
| `accent-hover` | `surface` | 11.71:1 | AAA |
| `on-accent` | `accent` | 8.57:1 | AAA |
| `on-accent` | `accent-hover` | 10.81:1 | AAA |
| `on-accent-tint` | `accent-tint` | 11.93:1 | AAA |
| `on-warning-tint` | `warning-tint` | 11.13:1 | AAA |
| `on-danger-tint` | `danger-tint` | 9.44:1 | AAA |
| `danger` | `paper` | 6.98:1 | AA |
| `danger` | `surface` | 6.45:1 | AA |
| `line-strong` | `paper` | 3.61:1 | AA, large/UI border |
| `line-strong` | `surface` | 3.33:1 | AA, large/UI border |
| `line` | `paper` | 1.41:1 | decorative, never the only border of a control |

The tightest text pair is `danger` on `surface` (6.45:1); the tightest non-text pair is `line-strong` on `surface` (3.33:1).

## CSS Variables

Variable names equal the token names. The Flask app keeps them in `templates/base.html`; `design-reference.html` carries the same block. Light is `:root`; dark overrides only what changes.

```css
:root {
  --paper: #FFFFFF;
  --surface: #F4F6F5;
  --surface-hover: #EAEEEC;
  --line: #DCE2DF;
  --line-strong: #0E1512;
  --ink: #0E1512;
  --muted: #56635D;
  --accent: #0A7A55;
  --accent-hover: #075C40;
  --on-accent: #FFFFFF;
  --accent-tint: #E3F3EC;
  --on-accent-tint: #064E36;
  --warning-tint: #FFF1D6;
  --on-warning-tint: #6B3F00;
  --danger: #B42318;
  --danger-tint: #FDE7E4;
  --on-danger-tint: #8A1C12;

  --font-sans: "Schibsted Grotesk", system-ui, sans-serif;
  --font-serif: "Newsreader", Georgia, serif;

  --space-1: 0.25rem;
  --space-2: 0.5rem;
  --space-3: 0.75rem;
  --space-4: 1rem;
  --space-6: 1.5rem;
  --space-9: 2.25rem;
  --space-12: 3rem;
  --space-24: 6rem;

  --radius-sm: 0.25rem;
  --radius-md: 0.5rem;
  --radius-lg: 0.75rem;

  --ease-out: cubic-bezier(0.16, 1, 0.3, 1);
  --dur-1: 0.18s;
  --dur-2: 0.6s;
  --glow: #0A7A5529;
  --header-glass: #FFFFFFB8;

  --width-page: 70rem;
  --width-prose: 47.5rem;
  --display-fluid: clamp(2.5rem, 6vw, 4rem);
  --title-fluid: clamp(1.875rem, 5vw, 2.75rem);
}

@media (prefers-color-scheme: dark) {
  :root {
    --paper: #0A0F0D;
    --surface: #121916;
    --surface-hover: #18211D;
    --line: #24302B;
    --line-strong: #5C6F66;
    --ink: #EEF2F0;
    --muted: #9AA8A1;
    --accent: #34D399;
    --accent-hover: #6EE7B7;
    --on-accent: #06241A;
    --accent-tint: #0F2A20;
    --on-accent-tint: #A7F3D0;
    --warning-tint: #2A2008;
    --on-warning-tint: #FCD34D;
    --danger: #F87171;
    --danger-tint: #2A0F0C;
    --on-danger-tint: #FCA5A5;
    --glow: #34D39933;
    --header-glass: #0A0F0DA8;
  }
}
```

## Endringer

| Date | Change | Reason |
|---|---|---|
| 2026-10-09 | Initial DESIGN.md and design-reference.html for the Podskrift redesign (branch `feat/redesign-blekk`) | Tokens distilled from the approved canvas (`tokens.json`, README, five prototype pages). Nothing invented; values equal the token file. |
| 2026-10-09 | Added `surface-hover` (light `#EAEEEC`, dark `#18211D`) | Row hover; added by decision outside `tokens.json`. |
| 2026-10-09 | Location: repo root, not `nettsmed-shared/clients/` | Podskrift is Sindre's own product, not a Nettsmed client. Client card, `assets.md` and shared-repo commit steps skipped. |
| 2026-10-09 | Dark mode included, driven by `prefers-color-scheme` only, no toggle | Active opt-in per the dark-mode reference, but the convention there asks for a visible toggle and never auto-dark. Decided otherwise for Podskrift: the visitor's system setting decides, light is the fallback. The reference's `[data-theme]` strategy is not used. |
| 2026-10-09 | Validator warning: brand/extended palette 16 over soft limit 14 | All 17 colour tokens are decided and each has a documented role. Grouped as surfaces/lines (5), text (2), accent family (3), alert tints (6), semantic (1). Only `danger` counts as semantic by the validator's name match; the three tint pairs are alert backgrounds and are not renamed. |
| 2026-10-09 | Validator: git-tracking check fails until both files are committed | The check requires `DESIGN.md` and `design-reference.html` to be tracked; they are intentionally uncommitted when delivered. Clears on commit. |
| 2026-10-09 | Contrast and token-override tables use Norwegian column names (foreground/background and light/dark in the validator's own words) | `validate.sh` finds the tables by these exact header words; prose and everything else is English. |
| 2026-10-09 | Prototype drift: 15px nav/compact-control/alert text, 19-32px headings and 22-30px figures are not in the type scale | `tokens.json` defines nine styles and these sizes are not among them. The reference maps them to the nearest token (`control` 16px, `body` 17px, `heading` 24px). Templates should do the same until a decision adds a style. |
| 2026-10-09 | Prototype drift: desktop nav "Sign up" is an ink-filled button in the prototypes | Superseded by the next row. |
| 2026-10-09 | Decided: the desktop nav call to action is ink-filled (`--ink` background, `--paper` text), the ONE allowed non-accent filled button; in the mobile menu panel the same link is the accent button | Matches the prototypes and keeps the page's single accent-filled button for the page action. Replaces the earlier outline rendering. `design-reference.html` nav updated. |
| 2026-10-09 | Decided: monospace is allowed only inside `<code>` and `<pre>` | API docs and key display need a fixed-width face; timestamps and all UI text stay Schibsted Grotesk. |
| 2026-10-09 | Component paddings snapped to the spacing scale (button 22px to 24px, search 18px to 16px, alert 14px to 12px) | The prototypes use 22/18/14px, which are not spacing tokens. The scale wins. |
| 2026-10-09 | Prototype drift: transcript 19px in `Komponenter`, 20px elsewhere; cover thumbnail radius 6px | Token file says 20px and `radius-sm` 4px; the tokens win. |
| 2026-10-09 | Open: PNG icons and `og-image.png` still need regenerating | Predate the redesign. |
| 2026-10-10 | Added Jinja macros (`templates/components/`), `static/components.css`, `/design/components` gallery, `test_design_system.py`, and "How to build a page" | Stop agent drift from DESIGN.md; rejected /connect draft is the anti-pattern. |
| 2026-10-10 | Added a static MCP chat illustration to the homepage and /ai, with matching reference markup | Show the transcript request in context, using existing tokens and an explicit example caption. |
| 2026-10-10 | Header and hero redesign ("signal"): glass sticky header, centred hero with a command bar, full-width signal wave, transcript window. New tokens `--radius-lg`, `--glow`, `--header-glass`, `--ease-out`, `--dur-1`, `--dur-2`; motion intensity minimal to moderate; two sanctioned effects (header blur, hero glow) | Sindre asked for a more modern feel in the style of x.ai, with motion and illustration, and approved the direction from a mockup. New macros: `announce`, `command_bar`, `works_with`, `signal_wave`, `transcript_window`; `proof_list` gains an inline variant. |
