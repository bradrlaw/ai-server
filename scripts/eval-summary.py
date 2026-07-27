#!/usr/bin/env python3
"""Rebuild the comparison summary page for an eval test from its run outputs.

Scans evals/<test>/outputs/*/meta.json (+ check.json when present) and writes
evals/<test>/summary.html — a single sortable table comparing every recorded run
on the metrics that matter (objective score, output size, TTFT, prefill/decode
throughput, MTP, wall time). Each row links to the run's own view page and to the
raw model output.

eval-run.py calls build_summary() after every run so the page is always current;
run this script directly to rebuild the whole page from scratch:

  scripts/eval-summary.py --test localmind-landing-page
  scripts/eval-summary.py --all        # rebuild every test under evals/
"""
import argparse
import html
import json
import os
from datetime import datetime, timezone

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def _fmt(v, nd=1, suffix=""):
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.{nd}f}{suffix}"
    return f"{v}{suffix}"


def collect_runs(test_dir):
    out_root = os.path.join(test_dir, "outputs")
    runs = []
    if not os.path.isdir(out_root):
        return runs
    for label in sorted(os.listdir(out_root)):
        d = os.path.join(out_root, label)
        meta = _load(os.path.join(d, "meta.json"))
        if not meta:
            continue
        meta["_label"] = label
        meta["_check"] = _load(os.path.join(d, "check.json"))
        runs.append(meta)
    return runs


ROW_CSS = """
:root{--bg:#09090b;--surface:#13111c;--border:rgba(255,255,255,.08);
--accent:#7c5cfc;--teal:#00d4aa;--fg:#fafafa;--muted:#71717a}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;padding:32px}
h1{font-size:26px;margin:0 0 4px}.sub{color:var(--muted);margin:0 0 24px}
.wrap{overflow-x:auto;border:1px solid var(--border);border-radius:12px}
table{border-collapse:collapse;width:100%;min-width:900px}
th,td{padding:10px 14px;text-align:right;white-space:nowrap;border-bottom:1px solid var(--border)}
th{background:var(--surface);color:var(--muted);font-weight:600;font-size:12px;
text-transform:uppercase;letter-spacing:.04em;position:sticky;top:0}
td.l,th.l{text-align:left}tr:last-child td{border-bottom:0}
tr:hover td{background:rgba(124,92,252,.06)}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
.model{font-weight:600}.name{color:var(--muted);font-size:12px}
.pill{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;
border:1px solid var(--border)}.mtp-on{color:var(--teal);border-color:rgba(0,212,170,.4)}
.mtp-off{color:var(--muted)}.score{color:var(--teal);font-weight:600}
.trunc{color:#f87171}.foot{color:var(--muted);font-size:12px;margin-top:18px}
"""


RUN_CSS = """
:root{--bg:#09090b;--surface:#13111c;--border:rgba(255,255,255,.08);
--accent:#7c5cfc;--teal:#00d4aa;--fg:#fafafa;--muted:#71717a}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.6 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;padding:32px;max-width:1100px;margin:auto}
h1{font-size:24px;margin:0 0 2px}h2{font-size:15px;text-transform:uppercase;
letter-spacing:.05em;color:var(--muted);margin:28px 0 10px}
.sub{color:var(--muted);margin:0 0 20px}a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:14px 16px}
.card .k{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.04em}
.card .v{font-size:22px;font-weight:600;margin-top:4px}
.card .v.teal{color:var(--teal)}
pre{background:var(--surface);border:1px solid var(--border);border-radius:10px;
padding:14px 16px;overflow-x:auto;font-size:13px;color:#d4d4d8;white-space:pre-wrap;word-break:break-word}
.frame{width:100%;height:70vh;border:1px solid var(--border);border-radius:10px;background:#fff}
.pill{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;border:1px solid var(--border)}
.mtp-on{color:var(--teal);border-color:rgba(0,212,170,.4)}.mtp-off{color:var(--muted)}
"""


def _card(k, v, teal=False):
    cls = "v teal" if teal else "v"
    return f'<div class="card"><div class="k">{html.escape(k)}</div><div class="{cls}">{v}</div></div>'


def write_run_html(out_dir, meta):
    """(Re)generate outputs/<label>/run.html from meta.json (+ its attached _check)."""
    perf = meta.get("performance") or {}
    mtp = meta.get("mtp") or {}
    usage = meta.get("usage") or {}
    chk = meta.get("_check") or {}
    out_file = meta.get("output_file", "index.html")
    is_html = out_file.rsplit(".", 1)[-1].lower() in ("html", "htm")

    def num(v, nd=1, s=""):
        return "—" if v is None else (f"{v:.{nd}f}{s}" if isinstance(v, float) else f"{v}{s}")

    mtp_pill = ('<span class="pill mtp-on">on</span>' if mtp.get("enabled")
                else '<span class="pill mtp-off">off</span>')
    if mtp.get("enabled") and mtp.get("accept_rate") is not None:
        mtp_pill += f' {mtp["accept_rate"]*100:.0f}% accept'

    cards = [
        _card("Objective", f'{chk.get("score")}/{chk.get("max")}' if chk.get("max") else "—", teal=True),
        _card("TTFT", num(perf.get("ttft_ms"), 0, " ms")),
        _card("Prefill", num(perf.get("prefill_tps"), 1, " t/s")),
        _card("Decode", num(perf.get("decode_tps"), 1, " t/s"), teal=True),
        _card("Wall time", num(meta.get("wall_secs"), 1, " s")),
        _card("Completion tok", num(usage.get("completion_tokens"))),
        _card("Output size", num(meta.get("output_bytes"), 0, " B")),
        _card("MTP", mtp_pill),
        _card("Finish", html.escape(str(meta.get("finish_reason")))),
    ]

    preview = ""
    if is_html:
        preview = (f'<h2>Rendered output</h2>\n'
                   f'<iframe class="frame" src="{html.escape(out_file)}" '
                   f'title="rendered output"></iframe>\n')

    meta_json = html.escape(json.dumps(
        {k: v for k, v in meta.items() if not k.startswith("_")}, indent=2))
    load_cmd = html.escape(meta.get("load_command") or "(unavailable)")
    title = html.escape(meta.get("model_slot") or meta.get("label") or "")

    doc = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} — {html.escape(meta.get('test') or '')}</title>
<style>{RUN_CSS}</style></head>
<body>
<h1>{title}</h1>
<p class="sub">{html.escape(meta.get('model_name') or '')} &middot; {html.escape(meta.get('test') or '')}
&middot; {html.escape((meta.get('created_utc') or '')[:19])} UTC
&middot; <a href="../../summary.html">← all runs</a>
&middot; <a href="{html.escape(out_file)}">output</a>
&middot; <a href="raw.txt">raw reply</a></p>
<div class="grid">
{os.linesep.join(cards)}
</div>
{preview}
<h2>llama.cpp load command</h2>
<pre>{load_cmd}</pre>
<h2>Sampler / request</h2>
<pre>{html.escape(json.dumps(meta.get('sampler'), indent=2))}</pre>
<h2>meta.json</h2>
<pre>{meta_json}</pre>
</body></html>
"""
    with open(os.path.join(out_dir, "run.html"), "w") as f:
        f.write(doc)


def build_summary(test_dir):
    """Regenerate <test_dir>/summary.html (and every outputs/<label>/run.html) from
    all recorded runs on disk. Returns the summary path."""
    test = os.path.basename(test_dir.rstrip("/"))
    runs = collect_runs(test_dir)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # refresh each run's own page from its meta.json (+ latest check.json)
    for m in runs:
        write_run_html(os.path.join(test_dir, "outputs", m["_label"]), m)

    rows = []
    # sort: highest objective score first, then fastest decode
    def sort_key(m):
        c = m.get("_check") or {}
        score = c.get("score", -1)
        perf = m.get("performance") or {}
        return (-score, -(perf.get("decode_tps") or 0))
    for m in sorted(runs, key=sort_key):
        label = m["_label"]
        perf = m.get("performance") or {}
        mtp = m.get("mtp") or {}
        chk = m.get("_check") or {}
        usage = m.get("usage") or {}
        run_html = f"outputs/{html.escape(label)}/run.html"
        out_file = m.get("output_file", "index.html")
        out_link = f"outputs/{html.escape(label)}/{html.escape(out_file)}"

        score_cell = "—"
        if chk.get("max"):
            pct = 100 * chk["score"] / chk["max"]
            score_cell = f'<span class="score">{chk["score"]}/{chk["max"]}</span> <span class="name">{pct:.0f}%</span>'

        mtp_cell = ('<span class="pill mtp-on">on</span>' if mtp.get("enabled")
                    else '<span class="pill mtp-off">off</span>')
        acc = mtp.get("accept_rate")
        if mtp.get("enabled") and acc is not None:
            mtp_cell += f' <span class="name">{acc*100:.0f}%</span>'

        finish = m.get("finish_reason")
        fin_cell = (f'<span class="trunc">{html.escape(str(finish))}</span>'
                    if finish == "length" else html.escape(str(finish)))

        model_name = m.get("model_name") or "—"
        created = (m.get("created_utc") or "")[:10]

        rows.append(f"""    <tr>
      <td class="l"><a href="{run_html}"><span class="model">{html.escape(m.get('model_slot') or label)}</span></a>
        <div class="name">{html.escape(model_name)}</div></td>
      <td>{score_cell}</td>
      <td>{mtp_cell}</td>
      <td>{_fmt(m.get('output_bytes'))} B</td>
      <td>{_fmt(usage.get('completion_tokens'))}</td>
      <td>{_fmt(perf.get('ttft_ms'))} ms</td>
      <td>{_fmt(perf.get('prefill_tps'))}</td>
      <td>{_fmt(perf.get('decode_tps'))}</td>
      <td>{_fmt(m.get('wall_secs'))} s</td>
      <td>{fin_cell}</td>
      <td class="name">{html.escape(created)}</td>
      <td class="l"><a href="{out_link}">output</a></td>
    </tr>""")

    body = "\n".join(rows) if rows else '    <tr><td class="l" colspan="12">no runs yet</td></tr>'
    doc = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(test)} — eval summary</title>
<style>{ROW_CSS}</style></head>
<body>
<h1>{html.escape(test)}</h1>
<p class="sub">Model-eval comparison &middot; {len(runs)} run(s) &middot; sorted by objective score, then decode speed</p>
<div class="wrap"><table>
  <thead><tr>
    <th class="l">Model / weights</th><th>Objective</th><th>MTP</th>
    <th>Output</th><th>Compl. tok</th><th>TTFT</th><th>Prefill t/s</th>
    <th>Decode t/s</th><th>Wall</th><th>Finish</th><th>Date</th><th class="l">Link</th>
  </tr></thead>
  <tbody>
{body}
  </tbody>
</table></div>
<p class="foot">Generated {html.escape(now)} by scripts/eval-summary.py.
TTFT / throughput are server-side llama.cpp timings (prefill = prompt_ms; decode = predicted_per_second).</p>
</body></html>
"""
    out_path = os.path.join(test_dir, "summary.html")
    with open(out_path, "w") as f:
        f.write(doc)
    return out_path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", help="test dir name under evals/")
    ap.add_argument("--all", action="store_true", help="rebuild every test")
    args = ap.parse_args()

    evals_root = os.path.join(REPO, "evals")
    if args.all:
        tests = [d for d in sorted(os.listdir(evals_root))
                 if os.path.isdir(os.path.join(evals_root, d, "outputs"))]
    elif args.test:
        tests = [args.test]
    else:
        ap.error("pass --test <name> or --all")

    for t in tests:
        p = build_summary(os.path.join(evals_root, t))
        print(f"✓ {os.path.relpath(p, REPO)}")


if __name__ == "__main__":
    main()
