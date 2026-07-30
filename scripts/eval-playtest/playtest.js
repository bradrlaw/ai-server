#!/usr/bin/env node
/*
 * Best-effort headless playthrough harness for browser-game evals.
 *
 * eval-run.py / check.py verify that the required STRINGS are present in a
 * generated index.html; they cannot tell whether the game is actually PLAYABLE.
 * This loads each outputs/<label>/index.html in jsdom, drives a scripted session
 * defined in evals/<test>/playtest.json, and reports whether key milestones are
 * reachable (e.g. "pull the wall lever without taking it, then reach the Secret
 * Chamber"). Results are written to <out_dir>/playtest.json and a PLAYTEST.md
 * scoreboard — DELIBERATELY separate from the objective score in check.json, so
 * a driver that simply can't drive a given UI never lowers a real score.
 *
 * Usage:
 *   node scripts/eval-playtest/playtest.js --test local-dungeon-web
 *   node scripts/eval-playtest/playtest.js --test local-dungeon-web --labels coding,chat
 */
"use strict";
const fs = require("fs");
const path = require("path");
const { JSDOM, VirtualConsole } = require("jsdom");

const REPO = path.resolve(__dirname, "..", "..");
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// Palette matches scripts/eval-summary.py's summary.html for a consistent look.
const PLAYTEST_CSS = `
:root{--bg:#09090b;--surface:#13111c;--border:rgba(255,255,255,.08);
--accent:#7c5cfc;--teal:#00d4aa;--red:#f87171;--fg:#fafafa;--muted:#71717a}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.6 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;padding:32px;max-width:1100px;margin:auto}
h1{font-size:26px;margin:0 0 4px}h2{font-size:15px;margin:30px 0 8px}
.sub{color:var(--muted);margin:0 0 22px}.navcur{color:var(--fg);font-weight:600}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
code{background:var(--surface);border:1px solid var(--border);border-radius:5px;padding:1px 5px;font-size:13px}
.wrap{overflow-x:auto;border:1px solid var(--border);border-radius:12px}
table{border-collapse:collapse;width:100%}
th,td{padding:10px 14px;text-align:center;border-bottom:1px solid var(--border)}
th{background:var(--surface);color:var(--muted);font-weight:600;font-size:12px;
text-transform:uppercase;letter-spacing:.04em}
td.l,th.l{text-align:left}tr:last-child td{border-bottom:0}
tr:hover td{background:rgba(124,92,252,.06)}
.model{font-weight:600}.ok{color:var(--teal);font-weight:700}.no{color:var(--red);font-weight:700}
.na{color:var(--muted)}.run{background:var(--surface);border:1px solid var(--border);
border-radius:10px;padding:14px 18px;margin:10px 0}
.run h3{margin:0 0 8px;font-size:14px}.step{font-size:13px;margin:3px 0;color:#d4d4d8}
.step .cmd{color:var(--fg);font-family:ui-monospace,'Courier New',monospace}
.step .ex{color:var(--muted)}.step.bad .cmd{color:var(--red)}
.err{color:var(--red);font-size:12px;margin-top:6px}
.foot{color:var(--muted);font-size:12px;margin-top:22px}
`;

function parseArgs(argv) {
  const a = { test: null, labels: null };
  for (let i = 2; i < argv.length; i++) {
    if (argv[i] === "--test") a.test = argv[++i];
    else if (argv[i] === "--labels") a.labels = argv[++i].split(",").map((s) => s.trim());
  }
  if (!a.test) {
    console.error("usage: playtest.js --test <name> [--labels a,b]");
    process.exit(2);
  }
  return a;
}

const CONSOLE_SELECTORS = [
  "#console", "#output", "#game-console", "#game-output", "#terminal",
  ".console", ".terminal", ".output", ".game-console", ".game-log",
  '[class*="console"]', '[class*="terminal"]', '[class*="output"]',
  '[id*="console"]', '[id*="output"]', '[id*="terminal"]', '[id*="log"]',
];

function pickConsole(doc) {
  let best = doc.body, bestLen = -1;
  const seen = new Set();
  for (const sel of CONSOLE_SELECTORS) {
    let nodes;
    try { nodes = doc.querySelectorAll(sel); } catch { continue; }
    for (const el of nodes) {
      if (seen.has(el)) continue;
      seen.add(el);
      const len = (el.textContent || "").length;
      if (len > bestLen) { best = el; bestLen = len; }
    }
  }
  return best;
}

function pickInput(doc) {
  return doc.querySelector('input[type="text"]')
    || doc.querySelector("input:not([type])")
    || doc.querySelector('input[type="search"]')
    || doc.querySelector("input")
    || doc.querySelector("textarea")
    || doc.querySelector('[contenteditable="true"]');
}

function fireEnter(win, input) {
  for (const type of ["keydown", "keypress", "keyup"]) {
    const ev = new win.KeyboardEvent(type, {
      key: "Enter", code: "Enter", bubbles: true, cancelable: true,
    });
    try {
      Object.defineProperty(ev, "keyCode", { get: () => 13 });
      Object.defineProperty(ev, "which", { get: () => 13 });
    } catch { /* ignore */ }
    input.dispatchEvent(ev);
  }
}

function submitFallback(win, doc, input) {
  const form = input.form || input.closest("form");
  if (form) {
    try { form.dispatchEvent(new win.Event("submit", { bubbles: true, cancelable: true })); } catch { /* */ }
    if (typeof form.requestSubmit === "function") { try { form.requestSubmit(); } catch { /* */ } }
  }
  // A nearby submit button, if any.
  const btn = doc.querySelector('button[type="submit"], form button, .send, #send, [class*="send"]');
  if (btn) { try { btn.click(); } catch { /* */ } }
}

// Wait until the console text stops growing (typewriter finished) or maxWait.
// Some games type char-by-char at ~15-30ms and DROP any command issued while a
// prior line is still typing, so we require a fairly long quiet period (~600ms)
// before treating output as settled.
async function settle(getText, maxWait = 12000) {
  const start = Date.now();
  let last = getText(), stable = 0;
  while (Date.now() - start < maxWait) {
    await sleep(100);
    const now = getText();
    if (now.length === last.length) { if (++stable >= 6) break; }
    else { stable = 0; last = now; }
  }
  return getText();
}

function matchAny(text, needles) {
  const low = text.toLowerCase();
  return (needles || []).find((n) => low.includes(String(n).toLowerCase())) || null;
}

// A command was genuinely NOT understood (so an alternate phrasing is worth
// trying) only when the parser said so, or produced nothing at all. A real
// "you cannot go X from here" is a handled move attempt — don't retry, since the
// other phrasing would behave identically and could clobber a prior success.
function unrecognized(delta) {
  if (!delta || delta.trim().length === 0) return true;
  // "you can't go from here" / "specify a direction" mean the verb ran with no
  // usable direction (bare "north" parsed as a no-arg `go`), so an alternate
  // phrasing is worth trying. A real "cannot go <north> from here" (direction
  // present) is a handled dead-end — do NOT retry.
  if (/can'?t go\s+from here|can'?t go\s*\.|cannot go\s+from here/i.test(delta)) return true;
  return /unknown command|don't (understand|know)|not a (valid )?command|please specify|specify a direction|which direction|didn't understand/i.test(delta);
}

async function runScenario(html, scenarioRun) {
  const jsErrors = [];
  const vc = new VirtualConsole();
  vc.on("jsdomError", (e) => {
    const msg = (e && e.message) || String(e);
    if (!/Not implemented/i.test(msg)) jsErrors.push(msg.split("\n")[0]);
  });

  const dom = new JSDOM(html, {
    runScripts: "dangerously",
    pretendToBeVisual: true,
    url: "http://localhost/",
    virtualConsole: vc,
    beforeParse(win) {
      win.scrollTo = () => {};
      win.HTMLElement.prototype.scrollIntoView = () => {};
      win.alert = () => {};
      // jsdom stores `innerText` as a detached property that never reflects into
      // the DOM, so games that type output via `el.innerText += c` render nothing
      // observable. Proxy innerText -> textContent so those UIs are drivable.
      try {
        Object.defineProperty(win.HTMLElement.prototype, "innerText", {
          configurable: true,
          get() { return this.textContent; },
          set(v) { this.textContent = v; },
        });
      } catch { /* ignore */ }
      // Neutralise a full-page reload so a reset that (wrongly) calls it can't nuke the run.
      try { win.location.reload = () => {}; } catch { /* */ }
    },
  });
  const { window } = dom;
  const doc = window.document;

  try {
    await new Promise((res) => {
      if (doc.readyState === "complete") return res();
      window.addEventListener("load", res);
      setTimeout(res, 2500);
    });
    await sleep(300);

    const input = pickInput(doc);
    const getText = () => doc.body.textContent || "";

    // Console detection without hardcoded selectors: track every container that
    // ISN'T the input wrapper / script / sidebar, and treat the one that GROWS the
    // most after a command as the narrative log. This excludes static <script>
    // source and pre-rendered lore, and adapts to each game's markup.
    const candidates = Array.from(doc.querySelectorAll("div,section,main,pre,article,ul,ol,p"))
      .filter((el) => !el.querySelector("input,textarea,script,style")
        && !el.closest("aside,header,nav,footer"));
    const snapshot = () => new Map(candidates.map((el) => [el, (el.textContent || "").length]));
    const consoleDelta = (before) => {
      let bestEl = null, bestGrow = 0;
      for (const el of candidates) {
        const grow = (el.textContent || "").length - (before.get(el) || 0);
        if (grow > bestGrow) { bestGrow = grow; bestEl = el; }
      }
      if (!bestEl) return "";
      return (bestEl.textContent || "").slice(before.get(bestEl) || 0);
    };

    await settle(getText, 12000); // let the intro typewriter finish

    const steps = [];
    if (!input) {
      return { ok_input: false, steps, jsErrors, note: "no input element found" };
    }

    for (const step of scenarioRun.steps) {
      const sends = Array.isArray(step.send) ? step.send : [step.send];
      let delta = "", good = null, bad = null, used = sends[0];
      for (const s of sends) {
        const before = snapshot();
        input.value = s;
        input.focus && input.focus();
        fireEnter(window, input);
        await settle(getText, step.maxWait || 12000);
        delta = consoleDelta(before);
        if (delta.trim().length === 0) {
          submitFallback(window, doc, input);
          await settle(getText, step.maxWait || 12000);
          delta = consoleDelta(before);
        }
        good = matchAny(delta, step.expect);
        bad = matchAny(delta, step.expectNone);
        used = s;
        // Stop trying alternate phrasings once the command was handled at all —
        // only advance when the parser genuinely didn't recognise it. Otherwise a
        // successful move ("north" → Library) would be clobbered by re-sending the
        // other phrasing from the new room, where that direction is a dead end.
        if (!unrecognized(delta)) break;
      }
      const ok = (!step.expect || good !== null) && (!step.expectNone || bad === null);
      steps.push({
        label: step.label, send: used, ok,
        matched: good, hitNegative: bad,
        excerpt: delta.replace(/\s+/g, " ").trim().slice(0, 160),
      });
    }
    return { ok_input: true, steps, jsErrors };
  } finally {
    window.close();
  }
}

async function main() {
  const args = parseArgs(process.argv);
  const testDir = path.join(REPO, "evals", args.test);
  const scenarioPath = path.join(testDir, "playtest.json");
  if (!fs.existsSync(scenarioPath)) {
    console.error(`no scenario at ${scenarioPath} — add one to enable playtests`);
    process.exit(1);
  }
  const scenario = JSON.parse(fs.readFileSync(scenarioPath, "utf8"));
  const outRoot = path.join(testDir, "outputs");
  let labels = args.labels || (fs.existsSync(outRoot)
    ? fs.readdirSync(outRoot).filter((d) =>
      fs.existsSync(path.join(outRoot, d, "index.html"))) : []);
  labels = labels.sort();
  if (!labels.length) { console.error("no outputs/<label>/index.html found"); process.exit(1); }

  const summary = [];
  for (const label of labels) {
    const outDir = path.join(outRoot, label);
    const html = fs.readFileSync(path.join(outDir, "index.html"), "utf8");
    const runs = {};
    for (const run of scenario.runs) {
      process.stdout.write(`  ${label} :: ${run.name} … `);
      let res;
      try { res = await runScenario(html, run); }
      catch (e) { res = { ok_input: false, steps: [], jsErrors: [String(e).split("\n")[0]], note: "harness error" }; }
      const goal = run.goal_label
        ? res.steps.find((s) => s.label === run.goal_label)
        : res.steps[res.steps.length - 1];
      res.goal_reached = !!(goal && goal.ok);
      runs[run.name] = res;
      console.log(res.goal_reached ? "reached ✓" : "not reached ✗");
    }
    const report = {
      label, test: args.test,
      runs,
      generated_utc: new Date().toISOString(),
      note: "best-effort jsdom playthrough; NOT part of the objective check.json score",
    };
    fs.writeFileSync(path.join(outDir, "playtest.json"), JSON.stringify(report, null, 2));
    summary.push(report);
  }

  writeMarkdown(testDir, args.test, scenario, summary);
  writeHtml(testDir, args.test, scenario, summary);
  console.log(`\n✓ wrote per-output playtest.json + ${path.relative(REPO, path.join(testDir, "PLAYTEST.md"))} + PLAYTEST.html`);
}

function writeMarkdown(testDir, test, scenario, summary) {
  const runNames = scenario.runs.map((r) => r.name);
  const lines = [
    `# ${test} — playtest (headless jsdom)`,
    "",
    "**Generated by `scripts/eval-playtest/playtest.js` — do not edit by hand.**",
    "Best-effort playthrough of each generated game. This is a *playability* signal,",
    "**separate from and not folded into** the objective `check.py` score — a UI the",
    "driver can't drive shows `✗`/`—` here without affecting that score.",
    "",
    `| Model | ${runNames.map((n) => scenario.runs.find((r) => r.name === n).title || n).join(" | ")} | JS errors |`,
    `| --- | ${runNames.map(() => ":--:").join(" | ")} | ---: |`,
  ];
  for (const rep of summary) {
    const cells = runNames.map((n) => {
      const r = rep.runs[n];
      if (!r) return "—";
      if (!r.ok_input) return "n/a";
      return r.goal_reached ? "✓" : "✗";
    });
    const errs = new Set();
    for (const n of runNames) (rep.runs[n]?.jsErrors || []).forEach((e) => errs.add(e));
    lines.push(`| \`${rep.label}\` | ${cells.join(" | ")} | ${errs.size || 0} |`);
  }
  lines.push("", "## Per-run milestones", "");
  for (const rep of summary) {
    lines.push(`### \`${rep.label}\``);
    for (const n of runNames) {
      const r = rep.runs[n];
      if (!r) continue;
      const title = scenario.runs.find((x) => x.name === n).title || n;
      lines.push(`- **${title}** — ${r.goal_reached ? "goal reached ✓" : "goal NOT reached ✗"}${r.note ? ` (${r.note})` : ""}`);
      for (const s of r.steps) {
        const mark = s.ok ? "✓" : "✗";
        const neg = s.hitNegative ? ` [hit: "${s.hitNegative}"]` : "";
        lines.push(`  - ${mark} \`${s.send}\` → ${s.label}${neg}: ${s.excerpt || "(no output)"}`);
      }
      if (r.jsErrors && r.jsErrors.length) lines.push(`  - ⚠ JS errors: ${r.jsErrors.slice(0, 3).join("; ")}`);
    }
    lines.push("");
  }
  lines.push(`_Last generated ${new Date().toISOString()}._`, "");
  fs.writeFileSync(path.join(testDir, "PLAYTEST.md"), lines.join("\n"));
}

// Header nav linking sibling generated pages that exist on disk.
function navHtml(testDir, active) {
  const pages = [["summary.html", "scoreboard"], ["RESULTS.html", "results"],
    ["PLAYTEST.html", "playtest"]];
  return pages
    .filter(([fn]) => fn === active || fs.existsSync(path.join(testDir, fn)))
    .map(([fn, lbl]) => fn === active
      ? `<span class="navcur">${lbl}</span>`
      : `<a href="${fn}">${lbl}</a>`)
    .join(" &middot; ");
}

function writeHtml(testDir, test, scenario, summary) {
  const runNames = scenario.runs.map((r) => r.name);
  const titleOf = (n) => scenario.runs.find((r) => r.name === n).title || n;

  const headCells = runNames.map((n) => `<th>${escapeHtml(titleOf(n))}</th>`).join("");
  const rows = summary.map((rep) => {
    const cells = runNames.map((n) => {
      const r = rep.runs[n];
      if (!r) return '<td><span class="na">—</span></td>';
      if (!r.ok_input) return '<td><span class="na">n/a</span></td>';
      return r.goal_reached
        ? '<td><span class="ok">✓</span></td>'
        : '<td><span class="no">✗</span></td>';
    }).join("");
    const errs = new Set();
    for (const n of runNames) (rep.runs[n]?.jsErrors || []).forEach((e) => errs.add(e));
    return `    <tr><td class="l"><span class="model">${escapeHtml(rep.label)}</span></td>`
      + `${cells}<td>${errs.size || 0}</td></tr>`;
  }).join("\n");

  const milestones = summary.map((rep) => {
    const runs = runNames.map((n) => {
      const r = rep.runs[n];
      if (!r) return "";
      const head = `<h3>${escapeHtml(titleOf(n))} — `
        + (r.goal_reached ? '<span class="ok">goal reached ✓</span>' : '<span class="no">goal NOT reached ✗</span>')
        + (r.note ? ` <span class="ex">(${escapeHtml(r.note)})</span>` : "") + "</h3>";
      const steps = (r.steps || []).map((s) => {
        const mark = s.ok ? '<span class="ok">✓</span>' : '<span class="no">✗</span>';
        const neg = s.hitNegative ? ` <span class="ex">[hit: "${escapeHtml(s.hitNegative)}"]</span>` : "";
        return `<div class="step${s.ok ? "" : " bad"}">${mark} `
          + `<span class="cmd">${escapeHtml(s.send)}</span> → ${escapeHtml(s.label)}${neg}`
          + `: <span class="ex">${escapeHtml(s.excerpt || "(no output)")}</span></div>`;
      }).join("\n      ");
      const err = (r.jsErrors && r.jsErrors.length)
        ? `\n      <div class="err">⚠ JS errors: ${escapeHtml(r.jsErrors.slice(0, 3).join("; "))}</div>` : "";
      return `      ${head}\n      ${steps}${err}`;
    }).filter(Boolean).join("\n");
    return `  <div class="run">\n    <h2 style="margin-top:0"><code>${escapeHtml(rep.label)}</code></h2>\n${runs}\n  </div>`;
  }).join("\n");

  const nav = navHtml(testDir, "PLAYTEST.html");
  const doc = `<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>${escapeHtml(test)} — playtest</title>
<style>${PLAYTEST_CSS}</style></head>
<body>
<h1>${escapeHtml(test)} — playtest</h1>
<p class="sub">${nav}<br>
Best-effort headless <a href="https://github.com/jsdom/jsdom">jsdom</a> playthrough — a
<b>playability</b> signal, <b>separate from and not folded into</b> the objective
<code>check.py</code> score. A UI the driver can't drive shows ✗/— here without
affecting that score.</p>

<div class="wrap"><table>
  <thead><tr><th class="l">Model</th>${headCells}<th>JS errors</th></tr></thead>
  <tbody>
${rows}
  </tbody>
</table></div>

<h2>Per-run milestones</h2>
${milestones}

<p class="foot">Generated ${escapeHtml(new Date().toISOString())} by scripts/eval-playtest/playtest.js.</p>
</body></html>
`;
  fs.writeFileSync(path.join(testDir, "PLAYTEST.html"), doc);
}

main().catch((e) => { console.error(e); process.exit(1); });
