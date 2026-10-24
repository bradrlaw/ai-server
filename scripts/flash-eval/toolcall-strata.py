#!/usr/bin/env python3
"""Tool-call regression suite for llama-swap slots (throwaway, untracked).

Sends function-calling requests straight to llama-swap (:9090, no auth) and checks that every
reply is a well-formed tool call (or a plain answer when no tool is needed): HTTP 200, the right
function name(s), JSON-parseable arguments that satisfy the schema, no template markup leaking
into `content`. Run it on the old build, then on the new build, and diff the pass counts.

  toolcall-suite.py --models coding chat small --repeat 2 --out /tmp/tc-A.json
"""
import argparse, concurrent.futures as cf, json, re, sys, time, urllib.error, urllib.request

URL = "http://127.0.0.1:9091/v1/chat/completions"


def fn(name, desc, props, required):
    return {"type": "function", "function": {"name": name, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": required}}}


S = {"type": "string"}
I = {"type": "integer"}
B = {"type": "boolean"}
TOOLS = [
    fn("read_file", "Read a text file, optionally a line range.",
       {"path": S, "start_line": I, "end_line": I}, ["path"]),
    fn("write_file", "Write text content to a file.", {"path": S, "content": S}, ["path", "content"]),
    fn("run_shell", "Run a shell command.", {"command": S, "timeout_s": I, "cwd": S}, ["command"]),
    fn("web_search", "Search the web.",
       {"query": S, "max_results": I, "recency": {"type": "string", "enum": ["day", "week", "month", "any"]}},
       ["query"]),
    fn("get_weather", "Current weather for a location.",
       {"location": S, "unit": {"type": "string", "enum": ["celsius", "fahrenheit"]}}, ["location", "unit"]),
    fn("create_event", "Create a calendar event.",
       {"title": S, "start": S, "duration_min": I, "attendees": {"type": "array", "items": S},
        "reminders": {"type": "array", "items": {"type": "object", "properties": {
            "minutes_before": I, "method": {"type": "string", "enum": ["popup", "email"]}},
            "required": ["minutes_before", "method"]}},
        "all_day": B},
       ["title", "start", "duration_min", "attendees"]),
    fn("calculator", "Evaluate an arithmetic expression.", {"expression": S}, ["expression"]),
    fn("send_message", "Send a chat message.",
       {"to": S, "body": S, "priority": {"type": "string", "enum": ["low", "normal", "high"]}}, ["to", "body"]),
    fn("search_repo", "Search the repository with a regex.",
       {"pattern": S, "glob": S, "case_sensitive": B}, ["pattern"]),
    fn("get_time", "Current time in an IANA timezone.", {"timezone": S}, ["timezone"]),
]
SCHEMAS = {t["function"]["name"]: t["function"]["parameters"] for t in TOOLS}
TYPES = {"string": str, "integer": int, "boolean": bool, "array": list, "object": dict}


def validate(schema, val, path="args"):
    """Minimal JSON-schema check: type, required, enum, nested items/properties."""
    t = schema.get("type")
    if t:
        py = TYPES[t]
        if not isinstance(val, py) or (t == "integer" and isinstance(val, bool)):
            return f"{path}: expected {t}, got {type(val).__name__}"
    if "enum" in schema and val not in schema["enum"]:
        return f"{path}: {val!r} not in {schema['enum']}"
    if t == "object":
        for k in schema.get("required", []):
            if k not in val:
                return f"{path}: missing required '{k}'"
        for k, v in val.items():
            if k in schema.get("properties", {}):
                e = validate(schema["properties"][k], v, f"{path}.{k}")
                if e:
                    return e
    if t == "array":
        for i, v in enumerate(val):
            e = validate(schema.get("items", {}), v, f"{path}[{i}]")
            if e:
                return e
    return None


def has(sub):
    return lambda a: sub.lower() in json.dumps(a).lower()


def eq(key, want):
    return lambda a: a.get(key) == want


def allof(*fs):
    return lambda a: all(f(a) for f in fs)


# (id, user prompt, expected tool names (multiset) or None for "no tool", extra arg check per call or None)
CASES = [
    ("weather", "What's the weather in Paris right now? Use celsius.", ["get_weather"],
     allof(has("paris"), eq("unit", "celsius"))),
    ("read_range", "Show me lines 10 to 40 of /etc/hosts.", ["read_file"],
     allof(eq("path", "/etc/hosts"), eq("start_line", 10), eq("end_line", 40))),
    ("shell_timeout", "Run `ls -la /var/log` and give it a 30 second timeout.", ["run_shell"],
     allof(has("ls"), eq("timeout_s", 30))),
    ("event_nested", "Schedule a 45 minute meeting titled 'Sprint review' starting 2026-11-03T15:00:00Z with "
     "alice@example.com and bob@example.com, and remind everyone by email 15 minutes before.", ["create_event"],
     lambda a: len(a.get("attendees", [])) == 2 and a.get("duration_min") == 45
     and any(r.get("method") == "email" and r.get("minutes_before") == 15 for r in a.get("reminders", []))),
    ("calc", "What is 1234 * 5678 + 91? Use the calculator.", ["calculator"], has("1234")),
    ("search_opts", "Search the web for the latest llama.cpp release, only the last week, at most 3 results.",
     ["web_search"], allof(eq("max_results", 3), eq("recency", "week"))),
    ("repo_grep", "Find every TODO comment in the python files of the repo, ignoring case.", ["search_repo"],
     allof(has("todo"), eq("case_sensitive", False))),
    ("parallel_weather", "Check the weather in Tokyo (celsius) and in Denver (fahrenheit).",
     ["get_weather", "get_weather"], None),
    ("parallel_time", "What time is it right now in America/New_York and in Europe/Berlin?",
     ["get_time", "get_time"], None),
    ("write_escapes", 'Write a file /tmp/hello.txt containing exactly this text (two lines): '
     'Hello, "quoted" world!\\nSecond line with a backslash \\\\ and a tab.', ["write_file"],
     lambda a: a.get("path") == "/tmp/hello.txt" and '"quoted"' in a.get("content", "")),
    ("unicode_url", "Send a high priority message to carol saying: Build is green \u2705 "
     "(see https://ci.example.com/run/42?x=1&y=2)", ["send_message"],
     allof(eq("priority", "high"), has("run/42"))),
    ("no_tool_1", "Explain in two sentences what a mutex is.", None, None),
    ("no_tool_2", "Say hi in five words or fewer.", None, None),
]

LEAKS = re.compile(r"<tool_call>|</tool_call>|<function=|<parameter=|<think>|</think>|<\|im_start\|>")


def post(model, messages, tools=True, tool_choice=None, max_tokens=4096, timeout=900):
    body = {"model": model, "messages": messages, "max_tokens": max_tokens,
            "temperature": 0.6, "top_p": 0.95, "top_k": 20}
    if tools:
        body["tools"] = TOOLS
    if tool_choice:
        body["tool_choice"] = tool_choice
    req = urllib.request.Request(URL, json.dumps(body).encode(), {"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read()), time.time() - t0
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode()[:300]}, time.time() - t0
    except Exception as e:  # noqa: BLE001 - report any transport failure as a failed case
        return 0, {"error": repr(e)[:300]}, time.time() - t0


def grade(expected, check, status, resp):
    if status != 200:
        return f"HTTP {status}: {resp.get('error', '')[:150]}"
    ch = resp["choices"][0]
    msg = ch["message"]
    content = msg.get("content") or ""
    calls = msg.get("tool_calls") or []
    if LEAKS.search(content):
        return f"template markup leaked into content: {LEAKS.search(content).group(0)!r}"
    if expected is None:
        if calls:
            return f"unexpected tool call {[c['function']['name'] for c in calls]}"
        return None if content.strip() else "empty content"
    names = sorted(c["function"]["name"] for c in calls)
    if names != sorted(expected):
        return f"tool calls {names} != expected {sorted(expected)} (finish={ch.get('finish_reason')})"
    if ch.get("finish_reason") != "tool_calls":
        return f"finish_reason={ch.get('finish_reason')}"
    for c in calls:
        try:
            args = json.loads(c["function"]["arguments"])
        except Exception as e:  # noqa: BLE001
            return f"arguments not JSON: {e}: {c['function']['arguments'][:100]!r}"
        err = validate(SCHEMAS[c["function"]["name"]], args)
        if err:
            return err
        if check and not check(args):
            return f"argument check failed: {json.dumps(args)[:200]}"
    return None


def run_case(model, case, rep):
    cid, prompt, expected, check = case
    status, resp, dt = post(model, [{"role": "user", "content": prompt}])
    why = grade(expected, check, status, resp)
    u = resp.get("usage", {}) if status == 200 else {}
    return {"model": model, "case": cid, "rep": rep, "ok": why is None, "why": why,
            "sec": round(dt, 1), "tokens": u.get("completion_tokens")}


def run_roundtrip(model, rep):
    """tool result -> final answer (no further tool call, no leaked markup)."""
    call = {"id": "call_1", "type": "function",
            "function": {"name": "get_weather", "arguments": json.dumps({"location": "Oslo", "unit": "celsius"})}}
    msgs = [{"role": "user", "content": "What's the weather in Oslo (celsius)? Answer in one sentence."},
            {"role": "assistant", "content": None, "tool_calls": [call]},
            {"role": "tool", "tool_call_id": "call_1", "content": json.dumps({"temp": 7, "sky": "light rain"})}]
    status, resp, dt = post(model, msgs)
    why = grade(None, None, status, resp)
    if why is None and "7" not in (resp["choices"][0]["message"].get("content") or ""):
        why = "final answer does not mention the tool result (7)"
    u = resp.get("usage", {}) if status == 200 else {}
    return {"model": model, "case": "roundtrip", "rep": rep, "ok": why is None, "why": why,
            "sec": round(dt, 1), "tokens": u.get("completion_tokens")}


def run_forced(model, rep):
    # llama.cpp ignores the object form of tool_choice (json_value() falls back to "auto" when the
    # field is not a string), so this case measures the model's own willingness to call the tool,
    # not the server. Use tool_choice="required" to force a call (see toolchoice-spot.py).
    status, resp, dt = post(model, [{"role": "user", "content": "Double the number 21."}],
                            tool_choice={"type": "function", "function": {"name": "calculator"}})
    why = grade(["calculator"], None, status, resp)
    u = resp.get("usage", {}) if status == 200 else {}
    return {"model": model, "case": "forced_choice", "rep": rep, "ok": why is None, "why": why,
            "sec": round(dt, 1), "tokens": u.get("completion_tokens")}


def run_model(model, repeat):
    out = []
    for rep in range(repeat):
        for case in CASES:
            out.append(run_case(model, case, rep))
        out.append(run_roundtrip(model, rep))
        out.append(run_forced(model, rep))
        print(f"  {model} rep {rep + 1}/{repeat} done: {sum(r['ok'] for r in out)}/{len(out)} ok", flush=True)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="+", default=["coding", "chat", "small"])
    ap.add_argument("--repeat", type=int, default=2)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    results = []
    with cf.ThreadPoolExecutor(len(a.models)) as ex:
        for fut in [ex.submit(run_model, m, a.repeat) for m in a.models]:
            results += fut.result()
    json.dump(results, open(a.out, "w"), indent=1)
    print("\nmodel      ok/total  mean s  failures")
    bad = 0
    for m in a.models:
        rs = [r for r in results if r["model"] == m]
        ok = sum(r["ok"] for r in rs)
        bad += len(rs) - ok
        print(f"{m:10s} {ok:3d}/{len(rs):<3d}  {sum(r['sec'] for r in rs) / len(rs):6.1f}")
        for r in rs:
            if not r["ok"]:
                print(f"    FAIL {r['case']} (rep {r['rep']}): {r['why']}")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
