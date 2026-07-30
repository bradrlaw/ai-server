#!/usr/bin/env python3
"""Objective constraint checker for the "Local Dungeon" browser-game eval.

Scores the automatable ("hard constraint") parts of the prompt for a generated
index.html: self-containment (zero external deps), the required page layout,
the ES6 class/state architecture, the mandated dungeon content, the interaction
features, and the dark-mode design tokens. Subjective design/gameplay quality is
NOT scored here — see the rubric in README.md for the manual portion.

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

# ---- self-containment (shared with the landing-page eval) -------------------
EXTERNAL_URL = re.compile(r"""(?:src|href)\s*=\s*['"]\s*(?:https?:)?//""", re.I)
IMPORT_URL = re.compile(r"""@import\s+(?:url\()?['"]?\s*(?:https?:)?//""", re.I)
CSS_URL_EXT = re.compile(r"""url\(\s*['"]?\s*(?:https?:)?//""", re.I)


def no_external(low, raw):
    return not (EXTERNAL_URL.search(raw) or IMPORT_URL.search(raw)
                or CSS_URL_EXT.search(raw))


# All six mandated rooms and five mandated items, matched case-insensitively.
ROOMS = ("entrance hall", "library", "armory", "treasure room",
         "secret chamber", "garden")
ITEMS = ("rusted key", "lever", "torch", "golden coin", "ancient scroll")


def all_rooms(low, raw):
    return all(r in low for r in ROOMS)


def all_items(low, raw):
    return all(i in low for i in ITEMS)


# Typewriter effect: a timer that appends characters one at a time. Detected by a
# timer call (setTimeout/setInterval/requestAnimationFrame) co-occurring with a
# per-character text op (charAt / [i] indexing / substring / slice on a string).
def typewriter(low, raw):
    has_timer = bool(re.search(r"settimeout|setinterval|requestanimationframe", low))
    per_char = ("charat" in low or "substring" in low or "substr(" in low
                or "typewriter" in low or bool(re.search(r"\.slice\(", low)))
    return has_timer and per_char


# Partial / abbreviated command matching (prompt: "n"=north, "inv"=inventory).
# Accept any of the legitimate mechanisms: prefix match (startsWith), a named
# partialMatch helper, a single-letter direction-abbreviation map ('n':'north'|
# 'go'|'move'), or case-insensitive substring matching (toLowerCase().includes).
def partial_match(low, raw):
    return ("startswith" in low
            or "partialmatch" in low or "partial match" in low
            or bool(re.search(r"""['"]n['"]\s*:\s*['"](go|north|move)""", low))
            or bool(re.search(r"\.tolowercase\(\)\s*\.\s*includes\(", low)))


# Reset must NOT refresh the page: a real reset button must exist and the code
# must not fall back to a full navigation reload.
def reset_no_reload(low, raw):
    has_reset = "reset game" in low
    reloads = bool(re.search(r"location\s*\.\s*reload|location\s*\.\s*href\s*=|"
                             r"location\s*\.\s*assign|window\s*\.\s*location\s*=", low))
    return has_reset and not reloads


CHECKS = [
    # --- one self-contained file -------------------------------------------
    ("doctype", 1, lambda l, r: "<!doctype html" in l, "Has <!DOCTYPE html>"),
    ("inline_style", 1, lambda l, r: "<style" in l, "Inline <style> block"),
    ("inline_script", 1, lambda l, r: "<script" in l and "</script>" in l,
     "Inline <script> block"),
    ("self_contained", 3, no_external,
     "Zero external deps (no http(s):// or //cdn in src/href/@import/url())"),
    ("no_ext_stylesheet", 1,
     lambda l, r: not re.search(r"<link[^>]+rel=['\"]?stylesheet", l),
     "No external <link rel=stylesheet> (system fonts only)"),
    ("inline_svg", 1, lambda l, r: "<svg" in l, "Inline SVG icon(s)"),
    # --- page layout --------------------------------------------------------
    ("title", 1, lambda l, r: "the local dungeon" in l, "Game title present"),
    ("sticky_header", 1, lambda l, r: bool(re.search(
        r"position\s*:\s*(sticky|fixed)", l)), "Sticky header (position:sticky|fixed)"),
    ("reset_button", 1, lambda l, r: "reset game" in l, "'Reset Game' button label"),
    ("how_to_play", 1, lambda l, r: "how to play" in l, "'How to Play' section"),
    ("accordion", 1, lambda l, r: "<details" in l or "accordion" in l
     or "collaps" in l, "Collapsible/accordion mechanism"),
    ("media_queries", 2, lambda l, r: "@media" in l, "Responsive @media queries"),
    ("two_column", 1, lambda l, r: "grid" in l or "flex" in l,
     "Two-column dashboard (grid/flex)"),
    ("input_line", 1, lambda l, r: "<input" in l, "Console input line"),
    ("stats", 1, lambda l, r: "moves made" in l and "items found" in l,
     "Stats: Moves Made + Items Found"),
    ("inventory_empty", 1, lambda l, r: "inventory is empty" in l,
     "'Inventory is empty' empty-state"),
    # --- ES6 architecture & state ------------------------------------------
    ("class_gameengine", 2, lambda l, r: bool(re.search(r"class\s+GameEngine", r)),
     "ES6 class GameEngine"),
    ("class_room", 1, lambda l, r: bool(re.search(r"class\s+Room", r)),
     "ES6 class Room"),
    ("class_item", 1, lambda l, r: bool(re.search(r"class\s+Item", r)),
     "ES6 class Item"),
    ("case_insensitive", 1, lambda l, r: "tolowercase" in l,
     "Case-insensitive parsing (toLowerCase)"),
    ("synonyms", 1, lambda l, r: "examine" in l and "grab" in l,
     "Command synonyms (examine / grab)"),
    ("partial_match", 1, partial_match,
     "Partial/abbreviated command matching (startsWith / abbrev map / substring)"),
    ("flags_state", 1, lambda l, r: "lever_pulled" in l
     or ("flag" in l and "lever" in l), "Boolean state flags (lever_pulled)"),
    # --- mandated content ---------------------------------------------------
    ("rooms", 2, all_rooms, "All six rooms present"),
    ("items", 2, all_items, "All five items present"),
    # --- interactions & polish ---------------------------------------------
    ("typewriter", 2, typewriter, "Typewriter char-by-char output"),
    ("command_history", 1, lambda l, r: "arrowup" in l and "arrowdown" in l,
     "Up/Down command history"),
    ("auto_scroll", 1, lambda l, r: "scrollheight" in l,
     "Console auto-scroll (scrollHeight)"),
    ("input_focus", 1, lambda l, r: ".focus()" in l,
     "Click-to-focus input (.focus())"),
    ("reset_no_reload", 1, reset_no_reload,
     "Reset clears state without page reload"),
    # --- design tokens ------------------------------------------------------
    ("dark_bg", 1, lambda l, r: "#1a1a1a" in l, "Dark background #1a1a1a"),
    ("console_color", 1, lambda l, r: "#00ff00" in l or "#ffb000" in l,
     "Console green/amber (#00ff00 or #ffb000)"),
    ("monospace", 1, lambda l, r: "monospace" in l, "Monospace console font"),
    ("sans_ui", 1, lambda l, r: "system-ui" in l or "sans-serif" in l,
     "Sans-serif UI font stack"),
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
