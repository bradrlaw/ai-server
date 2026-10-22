#!/usr/bin/env python3
"""Spot-check how often each slot calls the `calculator` tool for "Double the number 21.",
by tool_choice style. Reuses post()/grade() from toolcall-suite.py.

  named     {"type":"function","function":{"name":"calculator"}}  (llama.cpp ignores this object form:
            json_value() falls back to "auto" on a wrong-typed field, so this is just a model-behaviour rate)
  required  "required"                                            (honoured: grammar forces a tool call)
  auto      default

  toolchoice-spot.py --models coding chat small --n 10
"""
import argparse, importlib.util

spec = importlib.util.spec_from_file_location("tc", "/srv/ai/tmp/lcpp-test/toolcall-suite.py")
tc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tc)

STYLES = {
    "named": {"type": "function", "function": {"name": "calculator"}},
    "required": "required",
    "auto": None,
}

ap = argparse.ArgumentParser()
ap.add_argument("--models", nargs="+", default=["coding", "chat", "small"])
ap.add_argument("--n", type=int, default=10)
a = ap.parse_args()

print(f"{'model':12s} " + " ".join(f"{s:>9s}" for s in STYLES))
for m in a.models:
    cells = []
    for style, choice in STYLES.items():
        ok = 0
        for _ in range(a.n):
            status, resp, _dt = tc.post(m, [{"role": "user", "content": "Double the number 21."}], tool_choice=choice)
            ok += status == 200 and tc.grade(["calculator"], None, status, resp) is None
        cells.append(f"{ok}/{a.n}")
    print(f"{m:12s} " + " ".join(f"{c:>9s}" for c in cells), flush=True)
