"""News agent: reads official Indian finance feeds and picks today's video topic.

Sources are government/regulator feeds only (RBI press releases + notifications,
PIB). The pick is only a TOPIC - the Research agent still has to ground every
fact on the channel's official-source list before anything is scripted.
"""

import asyncio
import datetime
import email.utils
import json
import logging
import re
import xml.etree.ElementTree as ET

import httpx

from app import llm
from app.db import conn, recent_topics

log = logging.getLogger("news")

FEEDS = {
    "RBI press release": "https://www.rbi.org.in/pressreleases_rss.xml",
    "RBI notification": "https://rbi.org.in/notifications_rss.xml",
    "PIB": "https://pib.gov.in/RssMain.aspx?ModId=6&Lang=1&Regid=3",
}
MAX_AGE_DAYS = 5


def _clean(t: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", t or "")).strip()


def _fetch(name: str, url: str) -> list[dict]:
    try:
        r = httpx.get(url, headers={"User-Agent": "Mozilla/5.0"},
                      timeout=30, follow_redirects=True)
        root = ET.fromstring(r.text.lstrip("﻿").encode("utf-8"))
    except Exception as e:
        log.warning("feed %s failed: %s", name, e)
        return []
    out = []
    now = datetime.datetime.now(datetime.timezone.utc)
    for it in root.iter("item"):
        title = _clean(it.findtext("title"))
        if not title:
            continue
        when = None
        raw = it.findtext("pubDate")
        if raw:
            try:
                when = email.utils.parsedate_to_datetime(raw)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=datetime.timezone.utc)
            except Exception:
                when = None
        if when and (now - when).days > MAX_AGE_DAYS:
            continue
        out.append({"source": name, "title": title[:200],
                    "link": (it.findtext("link") or "").strip(),
                    "summary": _clean(it.findtext("description"))[:350],
                    "date": when.date().isoformat() if when else ""})
    return out


def fetch_all() -> list[dict]:
    items = []
    for name, url in FEEDS.items():
        items += _fetch(name, url)
    return items


async def pick(ch: dict) -> dict:
    items = await asyncio.to_thread(fetch_all)
    if not items:
        raise RuntimeError("no items from any official feed")
    covered = recent_topics(ch["id"], 15)
    listing = "\n".join(
        f"{i}. [{x['source']} {x['date']}] {x['title']} - {x['summary'][:200]}"
        for i, x in enumerate(items[:80]))
    prompt = f"""You are the News agent for the YouTube channel "{ch['name']}".
Audience: Indian salaried professionals. Today is {datetime.date.today().isoformat()}.

Official announcements from the last {MAX_AGE_DAYS} days (RBI, PIB):
{listing}

Already covered by the channel (do not repeat):
{chr(10).join('- ' + t for t in covered) or '- none'}

Rank up to 5 announcements, most useful first, that affect an ordinary person's money:
loans and EMIs, deposits, UPI and payments, credit cards, tax, provident fund,
insurance, savings schemes, KYC, bank charges, customer protection.
Skip routine market operations (repo auctions, T-bill results, forex notices),
purely institutional or corporate rules, and anything in another language.
Each candidate must be a different subject. If nothing qualifies, return
{{"candidates": []}}.

The video will be a calm 5-minute explainer of WHAT changed, WHO it affects and
WHAT to check on the official site. No predictions, no advice, no hype.

Return JSON only:
{{"candidates": [{{"index": 0, "topic": "specific named subject, under 90 chars",
  "angle": "what the viewer learns", "why_now": "the announcement and its date",
  "search_query": "3 to 6 plain words, no punctuation",
  "keywords": ["...", "...", "...", "...", "..."]}}]}}"""
    res = await llm.generate_json(prompt, temperature=0.3)
    cands = []
    for c in (res.get("candidates") or [])[:5]:
        if not c.get("topic"):
            continue
        try:
            src = items[int(c.get("index", -1))]
            c["source_url"], c["source_name"] = src["link"], src["source"]
        except Exception:
            pass
        c["from_news"] = True
        cands.append(c)
    return {"candidates": cands, "tried": []}


def done_today(ch_id: int) -> bool:
    with conn() as c, c.cursor() as cur:
        cur.execute("select 1 from news_picks where channel_id=%s and day=current_date",
                    (ch_id,))
        return cur.fetchone() is not None


async def run(ch_slug: str, force: bool = False) -> dict:
    from app.db import get_channel
    ch = get_channel(ch_slug)
    if not force and await asyncio.to_thread(done_today, ch["id"]):
        return {"skipped": "already picked today"}
    res = await pick(ch)

    def save():
        with conn() as c, c.cursor() as cur:
            cur.execute(
                """insert into news_picks (channel_id, day, pick, used)
                   values (%s, current_date, %s, false)
                   on conflict (channel_id, day) do update
                   set pick=excluded.pick, used=false""",
                (ch["id"], json.dumps(res, ensure_ascii=False)))
    await asyncio.to_thread(save)
    log.info("news candidates for %s: %s", ch_slug,
             [c["topic"] for c in res.get("candidates", [])])
    return res


def todays_pick(ch_id: int, avoid: list[str]) -> dict | None:
    """Next untried news candidate for today, or None (then Scout goes evergreen)."""
    with conn() as c, c.cursor() as cur:
        cur.execute("""select pick from news_picks
                       where channel_id=%s and day=current_date""", (ch_id,))
        row = cur.fetchone()
        if not row:
            return None
        pk = row[0]
        tried = list(pk.get("tried") or [])
        for cand in pk.get("candidates") or []:
            t = cand.get("topic")
            if t and t not in tried and t not in (avoid or []):
                tried.append(t)
                pk["tried"] = tried
                cur.execute("update news_picks set pick=%s where channel_id=%s "
                            "and day=current_date",
                            (json.dumps(pk, ensure_ascii=False), ch_id))
                return cand
        return None
