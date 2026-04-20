# Style guide — Elijah Frost visual language

Use this with **`tokens.css`**. The goal is a shared **look**: palette, edges, controls, type, and motion — not a single page layout.

---

## What this includes vs. what it does not

**Included (portable “skin”):**

- **Colors:** semantic CSS variables for dark-first and `html.light` (see table below).
- **Edges & surfaces:** which tokens to use for hairlines, panels, dividers, and inputs — no mandatory page grid.
- **Buttons & links:** outline-button and form-field recipes using the same borders and micro-typography.
- **Cursor (optional):** variables for arrow fill/stroke plus CSS that hides the OS cursor when you opt in; implementation details below.
- **Typography:** Inter for UI/body, Cormorant for display — scale recipes you can apply anywhere (not “you must build a hero”).
- **Motion:** global theme transitions, `fade-in-up`, reduced-motion behavior.

**Not prescribed (project-specific):**

- Headers, footers, nav bars, heroes, multi-column CV sections, or `max-w-*` shells. Use whatever structure fits the product.
- **When applicable:** the reference site uses patterns you can borrow — fixed top bar height with `section[id] { scroll-margin-top: … }` in `tokens.css`, `--color-chrome-border` for floating chrome, `--color-footer-year` if you add a footer line. Treat these as **hints**, not requirements.

---

## Principles

- **Dark-first:** `:root` is dark; `html.light` overrides the same token names.
- **Warm neutrals:** stone/warm gray, not cold blue-gray.
- **One transition language:** `--theme-transition-duration` (120ms) and `linear` on inherited color changes. Separate UI motion may use `cubic-bezier(0.16, 1, 0.3, 1)`.
- **Edges over shadows:** prefer **borders** and clear type hierarchy over heavy elevation.
- **Restraint:** uppercase micro-labels use tight tracking (`0.15em`–`0.28em`) and small sizes (`9px`–`13px`).

---

## CSS variables (semantic tokens)

Prefer these over raw hex (Tailwind: `text-[var(--color-fg-muted)]`, `border-[var(--color-border)]`).

| Token | Typical use |
|-------|-------------|
| `--color-bg-page` | Page background |
| `--color-fg` | Primary text, default icon color |
| `--color-fg-bright` | Stronger emphasis (e.g. hover) |
| `--color-fg-muted` | Secondary text |
| `--color-fg-dim` | Tertiary / quiet labels |
| `--color-fg-soft` | Muted control label (e.g. button before hover) |
| `--color-fg-body` | Long reading text / bullets (softer than `--color-fg`) |
| `--color-label` | Placeholders, very quiet UI |
| `--color-border` | Default 1px-style borders, outline controls |
| `--color-border-hover` | Border on hover |
| `--color-border-section` | Panels, inputs, stronger section frames |
| `--color-border-divider` | Horizontal rules between stacked blocks |
| `--color-numeral` | Decorative numerals (only if your layout uses them) |
| `--color-bullet` | List bullet dots |
| `--color-border-input-focus` / `--color-border-input-error*` | Form fields |
| `--color-chrome-border` | Optional: top bars, floating UI (often semi-transparent) |
| `--color-hero-grid` | Optional: line/grid art tied to `currentColor` |
| `--color-footer-year` | Optional: muted year or footer meta |
| `--color-error-text` | Errors (often with uppercase micro type) |
| `--color-selection-bg` / `--color-selection-fg` | Text selection |
| `--color-tap-highlight` | iOS tap flash |
| `--cursor-fill`, `--cursor-stroke`, `--cursor-arrow-fill` | Custom cursor SVG (if you implement one) |
| `--theme-transition-duration` / `--theme-transition-ease` | Theme-linked color transitions |

---

## Surfaces & edges

- **Default stroke:** `border-[var(--color-border)]` — hairline, neutral.
- **Stronger frame:** `border-[var(--color-border-section)]` — inputs, bordered cards, success states.
- **Hover (interactive):** `hover:border-[var(--color-border-hover)]` with `transition` aligned to globals (often `duration-[120ms]` on buttons).
- **Separators:** `border-t border-[var(--color-border-divider)]` between stacked items.
- **Radius:** the reference site keeps corners subtle (`rounded-sm`, `rounded-lg` where needed); avoid large pill radii unless intentional.
- **Background on nested panels:** often `bg-[var(--color-bg-page)]` so nested bordered regions match the page in both themes.

---

## Custom cursor (optional)

**Tokens:** `--cursor-fill`, `--cursor-stroke`, and `--cursor-arrow-fill` track fg/page so a custom SVG arrow matches light and dark.

**CSS (in `tokens.css`):** When you set `data-cursor-custom` on `<html>`, rules hide the system cursor; `[data-cursor-root]` marks where your custom cursor layer lives so it stays interactive. If you do **not** set `data-cursor-custom`, the browser cursor behaves normally — **no extra work**.

**Behavior / React:** The reference implementation (`app/components/CustomCursor.tsx` and `cursor-icon.tsx` in this repo) handles pointer move, interactive hover states, and reduced-motion/touch. Copy or adapt that code only if you want the same behavior; the design system only guarantees **tokens + hide-pointer CSS**.

---

## Typography

- **Body:** 15px / 1.55 mobile, 16px / 1.5 from `sm` — set on `body` in `tokens.css`. **Inter** (`font-sans` / variable `--font-inter`) for UI and paragraphs.
- **Display / emphasis:** **Cormorant Garamond** (`--font-cormorant`) — `font-serif`, often `font-light`, for titles and hero lines **if** your project has them. Example scales from the reference site:
  - Large display: `font-serif`, `font-light`, `tracking-tight`, responsive `text-*` or `clamp(...)`.
  - Micro-labels: `text-[9px]`–`text-[10px]`, `tracking-[0.2em]`–`0.28em]`, `uppercase`, `text-[var(--color-fg-dim)]` or `muted`.
- **Code:** `rounded`, `bg-[var(--color-border-section)]`, `px-1.5`, `py-0.5`, `font-mono`, `text-sm`, `text-[var(--color-fg)]`.

---

## Components (recipes)

These are **controls and content patterns**, not full page templates.

### Primary outline button

```txt
inline-flex h-10 min-h-[44px] items-center justify-center gap-2 border border-[var(--color-border)] bg-transparent px-5 text-[9px] tracking-[0.18em] text-[var(--color-fg-soft)] uppercase transition-all duration-[120ms] hover:border-[var(--color-border-hover)] hover:text-[var(--color-fg)] sm:h-11 sm:px-7 sm:text-[10px] sm:tracking-[0.2em]
```

### Text inputs / textarea

**Base:**

```txt
w-full border border-[var(--color-border-section)] bg-[var(--color-bg-page)] px-3 py-2.5 text-xs text-[var(--color-fg)] outline-none placeholder:text-[var(--color-label)] transition-colors duration-100 sm:px-4 sm:py-3 sm:text-sm
```

Add `focus:border-[var(--color-border-input-focus)]`, or error borders per `tokens.css`.

### Form submit

```txt
flex min-h-[44px] w-full items-center justify-start gap-2 border border-[var(--color-border)] px-6 py-2.5 text-[9px] tracking-[0.18em] text-[var(--color-fg-soft)] uppercase transition-all duration-[120ms] hover:border-[var(--color-border-hover)] hover:text-[var(--color-fg)] sm:w-auto sm:min-h-0 sm:px-8 sm:py-3 sm:text-[10px] sm:tracking-[0.2em]
```

### Inline link

```txt
text-[var(--color-fg)] underline underline-offset-2 hover:text-[var(--color-fg-bright)]
```

### Bullet list

Row: `flex gap-2.5 sm:gap-3`, `text-xs sm:text-sm`, `leading-relaxed`, `text-[var(--color-fg-body)]`. Dot: `h-[3px] w-[3px] rounded-full bg-[var(--color-bullet)]`.

### Theme toggle (if you add one)

`focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--color-fg-muted)]`. Put `data-theme-toggle` on the button so `tokens.css` can disable SVG stroke tween.

---

## Optional layout context (reference site only)

Use **only** when your project needs similar behavior:

- **Fixed top bar + in-page anchors:** `tokens.css` sets `scroll-margin-top` on `section[id]` to clear ~`3.5rem` / `4.5rem` nav height + safe area. Remove or adjust if your chrome height differs.
- **Wide vs. narrow content:** the CV site uses `max-w-7xl` for hero-like bands and `max-w-5xl` for dense text — examples only.
- **Section top rule:** `border-t border-[var(--color-border-section)]` before a heading block — optional visual rhythm, not a requirement.

---

## Motion

- **Theme / color:** `body, body *` transition in `tokens.css` (honors `prefers-reduced-motion`).
- **Entrance:** `.fade-in-up` + `.delay-1` … `.delay-4`; do not nest `.fade-in-up`.

---

## Do / don’t

**Do:** Use semantic tokens for anything that must track light/dark. Prefer borders and type over heavy shadows. Respect `prefers-reduced-motion`.

**Don’t:** Hard-code page bg hex in components when `--color-bg-page` exists. Force a specific header/footer/hero layout because this guide showed examples.

---

## Minimal checklist for a new UI

1. Root shell: `bg-[var(--color-bg-page)]`, `text-[var(--color-fg)]`, `antialiased` where appropriate.
2. Interactive: `--color-border` / `--color-border-hover`; visible focus rings on keyboard focus.
3. Light mode: test with `html.light` / `next-themes`.
