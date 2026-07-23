#!/usr/bin/env python3
"""llama-swap config mode switcher for /srv/ai.

Renders the ACTIVE llama-swap config (config/llama-swap.yaml, which the service
reads via -watch-config) from a canonical BASE (config/llama-swap.base.yaml)
plus a small MODE OVERLAY (config/modes/<mode>.yaml). Switching a mode only
touches a few per-model knobs (--parallel, concurrencyLimit, optional
--ctx-size) and the startup preload list — the base's heavily-commented model
blocks and matrix routing stay the single source of truth.

Because llama-swap runs with -watch-config, writing the active file is enough to
reload — no service restart / sudo needed. After writing we optionally warm the
mode's preload models so the new slot counts take effect immediately.

Subcommands:
  list                 list available modes
  current              print the active mode (read from the active file marker)
  show [mode]          show effective per-model config (parallel / ctx / gpus)
  set <mode>           render + activate <mode>, then warm its preload models

Add --json to any subcommand for machine-readable output (used by the MCP tool
and the status service).

Run with any Python that has PyYAML (system python3 has it):
  python3 scripts/llama-swap-mode.py set heavy-coding
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request

import yaml

ROOT = "/srv/ai"
CONFIG_DIR = os.path.join(ROOT, "config")
BASE = os.path.join(CONFIG_DIR, "llama-swap.base.yaml")
ACTIVE = os.path.join(CONFIG_DIR, "llama-swap.yaml")
MODES_DIR = os.path.join(CONFIG_DIR, "modes")
SWAP_URL = os.environ.get("LLAMASWAP_URL", "http://127.0.0.1:9090").rstrip("/")

MARKER_RE = re.compile(r"^#\s*ACTIVE-MODE:\s*([A-Za-z0-9_-]+)")
KEY_RE = re.compile(r'^  "([a-z0-9-]+)":\s*$')


# --------------------------------------------------------------------------- #
# mode overlays
# --------------------------------------------------------------------------- #
def list_modes() -> list[dict]:
    modes = []
    if not os.path.isdir(MODES_DIR):
        return modes
    for fn in sorted(os.listdir(MODES_DIR)):
        if not fn.endswith((".yaml", ".yml")):
            continue
        spec = load_mode(fn.rsplit(".", 1)[0])
        modes.append(spec)
    return modes


def load_mode(mode: str) -> dict:
    for ext in (".yaml", ".yml"):
        path = os.path.join(MODES_DIR, mode + ext)
        if os.path.exists(path):
            with open(path) as f:
                spec = yaml.safe_load(f) or {}
            spec.setdefault("name", mode)
            spec.setdefault("label", mode)
            spec.setdefault("description", "")
            spec.setdefault("overrides", {})
            spec.setdefault("preload", [])
            spec.setdefault("warm", None)
            return spec
    raise SystemExit(f"unknown mode '{mode}' (looked in {MODES_DIR})")


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #
def _apply_overrides(block: list[str], ov: dict) -> list[str]:
    """Edit a single model's YAML block in place per an override dict."""
    if "parallel" in ov:
        for k, bl in enumerate(block):
            if "--parallel" in bl:
                block[k] = re.sub(r"--parallel\s+\d+", f"--parallel {ov['parallel']}", bl)
    if "ctx_size" in ov:
        for k, bl in enumerate(block):
            if "--ctx-size" in bl:
                block[k] = re.sub(r"--ctx-size\s+\d+", f"--ctx-size {ov['ctx_size']}", bl)
    if "model" in ov:
        for k, bl in enumerate(block):
            if "--model" in bl:
                block[k] = re.sub(r"(--model\s+)\S+", rf"\g<1>{ov['model']}", bl)
    if "spec" in ov:
        # spec: none / false  -> strip the --spec-type ... draft flags (e.g. to disable
        # MTP for a parallel worker pool). spec: "draft-mtp:N" -> set draft n-max to N.
        want = ov["spec"]
        for k, bl in enumerate(block):
            if "--spec-type" in bl:
                if not want or want == "none":
                    block[k] = re.sub(r"\s*--spec-type\s+\S+(\s+--spec-draft-n-max\s+\d+)?",
                                      "", bl)
                elif ":" in str(want):
                    stype, nmax = str(want).split(":", 1)
                    block[k] = re.sub(r"--spec-type\s+\S+\s+--spec-draft-n-max\s+\d+",
                                      f"--spec-type {stype} --spec-draft-n-max {nmax}", bl)
    if "concurrencyLimit" in ov:
        # update if present, else insert right after the `name:` line
        found = False
        for k, bl in enumerate(block):
            if re.match(r"^    concurrencyLimit:", bl):
                block[k] = f"    concurrencyLimit: {ov['concurrencyLimit']}\n"
                found = True
                break
        if not found:
            for k, bl in enumerate(block):
                if re.match(r"^    name:", bl):
                    block.insert(k + 1, f"    concurrencyLimit: {ov['concurrencyLimit']}\n")
                    break
    return block


def _rewrite_preload(lines: list[str], preload: list[str]) -> list[str]:
    """Replace the on_startup.preload list items with `preload` (if non-empty)."""
    if not preload:
        return lines
    out, i, n = [], 0, len(lines)
    while i < n:
        line = lines[i]
        out.append(line)
        if re.match(r"^\s*preload:\s*$", line):
            indent = re.match(r"^(\s*)preload:", line).group(1)
            item_indent = indent + "  "
            # skip the existing list items
            j = i + 1
            while j < n and re.match(rf"^{item_indent}-\s", lines[j]):
                j += 1
            for model in preload:
                out.append(f'{item_indent}- "{model}"\n')
            i = j
            continue
        i += 1
    return out


def render(mode: str) -> str:
    spec = load_mode(mode)
    overrides = spec.get("overrides") or {}
    with open(BASE) as f:
        lines = f.read().splitlines(keepends=True)

    out, i, n = [], 0, len(lines)
    while i < n:
        line = lines[i]
        m = KEY_RE.match(line)
        if m and m.group(1) in overrides:
            block = [line]
            j = i + 1
            while j < n and not KEY_RE.match(lines[j]):
                block.append(lines[j])
                j += 1
            out.extend(_apply_overrides(block, overrides[m.group(1)]))
            i = j
        else:
            out.append(line)
            i += 1

    out = _rewrite_preload(out, spec.get("preload") or [])

    header = (
        f"# ACTIVE-MODE: {spec['name']}\n"
        f"# GENERATED by scripts/llama-swap-mode.py from config/llama-swap.base.yaml\n"
        f"#   + config/modes/{spec['name']}.yaml — DO NOT EDIT BY HAND.\n"
        f"#   Switch modes: scripts/llama-swap-mode.py set <mode>\n"
        f"# {spec['label']}\n"
        f"#\n"
    )
    return header + "".join(out)


# --------------------------------------------------------------------------- #
# active-file inspection
# --------------------------------------------------------------------------- #
def current_mode(path: str = ACTIVE) -> str:
    try:
        with open(path) as f:
            for line in f:
                mm = MARKER_RE.match(line)
                if mm:
                    return mm.group(1)
                if not line.startswith("#") and line.strip():
                    break
    except FileNotFoundError:
        return "unknown"
    return "unknown"


def effective_config(path: str = ACTIVE) -> dict:
    """Parse per-model --parallel / --ctx-size / concurrencyLimit / gpus."""
    try:
        with open(path) as f:
            lines = f.read().splitlines(keepends=True)
    except FileNotFoundError:
        return {}
    models: dict[str, dict] = {}
    i, n = 0, len(lines)
    while i < n:
        m = KEY_RE.match(lines[i])
        if m:
            name = m.group(1)
            j = i + 1
            block = []
            while j < n and not KEY_RE.match(lines[j]):
                block.append(lines[j])
                j += 1
            text = "".join(block)
            info: dict = {}
            if (mm := re.search(r"--parallel\s+(\d+)", text)):
                info["parallel"] = int(mm.group(1))
            if (mm := re.search(r"--ctx-size\s+(\d+)", text)):
                info["ctx"] = int(mm.group(1))
            if (mm := re.search(r"concurrencyLimit:\s*(\d+)", text)):
                info["concurrencyLimit"] = int(mm.group(1))
            if (mm := re.search(r"CUDA_VISIBLE_DEVICES=([\d,]+)", text)):
                info["gpus"] = mm.group(1)
            if "parallel" in info or "ctx" in info:
                p = info.get("parallel", 1) or 1
                if info.get("ctx"):
                    info["ctx_per_slot"] = info["ctx"] // p
                models[name] = info
            i = j
        else:
            i += 1
    return models


# --------------------------------------------------------------------------- #
# activation
# --------------------------------------------------------------------------- #
def _warm(model: str, timeout: float = 600.0) -> bool:
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "ok"}],
        "max_tokens": 1,
        "stream": False,
    }).encode()
    req = urllib.request.Request(
        f"{SWAP_URL}/v1/chat/completions", data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def set_mode(mode: str, warm: bool = True) -> dict:
    spec = load_mode(mode)
    rendered = render(mode)
    # sanity: the rendered active config must be valid YAML
    yaml.safe_load(rendered)
    with open(ACTIVE, "w") as f:
        f.write(rendered)
    # give -watch-config a moment to reload
    time.sleep(3)
    warmed = {}
    warm_models = spec.get("warm")
    if warm_models is None:
        warm_models = spec.get("preload") or []
    if warm:
        for model in warm_models:
            warmed[model] = _warm(model)
    return {
        "mode": spec["name"],
        "label": spec["label"],
        "description": " ".join((spec.get("description") or "").split()),
        "preload": spec.get("preload") or [],
        "warm": warm_models,
        "warmed": warmed,
        "effective": effective_config(ACTIVE),
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _fmt_ctx(v: int | None) -> str:
    if not v:
        return "–"
    return f"{v/1000:.0f}k" if v >= 1000 else str(v)


def main() -> None:
    ap = argparse.ArgumentParser(description="llama-swap config mode switcher")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="list available modes")
    sub.add_parser("current", help="print the active mode")
    p_show = sub.add_parser("show", help="show effective per-model config")
    p_show.add_argument("mode", nargs="?", help="mode to render+show (default: active)")
    p_set = sub.add_parser("set", help="activate a mode")
    p_set.add_argument("mode")
    p_set.add_argument("--no-warm", action="store_true", help="don't warm preload models")
    a = ap.parse_args()

    if a.cmd == "list":
        modes = list_modes()
        cur = current_mode()
        if a.json:
            print(json.dumps({"current": cur, "modes": modes}, indent=2))
        else:
            for mspec in modes:
                mark = "* " if mspec["name"] == cur else "  "
                print(f"{mark}{mspec['name']:<14} {mspec['label']}")
                desc = " ".join((mspec.get("description") or "").split())
                if desc:
                    print(f"                 {desc}")
        return

    if a.cmd == "current":
        cur = current_mode()
        if a.json:
            print(json.dumps({"current": cur, "effective": effective_config()}, indent=2))
        else:
            print(cur)
        return

    if a.cmd == "show":
        if a.mode:
            rendered = render(a.mode)
            tmp = "/tmp/llama-swap-mode-show.yaml"
            with open(tmp, "w") as f:
                f.write(rendered)
            eff = effective_config(tmp)
            os.unlink(tmp)
            name = a.mode
        else:
            eff = effective_config()
            name = current_mode()
        if a.json:
            print(json.dumps({"mode": name, "effective": eff}, indent=2))
        else:
            print(f"mode: {name}")
            print(f"{'model':<16}{'parallel':>9}{'ctx':>8}{'ctx/slot':>10}{'gpus':>7}")
            for mdl, info in eff.items():
                print(f"{mdl:<16}{info.get('parallel','–'):>9}"
                      f"{_fmt_ctx(info.get('ctx')):>8}"
                      f"{_fmt_ctx(info.get('ctx_per_slot')):>10}"
                      f"{info.get('gpus','–'):>7}")
        return

    if a.cmd == "set":
        result = set_mode(a.mode, warm=not a.no_warm)
        if a.json:
            print(json.dumps(result, indent=2))
        else:
            print(f"activated mode: {result['mode']} — {result['label']}")
            if result["warmed"]:
                for mdl, ok in result["warmed"].items():
                    print(f"  warm {mdl}: {'ok' if ok else 'FAILED'}")
            print(f"{'model':<16}{'parallel':>9}{'ctx':>8}{'ctx/slot':>10}")
            for mdl, info in result["effective"].items():
                print(f"{mdl:<16}{info.get('parallel','–'):>9}"
                      f"{_fmt_ctx(info.get('ctx')):>8}"
                      f"{_fmt_ctx(info.get('ctx_per_slot')):>10}")
        return


if __name__ == "__main__":
    main()
