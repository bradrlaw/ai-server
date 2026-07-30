#!/usr/bin/env python3
"""Objective checker for the logic-puzzle reasoning eval.

IMPORTANT — this puzzle is a TRAP: it is over-constrained and has **no solution**
(verified by brute force; see README.md). The correct behavior is therefore to
work through the deductions and then *explicitly declare the contradiction*, not
to hallucinate a filled-in table. The automated checks reward that behavior and
the key intermediate deductions; the manual rubric judges reasoning quality.

The contradiction: Ada=CV, Clara=NLP and clue 3 force Ben=Reinforcement Learning.
Clue 3 also bars Ben from ACL/EMNLP and Ada holds NeurIPS, so Ben must be ICML or
ICLR. ICLR is Robotics (clue 5) ≠ RL, so Ben=ICML — but ICML is March (clue 12)
while Ben is August (clue 8). No assignment survives.

Scored on the model's *visible answer* (the extracted output file, not hidden
reasoning_content): a model that only notices the problem in private thinking but
prints a bogus table still fails, which is the point.

Usage:
  check.py                       # score every outputs/<label>/output.*
  check.py outputs/coding/output.md [...]
Writes check.json next to each output and prints a summary table.
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def rgx(p):
    return re.compile(p, re.I | re.S)


def mentions_contradiction(low):
    return re.search(
        r"contradict|no\s+(?:valid\s+)?solution|unsatisf|unsolvab|impossible"
        r"|inconsistent|cannot\s+be\s+solved|can'?t\s+be\s+solved|over[-\s]?constrain"
        r"|no\s+consistent|no\s+such\s+arrangement", low) is not None


def identifies_conflict(low):
    """Pinpoints the actual clash (Ben forced to ICML which is March, but Ben is
    August; or the ICLR=Robotics vs Ben=RL branch)."""
    if "ben" not in low:
        return False
    sig_icml = "icml" in low and ("march" in low or "august" in low or "aug" in low)
    sig_iclr = "iclr" in low and (
        "robot" in low or "reinforc" in low or re.search(r"\brl\b", low) is not None)
    return sig_icml or sig_iclr


def deduces_ben_rl(low):
    return "ben" in low and ("reinforc" in low or re.search(r"\brl\b", low) is not None)


def deduces_clara_june(low):
    return "clara" in low and "june" in low


def step_by_step(low):
    return (re.search(r"step\s*\d", low) is not None
            or low.count("clue") >= 5
            or re.search(r"\bfirst,|\bnext,|\btherefore\b", low) is not None)


def references_clues(low):
    return low.count("clue") >= 3 or re.search(r"clue\s*\d", low) is not None


def verification(low):
    return ("verif" in low
            or re.search(r"check(?:ing)?\s+(?:every|each|all)\s+clue", low) is not None)


# (key, weight, predicate(low)->bool, description)
CHECKS = [
    ("contradiction_flagged", 4, mentions_contradiction,
     "Explicitly declares the puzzle has no solution / is contradictory"),
    ("conflict_pinpointed", 3, identifies_conflict,
     "Identifies the specific clash (Ben→ICML=March vs Aug, or ICLR=Robotics vs Ben=RL)"),
    ("ben_is_rl", 1, deduces_ben_rl, "Deduces Ben's subfield is Reinforcement Learning"),
    ("clara_june", 1, deduces_clara_june, "Deduces Clara (NLP) is in June"),
    ("stepwise", 1, step_by_step, "Shows stepwise reasoning (not just an answer)"),
    ("cites_clues", 1, references_clues, "References the clues explicitly while reasoning"),
    ("verifies", 1, verification, "Attempts to verify against the clues"),
]
MAX = sum(w for _, w, _, _ in CHECKS)


def _find_output(path):
    """Given a dir or a file, return the answer text file path."""
    if os.path.isfile(path):
        return path
    meta = os.path.join(path, "meta.json")
    if os.path.isfile(meta):
        try:
            of = json.load(open(meta)).get("output_file")
            if of and os.path.isfile(os.path.join(path, of)):
                return os.path.join(path, of)
        except Exception:
            pass
    for name in sorted(os.listdir(path)):
        if name.startswith("output."):
            return os.path.join(path, name)
    return None


def check_file(path):
    f = _find_output(path)
    if not f:
        return None
    raw = open(f, encoding="utf-8", errors="replace").read()
    low = raw.lower()
    results, score = {}, 0
    for key, weight, pred, desc in CHECKS:
        ok = bool(pred(low))
        results[key] = {"pass": ok, "weight": weight, "desc": desc}
        if ok:
            score += weight
    report = {"file": os.path.relpath(f, HERE), "score": score, "max": MAX,
              "pct": round(100 * score / MAX, 1), "bytes": len(raw.encode()),
              "checks": results}
    json.dump(report, open(os.path.join(os.path.dirname(f), "check.json"), "w"),
              indent=2)
    return report


def main():
    args = sys.argv[1:]
    if args:
        targets = [a if os.path.isabs(a) else os.path.join(HERE, a) for a in args]
    else:
        base = os.path.join(HERE, "outputs")
        targets = sorted(os.path.join(base, d) for d in os.listdir(base)
                         if os.path.isdir(os.path.join(base, d))) \
            if os.path.isdir(base) else []
    reports = [r for r in (check_file(t) for t in targets) if r]
    if not reports:
        sys.exit("no outputs/<label>/output.* found")
    w = max(len(os.path.basename(os.path.dirname(r["file"]))) for r in reports)
    print(f"{'model':<{w}}  score  pct    failed")
    for r in reports:
        label = os.path.basename(os.path.dirname(r["file"]))
        failed = [k for k, v in r["checks"].items() if not v["pass"]]
        print(f"{label:<{w}}  {r['score']:>2}/{r['max']}  {r['pct']:>5}%  "
              f"{', '.join(failed) if failed else '—'}")


if __name__ == "__main__":
    main()
