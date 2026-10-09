# Podskrift

Podskrift turns a podcast episode into text. The text is the product, so the interface looks like a page: white paper, black ink, and green only where something happens.

## Content

- Interface copy is English, plain and short. Say what happens: "It keeps going if you close the page."
- Sentence case everywhere. No exclamation marks, no emoji.
- Name limits and prices in the first sentence: "First 60 minutes free", "300 minutes for $5", "No subscription".
- Error text says what went wrong and what to do next.

## Colour

Two themes, `light` first. Light is the default; dark follows the visitor's system setting.

- `paper` is the page, `surface` is the one step up for cards. Separate areas with `line`, never with shadows or gradients.
- `ink` for text, `muted` for supporting text.
- `accent` marks action and progress: the primary button, links, timestamps, the active tab, the progress fill. Use one `accent` button per screen; other buttons take a `line-strong` border or are plain text.
- Text on `accent` is `on-accent`. In dark that is dark text on bright green.
- Alerts use a tint with its matching `on-…` text: `accent-tint`, `warning-tint`, `danger-tint`.
- `danger` is for errors and the cancel action only.

## Type

- Schibsted Grotesk (`sans`) for the whole interface, weights 400, 500, 700, 800. Titles are set tight and upright: `display` and `title` at weight 800. Never italic titles.
- Newsreader (`serif`) only for transcript text, style `transcript`.
- Timestamps use style `timestamp` in `accent` with `font-variant-numeric: tabular-nums`. No monospace anywhere.
- Both faces load from Google Fonts: `Schibsted+Grotesk:wght@400;500;600;700;800` and `Newsreader:opsz,wght@6..72,400;6..72,500`.

## Shape

- `radius-md` (8px) on buttons, inputs, cards and alerts. No pill buttons.
- 1px borders. Controls are at least 44px tall; primary inputs and buttons 48 to 56px.
- No shadows, no gradients, no left-border cards.

## Logo and motif

The mark is three vertical bars followed by three horizontal lines: sound turning into text. Bars are `ink`, lines are `accent`. The wordmark is "podskrift" in lower case, Schibsted Grotesk 800, letter-spacing -0.035em.

```html
<svg width="30" height="28" viewBox="0 0 30 28" aria-hidden="true">
  <g fill="var(--ink)"><rect x="2" y="9" width="3" height="10" rx="1.5"/><rect x="8" y="4" width="3" height="20" rx="1.5"/><rect x="14" y="8" width="3" height="12" rx="1.5"/></g>
  <g fill="var(--accent)"><rect x="20" y="7" width="8" height="3" rx="1.5"/><rect x="20" y="12.5" width="8" height="3" rx="1.5"/><rect x="20" y="18" width="5" height="3" rx="1.5"/></g>
</svg>
```

The same idea stretched wide (a row of bars that becomes three text lines) is the one decorative motif. Use it in the hero and while a transcription runs.

## Iconography

Few icons. Inline stroke SVG, 2px stroke, round caps, `ink` or `accent`. No emoji.

## Components

There is no code bundle yet. The reference for buttons, search, form fields, episode rows, the job card, the transcript block, alerts and the credit pack is the "Komponenter i A" artboard and the prototype pages on the Podskrift redesign canvas.
