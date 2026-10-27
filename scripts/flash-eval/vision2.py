#!/usr/bin/env python3
"""vision2.py <chat-url> <model> <out.json>: 6 image-understanding checks, temp 0."""
import base64, io, json, sys, urllib.request
from PIL import Image, ImageDraw, ImageFont
url, model, out = sys.argv[1:4]
F = ImageFont.load_default(size=48)
def b64(im):
    b = io.BytesIO(); im.save(b, "PNG"); return "data:image/png;base64," + base64.b64encode(b.getvalue()).decode()
def red(): return Image.new("RGB", (256, 256), (220, 20, 20))
def blue(): return Image.new("RGB", (256, 256), (20, 40, 220))
def text():
    im = Image.new("RGB", (640, 200), "white"); d = ImageDraw.Draw(im); d.text((20, 30), "INVOICE 4417", fill="black", font=F); d.text((20, 105), "TOTAL $82.50", fill="black", font=F); return im
def shapes():
    im = Image.new("RGB", (512, 256), "white"); d = ImageDraw.Draw(im)
    d.ellipse((30, 60, 130, 160), fill=(20, 60, 220)); d.ellipse((190, 60, 290, 160), fill=(20, 60, 220)); d.rectangle((350, 60, 450, 160), fill=(20, 160, 40)); return im
def chart():
    im = Image.new("RGB", (480, 320), "white"); d = ImageDraw.Draw(im)
    for i, (n, h) in enumerate([("A", 90), ("B", 220), ("C", 140)]):
        x = 60 + i * 140; d.rectangle((x, 290 - h, x + 80, 290), fill=(80, 80, 80)); d.text((x + 28, 292), n, fill="black", font=ImageFont.load_default(size=24))
    return im
def img(i): return {"type": "image_url", "image_url": {"url": b64(i)}}
def T(s): return {"type": "text", "text": s}
CORPUS = open("/srv/ai/var/flash-eval/corpus-frozen-20261023.txt", errors="replace").read()
long_txt = CORPUS[:150000][:75000] + "\nThe passphrase for the blue door is TANGERINE-4471.\n" + CORPUS[75000:150000]
CASES = [
 ("color", [T("What single color is this image? One word."), img(red())], lambda t: "red" in t.lower()),
 ("ocr", [T("Read the text in this image exactly."), img(text())], lambda t: "4417" in t and "82.50" in t),
 ("count", [T("How many circles are there, and what color are they?"), img(shapes())], lambda t: ("2" in t or "two" in t.lower()) and "blue" in t.lower()),
 ("chart", [T("Which bar in this chart is the tallest? Answer with the letter."), img(chart())], lambda t: "B" in t.split()[0:3] or t.strip().startswith("B") or "bar B" in t or "**B**" in t),
 ("two-images", [T("What are the dominant colors of these two images, in order?"), img(red()), img(blue())], lambda t: "red" in t.lower() and "blue" in t.lower()),
 ("long-ctx", [T(long_txt + "\n\nQuestion 1: what is the passphrase for the blue door? Question 2: what color is the attached image? Answer both briefly."), img(red())],
  lambda t: "TANGERINE-4471" in t and "red" in t.lower()),
]
res = []
for name, content, chk in CASES:
    b = {"model": model, "messages": [{"role": "user", "content": content}], "temperature": 0, "max_tokens": 400,
         "chat_template_kwargs": {"enable_thinking": False}}
    try:
        import time; t0 = time.time()
        r = urllib.request.Request(url, json.dumps(b).encode(), {"Content-Type": "application/json"})
        d = json.load(urllib.request.urlopen(r, timeout=1800)); txt = d["choices"][0]["message"].get("content") or ""
        res.append({"case": name, "pass": bool(chk(txt)), "sec": round(time.time() - t0, 1), "answer": txt[:300], "ptok": d.get("usage", {}).get("prompt_tokens")})
    except Exception as e:
        res.append({"case": name, "pass": False, "error": str(e)[:300]})
    print(res[-1], flush=True)
json.dump(res, open(out, "w"), indent=1)
print("PASSED", sum(r["pass"] for r in res), "of", len(res))
