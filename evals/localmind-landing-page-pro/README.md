# localmind-landing-page-pro

A harder version of [`localmind-landing-page`](../localmind-landing-page/). Same
domain — a self-contained dark-mode landing page for a fictional local-LLM app
called **LocalMind** — but with a substantially larger surface of required
sections and working interactions, and stricter design direction. It stresses a
model's ability to hold a long, detailed spec together in one file without
external dependencies.

## The prompt

Verbatim in [`prompt.txt`](prompt.txt). Summary of what's demanded beyond the
base test:

- **Nav:** active link highlight on scroll via `IntersectionObserver`,
  `backdrop-filter: blur` background on scroll past 50px, full-screen mobile
  overlay menu, hamburger→X morph.
- **Hero:** `<canvas>` particle/mesh reacting to the mouse (CSS-animated orb is an
  allowed fallback), two CTAs (filled + outlined).
- **Features:** 2×2 grid, inline-SVG icons, hover glow + lift.
- **How It Works:** 3 steps with connecting lines (vertical on mobile).
- **Pricing:** Free $0 / Pro $12 (Most Popular, glowing) / Studio $29, checkmark
  feature lists.
- **Interactive demo:** a fake chat *terminal* (title bar w/ three dots,
  monospace, alternating bubbles) with an animated three-dot typing indicator.
- **Testimonials:** three cards, gradient initial-circles, no images.
- **FAQ:** five accordion items, one-open-at-a-time, smooth `max-height`, chevron
  rotates 180°.
- **CTA banner:** email waitlist input whose submit flips the button to
  **"You're in! 🎉"** and clears the field.
- **Footer:** JS-computed copyright year (`new Date().getFullYear()`).
- **Polish:** smooth scroll, staggered scroll-reveal, button hover/active scale,
  animated link underlines, explicit breakpoints at 320/768/1024/1440.

## How to run

```bash
scripts/eval-run.py --test localmind-landing-page-pro --model <model>
evals/localmind-landing-page-pro/check.py            # objective score -> check.json
scripts/eval-summary.py --test localmind-landing-page-pro
```

## Scoring

**Objective (automated, `check.py` → `check.json`).** Weighted checks for
self-containment, every required section, and the load-bearing interactions
(IntersectionObserver active-nav + scroll-reveal, blur-on-scroll, canvas/animated
hero, terminal demo, animated typing indicator, FAQ max-height+chevron, waitlist
success state, `getFullYear`, ≥2 media queries, etc.). Tag-tolerant where markup
splitting would cause false negatives (e.g. prices split across spans).

**Design quality (manual, `scores.json`).** Same five axes as the base landing
test (this test uses the default rubric):

- **Visual polish** — hierarchy, spacing, color/glow discipline, "Linear/Vercel" feel.
- **Responsiveness** — actually correct at 320/768/1024/1440, not just present media queries.
- **Interaction** — do the terminal, FAQ, overlay menu, waitlist, hovers feel real and smooth?
- **Hero visual** — quality of the canvas/orb; subtle, not gaudy.
- **Code quality** — structure, naming, no dead/placeholder code.

Enter 0–5 per axis in each `outputs/<label>/scores.json`, then rerun
`scripts/eval-summary.py --test localmind-landing-page-pro`. `RESULTS.md` and
`summary.html` are generated — don't hand-edit them.
