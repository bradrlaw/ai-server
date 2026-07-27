#!/usr/bin/env python3
"""Objective constraint checker for the LocalMind landing-page-PRO eval.

A harder variant of the landing-page test: same self-containment rules but with
more required sections and interactions (interactive terminal, FAQ accordion,
waitlist form, canvas/animated hero, active-nav-on-scroll, blur-on-scroll, JS
copyright year, etc.). This scores only the automatable ("hard constraint")
parts; subjective design quality is the manual rubric in README.md.

Usage:
  check.py                       # check every outputs/<label>/index.html
  check.py outputs/coding/index.html [more.html ...]
Writes check.json next to each index.html and prints a summary table.
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

EXTERNAL_URL = re.compile(r"""(?:src|href)\s*=\s*['"]\s*(?:https?:)?//""", re.I)
IMPORT_URL = re.compile(r"""@import\s+(?:url\()?['"]?\s*(?:https?:)?//""", re.I)
CSS_URL_EXT = re.compile(r"""url\(\s*['"]?\s*(?:https?:)?//""", re.I)
_NOTAGS = re.compile(r"<[^>]+>")


def no_external(low, raw):
    return not (EXTERNAL_URL.search(raw) or IMPORT_URL.search(raw)
                or CSS_URL_EXT.search(raw))


def has_prices(raw, amounts=("0", "12", "29")):
    # "$" and the amount are frequently split across elements for styling.
    text = _NOTAGS.sub(" ", raw)
    return all(re.search(rf"\$\s*{a}\b", text) for a in amounts)


def scroll_reveal(low, raw):
    return ("intersectionobserver" in low
            or ("getboundingclientrect" in low
                and re.search(r"addeventlistener\(\s*['\"]scroll", low) is not None))


def hero_visual(low, raw):
    # Canvas particle/mesh OR a CSS-animated orb (the prompt's stated fallback).
    return "<canvas" in low or ("@keyframes" in low
                                and re.search(r"orb|pulse|mesh|gradient", low) is not None)


def terminal(low, raw):
    # Fake chat terminal: a monospace window with a top bar of colored dots and a
    # typing indicator. Models realize this as either a literal "terminal"/monospace
    # block or as window chrome (three colored dots) + chat bubbles, so accept both.
    mono = any(fnt in low for fnt in (
        "monospace", "ui-monospace", "menlo", "consolas", "courier", "monaco",
        "sf mono", "roboto mono", "jetbrains", "'mono"))
    # three coloured "traffic-light" dots in the window title bar
    window_chrome = (sum(c in low for c in ("red", "green", "yellow")) >= 2
                     and "dot" in low) or low.count("dot") >= 3
    demo = ("typing" in low or "bubble" in low or "message" in low
            or "demo-msg" in low or "chat" in low)
    return ("terminal" in low or mono or window_chrome) and demo


def faq_accordion(low, raw):
    # Smooth max-height expand + chevron rotation.
    return "max-height" in low and re.search(r"rotate\(\s*-?180", low) is not None


def waitlist(low, raw):
    # Email input whose submit flips the button text to a success state.
    has_email = re.search(r"type\s*=\s*['\"]email", low) is not None
    success = ("🎉" in raw or "you're in" in low or "you&#39;re in" in low
               or "youre in" in low or "you\u2019re in" in low)
    return has_email and success


def count_svg(low):
    return low.count("<svg")


CHECKS = [
    ("doctype", 1, lambda l, r: "<!doctype html" in l, "Has <!DOCTYPE html>"),
    ("inline_style", 1, lambda l, r: "<style" in l, "Inline <style> block"),
    ("inline_script", 1, lambda l, r: "<script" in l and "</script>" in l,
     "Inline <script> block"),
    ("self_contained", 3, no_external,
     "Zero external deps (no http(s):// or //cdn in src/href/@import/url())"),
    ("no_ext_stylesheet", 1,
     lambda l, r: not re.search(r"<link[^>]+rel=['\"]?stylesheet", l),
     "No external <link rel=stylesheet>"),
    ("nav_sticky", 1, lambda l, r: bool(re.search(r"position\s*:\s*(sticky|fixed)", l)),
     "Sticky/fixed navigation"),
    ("hamburger", 1, lambda l, r: bool(re.search(r"hamburger|menu[-_]?toggle|nav[-_]?toggle", l)),
     "Hamburger / mobile menu toggle"),
    ("active_nav_observer", 2, lambda l, r: "intersectionobserver" in l,
     "Active-nav-on-scroll via IntersectionObserver"),
    ("nav_blur", 1, lambda l, r: "backdrop-filter" in l and "blur" in l,
     "Nav backdrop-filter blur on scroll"),
    ("hero_headline", 1, lambda l, r: "run ai locally" in l
     or ("own your data" in l and "no limits" in l), "Hero value-prop headline"),
    ("cta_early_access", 1, lambda l, r: "early access" in l,
     "Primary CTA 'Get Early Access'"),
    ("hero_visual", 2, hero_visual, "Canvas hero or CSS-animated orb"),
    ("inline_svg_icons", 1, lambda l, r: count_svg(l) >= 3,
     "Inline SVG icons (>=3)"),
    ("css_grid", 1, lambda l, r: "display:grid" in l.replace(" ", "")
     or "grid-template" in l, "CSS grid layout"),
    ("how_it_works", 1, lambda l, r: sum(re.search(rf"\b{w}\b", l) is not None
     for w in ("install", "choose", "chat")) >= 3, "How-It-Works install/choose/chat flow"),
    ("pricing_prices", 2, lambda l, r: has_prices(r),
     "Pricing tiers $0 / $12 / $29 (tag-tolerant)"),
    ("most_popular", 1, lambda l, r: "most popular" in l, "'Most Popular' tier badge"),
    ("terminal_demo", 2, terminal, "Interactive fake chat terminal"),
    ("typing_indicator", 1, lambda l, r: "typing" in l
     and ("@keyframes" in l or "animation" in l), "Animated typing indicator"),
    ("testimonials", 1, lambda l, r: "testimonial" in l
     or re.search(r"border-radius\s*:\s*50%", l) is not None, "Testimonials w/ initials"),
    ("faq", 2, lambda l, r: "faq" in l or "accordion" in l, "FAQ / accordion section"),
    ("faq_smooth", 1, faq_accordion, "Accordion max-height expand + chevron rotate"),
    ("waitlist_form", 2, waitlist,
     "Waitlist email input + success state (\u201cYou're in! \U0001f389\u201d)"),
    ("footer", 1, lambda l, r: "<footer" in l or "copyright" in l or "©" in r,
     "Footer present"),
    ("year_js", 1, lambda l, r: "getfullyear" in l, "JS copyright year (getFullYear)"),
    ("media_queries", 2, lambda l, r: l.count("@media") >= 2, "Responsive @media (>=2)"),
    ("transitions", 1, lambda l, r: "transition" in l, "CSS transitions"),
    ("scroll_reveal", 2, scroll_reveal, "Scroll entrance animation"),
    ("smooth_scroll", 1, lambda l, r: "scroll-behavior" in l or "scrollintoview" in l,
     "Smooth scroll"),
    ("system_font", 1, lambda l, r: "-apple-system" in l
     or "blinkmacsystemfont" in l or "system-ui" in l, "System font stack"),
    ("hover_states", 1, lambda l, r: ":hover" in l, "Hover states (:hover)"),
]
MAX = sum(w for _, w, _, _ in CHECKS)


def check_file(path):
    raw = open(path, encoding="utf-8", errors="replace").read()
    low = raw.lower()
    results, score = {}, 0
    for key, weight, pred, desc in CHECKS:
        ok = bool(pred(low, raw))
        results[key] = {"pass": ok, "weight": weight, "desc": desc}
        if ok:
            score += weight
    report = {"file": os.path.relpath(path, HERE), "score": score, "max": MAX,
              "pct": round(100 * score / MAX, 1), "bytes": len(raw.encode()),
              "checks": results}
    json.dump(report, open(os.path.join(os.path.dirname(path), "check.json"), "w"),
              indent=2)
    return report


def main():
    args = sys.argv[1:]
    if args:
        files = [a if os.path.isabs(a) else os.path.join(HERE, a) for a in args]
    else:
        base = os.path.join(HERE, "outputs")
        files = sorted(os.path.join(base, d, "index.html")
                       for d in os.listdir(base)
                       if os.path.isfile(os.path.join(base, d, "index.html"))) \
            if os.path.isdir(base) else []
    if not files:
        sys.exit("no outputs/<label>/index.html found")
    reports = [check_file(f) for f in files]
    w = max(len(os.path.basename(os.path.dirname(r["file"]))) for r in reports)
    print(f"{'model':<{w}}  score  pct    failed")
    for r in reports:
        label = os.path.basename(os.path.dirname(r["file"]))
        failed = [k for k, v in r["checks"].items() if not v["pass"]]
        print(f"{label:<{w}}  {r['score']:>2}/{r['max']}  {r['pct']:>5}%  "
              f"{', '.join(failed) if failed else '—'}")


if __name__ == "__main__":
    main()
