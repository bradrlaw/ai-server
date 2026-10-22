#!/usr/bin/env python3
"""Paired raw-vs-parsed check for parallel tool calls: each request asks llama-server for
`verbose` output, which carries the raw generated text next to the parsed message, so the
number of <tool_call> blocks the model wrote can be compared with len(message.tool_calls)
for the SAME sample.

  parcheck2.py chat 12
"""
import collections, importlib.util, json, re, sys, urllib.request

spec = importlib.util.spec_from_file_location("tc", "/srv/ai/tmp/lcpp-test/toolcall-suite.py")
tc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tc)

PROMPTS = {"weather": "Check the weather in Tokyo (celsius) and in Denver (fahrenheit).",
           "time": "What time is it right now in America/New_York and in Europe/Berlin?"}
MARK = {"chat": r"<tool_call>", "coding": r"<tool_call>", "small": r"<\|tool_call>"}

model, n = sys.argv[1], int(sys.argv[2])
for name, text in PROMPTS.items():
    pairs = collections.Counter()
    for _ in range(n):
        body = {"model": model, "messages": [{"role": "user", "content": text}], "tools": tc.TOOLS,
                "max_tokens": 1500, "temperature": 0.6, "top_p": 0.95, "top_k": 20, "verbose": True}
        req = urllib.request.Request("http://127.0.0.1:9090/v1/chat/completions", json.dumps(body).encode(),
                                     {"Content-Type": "application/json"})
        r = json.loads(urllib.request.urlopen(req, timeout=300).read())
        raw = (r.get("__verbose") or {}).get("content")
        parsed = len(r["choices"][0]["message"].get("tool_calls") or [])
        pairs[(None if raw is None else len(re.findall(MARK[model], raw)), parsed)] += 1
    print(f"{model} {name}: (raw <tool_call> blocks, parsed tool_calls) -> count: {dict(pairs)}", flush=True)
