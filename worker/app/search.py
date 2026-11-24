"""Web search with provider fallback and Redis caching.

Chain: Serper (India-localised Google) -> DuckDuckGo HTML (keyless).
Wikipedia is deliberately NOT used: it returns encyclopedic articles that are
irrelevant to commercial topics, and "relevant" is not the same as "official".

One search is 1 Serper credit; results are cached 7 days so repeats cost nothing.
"""

import asyncio
import hashlib
import html
import json
import os
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


async def _resolve_google(client, url: str) -> str:
    """Serper wraps some site: results in a google.com/goto redirect.

    The redirect host is google.com, so the official-domain check would discard
    a perfectly good incometax.gov.in page. Follow it once and keep the target.
    """
    if not url:
        return url
    if "google.com/goto" not in url and "google.co.in/goto" not in url:
        return url
    try:
        r = await client.get(url, follow_redirects=True, timeout=15)
        final = str(r.url)
        if final.startswith("http") and "goto" not in final:
            return final
    except Exception as e:
        log.info("google redirect resolve failed (%s): %s", url[:60], e)
    return url


async def _resolve_items(client, items):
    async def one(it):
        it["url"] = await _resolve_google(client, it.get("url", ""))
        return it

    return await asyncio.gather(*(one(it) for it in items))


async def fetch_text(url: str, limit: int = 5000) -> str:
    """Fetch a page and return visible text, truncated. Empty on any failure.

    Google snippets are often one generic sentence; the real rule lives in the
    page body. Fetching the official page lets Research quote actual wording.
    """
    if not url or not url.startswith("http"):
        return ""
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=20) as client:
            r = await client.get(url, headers={"User-Agent": UA})
        ctype = (r.headers.get("content-type") or "").lower()
        if "html" not in ctype and "text" not in ctype:
            return ""
        text = r.text
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        text = html.unescape(text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:limit]
    except Exception as e:
        log.info("fetch_text failed (%s): %s", url[:60], e)
        return ""


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
    await _resolve_items(client, items)
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


async def _tavily(client, query, num):
    """Tavily free tier (1,000 credits a month). `site:x` in the query becomes a domain filter."""
    key = os.environ.get("TAVILY_API_KEY", "").strip()
    if not key:
        raise RuntimeError("TAVILY_API_KEY not set")
    sites = re.findall(r"site:(\S+)", query)
    q = re.sub(r"site:\S+", "", query).strip()
    body = {"query": q, "max_results": min(num, 10), "search_depth": "basic"}
    if sites:
        body["include_domains"] = sites
    r = await client.post("https://api.tavily.com/search", json=body, timeout=30,
                          headers={"Authorization": "Bearer " + key})
    if r.status_code != 200:
        raise RuntimeError("tavily HTTP %s: %s" % (r.status_code, r.text[:120]))
    items = [{"title": o.get("title", ""), "url": o.get("url", ""),
              "snippet": (o.get("content") or "")[:500]}
             for o in r.json().get("results", []) if o.get("url")]
    if not items:
        raise RuntimeError("tavily returned no results")
    return items


async def web_search(query: str, num: int = 8, gl: str = "in", hl: str = "en") -> dict:
    """Tavily -> DuckDuckGo -> Serper (last, only while it has credits)."""
    key = "search:v2:" + hashlib.sha1(
        f"{query}|{num}|{gl}|{hl}".encode("utf-8")
    ).hexdigest()

    cached = await _cache_get(key)
    if cached:
        data = json.loads(cached)
        data["cached"] = True
        return data

    chain = [
        ("tavily", lambda c: _tavily(c, query, num)),
        ("duckduckgo", lambda c: _ddg(c, query, num)),
        ("serper", lambda c: _serper(c, query, num, gl, hl)),
    ]
    errors = []
    async with httpx.AsyncClient() as client:
        for name, call in chain:
            try:
                items = await call(client)
                res = {"ok": True, "provider": name, "query": query, "organic": items}
                break
            except Exception as e:
                errors.append(f"{name}: {str(e)[:80]}")
                log.info("%s unavailable (%s), trying next", name, str(e)[:80])
        else:
            log.warning("all search providers failed: %s", " / ".join(errors))
            return {"ok": False, "provider": None, "query": query,
                    "organic": [], "error": " / ".join(errors)[:300]}

    await _cache_set(key, json.dumps(res))
    res["cached"] = False
    return res
