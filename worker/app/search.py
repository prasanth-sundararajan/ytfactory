"""Web search with provider fallback and Redis caching.

Chain: Serper (India-localised Google) -> DuckDuckGo HTML (keyless).
Wikipedia is deliberately NOT used: it returns encyclopedic articles that are
irrelevant to commercial topics, and "relevant" is not the same as "official".

One search is 1 Serper credit; results are cached 7 days so repeats cost nothing.
"""

import hashlib
import html
import json
import logging
import re
import urllib.parse

import httpx
import redis.asyncio as aioredis

from app.config import CFG

log = logging.getLogger("search")

SERPER_URL = "https://google.serper.dev/search"
DDG_URL = "https://html.duckduckgo.com/html/"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
CACHE_TTL = 7 * 24 * 3600

_redis = None


def _client():
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(CFG.redis_url, decode_responses=True)
    return _redis


async def _cache_get(key):
    try:
        return await _client().get(key)
    except Exception as e:
        log.warning("cache read failed: %s", e)
        return None


async def _cache_set(key, value):
    try:
        await _client().set(key, value, ex=CACHE_TTL)
    except Exception as e:
        log.warning("cache write failed: %s", e)


def _strip(raw: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", raw or "")).strip()


def _clean_ddg(href: str) -> str:
    href = html.unescape(href or "")
    if href.startswith("//"):
        href = "https:" + href
    if "uddg=" in href:
        qs = urllib.parse.urlparse(href).query
        target = urllib.parse.parse_qs(qs).get("uddg", [""])[0]
        if target:
            return target
    return href


async def _serper(client, query, num, gl, hl):
    if not CFG.serper_key:
        raise RuntimeError("SERPER_API_KEY not set")
    r = await client.post(
        SERPER_URL,
        headers={"X-API-KEY": CFG.serper_key, "Content-Type": "application/json"},
        json={"q": query, "num": num, "gl": gl, "hl": hl},
        timeout=25,
    )
    if r.status_code != 200:
        raise RuntimeError("serper HTTP %s" % r.status_code)
    d = r.json()
    items = [
        {"title": o.get("title", ""), "url": o.get("link", ""),
         "snippet": o.get("snippet", "")}
        for o in d.get("organic", [])
        if o.get("link")
    ]
    if not items:
        raise RuntimeError("serper returned no results")
    return items


async def _ddg(client, query, num):
    r = await client.post(
        DDG_URL, data={"q": query}, headers={"User-Agent": UA}, timeout=25
    )
    if r.status_code != 200:
        raise RuntimeError("ddg HTTP %s" % r.status_code)
    text = r.text
    links = re.findall(
        r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', text, re.S
    )
    snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', text, re.S)
    items = []
    for i, (href, title) in enumerate(links[:num]):
        url = _clean_ddg(href)
        if not url.startswith("http"):
            continue
        items.append({
            "title": _strip(title),
            "url": url,
            "snippet": _strip(snippets[i]) if i < len(snippets) else "",
        })
    if not items:
        raise RuntimeError("ddg returned no results")
    return items


async def web_search(query: str, num: int = 8, gl: str = "in", hl: str = "en") -> dict:
    key = "search:" + hashlib.sha1(
        f"{query}|{num}|{gl}|{hl}".encode("utf-8")
    ).hexdigest()

    cached = await _cache_get(key)
    if cached:
        data = json.loads(cached)
        data["cached"] = True
        return data

    async with httpx.AsyncClient() as client:
        try:
            items = await _serper(client, query, num, gl, hl)
            res = {"ok": True, "provider": "serper", "query": query, "organic": items}
        except Exception as e:
            log.info("serper unavailable (%s), falling back to duckduckgo", e)
            try:
                items = await _ddg(client, query, num)
                res = {"ok": True, "provider": "duckduckgo", "query": query, "organic": items}
            except Exception as e2:
                log.warning("all search providers failed: %s / %s", e, e2)
                return {"ok": False, "provider": None, "query": query,
                        "organic": [], "error": "serper+ddg failed"}

    await _cache_set(key, json.dumps(res))
    res["cached"] = False
    return res
