#!/usr/bin/env python3
"""Render the ThinkingCap-vs-base comparison chart.
Reads the staged summaries in docs/data/thinkingcap-27b-20260726/ and writes
docs/img/thinkingcap-27b.png. Matplotlib Agg (headless)."""
import json
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

DD = "docs/data/thinkingcap-27b-20260726"


def load(p):
    return json.load(open(f"{DD}/{p}"))


# greedy summaries use flat keys; temp1 uses nested {mean,std}
gb, gt = load("greedy-base.summary.json"), load("greedy-thinkingcap.summary.json")
tb, tt = load("temp1-base.summary.json"), load("temp1-thinkingcap.summary.json")

fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11, 4.6))
BLUE, ORANGE = "#4C72B0", "#DD8452"

# Panel 1: mean reasoning tokens, grouped by sampler, base vs ThinkingCap
groups = ["greedy (temp 0)", "operating point (temp 1.0)"]
base_r = [gb["mean_reasoning_tokens"], tb["reasoning_tokens"]["mean"]]
tc_r = [gt["mean_reasoning_tokens"], tt["reasoning_tokens"]["mean"]]
base_err = [0, tb["reasoning_tokens"]["std"]]
tc_err = [0, tt["reasoning_tokens"]["std"]]
x = range(len(groups))
w = 0.36
ax1.bar([i - w / 2 for i in x], base_r, w, yerr=base_err, capsize=4,
        label="stock Qwen3.6-27B", color=BLUE)
ax1.bar([i + w / 2 for i in x], tc_r, w, yerr=tc_err, capsize=4,
        label="ThinkingCap-27B", color=ORANGE)
for i, (b, t) in enumerate(zip(base_r, tc_r)):
    ax1.text(i - w / 2, b + 20, f"{b:.0f}", ha="center", va="bottom", fontsize=8)
    ax1.text(i + w / 2, t + 20, f"{t:.0f}", ha="center", va="bottom", fontsize=8)
    ax1.text(i, max(b, t) * 0.5, f"\u2212{100*(1-t/b):.0f}%", ha="center",
             fontsize=10, fontweight="bold", color="darkgreen")
ax1.set_xticks(list(x)); ax1.set_xticklabels(groups)
ax1.set_ylabel("mean reasoning (thinking) tokens / problem")
ax1.set_title("Thinking-token cost — GSM8K\n(error bars = \u03c3 across samples)")
ax1.legend(fontsize=9)
ax1.grid(axis="y", alpha=0.3)

# Panel 2: accuracy (per-sample avg)
base_a = [gb["accuracy"] * 100, tb["accuracy_avg"] * 100]
tc_a = [gt["accuracy"] * 100, tt["accuracy_avg"] * 100]
ax2.bar([i - w / 2 for i in x], base_a, w, label="stock Qwen3.6-27B", color=BLUE)
ax2.bar([i + w / 2 for i in x], tc_a, w, label="ThinkingCap-27B", color=ORANGE)
for i, (b, t) in enumerate(zip(base_a, tc_a)):
    ax2.text(i - w / 2, b + 0.5, f"{b:.1f}", ha="center", va="bottom", fontsize=8)
    ax2.text(i + w / 2, t + 0.5, f"{t:.1f}", ha="center", va="bottom", fontsize=8)
ax2.set_xticks(list(x)); ax2.set_xticklabels(groups)
ax2.set_ylabel("GSM8K accuracy (%)")
ax2.set_ylim(70, 100)
ax2.set_title("Accuracy preserved\n(per-sample; CIs overlap)")
ax2.legend(fontsize=9)
ax2.grid(axis="y", alpha=0.3)

fig.suptitle("ThinkingCap-Qwen3.6-27B vs stock Qwen3.6-27B base — same Q6_K, MTP decode parity (35 t/s)",
             fontsize=11)
fig.tight_layout(rect=[0, 0, 1, 0.96])
out = "docs/img/thinkingcap-27b.png"
fig.savefig(out, dpi=120)
print("wrote", out)
