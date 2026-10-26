#!/usr/bin/env python3
"""Headless-browser fetch MCP server for /srv/ai.

For sites that refuse plain HTTP clients (Reddit returns 403 "blocked by network
policy" to curl/mcp-server-fetch/jina, but serves a real Chromium). Tools:

  * `browser_fetch`  - rendered text (or HTML) of any public http(s) URL
  * `reddit_search`  - Reddit search results (optionally within one subreddit)

Runs natively on the HOST (needs the Playwright Chromium under venvs/browser),
served over streamable-http like llama-swap-mode-mcp; mcpo and Hermes reach it at
host.docker.internal:9130. SSRF guard: every request the browser makes is refused
unless its host resolves only to public IPs (no loopback/LAN/Docker/link-local).
See scripts/browser-fetch-mcp.service and ADR-0034.
"""
from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
from urllib.parse import quote_plus, urlparse

os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/srv/ai/venvs/browser/pw")
from mcp.server.fastmcp import FastMCP  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/130.0.0.0 Safari/537.36")
mcp = FastMCP("browser-fetch", host=os.environ.get("BF_MCP_HOST", "0.0.0.0"),
              port=int(os.environ.get("BF_MCP_PORT", "9130")))
_sem = asyncio.Semaphore(int(os.environ.get("BF_MAX_CONCURRENT", "2")))
_pw = None
_browser = None
_lock = asyncio.Lock()
_ok_hosts: dict[str, bool] = {}


def _public_host(host: str) -> bool:
    if host in _ok_hosts:
        return _ok_hosts[host]
    try:
        ips = {i[4][0] for i in socket.getaddrinfo(host, None)}
        ok = bool(ips) and all(ipaddress.ip_address(i.split("%")[0]).is_global for i in ips)
    except Exception:
        ok = False
    _ok_hosts[host] = ok
    return ok


async def _get_browser():
    global _pw, _browser
    async with _lock:
        if _browser is None or not _browser.is_connected():
            if _pw is None:
                _pw = await async_playwright().start()
            _browser = await _pw.chromium.launch()
        return _browser


async def _guard(route):
    u = urlparse(route.request.url)
    if u.scheme in ("http", "https") and u.hostname and await asyncio.to_thread(_public_host, u.hostname):
        await route.continue_()
    elif u.scheme in ("data", "blob", "about"):
        await route.continue_()
    else:
        await route.abort()


async def _render(url: str, wait: float, html: bool, expand_reddit: bool) -> tuple[int, str, str]:
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError("only http(s) URLs are allowed")
    if not await asyncio.to_thread(_public_host, u.hostname):
        raise ValueError("refusing non-public host")
    if u.hostname.endswith("reddit.com"):
        url = url.replace("//old.reddit.com", "//www.reddit.com")
    async with _sem:
        browser = await _get_browser()
        ctx = await browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 2000})
        try:
            await ctx.route("**/*", _guard)
            pg = await ctx.new_page()
            r = await pg.goto(url, wait_until="domcontentloaded", timeout=60000)
            await pg.wait_for_timeout(int(wait * 1000))
            if expand_reddit and "reddit.com" in url and "/comments/" in url:
                for _ in range(5):
                    btns = pg.locator("button:has-text('more repl'), faceplate-partial button")
                    n = await btns.count()
                    if n == 0:
                        break
                    for i in range(min(n, 20)):
                        try:
                            await btns.nth(i).click(timeout=1500)
                        except Exception:
                            pass
                    await pg.wait_for_timeout(1500)
            body = await pg.content() if html else await pg.inner_text("body")
            return (r.status if r else 0), await pg.title(), body
        finally:
            await ctx.close()


@mcp.tool()
async def browser_fetch(url: str, max_chars: int = 30000, wait_seconds: float = 4.0,
                        html: bool = False) -> str:
    """Fetch a public web page with a real headless Chromium and return its rendered
    text (or HTML if html=true). Use for sites that block plain fetch tools, e.g.
    Reddit threads (comments are expanded). Slow (~5-15 s); prefer the normal fetch
    tool first for sites that don't block it. Public http(s) hosts only."""
    try:
        status, title, body = await _render(url, wait_seconds, html, True)
    except Exception as e:
        return f"ERROR: {e}"
    return f"[status {status}] {title}\n\n{body[:max_chars]}"


@mcp.tool()
async def reddit_search(query: str, subreddit: str = "", sort: str = "relevance",
                        time: str = "all", max_chars: int = 15000) -> str:
    """Search Reddit (optionally inside one subreddit, e.g. 'LocalLLaMA') and return
    result titles, subreddits, ages and vote/comment counts. sort: relevance|hot|top|new|
    comments; time: hour|day|week|month|year|all. Then pass a post's URL to browser_fetch
    to read the thread and comments."""
    base = f"https://www.reddit.com/r/{quote_plus(subreddit)}/search/" if subreddit else "https://www.reddit.com/search/"
    url = f"{base}?q={quote_plus(query)}&sort={quote_plus(sort)}&t={quote_plus(time)}" + ("&restrict_sr=1" if subreddit else "")
    try:
        status, title, body = await _render(url, 4.0, False, False)
    except Exception as e:
        return f"ERROR: {e}"
    return f"[status {status}] {title}\n\n{body[:max_chars]}"


if __name__ == "__main__":
    mcp.run(transport=os.environ.get("BF_MCP_TRANSPORT", "streamable-http"))
