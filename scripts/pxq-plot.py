#!/usr/bin/env python3
"""Render the pxq_llama benchmark CSVs as bar-chart PNGs for the doc.

Reads docs/data/pxq/*.csv (columns: engine,target,spec,ubatch,prompt_tokens,
ttft_s,prefill_tok_s,decode_tok_s,vram_mib) and emits three PNGs into docs/img/:

  pxq-apples-to-apples.png  the key result: identical Q6_K on both engines, then
                            fork PXQ tiers stacked on top (prefill / decode / VRAM).
  pxq-v100-all.png          prefill & decode across every V100 config.
  pxq-p100.png              the P100 headline (35B MoE where no standard quant fits).

Uses matplotlib's Agg backend — no browser needed. Run with a venv that has
matplotlib:

  benchmarks/llm-scaling-bench/.venv/bin/python scripts/pxq-plot.py

Steady-state numbers use the 4091-token prompt row.
"""
from __future__ import annotations

import csv
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

DATA = "/srv/ai/docs/data/pxq"
OUT = "/srv/ai/docs/img"
STEADY_PROMPT = 4091  # steady-state prefill row

# consistent colours: stock = grey, fork = blues (darker = higher bpw)
C_STOCK = "#8a8a8a"
C_FORK_Q6 = "#08519c"
C_FORK_PXQ6 = "#3182bd"
C_FORK_PXQ4 = "#6baed6"
C_FORK_PXQ3 = "#9ecae1"
C_FORK_PXQ2 = "#c6dbef"


def load(target):
    """Return {engine: {prompt_tokens: row}} for a target CSV."""
    path = os.path.join(DATA, f"{target}.csv")
    out = {}
    with open(path) as f:
        for r in csv.DictReader(f):
            out.setdefault(r["engine"], {})[int(r["prompt_tokens"])] = r
    return out


def steady(target, engine):
    rows = load(target)[engine]
    r = rows[max(rows)]  # largest prompt-size row = steady state
    return (float(r["prefill_tok_s"]), float(r["decode_tok_s"]),
            int(r["vram_mib"]) / 1024.0)


def bars(ax, labels, values, colors, title, ylabel, fmt="{:.0f}"):
    x = range(len(labels))
    b = ax.bar(x, values, color=colors, edgecolor="#333", linewidth=0.6)
    ax.set_xticks(list(x))
    ax.set_xticklabels(labels, fontsize=9)
    ax.set_title(title, fontsize=11, fontweight="bold")
    ax.set_ylabel(ylabel, fontsize=9)
    ax.grid(axis="y", alpha=0.3)
    ax.set_axisbelow(True)
    top = max(values) if values else 1
    for rect, v in zip(b, values):
        ax.text(rect.get_x() + rect.get_width() / 2, v + top * 0.015,
                fmt.format(v), ha="center", va="bottom", fontsize=8.5)
    ax.set_ylim(0, top * 1.18)


def fig_apples():
    labels = ["stock\nQ6_K", "fork\nQ6_K", "fork\nPXQ6", "fork\nPXQ4"]
    colors = [C_STOCK, C_FORK_Q6, C_FORK_PXQ6, C_FORK_PXQ4]
    pre, dec, vram = [], [], []
    for tgt, eng in [("v100-qwen35-q6k", "stock"), ("v100-qwen35-q6k", "fork"),
                     ("v100-qwen35-pxq6", "fork"), ("v100-qwen35-pxq4", "fork")]:
        p, d, v = steady(tgt, eng)
        pre.append(p); dec.append(d); vram.append(v)

    fig, axes = plt.subplots(1, 3, figsize=(13, 4.4))
    bars(axes[0], labels, pre, colors,
         "Prefill @4k prompt (higher = better)", "tokens / s")
    bars(axes[1], labels, dec, colors,
         "Decode @4k prompt (higher = better)", "tokens / s", "{:.1f}")
    bars(axes[2], labels, vram, colors,
         "Peak VRAM (lower = better)", "GB", "{:.1f}")

    # annotate the engine-only vs quant contribution on prefill
    base = pre[0]
    axes[0].annotate(f"engine only\n{pre[1]/base:.2f}x", xy=(1, pre[1]),
                     xytext=(1, pre[1] * 0.55), ha="center", fontsize=8.5,
                     color="white", fontweight="bold")
    axes[0].annotate(f"+ PXQ4\n{pre[3]/base:.2f}x", xy=(3, pre[3]),
                     xytext=(3, pre[3] * 0.55), ha="center", fontsize=8.5,
                     color="#08306b", fontweight="bold")

    fig.suptitle("pxq_llama vs stock — Qwen3.6-35B-A3B on one V100 (identical Q6_K weights, "
                 "then fork PXQ tiers)", fontsize=12, fontweight="bold")
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    p = os.path.join(OUT, "pxq-apples-to-apples.png")
    fig.savefig(p, dpi=130); plt.close(fig)
    print("wrote", p)


def fig_v100_all():
    specs = [("stock Q6_K", "v100-qwen35-q6k", "stock", C_STOCK),
             ("fork Q6_K", "v100-qwen35-q6k", "fork", C_FORK_Q6),
             ("fork PXQ6", "v100-qwen35-pxq6", "fork", C_FORK_PXQ6),
             ("fork PXQ4", "v100-qwen35-pxq4", "fork", C_FORK_PXQ4),
             ("fork PXQ3", "v100-qwen35-pxq3", "fork", C_FORK_PXQ3),
             ("fork PXQ2", "v100-qwen35-pxq2", "fork", C_FORK_PXQ2)]
    labels = [s[0].replace(" ", "\n") for s in specs]
    colors = [s[3] for s in specs]
    pre, dec = [], []
    for _, tgt, eng, _ in specs:
        p, d, _ = steady(tgt, eng)
        pre.append(p); dec.append(d)
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.4))
    bars(axes[0], labels, pre, colors, "Prefill @4k prompt", "tokens / s")
    bars(axes[1], labels, dec, colors, "Decode @4k prompt", "tokens / s", "{:.1f}")
    fig.suptitle("V100 (idx1) — Qwen3.6-35B-A3B, all engines/quants",
                 fontsize=12, fontweight="bold")
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    p = os.path.join(OUT, "pxq-v100-all.png")
    fig.savefig(p, dpi=130); plt.close(fig)
    print("wrote", p)


def fig_p100():
    specs = [("current fast:\nGemma-26B-A4B\nQ4_K_XL", "p100-gemma26b", C_STOCK),
             ("fork 35B\nPXQ2\n(11.5 GB)", "p100-qwen35-pxq2", C_FORK_PXQ2),
             ("fork 35B\nPXQ3\n(15.4 GB)", "p100-qwen35-pxq3", C_FORK_PXQ3)]
    labels = [s[0] for s in specs]
    colors = [s[2] for s in specs]
    pre, dec, vram = [], [], []
    for _, tgt, _ in specs:
        p, d, v = steady(tgt, "stock" if tgt == "p100-gemma26b" else "fork")
        pre.append(p); dec.append(d); vram.append(v)
    fig, axes = plt.subplots(1, 3, figsize=(11.5, 4.4))
    bars(axes[0], labels, pre, colors, "Prefill @4k prompt", "tokens / s")
    bars(axes[1], labels, dec, colors, "Decode @4k prompt", "tokens / s", "{:.1f}")
    bars(axes[2], labels, vram, colors, "Peak VRAM (of 16 GB)", "GB", "{:.1f}")
    axes[2].axhline(16, color="#cc0000", ls="--", lw=1)
    axes[2].text(len(labels) - 1, 16.1, "P100 16 GB", color="#cc0000",
                 ha="right", va="bottom", fontsize=8)
    fig.suptitle("P100 (idx0, 16 GB) — fork's 35B MoE vs our current Gemma-26B-A4B",
                 fontsize=12, fontweight="bold")
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    p = os.path.join(OUT, "pxq-p100.png")
    fig.savefig(p, dpi=130); plt.close(fig)
    print("wrote", p)


def fig_round2_dual():
    # Dual-V100 layer split: stock Q6_K (R1) vs fork2 PXQ4/PXQ6, plus single-card ref.
    specs = [("stock Q6_K\ndual-V100", "dualv100-qwen35-q6k", "stock", C_STOCK),
             ("fork PXQ4\ndual-V100", "dualv100-qwen35-pxq4", "fork2", C_FORK_PXQ4),
             ("fork PXQ6\ndual-V100", "dualv100-qwen35-pxq6", "fork2", C_FORK_PXQ6),
             ("fork PXQ4\nsingle-V100", "v100-qwen35-pxq4", "fork2", C_FORK_Q6)]
    labels = [s[0] for s in specs]
    colors = [s[3] for s in specs]
    pre, dec = [], []
    for _, tgt, eng, _ in specs:
        p, d, _ = steady(tgt, eng)
        pre.append(p); dec.append(d)
    fig, axes = plt.subplots(1, 2, figsize=(9.5, 4.4))
    bars(axes[0], labels, pre, colors, "Prefill @4k prompt", "tokens / s")
    bars(axes[1], labels, dec, colors, "Decode @4k prompt", "tokens / s", "{:.1f}")
    fig.suptitle("Round 2 — dual-V100 (v2026.07.23): fork fixes the crash, wins prefill, "
                 "loses decode", fontsize=11.5, fontweight="bold")
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    p = os.path.join(OUT, "pxq-round2-dual.png")
    fig.savefig(p, dpi=130); plt.close(fig)
    print("wrote", p)


def steady_spec(target, spec):
    """Steady-state (prefill, decode, vram_gb) for a given spec value (fork2)."""
    path = os.path.join(DATA, f"{target}.csv")
    best = None
    with open(path) as f:
        for r in csv.DictReader(f):
            if r["spec"] == spec:
                pt = int(r["prompt_tokens"])
                if best is None or pt > best[0]:
                    best = (pt, r)
    r = best[1]
    return (float(r["prefill_tok_s"]), float(r["decode_tok_s"]),
            int(r["vram_mib"]) / 1024.0)


def fig_round3_fusion4():
    # Round 3: the author's own PXA-Fusion4-35B model. Two stories in one figure:
    #  (left)  bitrate barely moves decode within one engine (quant-size ≠ the +30%)
    #  (right) MTP spec-decode is the real, orthogonal decode win, on both cards.
    C_MTP = "#31a354"
    fig, axes = plt.subplots(1, 2, figsize=(12.5, 4.6))

    # left: P100 decode across the three quant tiers (all fork2, no spec)
    q = [("PXQ2\n2.27 bpw", "p100-fusion4-pxq2", C_FORK_PXQ2),
         ("PXQU12\n2.65 bpw", "p100-fusion4-pxqu12", C_FORK_PXQ4),
         ("PXQU16\n3.20 bpw", "p100-fusion4-pxqu16", C_FORK_PXQ6)]
    dec = [steady_spec(t, "")[1] for _, t, _ in q]
    bars(axes[0], [s[0] for s in q], dec, [s[2] for s in q],
         "P100 decode vs quant bitrate (same engine)\n→ quant size barely moves decode",
         "tokens / s", "{:.1f}")

    # right: MTP off vs on, P100 & V100 PXQ2
    labels = ["P100\nPXQ2", "P100\nPXQ2\n+MTP", "V100\nPXQ2", "V100\nPXQ2\n+MTP"]
    dec2 = [steady_spec("p100-fusion4-pxq2", "")[1],
            steady_spec("p100-fusion4-pxq2", "mtp:n_max=1")[1],
            steady_spec("v100-fusion4-pxq2", "")[1],
            steady_spec("v100-fusion4-pxq2", "mtp:n_max=1")[1]]
    colors2 = [C_FORK_PXQ2, C_MTP, C_FORK_Q6, C_MTP]
    bars(axes[1], labels, dec2, colors2,
         "MTP spec-decode is the real decode win\n(n_max=1, orthogonal to quant)",
         "tokens / s", "{:.1f}")
    axes[1].annotate(f"+{dec2[1]/dec2[0]-1:+.0%}".replace("++", "+"), xy=(1, dec2[1]),
                     xytext=(1, dec2[1] * 0.5), ha="center", fontsize=9,
                     color="white", fontweight="bold")
    axes[1].annotate(f"{dec2[3]/dec2[2]-1:+.0%}", xy=(3, dec2[3]),
                     xytext=(3, dec2[3] * 0.5), ha="center", fontsize=9,
                     color="white", fontweight="bold")

    fig.suptitle("Round 3 — author's PXA-Fusion4-35B: the +30% decode is quant-class, "
                 "MTP is the real engine-side win", fontsize=11.5, fontweight="bold")
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    p = os.path.join(OUT, "pxq-round3-fusion4.png")
    fig.savefig(p, dpi=130); plt.close(fig)
    print("wrote", p)


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    fig_apples()
    fig_v100_all()
    fig_p100()
    fig_round2_dual()
    fig_round3_fusion4()
