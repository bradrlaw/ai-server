#!/usr/bin/env python3
"""Objective constraint checker for the LocalMind landing-page eval.

Scores the automatable ("hard constraint") parts of the prompt for a generated
index.html: self-containment (zero external deps), required sections, and the
required interactions. Subjective design quality is NOT scored here — see the
rubric in README.md for the manual portion.

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

# Each check: (key, weight, predicate(html_lower, html_raw) -> bool, description)
EXTERNAL_URL = re.compile(r"""(?:src|href)\s*=\s*['"]\s*(?:https?:)?//""", re.I)
IMPORT_URL = re.compile(r"""@import\s+(?:url\()?['"]?\s*(?:https?:)?//""", re.I)
CSS_URL_EXT = re.compile(r"""url\(\s*['"]?\s*(?:https?:)?//""", re.I)


def no_external(low, raw):
    return not (EXTERNAL_URL.search(raw) or IMPORT_URL.search(raw)
                or CSS_URL_EXT.search(raw))


# Tag/space-tolerant price match: the "$" and the amount are frequently split
# across separate elements (e.g. <span>$</span><span>12</span>) purely for
# styling, so a naive "$12" substring misses a correctly-rendered price.
_NOTAGS = re.compile(r"<[^>]+>")


def has_prices(raw, amounts=("0", "12", "29")):
    text = _NOTAGS.sub(" ", raw)
    return all(re.search(rf"\$\s*{a}\b", text) for a in amounts)


# Scroll-triggered entrance animation. IntersectionObserver is the prompt's
# suggested ("is fine") approach, but a scroll listener that toggles a reveal
# class via getBoundingClientRect satisfies the same requirement.
def scroll_reveal(low, raw):
    return ("intersectionobserver" in low
            or ("getboundingclientrect" in low
                and re.search(r"addeventlistener\(\s*['\"]scroll", low) is not None))


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
    ("nav_sticky", 1, lambda l, r: bool(re.search(
        r"position\s*:\s*(sticky|fixed)", l)),
     "Sticky/fixed navigation (position:sticky|fixed)"),
    ("hamburger", 2, lambda l, r: bool(re.search(r"hamburger|menu[-_]?toggle|nav[-_]?toggle", l)),
     "Hamburger / mobile menu toggle"),
    ("hero_headline", 1, lambda l, r: "run ai locally" in l
     or ("own your data" in l and "no limits" in l), "Hero value-prop headline"),
    ("cta_early_access", 1, lambda l, r: "early access" in l,
     "Primary CTA 'Get Early Access'"),
    ("features", 1, lambda l, r: sum(k in l for k in
     ["any model", "zero latency", "full privacy", "gpu"]) >= 2,
     "Features grid (>=2 named features)"),
    ("how_it_works", 1, lambda l, r: sum(re.search(rf"\b{w}\b", l) is not None
     for w in ("install", "choose", "chat")) >= 3,
     "How-It-Works steps (install / choose / chat flow)"),
    ("pricing_prices", 2, lambda l, r: has_prices(r),
     "Pricing tiers $0 / $12 / $29 (tag-tolerant)"),
    ("most_popular", 1, lambda l, r: "most popular" in l, "'Most Popular' tier"),
    ("testimonials", 1, lambda l, r: bool(re.search(
        r"personal|testimonial|we believe", l)), "About / social-proof copy"),
    ("footer", 1, lambda l, r: "<footer" in l or "copyright" in l or "©" in r,
     "Footer present"),
    ("media_queries", 2, lambda l, r: "@media" in l, "Responsive @media queries"),
    ("transitions", 1, lambda l, r: "transition" in l, "CSS transitions"),
    ("scroll_reveal", 2, scroll_reveal,
     "Scroll entrance animation (IntersectionObserver or scroll-driven reveal)"),
    ("smooth_scroll", 1, lambda l, r: "scroll-behavior" in l
     or "scrollintoview" in l, "Smooth scroll"),
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
