#!/usr/bin/env python3
"""Fit-vs-quality frontier on the 16 GB P100 (Round 7).

Scatter of GSM8K 5-shot exact_match vs on-disk size for every model that FITS the
P100, so the author's PXA-Fusion4 mixed-tier (PXQU16/PXQU12) can be placed against
the models we already serve there (the Gemmas) and the earlier uniform Qwen PXQ2.
Data: docs/data/pxq/fusion4-gsm8k-ppl.csv (Fusion4) + the Round 6 §17 numbers.
"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# label, size_gb, gsm8k_flex_%, group
points = [
    ("Gemma-4-12B\nQ4_K_XL",        6.7,  95.5, "gemma"),
    ("Gemma-4-26B-A4B\nQ4_K_XL",   13.6,  95.0, "gemma"),
    ("Qwen3.6-35B\nPXQ2 (uniform)",10.8,  85.5, "pxq2"),
    ("Fusion4-35B\nPXQU12",        12.18, 82.0, "fusion"),
    ("Fusion4-35B\nPXQU16",        14.6,  93.0, "fusion"),
]
color = {"gemma": "#2e8b57", "pxq2": "#9aa0a6", "fusion": "#1f77b4"}

fig, ax = plt.subplots(figsize=(9.5, 6))
for lbl, sz, acc, grp in points:
    ax.scatter(sz, acc, s=140, color=color[grp], edgecolor="black", zorder=3)
    ax.annotate(lbl, (sz, acc), textcoords="offset points", xytext=(8, 6), fontsize=8)

ax.axvline(16.0, color="crimson", ls="--", lw=1)
ax.text(16.0, 80.6, " 16 GB P100 ceiling", color="crimson", fontsize=8, va="bottom")
ax.set_xlabel("On-disk size (GB) — must fit the 16 GB P100 with room for KV")
ax.set_ylabel("GSM8K 5-shot exact_match (%)")
ax.set_xlim(5, 17)
ax.set_ylim(80, 98)
ax.set_title("Fit-vs-quality on the P100 — PXA-Fusion4 mixed-tier vs the served Gemmas")
from matplotlib.patches import Patch
ax.legend(handles=[
    Patch(facecolor="#1f77b4", edgecolor="black", label="PXA-Fusion4 (fork mixed-tier)"),
    Patch(facecolor="#2e8b57", edgecolor="black", label="Gemma-4 (served on P100 today)"),
    Patch(facecolor="#9aa0a6", edgecolor="black", label="Qwen3.6-35B PXQ2 (uniform low-bit)"),
], loc="lower left", fontsize=9)
ax.grid(alpha=0.3)
fig.tight_layout()
import pathlib
out = pathlib.Path(__file__).resolve().parents[1] / "docs/img/pxq-fusion4-fit-quality.png"
fig.savefig(out, dpi=130)
print("wrote", out)
