import base64, json, struct, sys, urllib.request, zlib
def png(w, h, rgb):
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))
    def ck(t, d): c = struct.pack(">I", len(d)) + t + d; return c + struct.pack(">I", zlib.crc32(t + d))
    return b"\x89PNG\r\n\x1a\n" + ck(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + ck(b"IDAT", zlib.compress(raw)) + ck(b"IEND", b"")
img = base64.b64encode(png(128, 128, (220, 20, 20))).decode()
b = {"model": sys.argv[2], "max_tokens": 100, "temperature": 0, "chat_template_kwargs": {"enable_thinking": False},
     "messages": [{"role": "user", "content": [{"type": "text", "text": "What single color is this image?"},
                  {"type": "image_url", "image_url": {"url": "data:image/png;base64," + img}}]}]}
try:
    r = urllib.request.Request(sys.argv[1], json.dumps(b).encode(), {"Content-Type": "application/json"})
    print(json.load(urllib.request.urlopen(r, timeout=300))["choices"][0]["message"])
except urllib.error.HTTPError as e: print("HTTP", e.code, e.read()[:400])
