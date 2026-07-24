#!/usr/bin/env python3
"""Render the PXQ vs K-quant perplexity comparison (Round 5, §16).

Reads docs/data/pxq/quality-ppl.csv and writes docs/img/pxq-quality-ppl.png.
Bars grouped by quant, colored by engine; the stock Q8_0 near-lossless floor is
drawn as a horizontal reference. matplotlib Agg backend (no Chrome needed).
"""
import csv
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

CSV = "docs/data/pxq/quality-ppl.csv"
OUT = "docs/img/pxq-quality-ppl.png"

rows = list(csv.DictReader(open(CSV)))
for r in rows:
    r["ppl"] = float(r["ppl"])

floor = next(r["ppl"] for r in rows if r["key"] == "q8")

# Display order: stock standard quants, then fork standard, then fork PXQ.
order = [
    ("stock", "q8",   "Q8_0\n(stock)"),
    ("stock", "q6k",  "Q6_K\n(stock)"),
    ("stock", "q4km", "Q4_K_M\n(stock)"),
    ("fork",  "q6k",  "Q6_K\n(fork)"),
    ("fork",  "q4km", "Q4_K_M\n(fork)"),
    ("fork",  "pxq6", "PXQ6\n(fork)"),
    ("fork",  "pxq4", "PXQ4\n(fork)"),
    ("fork",  "pxq4_fromq8", "PXQ4·fromQ8\n(fork)"),
]
lut = {(r["engine"], r["key"]): r for r in rows}
labels, vals, colors = [], [], []
for eng, key, lab in order:
    r = lut[(eng, key)]
    labels.append(lab)
    vals.append(r["ppl"])
    if key.startswith("pxq"):
        colors.append("#d1495b")          # PXQ — red
    elif eng == "stock":
        colors.append("#2e7d32")          # stock K-quant — green
    else:
        colors.append("#6a8caf")          # fork K-quant — blue

fig, ax = plt.subplots(figsize=(11, 5.5))
bars = ax.bar(range(len(vals)), vals, color=colors, width=0.72)
ax.axhline(floor, color="#2e7d32", ls="--", lw=1.2, alpha=0.8)
ax.text(len(vals) - 0.4, floor + 0.01, f"Q8_0 floor {floor:.3f}",
        color="#2e7d32", va="bottom", ha="right", fontsize=9)

for b, v in zip(bars, vals):
    ax.text(b.get_x() + b.get_width() / 2, v + 0.015, f"{v:.3f}",
            ha="center", va="bottom", fontsize=9)

ax.set_xticks(range(len(labels)))
ax.set_xticklabels(labels, fontsize=8.5)
ax.set_ylabel("Perplexity (wikitext-2, ctx 512, 100 chunks) — lower is better")
ax.set_ylim(6.4, 8.1)
ax.set_title("Qwen3.6-35B-A3B — output quality: PXQ vs standard K-quants\n"
             "PXQ4/PXQ6 are ~8–12% worse than same-size K-quants (same engine)")
from matplotlib.patches import Patch
ax.legend(handles=[
    Patch(color="#2e7d32", label="standard K-quant (stock engine)"),
    Patch(color="#6a8caf", label="standard K-quant (fork engine, +~3% offset)"),
    Patch(color="#d1495b", label="fork PXQ tiers"),
], loc="upper left", fontsize=9, framealpha=0.9)
ax.grid(axis="y", alpha=0.25)
fig.tight_layout()
fig.savefig(OUT, dpi=130)
print("wrote", OUT)
