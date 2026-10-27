#!/srv/ai/venvs/browser/bin/python
"""Fetch a page's rendered text with headless Chromium (for sites that block plain HTTP clients, e.g. Reddit).

Usage: browser-fetch.py URL [--max-chars N] [--wait SECONDS] [--html]
Reddit thread URLs are rewritten to www.reddit.com and expanded ("more replies" clicked) before extraction.
"""
import argparse
import os
import sys

os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/srv/ai/venvs/browser/pw")
from playwright.sync_api import sync_playwright  # noqa: E402

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/130.0.0.0 Safari/537.36")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--max-chars", type=int, default=30000)
    ap.add_argument("--wait", type=float, default=4.0)
    ap.add_argument("--html", action="store_true", help="return HTML instead of text")
    a = ap.parse_args()
    url = a.url.replace("//old.reddit.com", "//www.reddit.com")
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_context(user_agent=UA, viewport={"width": 1280, "height": 2000}).new_page()
        r = pg.goto(url, wait_until="domcontentloaded", timeout=60000)
        pg.wait_for_timeout(int(a.wait * 1000))
        if "reddit.com" in url and "/comments/" in url:
            for _ in range(5):
                btns = pg.locator("button:has-text('more repl'), faceplate-partial button")
                if btns.count() == 0:
                    break
                for i in range(min(btns.count(), 20)):
                    try:
                        btns.nth(i).click(timeout=1500)
                    except Exception:
                        pass
                pg.wait_for_timeout(1500)
        out = pg.content() if a.html else pg.inner_text("body")
        print(f"[status {r.status if r else '?'}] {pg.title()}\n", file=sys.stderr)
        print(out[: a.max_chars])
        b.close()


if __name__ == "__main__":
    main()
