#!/usr/bin/env python3
"""Plot GSM8K 5-shot exact_match across the served-model roster (2026-07-24).

Bars = flexible-extract exact_match, error bars = stderr. Bars are colour-coded by
model family. `gemma-26b` is omitted from the chart (identical GGUF to `fast`).
"""
import csv, pathlib
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Patch

root = pathlib.Path(__file__).resolve().parents[1]
rows = [r for r in csv.DictReader(open(root / "docs/data/lm-eval/roster-gsm8k.csv"))
        if r["served_model"] != "gemma-26b"]
rows.sort(key=lambda r: float(r["flexible_extract"]), reverse=True)

fam_color = {
    "qwen36-moe": "#1f77b4",
    "qwen36-dense": "#4c9be8",
    "qwen3-coder-moe": "#17becf",
    "gemma4-dense": "#2e8b57",
    "gemma4-moe": "#8fbc8f",
}
fam_label = {
    "qwen36-moe": "Qwen3.6 MoE",
    "qwen36-dense": "Qwen3.6 dense",
    "qwen3-coder-moe": "Qwen3-Coder MoE",
    "gemma4-dense": "Gemma-4 dense",
    "gemma4-moe": "Gemma-4 MoE",
}

labels, vals, errs, colors = [], [], [], []
for r in rows:
    labels.append(f"{r['served_model']}\n{r['params']} {r['quant']}")
    vals.append(float(r["flexible_extract"]) * 100)
    errs.append(float(r["stderr"]) * 100)
    colors.append(fam_color.get(r["family"], "#999999"))

fig, ax = plt.subplots(figsize=(12, 6))
x = range(len(labels))
ax.bar(x, vals, yerr=errs, capsize=4, color=colors, edgecolor="black", linewidth=0.6)
for i, (v, r) in enumerate(zip(vals, rows)):
    ax.text(i, v + max(errs) + 0.8, f"{v:.1f}%", ha="center", va="bottom",
            fontsize=9, fontweight="bold")

ax.set_ylabel("GSM8K 5-shot exact_match (%) — flexible-extract")
ax.set_ylim(0, 105)
ax.set_xticks(list(x))
ax.set_xticklabels(labels, fontsize=8)
ax.set_title("Output quality across the served-model roster — GSM8K (higher = better)")
ax.legend(handles=[Patch(facecolor=c, edgecolor="black", label=fam_label[f])
                   for f, c in fam_color.items()],
          loc="lower left", fontsize=9)
ax.grid(axis="y", alpha=0.3)
fig.tight_layout()
out = root / "docs/img/roster-gsm8k.png"
fig.savefig(out, dpi=130)
print("wrote", out)
