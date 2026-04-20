# Elijah Frost design system (portable)

This folder packages the **visual language** from [elijahfrost.com](https://elijahfrost.com): **colors (CSS variables)**, **edges and surfaces** (borders, hairlines, panels), **buttons and form controls**, **motion defaults**, optional **custom cursor** CSS, and **typography pairing** (Inter + Cormorant). It does **not** prescribe page structure: headers, footers, heroes, and nav are **up to each project**. `STYLE_GUIDE.md` includes optional reference patterns (e.g. fixed nav + `section[id]` scroll margin) only when they apply.

Copy the whole `design-system/` directory into a new project and point an AI at `STYLE_GUIDE.md` plus `tokens.css`.

## Contents

| File | Role |
|------|------|
| `STYLE_GUIDE.md` | What’s core vs optional; token map; surfaces/edges; buttons; cursor; type; optional layout notes; do/don’t. |
| `tokens.css` | `:root` / `html.light`, body, selection, transitions, optional cursor hide rules, `section[id]` scroll-margin (useful if you add a fixed top bar), `fade-in-up`. |
| `tailwind-theme-snippet.css` | Tailwind v4 `@theme inline` font aliases (`font-sans`, `font-serif`, `font-display`). |

## Quick integration (Next.js App Router + Tailwind v4)

1. **Dependencies** (match your stack): `next`, `tailwindcss` v4, `@tailwindcss/postcss`, `next-themes` if you want light/dark.

2. **`postcss.config.mjs`** — include `@tailwindcss/postcss` (see the reference site).

3. **`app/globals.css`** — use this **order**:

   ```css
   @import "tailwindcss";

   /* Paste contents of tailwind-theme-snippet.css here */
   @theme inline {
     --font-sans: var(--font-inter);
     --font-serif: var(--font-cormorant);
     --font-display: var(--font-cormorant);
   }

   /* Paste contents of tokens.css here (everything in that file) */
   ```

4. **Fonts** — in `app/layout.tsx`, load Inter and Cormorant Garamond with `next/font/google` and attach **both** `variable` props to `<html>` so `--font-inter` and `--font-cormorant` exist:

   - Inter: `variable: "--font-inter"`, weights `300`–`600`.
   - Cormorant Garamond: `variable: "--font-cormorant"`, weights `300`–`700`, styles normal + italic.

5. **Body shell** — typical root classes:

   ```tsx
   <body className="min-h-screen bg-[var(--color-bg-page)] text-[var(--color-fg)] antialiased">
   ```

6. **Light theme** — `next-themes` with `attribute="class"`, `defaultTheme="system"`, `enableSystem`. Toggle by adding/removing `light` on `<html>` (class strategy). All semantic colors live in `tokens.css` under `html.light { ... }`.

7. **`theme-color` meta** (optional) — default dark `#0d0d0d`, light `#faf8f5`; sync on theme change if you use a small client component (see reference `ThemeColorMeta.tsx`).

8. **Viewport** (optional) — `colorScheme: "dark light"` in Next `viewport` export; initial `themeColor` can match dark page bg.

## Custom cursor (optional)

`tokens.css` includes the **CSS** that hides the system pointer when you opt in (`data-cursor-custom` on `<html>`, cursor UI on an element with `[data-cursor-root]`). The **React component** that positions the arrow and handles hover/selection lives in the reference repo (`app/components/CustomCursor.tsx`, `cursor-icon.tsx`). You can reuse that code, or only use the color tokens (`--cursor-fill`, etc.) with your own implementation. Many projects skip a custom cursor entirely.

## Keeping tokens in sync

When the canonical site updates `app/globals.css`, update `tokens.css` here the same way (or treat `tokens.css` as the single export and merge changes back into the site — your choice).
