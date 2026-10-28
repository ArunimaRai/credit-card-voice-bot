# Call console design system

The single reference for every UI change to the call console (`client/index.html`).
Read this before touching the console's markup or CSS, and update it in the same change
if you add or change a pattern.

All tokens live as CSS custom properties on `:root` at the top of `client/index.html`.
Use the token, never a raw value, when one exists.

## 1. Principles

1. **The orb is the source of the palette.** Indigo, cyan, pink and teal all come from the
   orb shader. Surfaces pick up a *faint* tint of those colours, not a strong one.
2. **Calm, flat and professional.** Soft gradients are a tint, not an effect. No 3D bevels,
   inner shadows or glossy highlights on cards, chips or bubbles.
3. **One dark primary, one white secondary.** The dark pill (Start Speaking, + New call) is
   the only primary style: it marks the action the user should take next. A selected
   segment in a toggle is a secondary state and stays white (see §4).
4. **Motion means a live call.** The orb, halo and contact shadow move only while a call is
   connecting or live. Before the first call and after a call ends, everything holds still.

## 2. Colour tokens

| Role | Token | Value |
| --- | --- | --- |
| Page / surface | `--bg`, `--surface` | `#ffffff` |
| Muted surface | `--surface-2` | `#f6f7fb` |
| Ink | `--ink` / `--ink-2` / `--ink-3` | `#15161a` / `#52555d` / `#8a8d96` |
| Hairlines | `--line` / `--line-soft` | `#e9e9f0` / `#f1f1f6` |
| Accent (indigo) | `--accent-50 … --accent-700` | `#f0efff` → `#4033b0` |
| Live / positive (teal) | `--live`, `--live-50`, `--live-100`, `--ok-700` | `#23a7a3` family |
| Warning | `--amber`, `--amber-50`, `--amber-line` | `#a1661a` family |
| Error / frustrated | `--red`, `--red-50`, `--red-line` | `#c8413a` family |
| Hang up | `--coral`, `--coral-700` | `#c94b41`, `#b03d34` |
| Speaker ink | `--lav-ink` (Shreya), `--rose-ink` (customer) | `#4150a8`, `#a83f7a` |

Orb source colours (shader only): anchor `#250785`, cyan `#3CE0FF`, teal `#36ABAD`,
pink `#FF5EA8`. In CSS they appear as `rgba(124,108,255,…)`, `rgba(60,224,255,…)` and
`rgba(255,94,168,…)`.

**No colours outside the orb palette.** Orange, peach and other hues not in the orb are
not used for decoration. Amber is kept only as the semantic warning colour (chips, the
dispute dot, the connecting dot).

## 3. Surface patterns

### Orb wash (cards, chips, header status pill)
- Background: `var(--orb-wash)`, white with three radial tints at about 3–5% alpha.
- Border: `1px solid var(--orb-line)`.
- Shadow: `var(--card-shadow)`, a hairline plus a soft drop. Nothing inset.
- Radius: 20px for cards, 99px for pills and chips.
- Used by `.an-hero`, `.an-card`, `.stat`, `.flow li`, `.caller-chip`.

### Strong wash (one highlighted block inside a card)
- Background: `var(--orb-wash-strong)` (about 7–9% alpha), border `var(--accent-100)`, radius 14px.
- Used by the follow-up row `.an-follow` and the walkthrough note. Its "no follow-up" variant is flat `--surface-2`.
- Use at most one per card.

### Walkthrough note (pop-up)
A card shown at the moment the viewer reaches something worth a word: either a place where
the demo knowingly falls short of production ("Demo note"), so the gap is ours to name
before they notice it, or what a feature on screen does ("How it works").
- Fixed to the top-right corner, just under the header (`top: 84px; right: 24px`, 72px/16px
  below 860px), `min(440px, 100vw - 32px)` wide. It is meant to be seen, so it sits where
  the eye lands and can cover the panel header until dismissed.
- Strong orb wash on an opaque `--surface` base (it floats over content), `--accent-100`
  border, `--card-shadow` plus a deeper indigo drop, radius 22px.
- Enters with `noteIn`: slides in from the top right (translate 32px, -20px, scale 0.96)
  over 0.6s on `--ease-smooth`, `transform-origin: top right`.
- An uppercase kind label (12px, `--accent-700`, ringed `--accent-500` dot), a 19px/600
  title, 16px body in `--ink-2`, a "Note n of m" step in `--ink-3`, and a dark-pill **Got it**
  button (10px × 20px, 14px).
- It never blocks the call: no backdrop, and the call stays usable underneath.
- Each note is shown once per page load. Several queue, one at a time.
- Add a note by adding `{ kind, title, text }` to `GAP_NOTES` in `client/index.html` and
  calling `showGapNote(key)` where it happens. Keep the copy to two plain sentences.

| Key | Kind | Shown when | Says |
| --- | --- | --- | --- |
| `accent` | Demo note | Shreya first speaks on a call | We'd have liked an Indian accent; the demo uses OpenAI's voice for simplicity, which doesn't support one. |
| `evals` | How it works | The Agent performance card first renders | Code checks review Shreya's side of every call; each issue is queued as an eval case so the next prompt version is tested against it. |

**Do not** raise the wash alphas above the token values, add `inset` shadows, or stack
`linear-gradient` highlights on top. If a surface needs more emphasis, use a border or
text weight instead.

## 4. Controls

### Primary CTA: the dark pill
Tokens: `--cta-bg`, `--cta-border`, `--cta-ink`, `--cta-shadow`, `--cta-shadow-hover`.

| Component | Where | Notes |
| --- | --- | --- |
| `.call-btn` (Start Speaking) | Orb panel | 15px × 30px padding, 16px/500 type. Shown only on the home screen. |
| `.new-call-btn` (+ New call) | Header, right | Same size as Start Speaking (16px/500, 19px icon, about 48px tall). Shown only on the console page, after a call ends. |

Hover: lift 1px and use `--cta-shadow-hover`. Active: `scale(0.97)`.
Focus: `outline: 2–3px solid var(--accent-600); outline-offset: 2–3px`.

### Secondary: the white segmented toggle
Used for switching views, never for "do this next".

| Component | Where | Notes |
| --- | --- | --- |
| `.tabs` | Analysis / Transcript toggle | White track, `--line` border, 3px padding. Segments 13px/500, 7px × 16px, unselected text in `--ink-2`. |
| `.tabs button[aria-selected="true"]` | | Stays white. Marked by `--accent-700` text at 600, a 1px `--accent-300` ring and a soft indigo drop. Never the dark fill. |

### State variants of the call button
| `.seg` | Page filters (Personas) | Same pattern at 13px, `aria-pressed` marks the selected segment. |

### Header and page nav
- Left: the brand mark and the "Mumbai Bank" wordmark only, no product name or divider.
  The brand links to `#/`, the call console; there is no "Call console" nav item.
- Right (`.header-end`): the `.site-nav` links, then **+ New call** when it shows.
- `.site-nav` links sit directly on the header: 15.5px/500 `--ink-2`, 32px apart, no pill,
  track or outline. Hover goes to `--ink`. The current page (`aria-current="page"`) is
  `--ink` at 600 with a 2px `--accent-500` rule on the header's bottom edge.
- At 600px and below the nav takes its own row under the brand (15px, 22px apart).
- There is no status pill; the orb panel's timer and caption carry the call state.
- `data-mode="hangup"`: coral fill, hang-up icon.
- `data-mode="connecting"`: `--surface-2` fill, `--ink-2` text, spinner, no shadow.

### Chips and pills
- `.chip`: white, `--line` border, 11.5px/500. Tone variants `.warn` and `.bad` use the status families.
- `.sent-pill`: a flat tint and its matching border. Neutral uses `--accent-50`, frustrated uses `--red-50`, positive uses `--live-50`. No gradients.
- Severity chips (Agent performance): Critical `.chip.bad`, Major `.chip.warn`, Minor plain `.chip`.

### Analysis card order
Outcome (hero) → Customer sentiment + Workflow (grid) → **Call summary** → **Agent performance** → footnote.

Agent performance (`performanceHtml`) shows code-only checks on Shreya's own handling of
the call (`review_agent` in `bot/analysis.py`). The head row is the `.an-k` label with a
"n checks · m issues" count in `--ink-3`. Each `.an-issue` row is a severity chip, a 14px/600
title, a 13.5px detail, the offending turn as an `.an-quote` (bot fill when it's Shreya's
turn, field names in `<code>`), and an "Queued as eval case · `name`" line with the eval in an
`--accent-50` code tag. With no issues it shows a teal tick and "No issues on this call".

## 5. Conversation bubbles

| Speaker | Fill | Border | Tail corner |
| --- | --- | --- | --- |
| Shreya (bot) | `var(--bubble-bot)`, indigo/cyan wash on `#f4f4fe` | none | bottom-left 8px |
| Customer | `var(--bubble-user)`, pink/indigo wash on `#fbf4fa` | none | bottom-right 8px |

- Radius 24px, padding 15px × 22px, 16.5px text. No border and no inner shadow: the two
  speakers are told apart by fill and alignment only.
- The same two fills are used everywhere a speaker is shown: live bubbles (`.msg .bubble`),
  the empty-state illustration (`.empty-art .e1/.e2/.e3`) and the analysis quote cards
  (`.an-quote`, customer fill).

### Unclear turn (`.msg.unclear`)
A customer turn the STT filter (`bot/stt_filter.py`) held back from Shreya. It stays in the
transcript, so the customer can see what was misheard, but it must read as "not used":
- Bubble: `--surface-2` fill, `--ink-3` text struck through in `--ink-3`, a 1px inset
  `--line` ring in place of the customer wash.
- Meta row: an `.unclear-tag` pill after the time, "Unclear · not used", 11px/500 in
  `--ink-2` on `--surface-2` with a `--line` border. Its tooltip gives the reason in plain
  words (`UNCLEAR_LABELS`).
- Set live by the server's `turn-unclear` message, and from `unclear` on saved transcripts.
- While streaming, a customer bubble builds up from interim deltas (appended), then the final
  text replaces it.

## 6. Brand mark

- The `.brand-mark` SVG in the header is the dark CTA squircle (`#2c2c35 → #15161a`, 14px
  radius on a 48px box, a 12% white hairline) holding a small orb: a white-to-indigo sphere
  with a cyan glow at top right and a pink glow at bottom left.
- It is shown at 34px next to the "Mumbai Bank" wordmark (17px/600). Don't use line icons,
  bank pillars or other pictograms in the mark; it is built only from the CTA and the orb.

## 7. Typography

- Family: `--font` (system SF stack). Use `--font-mono` only for the phone number.
- Hero greeting: 26–34px (clamp), 600, `-0.03em`.
- Panel title: 19px/600. Card title: 22px/600 (outcome).
- Section label `.an-k`: 11px, uppercase, `0.07em` tracking, `--ink-3`.
- Body: 13.5–14.5px, line-height 1.5–1.6. Timer: 30px/500, tabular numbers.

## 8. Layout

- Console: a 5/7 split grid inside a 28px-radius shell with `--shadow-md`. It stacks below 860px.
- Panel padding: 32px on desktop, 20px on mobile. Card gap: 14px.

## 9. Motion

- Orb shader clock, the `.orb` breathe and the `.orb-wrap::after` contact shadow run only
  in the `connecting`, `connected-idle`, `listening` and `bot-speaking` states. In `idle`
  and `ended` the shader clock stops (`orb.setActive(false)`) and the CSS animations are
  paused.
- Easing: `--ease-smooth` for entrances, `--ease-out-quint` for button presses.
- Entrances: `riseIn` / `msgIn`, 0.35–0.6s.
- Respect `prefers-reduced-motion`; it is already handled globally.

## 10. Call states (console `data-state`)

| State | Orb | Orb-panel button | Header right |
| --- | --- | --- | --- |
| `idle` | still | Start Speaking | nothing |
| `connecting` | moving, halo breathes | Connecting… (muted) | nothing |
| `connected-idle` / `listening` / `bot-speaking` | moving, audio-reactive | Hang Up (coral) | nothing |
| `ended` | still, grey | hidden | **+ New call** (dark pill). Clicking it goes back to `idle`. |

## 11. Pages: Guidelines, Personas, Analytics

The header nav (and the brand, for the console) switches between four views by hash (`#/`,
`#/guidelines`, `#/personas`, `#/analytics`). They live in the same document, so a live call keeps running while the
user reads another page. `client/pages.js` holds the router and the three renderers; each
page fetches `/api/insights/<page>` (`bot/insights.py`) when it opens and every 15s while
open. Every number comes from files on disk; never hard-code one in the page.

Page anatomy (`.page.doc`):
- **Head** (`.doc-head`): `.an-k` kicker, 26–32px/600 `h1`, 15px `.doc-lead` in `--ink-2`
  naming the source files in `<code>`, and on the right the `.doc-sync` line ("Live ·
  updated hh:mm:ss" with a teal dot, amber when a refresh failed) plus a small white
  Refresh button.
- **Stat tiles** (`.stats` > `.stat`): orb wash, an `.an-k` label, a 28px/600 tabular value
  (unit in a 14px `<small>`), a 12.5px `--ink-3` note. One row per page, at the top.
- **Sections** (`.doc-section`): 19px/600 `h2`, a 13.5px `.doc-sub`, then cards in a
  `.doc-grid` (two columns, one below 1000px).
- **Tables** (`.tbl` in `.tbl-wrap`, inside an `.an-card`): uppercase 11px headers, hairline
  rows, numbers right-aligned and tabular, IDs in `--font-mono` `--ink-3`. The wrapper
  scrolls sideways on phones so the page itself never does.
- **Rule lists** (`.rules`): a 26px `--accent-50` numbered disc and the rule text.
- **Call flow** (`.flow`): one orb-wash card per stage; the sample line uses the bot
  bubble fill (`.flow-say`).
- **Metric rows** (`.metric`, Analytics): name 14px/600, value 18px/600 right, description
  12.5px `--ink-3`. A criterion with no data shows "Not measured yet" in `--ink-3`, never a
  placeholder number.
- **Bars** (`.bars-list`): single-series horizontal bars, one hue (`--accent-500`), 10px
  tall, 4px radius on a `--surface-2` track, the value in ink beside the bar. `.hl` marks the
  one live item in teal (the STT config in use). No legends, since there is one series.
- **Self-evolving callout** (`.evolve`, Personas): the strong wash block with an icon tile,
  an explanation and a `.loop` row of four counts on white. One per page.
- New chips: `.chip.ok` (teal: passed, easy, learned live) and `.chip.accent` (indigo:
  from the bank's brief).

## 12. Checklist for any UI change

- [ ] Uses existing tokens. Any new colour is added to `:root` and to this file.
- [ ] Primary actions use the dark CTA pill; selected toggle segments use the white secondary style.
- [ ] Cards and chips use the orb wash with no inset shadows or glossy highlights.
- [ ] Speaker colours use the bubble tokens, and no hue outside the orb palette appears.
- [ ] Nothing animates in `idle` or `ended`.
- [ ] Checked at desktop width, below 860px and at 375px (no horizontal page scroll).
- [ ] Page numbers come from `/api/insights/*`, not from the markup.
