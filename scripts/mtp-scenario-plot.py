#!/usr/bin/env python3
"""Plot MTP speculative-decode benefit by prompt type and temperature.

Reads docs/data/mtp/scenario-sweep.csv and renders two panels:
  (a) decode tok/s per scenario for baseline / MTP n=2 / MTP n=5 (temp 0 & 1),
      with the baseline drawn as a reference line;
  (b) draft acceptance % per scenario, showing how it collapses from code to
      creative writing and with rising temperature.

Render with venvs/comfyui/bin/python scripts/mtp-scenario-plot.py
"""
import csv, collections, pathlib
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

CSV = pathlib.Path(__file__).resolve().parents[1] / "docs/data/mtp/scenario-sweep.csv"
OUT = pathlib.Path(__file__).resolve().parents[1] / "docs/img/mtp-scenario-sweep.png"

rows = list(csv.DictReader(open(CSV)))
def get(scn, temp, nmax, field):
    for r in rows:
        if r["scenario"] == scn and float(r["temp"]) == temp and int(r["nmax"]) == nmax:
            return float(r[field]) if r[field] else None
    return None

scenarios = ["code", "technical-prose", "creative"]
labels = ["code\n(BST-style)", "technical\nprose", "creative\nwriting"]
baseline = [get(s, 0.0, 0, "decode_tok_s") for s in scenarios]

# config series: (legend, temp, nmax, color)
series = [
    ("MTP n=2, T=0", 0.0, 2, "#7fb3d5"),
    ("MTP n=2, T=1", 1.0, 2, "#2e86c1"),
    ("MTP n=5, T=0", 0.0, 5, "#f1948a"),
    ("MTP n=5, T=1", 1.0, 5, "#c0392b"),
]

x = np.arange(len(scenarios))
w = 0.19
fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 6))

# Panel (a): decode tok/s
for i, (lbl, temp, nmax, col) in enumerate(series):
    vals = [get(s, temp, nmax, "decode_tok_s") for s in scenarios]
    bars = ax1.bar(x + (i - 1.5) * w, vals, w, label=lbl, color=col, edgecolor="black", lw=0.4)
    for b, v, base in zip(bars, vals, baseline):
        ax1.annotate(f"{v/base:.2f}x", (b.get_x() + b.get_width()/2, v),
                     ha="center", va="bottom", fontsize=7)
bmean = float(np.mean(baseline))
ax1.axhline(bmean, color="black", ls="--", lw=1)
ax1.text(2.35, bmean + 0.3, f"baseline (MTP off) ~{bmean:.1f} t/s", fontsize=8,
         ha="right", va="bottom")
ax1.set_xticks(x); ax1.set_xticklabels(labels)
ax1.set_ylabel("decode tok/s (single stream, V100)")
ax1.set_title("(a) MTP decode speed by prompt type & temperature")
ax1.legend(fontsize=8, loc="upper right")
ax1.grid(axis="y", alpha=0.3)

# Panel (b): draft acceptance %
for i, (lbl, temp, nmax, col) in enumerate(series):
    vals = [get(s, temp, nmax, "accept_pct") for s in scenarios]
    ax2.bar(x + (i - 1.5) * w, vals, w, label=lbl, color=col, edgecolor="black", lw=0.4)
ax2.set_xticks(x); ax2.set_xticklabels(labels)
ax2.set_ylabel("draft acceptance (%)")
ax2.set_title("(b) MTP draft acceptance — falls with prose/creative & temperature")
ax2.set_ylim(0, 100)
ax2.legend(fontsize=8, loc="upper right")
ax2.grid(axis="y", alpha=0.3)

fig.suptitle("Qwen3.6-27B MTP self-speculation on CUDA/V100 — benefit depends on prompt type, "
             "not the backend", fontsize=12)
fig.tight_layout(rect=[0, 0, 1, 0.96])
fig.savefig(OUT, dpi=130)
print("wrote", OUT)
