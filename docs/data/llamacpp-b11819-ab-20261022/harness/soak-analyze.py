import json, re, sys, statistics as st, csv, collections
rows = json.load(open(sys.argv[1])) if sys.argv[1].endswith(".json") else None
if rows is None:
    rows = []
    for l in open(sys.argv[1]):
        m = re.match(r"(\d\d:\d\d:\d\d) (\w+)\s+(\S+)\s+(ok|FAIL)\s+([\d.]+)s p=(\S+) g=(\S+) dec=(\S+) pre=(\S+) ?(.*)", l)
        if m:
            t, model, kind, ok, sec, p, g, dec, pre, why = m.groups()
            rows.append({"t": t, "model": model, "kind": kind, "ok": ok == "ok", "sec": float(sec),
                         "prompt_tok": None if p == "None" else int(p), "gen_tok": None if g == "None" else int(g),
                         "decode_tps": None if dec == "None" else float(dec), "why": why or None})
print("model   kind        n  ok  trunc  fail   decode t/s (first third / middle / last third)")
for m in ("coding", "chat", "small"):
    for kind in ("code", "summary", "qa", "long-needle"):
        rs = [r for r in rows if r["model"] == m and r["kind"] == kind]
        if not rs:
            continue
        trunc = sum(1 for r in rs if not r["ok"] and r["why"] and "finish=length" in r["why"])
        fail = sum(1 for r in rs if not r["ok"]) - trunc
        dec = [r["decode_tps"] for r in rs if r["decode_tps"]]
        n = len(dec)
        thirds = [dec[: n // 3], dec[n // 3: 2 * n // 3], dec[2 * n // 3:]] if n >= 3 else [dec, [], []]
        fmt = " / ".join(f"{st.mean(x):.1f}" if x else "-" for x in thirds)
        print(f"{m:7s} {kind:11s}{len(rs):3d} {sum(r['ok'] for r in rs):3d} {trunc:5d} {fail:5d}   {fmt}")
