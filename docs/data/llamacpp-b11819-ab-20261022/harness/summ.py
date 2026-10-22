#!/usr/bin/env python3
"""Summarise an ab.sh run: llama-bench, mtp-bench and KLD results, A (b9850) vs B (b11819)."""
import csv, glob, os, re, statistics as st, sys
from collections import defaultdict

RUN = sys.argv[1] if len(sys.argv) > 1 else "/srv/ai/tmp/lcpp-test/ab1"


def arm_of(name):
    m = re.search(r"-([AB])(\d)$", name)
    return (m.group(1), m.group(2)) if m else (None, None)


def base_of(name):
    return re.sub(r"-[AB]\d$", "", name)


def spread(vals):
    if len(vals) < 2:
        return None
    m = st.mean(vals)
    return (max(vals) - min(vals)) / m * 100 if m else None


def fmt(x, nd=1):
    return "-" if x is None else f"{x:.{nd}f}"


def pct(a, b):
    return None if not a else (b - a) / a * 100


def verdict(d, sa, sb):
    if d is None:
        return ""
    noise = max(x for x in (sa, sb, 1.0) if x is not None)
    return "noise" if abs(d) < noise else ("FASTER" if d > 0 else "SLOWER")


def lbench():
    groups = defaultdict(lambda: defaultdict(list))  # base -> (test,depth) -> {arm: [avg_ts...]}
    for f in sorted(glob.glob(f"{RUN}/lb/*.csv")):
        name = os.path.basename(f)[:-4]
        arm, _ = arm_of(name)
        if not arm:
            continue
        try:
            rows = list(csv.DictReader(open(f)))
        except Exception:
            continue
        for r in rows:
            try:
                npmt, ngen, dep = int(r["n_prompt"]), int(r["n_gen"]), int(r["n_depth"])
                ts = float(r["avg_ts"])
            except (KeyError, ValueError):
                continue
            test = f"pp{npmt}" if npmt and not ngen else f"tg{ngen}"
            groups[base_of(name)][(test, dep)].append((arm, ts, r.get("build_number", "")))
    if not groups:
        return
    print("## llama-bench (t/s, mean of repetitions per run; spread = run-to-run within an arm)\n")
    print("| slot | test | depth | A b9850 | B b11819 | Δ% | spread A | spread B | verdict |")
    print("|---|---|---:|---:|---:|---:|---:|---:|---|")
    for base, tests in groups.items():
        for (test, dep), vals in sorted(tests.items(), key=lambda kv: (kv[0][1], kv[0][0])):
            a = [v for arm, v, _ in vals if arm == "A"]
            b = [v for arm, v, _ in vals if arm == "B"]
            if not a or not b:
                continue
            ma, mb = st.mean(a), st.mean(b)
            d = pct(ma, mb)
            sa, sb = spread(a), spread(b)
            print(f"| {base} | {test} | {dep} | {ma:.1f} | {mb:.1f} | {fmt(d)} | {fmt(sa)} | {fmt(sb)} | {verdict(d, sa, sb)} |")
    print()


def mtp():
    groups = defaultdict(lambda: defaultdict(list))  # base -> (nmax, idx) -> [(arm, row)]
    for f in sorted(glob.glob(f"{RUN}/csv/*-mtp.csv")):
        name = os.path.basename(f)[:-len("-mtp.csv")]
        arm, _ = arm_of(name)
        if not arm or name.startswith("ctxfit") or name.startswith("smoke"):
            continue
        rows = list(csv.DictReader(open(f)))
        seen = defaultdict(int)
        for r in rows:
            key = int(r["nmax"])
            idx = seen[key]; seen[key] += 1
            groups[base_of(name)][(key, idx)].append((arm, r))
    if not groups:
        return
    print("## mtp-bench end-to-end (decode / prefill t/s; medians over the ABBA runs of each arm)\n")
    print("| run | nmax | prompt tok | prefill A | prefill B | Δ% | decode A | decode B | Δ% | spread A/B | accept A→B | VRAM A→B MiB | verdict (decode) |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---|---|---|")
    for base, cells in groups.items():
        for (nmax, idx), vals in sorted(cells.items()):
            A = [r for arm, r in vals if arm == "A"]
            B = [r for arm, r in vals if arm == "B"]
            if not A or not B:
                continue
            f = lambda rs, k: [float(r[k]) for r in rs if r[k] not in ("", None)]
            pa, pb = st.median(f(A, "prefill_tok_s")), st.median(f(B, "prefill_tok_s"))
            da, db = st.median(f(A, "decode_tok_s")), st.median(f(B, "decode_tok_s"))
            sa, sb = spread(f(A, "decode_tok_s")), spread(f(B, "decode_tok_s"))
            aa, ab = f(A, "accept_pct"), f(B, "accept_pct")
            va, vb = max(f(A, "vram_mib")), max(f(B, "vram_mib"))
            tok = A[0]["prompt_tokens"]
            dd = pct(da, db)
            print(f"| {base} | {nmax} | {tok} | {pa:.0f} | {pb:.0f} | {fmt(pct(pa, pb))} | {da:.1f} | {db:.1f} | {fmt(dd)} | "
                  f"{fmt(sa)}/{fmt(sb)} | {fmt(st.median(aa) if aa else None)}→{fmt(st.median(ab) if ab else None)} | "
                  f"{va:.0f}→{vb:.0f} ({vb - va:+.0f}) | {verdict(dd, sa, sb)} |")
    print()


def ctxfit():
    files = sorted(glob.glob(f"{RUN}/csv/ctxfit-*-mtp-ctxfit.csv"))
    if not files:
        return
    print("## ctx-fit at production context (peak VRAM, MiB; gate: B within +300 of A)\n")
    print("| slot | A VRAM | B VRAM | Δ MiB | A prefill / decode | B prefill / decode | ok |")
    print("|---|---:|---:|---:|---|---|---|")
    by = defaultdict(dict)
    for f in files:
        name = os.path.basename(f)[:-len("-mtp-ctxfit.csv")]
        arm, _ = arm_of(name)
        slot = re.sub(r"^ctxfit-", "", re.sub(r"-[AB]$", "", name))
        arm = name[-1]
        rows = list(csv.DictReader(open(f)))
        if rows:
            by[slot][arm] = rows[-1]
    for slot, d in by.items():
        if "A" in d and "B" in d:
            va, vb = float(d["A"]["vram_mib"]), float(d["B"]["vram_mib"])
            fa = f"{d['A']['prefill_tok_s']} / {d['A']['decode_tok_s']}"
            fb = f"{d['B']['prefill_tok_s']} / {d['B']['decode_tok_s']}"
            print(f"| {slot} | {va:.0f} | {vb:.0f} | {vb - va:+.0f} | {fa} | {fb} | {'OK' if vb - va <= 300 else 'FAIL'} |")
        else:
            print(f"| {slot} | partial: {sorted(d)} | | | | | |")
    print()


def kld():
    files = sorted(glob.glob(f"{RUN}/kld/*.B.txt"))
    if not files:
        return
    print("## KLD / PPL gate (B vs the A base logits; wikitext-2 8x2048)\n")
    for f in files:
        slot = os.path.basename(f)[:-len(".B.txt")]
        txt = open(f, errors="ignore").read()
        keep = [l.strip() for l in txt.splitlines()
                if re.search(r"Mean\s+KLD|Mean\s+ln\(PPL|Final estimate|Same top|RMS Δp|Maximum KLD|99\.0%\s+KLD|Mean\s+Δp|Median\s+KLD", l)]
        pa = ""
        fa = f"{RUN}/kld/{slot}.A.txt"
        if os.path.exists(fa):
            m = re.findall(r"Final estimate: PPL = ([\d.]+)", open(fa, errors="ignore").read())
            pa = m[-1] if m else ""
        print(f"**{slot}** (A PPL {pa or '?'})")
        for l in keep:
            print(f"    {l}")
        if not keep:
            print("    (no KLD lines found; see the .B.txt)")
    print()


def cells(base, arm):
    """(nmax, idx) -> list of csv rows, for one label base and one arm."""
    out = defaultdict(list)
    for f in sorted(glob.glob(f"{RUN}/csv/{base}-{arm}[0-9]-mtp.csv")):
        seen = defaultdict(int)
        for r in csv.DictReader(open(f)):
            k = int(r["nmax"]); out[(k, seen[k])].append(r); seen[k] += 1
    return out


def extras():
    pairs = [("coding-sumE2", "coding-sum", "E2 --spec-draft-sampling probabilistic, coding summary temp 0.6"),
             ("chat-sumE2", "chat-sum", "E2 --spec-draft-sampling probabilistic, chat summary temp 0.6"),
             ("coding-sumE10", "coding-sum", "E10 GGML_CUDA_GRAPH_OPT=1, coding summary temp 0.6"),
             ("coding-sumN", "coding-sum", "E5 n_max sweep, coding summary temp 0.6 (baseline = n_max 3)"),
             ("coding-codeN", "coding-code", "E5 n_max sweep, coding code temp 0.0 (baseline = n_max 3)"),
             ("coding-sumP5", "coding-sum", "E5 --spec-draft-p-min 0.5, coding summary temp 0.6"),
             ("coding-sumBS", "coding-sum", "E5 --no-spec-draft-backend-sampling, coding summary temp 0.6")]
    shown = False
    for var, base, desc in pairs:
        v, b = cells(var, "B"), cells(base, "B")
        if not v or not b:
            continue
        if not shown:
            print("## B-build flag experiments (B arm only; baseline = the same B build without the flag)\n"); shown = True
        print(f"**{desc}**\n")
        print("| nmax | prompt tok | decode base | decode variant | Δ% | accept base→var | prefill Δ% | VRAM var−base MiB |")
        print("|---:|---:|---:|---:|---:|---|---:|---:|")
        for (nmax, idx), vr in sorted(v.items()):
            br = b.get((nmax, idx)) or b.get((3 if "coding" in var else 2, idx))
            if not br:
                continue
            f = lambda rs, k: [float(r[k]) for r in rs if r[k] not in ("", None)]
            dv, db = st.median(f(vr, "decode_tok_s")), st.median(f(br, "decode_tok_s"))
            pv, pb = st.median(f(vr, "prefill_tok_s")), st.median(f(br, "prefill_tok_s"))
            av, ab_ = f(vr, "accept_pct"), f(br, "accept_pct")
            note = "" if (nmax, idx) in b else f" (vs n_max {br[0]['nmax']})"
            print(f"| {nmax}{note} | {vr[0]['prompt_tokens']} | {db:.1f} | {dv:.1f} | {fmt(pct(db, dv))} | "
                  f"{fmt(st.median(ab_) if ab_ else None)}→{fmt(st.median(av) if av else None)} | {fmt(pct(pb, pv))} | "
                  f"{max(f(vr, 'vram_mib')) - max(f(br, 'vram_mib')):+.0f} |")
        print()


def e6():
    import json
    a, b = f"{RUN}/e6/e6-A.json", f"{RUN}/e6/e6-B.json"
    if not (os.path.exists(a) and os.path.exists(b)):
        return
    A, B = json.load(open(a)), json.load(open(b))
    print("## E6 multi-turn prompt cache (coding, production flags, ~20k-token document, thinking on)\n")
    print("| turn | A reused / processed | A prefill s | A wall s | A decode t/s | B reused / processed | B prefill s | B wall s | B decode t/s |")
    print("|---:|---|---:|---:|---:|---|---:|---:|---:|")
    for x, y in zip(A, B):
        print(f"| {x['turn']} | {x['cache_n']} / {x['prompt_n']} | {x['prefill_s']} | {x['wall_s']} | {x['decode_tps']} | "
              f"{y['cache_n']} / {y['prompt_n']} | {y['prefill_s']} | {y['wall_s']} | {y['decode_tps']} |")
    print()


if __name__ == "__main__":
    lbench(); mtp(); ctxfit(); kld(); extras(); e6()
