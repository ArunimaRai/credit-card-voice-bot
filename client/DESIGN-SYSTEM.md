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
3. **One dark primary.** The dark pill used by Start Speaking is the only primary or
   "selected" style. Anything the user is meant to press next, or anything currently
   selected in a segmented control, uses it.
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
| Speaker ink | `--lav-ink` (Shreya), `--peach-ink` (customer) | `#4150a8`, `#a3582b` |

Orb source colours (shader only): anchor `#250785`, cyan `#3CE0FF`, teal `#36ABAD`,
pink `#FF5EA8`. In CSS they appear as `rgba(124,108,255,…)`, `rgba(60,224,255,…)` and
`rgba(255,94,168,…)`.

## 3. Surface patterns

### Orb wash (cards, chips, header status pill)
- Background: `var(--orb-wash)`, white with three radial tints at about 3–5% alpha.
- Border: `1px solid var(--orb-line)`.
- Shadow: `var(--card-shadow)`, a hairline plus a soft drop. Nothing inset.
- Radius: 20px for cards, 99px for pills and chips.
- Used by `.an-hero`, `.an-card`, `.status-pill`, `.caller-chip`.

### Strong wash (one highlighted block inside a card)
- Background: `var(--orb-wash-strong)` (about 7–9% alpha), border `var(--accent-100)`, radius 14px.
- Used by the follow-up row `.an-follow`. Its "no follow-up" variant is flat `--surface-2`.
- Use at most one per card.

**Do not** raise the wash alphas above the token values, add `inset` shadows, or stack
`linear-gradient` highlights on top. If a surface needs more emphasis, use a border or
text weight instead.

## 4. Controls

### Primary CTA: the dark pill
Tokens: `--cta-bg`, `--cta-border`, `--cta-ink`, `--cta-shadow`, `--cta-shadow-hover`.

| Component | Where | Notes |
| --- | --- | --- |
| `.call-btn` (Start Speaking) | Orb panel | 15px × 30px padding, 16px/500 type. Shown only on the home screen. |
| `.new-call-btn` (+ New call) | Header, right | Same pill, compact (8px × 16px, 13px). Replaces the status pill after a call ends. |
| `.tabs button[aria-selected="true"]` | Analysis / Transcript toggle | The selected segment is the dark pill. The track is plain white with a `--line` border. Unselected segments are text only in `--ink-2`. |

Hover: lift 1px and use `--cta-shadow-hover`. Active: `scale(0.97)`.
Focus: `outline: 2–3px solid var(--accent-600); outline-offset: 2–3px`.

### State variants of the call button
- `data-mode="hangup"`: coral fill, hang-up icon.
- `data-mode="connecting"`: `--surface-2` fill, `--ink-2` text, spinner, no shadow.

### Chips and pills
- `.chip`: white, `--line` border, 11.5px/500. Tone variants `.warn` and `.bad` use the status families.
- `.sent-pill`: a flat tint and its matching border. Neutral uses `--accent-50`, frustrated uses `--red-50`, positive uses `--live-50`. No gradients.

## 5. Conversation bubbles

| Speaker | Fill | Border | Tail corner |
| --- | --- | --- | --- |
| Shreya (bot) | `var(--bubble-bot)`, indigo/cyan wash on `#f6f6ff` | `var(--bubble-bot-line)` | bottom-left 8px |
| Customer | `var(--bubble-user)`, peach/pink wash on `#fff7f2` | `var(--bubble-user-line)` | bottom-right 8px |

- Radius 24px, padding 15px × 22px, 16.5px text. No inner shadow.
- The same two fills are used everywhere a speaker is shown: live bubbles (`.msg .bubble`),
  the empty-state illustration (`.empty-art .e1/.e2/.e3`) and the analysis quote cards
  (`.an-quote`, customer fill).

## 6. Typography

- Family: `--font` (system SF stack). Use `--font-mono` only for the phone number.
- Hero greeting: 26–34px (clamp), 600, `-0.03em`.
- Panel title: 19px/600. Card title: 22px/600 (outcome).
- Section label `.an-k`: 11px, uppercase, `0.07em` tracking, `--ink-3`.
- Body: 13.5–14.5px, line-height 1.5–1.6. Timer: 30px/500, tabular numbers.

## 7. Layout

- Console: a 5/7 split grid inside a 28px-radius shell with `--shadow-md`. It stacks below 860px.
- Panel padding: 32px on desktop, 20px on mobile. Card gap: 14px.

## 8. Motion

- Orb shader clock, the `.orb` breathe and the `.orb-wrap::after` contact shadow run only
  in the `connecting`, `connected-idle`, `listening` and `bot-speaking` states. In `idle`
  and `ended` the shader clock stops (`orb.setActive(false)`) and the CSS animations are
  paused.
- Easing: `--ease-smooth` for entrances, `--ease-out-quint` for button presses.
- Entrances: `riseIn` / `msgIn`, 0.35–0.6s.
- Respect `prefers-reduced-motion`; it is already handled globally.

## 9. Call states (console `data-state`)

| State | Orb | Orb-panel button | Header right |
| --- | --- | --- | --- |
| `idle` | still | Start Speaking | status pill "Ready" |
| `connecting` | moving, halo breathes | Connecting… (muted) | status pill "Connecting" |
| `connected-idle` / `listening` / `bot-speaking` | moving, audio-reactive | Hang Up (coral) | status pill "On call · mm:ss" (teal) |
| `ended` | still, grey | hidden | **+ New call** (dark pill). Clicking it goes back to `idle`. |

## 10. Checklist for any UI change

- [ ] Uses existing tokens. Any new colour is added to `:root` and to this file.
- [ ] Primary or selected controls use the dark CTA pill, and nothing else does.
- [ ] Cards and chips use the orb wash with no inset shadows or glossy highlights.
- [ ] Speaker colours use the bubble tokens.
- [ ] Nothing animates in `idle` or `ended`.
- [ ] Checked at desktop width and below 860px.
