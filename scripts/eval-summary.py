#!/usr/bin/env python3
"""Rebuild the comparison summary + scoreboard for an eval test from its runs.

Scans evals/<test>/outputs/*/ (meta.json, check.json, scores.json) and writes:
  - evals/<test>/summary.html — sortable comparison table (objective score, manual
    design subtotal, output size, TTFT, prefill/decode throughput, MTP, wall time),
    plus a refreshed run.html per model.
  - evals/<test>/RESULTS.md   — a markdown scoreboard merging the automated
    objective/perf metrics with the manual design scores.

Objective scores come from check.py; design scores are hand-entered per run in
outputs/<label>/scores.json (five 0–5 axes + notes) — a blank stub is created
automatically for any run that lacks one. summary.html / RESULTS.md are generated
artifacts: edit scores.json, not them.

eval-run.py calls build_summary() after every run so both stay current; run this
script directly to rebuild from scratch:

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


# Manual design-quality axes (0-5 each). Scored by a human in each output dir's
# scores.json; absent/None = unscored. Keys map to the rubric in the test README.
# These are the DEFAULT axes (fit the landing-page tests). A test can override
# them with its own <test_dir>/rubric.json — see load_axes().
DEFAULT_AXES = [
    ("visual_polish", "Visual polish"),
    ("responsiveness", "Responsiveness"),
    ("interaction", "Interaction"),
    ("hero_visual", "Hero visual"),
    ("code_quality", "Code quality"),
]
AXIS_MAX = 5


def load_axes(test_dir):
    """Return the manual-scoring axes as a list of (key, label) tuples for a test.

    A test may define its own rubric in <test_dir>/rubric.json:
        {"axes": [["correctness", "Correctness"], ["rigor", "Rigor"], ...]}
    Falls back to DEFAULT_AXES (the landing-page rubric) when absent/invalid."""
    spec = _load(os.path.join(test_dir, "rubric.json"))
    axes = (spec or {}).get("axes") if isinstance(spec, dict) else None
    if not axes:
        return list(DEFAULT_AXES)
    out = []
    for a in axes:
        if isinstance(a, (list, tuple)) and len(a) == 2:
            out.append((str(a[0]), str(a[1])))
        elif isinstance(a, dict) and "key" in a:
            out.append((str(a["key"]), str(a.get("label", a["key"]))))
    return out or list(DEFAULT_AXES)


def design_max(axes):
    return len(axes) * AXIS_MAX


def design_subtotal(scores, axes):
    """Return (subtotal, n_scored). A run counts as scored only when every axis
    has a numeric value, so partial scoring never masquerades as a real total."""
    if not scores:
        return None, 0
    vals = [scores.get(k) for k, _ in axes]
    nums = [v for v in vals if isinstance(v, (int, float))]
    if len(nums) != len(axes):
        return None, len(nums)
    return sum(nums), len(nums)


def score_template(axes):
    d = {k: None for k, _ in axes}
    d["notes"] = ""
    return d


def ensure_score_stub(out_dir, axes):
    """Create a blank scores.json in a run dir if none exists (so it's obvious
    what to fill in). Never overwrites an existing one."""
    p = os.path.join(out_dir, "scores.json")
    if not os.path.exists(p):
        with open(p, "w") as f:
            json.dump(score_template(axes), f, indent=2)
    return p


def collect_runs(test_dir, axes):
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
        ensure_score_stub(d, axes)
        meta["_scores"] = _load(os.path.join(d, "scores.json"))
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


def write_run_html(out_dir, meta, axes):
    """(Re)generate outputs/<label>/run.html from meta.json (+ its attached _check)."""
    perf = meta.get("performance") or {}
    mtp = meta.get("mtp") or {}
    usage = meta.get("usage") or {}
    chk = meta.get("_check") or {}
    out_file = meta.get("output_file", "index.html")
    is_html = out_file.rsplit(".", 1)[-1].lower() in ("html", "htm")
    dmax = design_max(axes)

    def num(v, nd=1, s=""):
        return "—" if v is None else (f"{v:.{nd}f}{s}" if isinstance(v, float) else f"{v}{s}")

    mtp_pill = ('<span class="pill mtp-on">on</span>' if mtp.get("enabled")
                else '<span class="pill mtp-off">off</span>')
    if mtp.get("enabled") and mtp.get("accept_rate") is not None:
        mtp_pill += f' {mtp["accept_rate"]*100:.0f}% accept'

    cards = [
        _card("Objective", f'{chk.get("score")}/{chk.get("max")}' if chk.get("max") else "—", teal=True),
        _card("Design", (f'{sub}/{dmax}'
                         if (sub := design_subtotal(meta.get("_scores"), axes)[0]) is not None
                         else "unscored"), teal=True),
        _card("TTFT", num(perf.get("ttft_ms"), 0, " ms")),
        _card("Prefill", num(perf.get("prefill_tps"), 1, " t/s")),
        _card("Decode", num(perf.get("decode_tps"), 1, " t/s"), teal=True),
        _card("Wall time", num(meta.get("wall_secs"), 1, " s")),
        _card("Completion tok", num(usage.get("completion_tokens"))),
        _card("Output size", num(meta.get("output_bytes"), 0, " B")),
        _card("MTP", mtp_pill),
        _card("Finish", html.escape(str(meta.get("finish_reason")))),
    ]

    scores = meta.get("_scores") or {}
    axis_cards = [
        _card(lbl, (f'{scores.get(k)}/{AXIS_MAX}'
                    if isinstance(scores.get(k), (int, float)) else "—"))
        for k, lbl in axes
    ]
    notes = (scores.get("notes") or "").strip()
    design_section = (
        f'<h2>Design scores (manual, 0–{AXIS_MAX} each)</h2>\n'
        f'<div class="grid">\n{os.linesep.join(axis_cards)}\n</div>\n'
        + (f'<pre>{html.escape(notes)}</pre>\n' if notes else ""))

    preview = ""
    if is_html:
        preview = (f'<h2>Rendered output</h2>\n'
                   f'<iframe class="frame" src="{html.escape(out_file)}" '
                   f'title="rendered output"></iframe>\n')
    else:
        # Non-HTML output (code, prose): show the extracted answer inline as text.
        try:
            with open(os.path.join(out_dir, out_file), encoding="utf-8",
                      errors="replace") as f:
                body_txt = f.read()
            preview = (f'<h2>Output ({html.escape(out_file)})</h2>\n'
                       f'<pre>{html.escape(body_txt)}</pre>\n')
        except OSError:
            preview = ""

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
{design_section}
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


def build_results_md(test_dir, runs_sorted, now, axes):
    """Write <test_dir>/RESULTS.md — a markdown scoreboard merging the automated
    objective/perf metrics with the manual design scores from each scores.json.
    Generated file: edit scores.json (not this) and rerun to refresh."""
    test = os.path.basename(test_dir.rstrip("/"))
    dmax = design_max(axes)
    axis_hdr = " | ".join(lbl for _, lbl in axes)
    axis_sep = " | ".join("---:" for _ in axes)

    lines = [
        f"# {test} — results scoreboard",
        "",
        "**Generated by `scripts/eval-summary.py` — do not edit by hand.**",
        "To record design scores, edit each run's `outputs/<label>/scores.json`",
        f"(axes 0–{AXIS_MAX} each) and rerun `scripts/eval-summary.py --test {test}`.",
        "",
        "## Objective + performance (automated)",
        "",
        "| Model | Weights | Objective | Decode t/s | TTFT | Compl. tok | Output | MTP | Finish |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | :--: | :--: |",
    ]
    for m in runs_sorted:
        chk = m.get("_check") or {}
        perf = m.get("performance") or {}
        mtp = m.get("mtp") or {}
        usage = m.get("usage") or {}
        obj = f'{chk["score"]}/{chk["max"]}' if chk.get("max") else "—"
        dec = _fmt(perf.get("decode_tps"), 1)
        ttft = _fmt(perf.get("ttft_ms"), 0, " ms")
        mtp_s = "on" if mtp.get("enabled") else "off"
        if mtp.get("enabled") and mtp.get("accept_rate") is not None:
            mtp_s += f' {mtp["accept_rate"]*100:.0f}%'
        lines.append(
            f'| `{m.get("model_slot") or m["_label"]}` | {m.get("model_name") or "—"} '
            f'| {obj} | {dec} | {ttft} | {_fmt(usage.get("completion_tokens"))} '
            f'| {_fmt(m.get("output_bytes"))} B | {mtp_s} | {m.get("finish_reason")} |')

    lines += [
        "",
        f"## Design quality (manual, 0–{AXIS_MAX} each; subtotal /{dmax})",
        "",
        f"| Model | {axis_hdr} | Design | Notes |",
        f"| --- | {axis_sep} | ---: | --- |",
    ]
    for m in runs_sorted:
        scores = m.get("_scores") or {}
        cells = []
        for k, _ in axes:
            v = scores.get(k)
            cells.append(str(v) if isinstance(v, (int, float)) else "·")
        sub, _ = design_subtotal(scores, axes)
        sub_s = f"{sub}/{dmax}" if sub is not None else "—"
        note = (scores.get("notes") or "").replace("|", "\\|").replace("\n", " ").strip()
        lines.append(
            f'| `{m.get("model_slot") or m["_label"]}` | ' + " | ".join(cells)
            + f' | {sub_s} | {note} |')

    lines += [
        "",
        "Axes (see this test's `README.md` for what each measures): "
        + ", ".join(lbl for _, lbl in axes) + ".",
        "`·` = not yet scored.",
        "",
        f"_Last generated {now}._",
        "",
    ]
    out_path = os.path.join(test_dir, "RESULTS.md")
    with open(out_path, "w") as f:
        f.write("\n".join(lines))
    return out_path


def build_summary(test_dir):
    """Regenerate <test_dir>/summary.html (and every outputs/<label>/run.html) from
    all recorded runs on disk. Returns the summary path."""
    test = os.path.basename(test_dir.rstrip("/"))
    axes = load_axes(test_dir)
    dmax = design_max(axes)
    runs = collect_runs(test_dir, axes)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # refresh each run's own page from its meta.json (+ latest check.json)
    for m in runs:
        write_run_html(os.path.join(test_dir, "outputs", m["_label"]), m, axes)

    rows = []
    # sort: highest objective score first, then design subtotal, then decode speed
    def sort_key(m):
        c = m.get("_check") or {}
        score = c.get("score", -1)
        sub, _ = design_subtotal(m.get("_scores"), axes)
        perf = m.get("performance") or {}
        return (-score, -(sub if sub is not None else -1), -(perf.get("decode_tps") or 0))
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

        sub, _ = design_subtotal(m.get("_scores"), axes)
        design_cell = (f'<span class="score">{sub}/{dmax}</span>'
                       if sub is not None else '<span class="name">—</span>')

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
      <td>{design_cell}</td>
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

    body = "\n".join(rows) if rows else '    <tr><td class="l" colspan="13">no runs yet</td></tr>'
    doc = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(test)} — eval summary</title>
<style>{ROW_CSS}</style></head>
<body>
<h1>{html.escape(test)}</h1>
<p class="sub">Model-eval comparison &middot; {len(runs)} run(s) &middot; sorted by objective score, then design, then decode speed</p>
<div class="wrap"><table>
  <thead><tr>
    <th class="l">Model / weights</th><th>Objective</th><th>Design</th><th>MTP</th>
    <th>Output</th><th>Compl. tok</th><th>TTFT</th><th>Prefill t/s</th>
    <th>Decode t/s</th><th>Wall</th><th>Finish</th><th>Date</th><th class="l">Link</th>
  </tr></thead>
  <tbody>
{body}
  </tbody>
</table></div>
<p class="foot">Generated {html.escape(now)} by scripts/eval-summary.py.
Objective = automated check.py. Design = manual 0–{AXIS_MAX} axes from each run's scores.json (subtotal /{dmax}; “—” = unscored) — see RESULTS.md for the breakdown.
TTFT / throughput are server-side llama.cpp timings (prefill = prompt_ms; decode = predicted_per_second).</p>
</body></html>
"""
    out_path = os.path.join(test_dir, "summary.html")
    with open(out_path, "w") as f:
        f.write(doc)
    build_results_md(test_dir, sorted(runs, key=sort_key), now, axes)
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
