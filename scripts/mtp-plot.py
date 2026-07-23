#!/usr/bin/env python3
"""Chart the MTP on/off benchmark (scripts/mtp-bench.py output).

Two panels: (left) steady-state @4k decode t/s for baseline vs MTP n_max sweep,
(right) draft acceptance % vs n_max at each prompt size. Matplotlib Agg backend.

  benchmarks/llm-scaling-bench/.venv/bin/python scripts/mtp-plot.py
"""
import argparse
import csv
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

CSV = "/srv/ai/docs/data/mtp/qwen35-chat-mtp.csv"
OUT = "/srv/ai/docs/img/mtp-qwen35-chat.png"
STEADY = 4091


def load():
    rows = []
    with open(CSV) as f:
        for r in csv.DictReader(f):
            rows.append(r)
    return rows


def main():
    global CSV, OUT
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=CSV)
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--weights", default="UD-Q6_K",
                    help="weight-label shown in the left-panel subtitle")
    ap.add_argument("--suptitle",
                    default="Qwen3.6-35B-A3B (our daily `chat`) — built-in MTP "
                            "speculative decode, stock llama.cpp")
    a = ap.parse_args()
    CSV, OUT = a.csv, a.out
    rows = load()
    nmaxes = sorted({int(r["nmax"]) for r in rows})
    # steady-state decode per nmax
    dec = {}
    for r in rows:
        if int(r["prompt_tokens"]) == STEADY:
            dec[int(r["nmax"])] = float(r["decode_tok_s"])
    base = dec[0]

    fig, axes = plt.subplots(1, 2, figsize=(12.5, 4.6))

    labels = ["MTP off" if n == 0 else f"n_max={n}" for n in nmaxes]
    vals = [dec[n] for n in nmaxes]
    colors = ["#8a8a8a"] + ["#31a354"] * (len(nmaxes) - 1)
    x = range(len(nmaxes))
    b = axes[0].bar(x, vals, color=colors, edgecolor="#333", linewidth=0.6)
    axes[0].set_xticks(list(x)); axes[0].set_xticklabels(labels, fontsize=9)
    axes[0].set_ylabel("tokens / s"); axes[0].grid(axis="y", alpha=0.3)
    axes[0].set_axisbelow(True)
    axes[0].set_title("Decode @4k prompt — MTP off vs on\n(same " + a.weights +
                      " weights, stock llama.cpp, one V100)", fontsize=11, fontweight="bold")
    for rect, v, n in zip(b, vals, nmaxes):
        lbl = f"{v:.0f}" + ("" if n == 0 else f"\n+{v/base-1:.0%}")
        axes[0].text(rect.get_x() + rect.get_width() / 2, v + 2, lbl,
                     ha="center", va="bottom", fontsize=8.5)
    axes[0].set_ylim(0, max(vals) * 1.22)

    # acceptance vs prompt size for each MTP nmax
    sizes = sorted({int(r["prompt_tokens"]) for r in rows})
    for n in nmaxes:
        if n == 0:
            continue
        acc = []
        for s in sizes:
            m = [r for r in rows if int(r["nmax"]) == n and int(r["prompt_tokens"]) == s]
            acc.append(float(m[0]["accept_pct"]) if m and m[0]["accept_pct"] else None)
        axes[1].plot(sizes, acc, marker="o", label=f"n_max={n}")
    axes[1].set_xlabel("prompt size (tokens)"); axes[1].set_ylabel("draft acceptance %")
    axes[1].set_title("MTP draft acceptance vs prompt size\n(low-entropy summary prompt "
                      "— optimistic)", fontsize=11, fontweight="bold")
    axes[1].grid(alpha=0.3); axes[1].legend(fontsize=8.5); axes[1].set_ylim(0, 100)

    fig.suptitle(a.suptitle, fontsize=12, fontweight="bold")
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    fig.savefig(OUT, dpi=130); plt.close(fig)
    print("wrote", OUT)


if __name__ == "__main__":
    main()
