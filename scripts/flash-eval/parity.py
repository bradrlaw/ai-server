#!/usr/bin/env python3
"""parity.py <url> <model> <out.json>: 20 greedy prompts x 128 tokens, thinking off."""
import json, sys, urllib.request
url, model, out = sys.argv[1:4]
P = ["Explain what a hash map is in two sentences.", "Write a Python function that returns the nth Fibonacci number.",
 "List the planets of the solar system in order from the sun.", "What is the capital of Australia, and why is it not Sydney?",
 "Translate 'Good morning, how are you?' into French, Spanish and German.", "Write a haiku about winter.",
 "Solve: 17 * 23 + 49. Show the steps.", "Explain the difference between TCP and UDP briefly.",
 "Write a SQL query selecting the top 5 customers by total order value.", "Summarize the plot of Romeo and Juliet in three sentences.",
 "What does the Linux command 'chmod 755 file' do?", "Give three tips for writing readable code.",
 "Write a JavaScript function that debounces another function.", "Why is the sky blue?",
 "Describe the water cycle.", "Write a bash one-liner that counts lines in all .py files in a directory tree.",
 "What is the time complexity of binary search and why?", "Explain recursion to a ten year old.",
 "Name five common HTTP status codes and their meanings.", "Write a regex that matches a US ZIP code."]
res = []
for p in P:
    b = {"model": model, "messages": [{"role": "user", "content": p}], "temperature": 0, "max_tokens": 128,
         "chat_template_kwargs": {"enable_thinking": False}}
    r = urllib.request.Request(url, json.dumps(b).encode(), {"Content-Type": "application/json"})
    d = json.load(urllib.request.urlopen(r, timeout=900))
    res.append({"prompt": p, "text": d["choices"][0]["message"].get("content") or ""})
json.dump(res, open(out, "w"), indent=1)
