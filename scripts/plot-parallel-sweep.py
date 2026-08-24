#!/usr/bin/env python3
"""Render the --parallel throughput sweep CSV as a grouped bar chart PNG.

Reads a sweep CSV (produced by scripts/parallel-sweep.py: columns model,
parallel, concurrent_users, tokens_per_second, ...), reduces it to the PEAK
aggregate tokens/sec per (model, --parallel) — the best over the concurrency
sweep — and plots grouped bars (one group per model, one bar per --parallel).

Uses matplotlib's Agg backend, so it needs NO browser (unlike the harness's
Plotly+Kaleido PNG path, which requires Chrome). Run with the harness venv (has
matplotlib):

  benchmarks/llm-scaling-bench/.venv/bin/python scripts/plot-parallel-sweep.py \
      docs/data/parallel-sweep-20260721.csv -o docs/img/parallel-sweep-20260721.png
"""
from __future__ import annotations

import argparse
import csv
import re
from collections import defaultdict

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# Bar colours per --parallel value (colour-blind-friendly, dark-theme friendly).
PCOLORS = {1: "#4b8ce0", 2: "#7fce7f", 4: "#e6c04b", 8: "#e07f7f"}

# Default config read for slot -> model-name labels (overridable via --config).
DEFAULT_CONFIG = "/srv/ai/config/llama-swap.yaml"


def load_peaks(path: str) -> dict[str, dict[int, float]]:
    """peak[model][parallel] = max tokens/sec over the concurrency sweep."""
    peak: dict[str, dict[int, float]] = defaultdict(lambda: defaultdict(float))
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            try:
                model = row["model"]
                p = int(row["parallel"])
                tps = float(row["tokens_per_second"])
            except (KeyError, ValueError):
                continue
            if tps > peak[model][p]:
                peak[model][p] = tps
    return peak


def _shorten_model(gguf: str) -> str:
    """Turn a GGUF filename into a compact human model name for the chart.

    e.g. 'Qwen3.8-27B-UD-Q8_K_XL.gguf' -> 'Qwen3.8-27B Q8_K_XL'
         'gemma-4-12B-it-qat-UD-Q4_K_XL.gguf' -> 'Gemma-4-12B Q4_K_XL'
    """
    name = re.sub(r"\.gguf$", "", gguf)
    # Split the quant suffix (Q<n>_... or BF16/F16) from the family stem.
    m = re.search(r"(Q\d[\w]*|BF16|F16)$", name)
    quant = m.group(1) if m else ""
    stem = name[: m.start()] if m else name
    stem = stem.rstrip("-_")
    # Drop noisy tuning tokens from the family stem.
    for tok in ("-it-qat", "-it", "-qat", "-UD", "-instruct", "-Instruct"):
        stem = stem.replace(tok, "")
    stem = stem.replace("gemma", "Gemma").rstrip("-_")
    return f"{stem} {quant}".strip()


def load_model_names(config: str) -> dict[str, str]:
    """Map each router slot key -> compact model name from its --model GGUF."""
    names: dict[str, str] = {}
    try:
        text = open(config).read()
    except OSError:
        return names
    # Slot keys are 2-space-indented quoted keys; capture each block's --model.
    for m in re.finditer(r'^  "([a-z0-9-]+)":\s*$', text, re.M):
        slot = m.group(1)
        blk = text[m.end():]
        nxt = re.search(r'^  "[a-z0-9-]+":\s*$', blk, re.M)
        if nxt:
            blk = blk[: nxt.start()]
        gguf = re.search(r"--model\s+\S*/([^/\s]+\.gguf)", blk)
        if gguf:
            names[slot] = _shorten_model(gguf.group(1))
    return names


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("csv", help="sweep CSV (from scripts/parallel-sweep.py)")
    ap.add_argument("-o", "--out", default="docs/img/parallel-sweep.png")
    ap.add_argument("--config", default=DEFAULT_CONFIG,
                    help="llama-swap config to read slot->model names from")
    ap.add_argument("--title", default="llama-swap --parallel throughput sweep "
                    "— peak aggregate tok/s")
    a = ap.parse_args()

    peak = load_peaks(a.csv)
    model_names = load_model_names(a.config)
    # Order models by their best throughput (tallest first) for a clean read.
    models = sorted(peak, key=lambda m: -max(peak[m].values()))
    parallels = sorted({p for m in peak for p in peak[m]})

    fig, ax = plt.subplots(figsize=(11, 5.5))
    fig.patch.set_facecolor("#0f1115")
    ax.set_facecolor("#0f1115")
    n = len(parallels)
    group_w = 0.8
    bar_w = group_w / n
    x = range(len(models))
    for i, p in enumerate(parallels):
        offs = [xi - group_w / 2 + bar_w * (i + 0.5) for xi in x]
        vals = [peak[m].get(p, 0) for m in models]
        bars = ax.bar(offs, vals, bar_w, label=f"P={p}",
                      color=PCOLORS.get(p, "#888"), edgecolor="#0f1115")
        for rect, v in zip(bars, vals):
            if v > 0:
                ax.text(rect.get_x() + rect.get_width() / 2, v + 3,
                        f"{v:.0f}", ha="center", va="bottom",
                        fontsize=7, color="#cdd6e4")

    ax.set_title(a.title, color="#e6e6e6", fontsize=12)
    ax.set_ylabel("peak aggregate tokens / sec", color="#cdd6e4")
    ax.set_xticks(list(x))
    # Multi-line tick labels: slot name + backing model family + quant, each on
    # its own line so long names don't collide with neighbouring groups.
    xlabels = []
    for m in models:
        mn = model_names.get(m)
        if mn:
            # _shorten_model joins "<family> <quant>" with a single space; stack
            # the quant onto its own line to keep each label narrow.
            xlabels.append(f"{m}\n{mn.replace(' ', chr(10))}")
        else:
            xlabels.append(m)
    ax.set_xticklabels(xlabels, color="#cdd6e4", fontsize=8)
    ax.tick_params(axis="y", colors="#8b95a7")
    for spine in ax.spines.values():
        spine.set_color("#232a36")
    ax.grid(axis="y", color="#202634", linewidth=0.6)
    ax.set_axisbelow(True)
    leg = ax.legend(title="--parallel", facecolor="#151922",
                    edgecolor="#232a36", labelcolor="#cdd6e4")
    leg.get_title().set_color("#8b95a7")
    fig.tight_layout()
    fig.savefig(a.out, dpi=140, facecolor=fig.get_facecolor())
    print(f"wrote {a.out}")


if __name__ == "__main__":
    main()
