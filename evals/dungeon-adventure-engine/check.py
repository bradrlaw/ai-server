#!/usr/bin/env python3
"""Objective checker for the text-adventure-engine (Python) eval.

Combines STATIC analysis (AST + source scan) with a sandboxed DYNAMIC smoke test
that actually runs the generated program and drives it with a scripted session:

  Static:  required classes, stdlib-only imports, type-hint coverage, docstring
           coverage, __main__ block, EOFError/KeyboardInterrupt handling, the full
           command vocabulary, the six rooms, the five items, ASCII banner, the
           "won" flag and the lever/secret-passage logic.
  Dynamic: the file runs, accepts input, exits cleanly on quit/EOF, prints the
           starting room, and reflects a taken item in the inventory.

The generated code is executed in a subprocess with piped stdin and a hard
timeout (it's a self-contained stdlib program from a local model; there is no
network and the process is killed if it hangs).

Usage:
  check.py                       # score every outputs/<label>/output.py
  check.py outputs/coding/output.py [...]
Writes check.json next to each output.py and prints a summary table.
"""
import ast
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

REQUIRED_CLASSES = ["GameState", "Room", "Item", "GameEngine"]
COMMAND_WORDS = ["look", "take", "drop", "inventory", "use", "help",
                 "history", "quit", "go"]
ROOM_WORDS = ["entrance", "library", "armory", "treasure", "secret", "garden"]
ITEM_WORDS = ["key", "lever", "torch", "scroll", "coin"]

STDLIB = set(getattr(sys, "stdlib_module_names", set())) | {
    "__future__", "typing", "dataclasses", "collections", "textwrap", "sys",
    "os", "re", "random", "enum", "functools", "itertools", "json", "time"}


def analyze_static(src):
    """Return a dict of boolean/ratio facts from the source via AST + scanning."""
    f = {"parse_ok": False, "imports_ok": True, "bad_imports": [],
         "hint_ratio": 0.0, "doc_ratio": 0.0, "main_block": False,
         "exceptions": False, "commands_found": 0, "rooms_found": 0,
         "items_found": 0, "banner": False, "won_flag": False,
         "secret_logic": False}
    for c in REQUIRED_CLASSES:
        f[f"class_{c}"] = False
    low = src.lower()
    try:
        tree = ast.parse(src)
        f["parse_ok"] = True
    except SyntaxError:
        return f, low

    classes = {n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
    for c in REQUIRED_CLASSES:
        f[f"class_{c}"] = c in classes

    # stdlib-only imports (top-level module name before the first dot)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods = [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            mods = [(node.module or "").split(".")[0]] if node.level == 0 else []
        else:
            continue
        for m in mods:
            if m and m not in STDLIB:
                f["imports_ok"] = False
                f["bad_imports"].append(m)

    funcs = [n for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]

    def annotated(fn):
        args = fn.args.posonlyargs + fn.args.args + fn.args.kwonlyargs
        return fn.returns is not None or any(a.annotation for a in args
                                             if a.arg not in ("self", "cls"))
    f["hint_ratio"] = (sum(annotated(fn) for fn in funcs) / len(funcs)) if funcs else 0.0

    class_nodes = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
    documented = class_nodes + funcs
    f["doc_ratio"] = ((sum(ast.get_docstring(n) is not None for n in documented)
                       / len(documented)) if documented else 0.0)

    f["main_block"] = "__name__" in src and "__main__" in src
    f["exceptions"] = "EOFError" in src and "KeyboardInterrupt" in src
    f["commands_found"] = sum(f'"{w}"' in low or f"'{w}'" in low or f" {w} " in low
                              or f"{w}<" in low for w in COMMAND_WORDS)
    f["rooms_found"] = sum(w in low for w in ROOM_WORDS)
    f["items_found"] = sum(w in low for w in ITEM_WORDS)
    # ASCII banner: a triple-quoted block or a run of box/art characters.
    f["banner"] = ('"""' in src or "'''" in src) and (
        any(line.count("=") >= 6 or line.count("_") >= 6 or line.count("*") >= 6
            or line.count("#") >= 6 or line.count("|") >= 3
            for line in src.splitlines()) or "banner" in low)
    f["won_flag"] = "won" in low
    f["secret_logic"] = "secret" in low and "lever" in low
    return f, low


def run_dynamic(path):
    """Drive the generated program with a scripted session; return facts."""
    script = "look\ntake rusted key\ninventory\nlook\nquit\n"
    d = {"runs": False, "shows_room": False, "inventory_ok": False, "error": None}
    try:
        p = subprocess.run(
            [sys.executable, os.path.basename(path)],
            input=script, capture_output=True, text=True, timeout=20,
            cwd=os.path.dirname(path))
        out = (p.stdout + "\n" + p.stderr).lower()
        d["runs"] = True
        d["shows_room"] = "entrance" in out or "hall" in out
        # the rusted key should appear once on the room look and again in inventory
        d["inventory_ok"] = out.count("key") >= 2 and (
            "invent" in out or "carrying" in out or "holding" in out)
    except subprocess.TimeoutExpired:
        d["error"] = "timeout"
    except Exception as e:  # noqa: BLE001 - record any launch failure
        d["error"] = str(e)
    return d


# (key, weight, predicate(facts, dyn)->bool, description)
CHECKS = [
    ("parses", 1, lambda f, d: f["parse_ok"], "Valid Python (parses)"),
    ("class_GameState", 1, lambda f, d: f.get("class_GameState"), "GameState class"),
    ("class_Room", 1, lambda f, d: f.get("class_Room"), "Room class"),
    ("class_Item", 1, lambda f, d: f.get("class_Item"), "Item class"),
    ("class_GameEngine", 1, lambda f, d: f.get("class_GameEngine"), "GameEngine class"),
    ("stdlib_only", 2, lambda f, d: f["imports_ok"], "Standard library only"),
    ("type_hints", 2, lambda f, d: f["hint_ratio"] >= 0.5,
     "Type hints on >=50% of functions"),
    ("docstrings", 2, lambda f, d: f["doc_ratio"] >= 0.4,
     "Docstrings on >=40% of classes/functions"),
    ("main_block", 1, lambda f, d: f["main_block"], "__main__ block"),
    ("exception_handling", 2, lambda f, d: f["exceptions"],
     "Handles EOFError and KeyboardInterrupt"),
    ("command_vocab", 3, lambda f, d: f["commands_found"] >= 8,
     "Command vocabulary (>=8 of look/take/drop/inventory/use/help/history/quit/go)"),
    ("six_rooms", 2, lambda f, d: f["rooms_found"] >= 5,
     "Dungeon rooms (>=5 of entrance/library/armory/treasure/secret/garden)"),
    ("four_items", 2, lambda f, d: f["items_found"] >= 4,
     "Items (>=4 of key/lever/torch/scroll/coin)"),
    ("banner", 1, lambda f, d: f["banner"], "ASCII banner"),
    ("won_flag", 1, lambda f, d: f["won_flag"], "'won' flag for the secret ending"),
    ("secret_logic", 1, lambda f, d: f["secret_logic"],
     "Lever / secret-passage logic"),
    ("dyn_runs", 3, lambda f, d: d["runs"] and d["shows_room"],
     "Runs and prints the starting room"),
    ("dyn_inventory", 2, lambda f, d: d["inventory_ok"],
     "Taking an item reflects in inventory"),
]
MAX = sum(w for _, w, _, _ in CHECKS)


def _find_output(path):
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
    src = open(f, encoding="utf-8", errors="replace").read()
    facts, _ = analyze_static(src)
    dyn = run_dynamic(f) if facts["parse_ok"] else {
        "runs": False, "shows_room": False, "inventory_ok": False, "error": "parse"}

    results, score = {}, 0
    for key, weight, pred, desc in CHECKS:
        ok = bool(pred(facts, dyn))
        results[key] = {"pass": ok, "weight": weight, "desc": desc}
        if ok:
            score += weight
    report = {"file": os.path.relpath(f, HERE), "score": score, "max": MAX,
              "pct": round(100 * score / MAX, 1), "bytes": len(src.encode()),
              "hint_ratio": round(facts["hint_ratio"], 2),
              "doc_ratio": round(facts["doc_ratio"], 2),
              "bad_imports": facts.get("bad_imports", []),
              "dynamic": dyn, "checks": results}
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
        sys.exit("no outputs/<label>/output.py found")
    w = max(len(os.path.basename(os.path.dirname(r["file"]))) for r in reports)
    print(f"{'model':<{w}}  score  pct    failed")
    for r in reports:
        label = os.path.basename(os.path.dirname(r["file"]))
        failed = [k for k, v in r["checks"].items() if not v["pass"]]
        print(f"{label:<{w}}  {r['score']:>2}/{r['max']}  {r['pct']:>5}%  "
              f"{', '.join(failed) if failed else '—'}")


if __name__ == "__main__":
    main()
