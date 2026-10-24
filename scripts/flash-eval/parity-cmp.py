import json, sys
a = json.load(open(sys.argv[1])); b = json.load(open(sys.argv[2]))
tot = 0; rows = []
for x, y in zip(a, b):
    n = 0
    for c, d in zip(x["text"], y["text"]):
        if c != d: break
        n += 1
    full = n == len(x["text"]) == len(y["text"])
    rows.append((n, len(x["text"]), len(y["text"]), full)); tot += full
print("identical:", tot, "of", len(rows))
for r, x in zip(rows, a): print(r, x["prompt"][:50])
