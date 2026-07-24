#!/usr/bin/env python3
"""Plot the cross-model P100 GSM8K quality comparison (Round 6).

Bars = GSM8K 5-shot exact_match (flexible-extract), error bars = stderr.
Green  = quant fits the 16 GB P100 (a real P100 serving option).
Grey   = needs a V100 / dual-V100 (shown only as the 35B quality ceiling).
"""
import csv, pathlib
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

root = pathlib.Path(__file__).resolve().parents[1]
rows = list(csv.DictReader(open(root / "docs/data/pxq/xmodel-gsm8k.csv")))

labels, vals, errs, colors = [], [], [], []
for r in rows:
    fit = r["fits_p100"] == "yes"
    labels.append(f"{r['model']}\n{r['quant']} ({r['size_gb']}GB)")
    vals.append(float(r["flexible_extract"]) * 100)
    errs.append(float(r["stderr"]) * 100)
    colors.append("#2e8b57" if fit else "#9aa0a6")

fig, ax = plt.subplots(figsize=(10, 5.5))
x = range(len(labels))
bars = ax.bar(x, vals, yerr=errs, capsize=4, color=colors, edgecolor="black", linewidth=0.6)
for i, v in enumerate(vals):
    ax.text(i, v + max(errs) + 0.6, f"{v:.1f}%", ha="center", va="bottom", fontsize=10, fontweight="bold")

ax.set_ylabel("GSM8K 5-shot exact_match (%)")
ax.set_ylim(75, 102)
ax.set_xticks(list(x))
ax.set_xticklabels(labels, fontsize=8)
ax.set_title("Cross-model output quality on the P100 — GSM8K (higher = better)")
ax.axhspan(75, 102, xmin=0, xmax=0, alpha=0)  # noop keep layout
from matplotlib.patches import Patch
ax.legend(handles=[Patch(facecolor="#2e8b57", edgecolor="black", label="Fits 16 GB P100 (real option)"),
                   Patch(facecolor="#9aa0a6", edgecolor="black", label="Needs V100 / dual-V100 (ceiling only)")],
          loc="lower right", fontsize=9)
ax.grid(axis="y", alpha=0.3)
fig.tight_layout()
out = root / "docs/img/pxq-xmodel-gsm8k.png"
fig.savefig(out, dpi=130)
print("wrote", out)
