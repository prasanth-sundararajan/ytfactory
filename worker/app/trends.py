"""Trend analyst: scans YouTube worldwide in a channel's categories and turns it
into topic ideas, hook patterns and title formulas that Scout and Brief use.

Data is official YouTube Data API v3 (mostPopular charts + search by view count).
Ideas only - every fact in a video still has to come from the official sources.
"""

import asyncio
import datetime
import json
import logging
import re

import httpx

from app import llm, youtube
from app.db import conn

log = logging.getLogger("trends")
API = "https://www.googleapis.com/youtube/v3"

# Per channel: which YouTube categories to watch, which regions, which seed searches.
PROFILE = {
    "money-clear": {
        "categories": {"27": "Education", "26": "Howto & Style",
                       "25": "News & Politics", "28": "Science & Technology"},
        "regions": ["IN", "US", "GB", "CA", "AU", "SG"],
        "queries": [
            "income tax India explained", "mutual fund SIP India",
            "salary tax saving India", "credit card mistakes India",
            "money mistakes salaried", "personal finance India",
        ],
        "search_regions": ["IN", "US"],
    },
}
DEFAULT = PROFILE["money-clear"]


def _get(token: str, path: str, **p) -> dict:
    r = httpx.get(API + path, params=p,
                  headers={"Authorization": "Bearer " + token}, timeout=40)
    d = r.json()
    return d if r.status_code == 200 else {"items": [], "_err": r.status_code}


def _secs(iso: str) -> int:
    m = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso or "")
    if not m:
        return 0
    h, mi, s = (int(x or 0) for x in m.groups())
    return h * 3600 + mi * 60 + s


def _row(v: dict, src: str) -> dict:
    sn, st = v["snippet"], v.get("statistics", {})
    pub = datetime.datetime.fromisoformat(sn["publishedAt"].replace("Z", "+00:00"))
    age = max(1.0, (datetime.datetime.now(datetime.timezone.utc) - pub).total_seconds() / 86400)
    views = int(st.get("viewCount", 0))
    dur = _secs(v.get("contentDetails", {}).get("duration", ""))
    return {"id": v["id"], "title": sn["title"], "channel": sn["channelTitle"],
            "views": views, "age_days": round(age, 1),
            "views_per_day": int(views / age), "seconds": dur,
            "format": "short" if 0 < dur <= 60 else "long", "src": src}


def scan(slug: str) -> list[dict]:
    prof = PROFILE.get(slug, DEFAULT)
    token = youtube.access_token()
    seen, rows = set(), []

    def add(items, src):
        for v in items:
            if v["id"] not in seen:
                seen.add(v["id"])
                rows.append(_row(v, src))

    for region in prof["regions"]:
        for cid, cname in prof["categories"].items():
            d = _get(token, "/videos", part="snippet,statistics,contentDetails",
                     chart="mostPopular", regionCode=region,
                     videoCategoryId=cid, maxResults=25)
            add(d["items"], f"chart:{region}:{cname}")

    since = (datetime.datetime.now(datetime.timezone.utc)
             - datetime.timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for region in prof["search_regions"]:
        for q in prof["queries"]:
            s = _get(token, "/search", part="id", q=q, type="video",
                     order="viewCount", publishedAfter=since,
                     regionCode=region, maxResults=10)
            ids = [i["id"]["videoId"] for i in s["items"]]
            if ids:
                d = _get(token, "/videos", part="snippet,statistics,contentDetails",
                         id=",".join(ids))
                add(d["items"], f"search:{region}:{q}")
    rows.sort(key=lambda r: -r["views_per_day"])
    return rows


async def analyse(slug: str, ch: dict, rows: list[dict]) -> dict:
    top = rows[:60]
    listing = "\n".join(
        f"- [{r['format']}] {r['title']} | {r['views']:,} views in {r['age_days']}d "
        f"({r['views_per_day']:,}/day) | {r['channel']} | {r['src']}" for r in top)
    prompt = f"""You are the Trend Analyst for the YouTube channel "{ch['name']}".
Niche: {ch['niche']}. Audience: Indian salaried professionals. Language: English.

Below are the fastest-growing videos YouTube shows right now in Education,
How-to, News and Science & Tech charts across India, US, UK, Canada, Australia
and Singapore, plus top recent videos for finance searches.

{listing}

Find what is working and adapt it. Rules:
- Topics must be CONCRETE and NAMED (a rule, scheme, charge, form, limit,
  process) so they can be verified on Indian official websites. No hype, no
  get-rich-quick, no guaranteed returns, no stock tips.
- Do not copy any title. Learn the pattern, write original ones.
- Prefer topics a 5-minute explainer can answer and a 60-second Short can tease.

Return JSON only:
{{"summary": "3 sentences: what the audience wants right now",
  "formats": {{"short_share_pct": 0, "best_long_length_min": "..."}},
  "topic_ideas": [{{"topic": "...", "why_trending": "...", "angle": "...",
                    "search_query": "3-6 plain words"}}, ... 8 items],
  "hook_patterns": [{{"pattern": "template with [blanks]", "example": "...",
                      "why_it_works": "..."}}, ... 6 items],
  "title_formulas": ["...", "...", "...", "...", "..."],
  "avoid": ["overused or risky angles"]}}"""
    return await llm.generate_json(prompt, temperature=0.7)


def _save(ch_id: int, report: dict, n: int) -> None:
    with conn() as c, c.cursor() as cur:
        cur.execute(
            """insert into trend_reports (channel_id, day, videos_scanned, report)
               values (%s, current_date, %s, %s)
               on conflict (channel_id, day) do update
               set videos_scanned=excluded.videos_scanned, report=excluded.report""",
            (ch_id, n, json.dumps(report, ensure_ascii=False)))


async def run(slug: str) -> dict:
    from app.db import get_channel
    ch = get_channel(slug)
    rows = await asyncio.to_thread(scan, slug)
    if len(rows) < 10:
        raise RuntimeError(f"only {len(rows)} videos scanned - not enough signal")
    report = await analyse(slug, ch, rows)
    report["top_videos"] = rows[:15]
    await asyncio.to_thread(_save, ch["id"], report, len(rows))
    log.info("trends %s: scanned %d videos", slug, len(rows))
    return report


def latest(ch_id: int) -> dict | None:
    with conn() as c, c.cursor() as cur:
        cur.execute("""select report from trend_reports where channel_id=%s
                       and day > current_date - 4 order by day desc limit 1""", (ch_id,))
        r = cur.fetchone()
        return r[0] if r else None


def scout_block(ch_id: int) -> str:
    rep = latest(ch_id)
    if not rep:
        return ""
    ideas = "\n".join(f"- {i['topic']} ({i.get('why_trending', '')})"
                      for i in rep.get("topic_ideas", [])[:8])
    return ("\nTREND ANALYST - what is working on YouTube right now (ideas only; "
            "still pick something concrete and verifiable on official sites, "
            "and never repeat a covered topic):\n" + ideas +
            "\nAudience now: " + str(rep.get("summary", "")) + "\n")


def brief_block(ch_id: int) -> str:
    rep = latest(ch_id)
    if not rep:
        return ""
    hooks = "\n".join(f"- {h['pattern']}  e.g. {h.get('example', '')}"
                      for h in rep.get("hook_patterns", [])[:6])
    titles = "\n".join("- " + t for t in rep.get("title_formulas", [])[:5])
    return ("\nHOOK PATTERNS proven on YouTube right now - write the hook using ONE "
            "of these, with a concrete named subject. NEVER invent a percentage, statistic or claim about what nobody tells you; use only numbers from the verified fact sheet; no hype or false promise; use the current financial year, never an old one:\n"
            + hooks + "\nTitle formulas for working_title:\n" + titles + "\n")


def report_text(slug: str, rep: dict, n: int) -> str:
    lines = [f"<b>Trends · {slug}</b> ({n} videos scanned)", "",
             str(rep.get("summary", "")), "", "<b>Topic ideas</b>"]
    for i in rep.get("topic_ideas", [])[:6]:
        lines.append(f"• {i['topic']}")
    lines += ["", "<b>Hook patterns</b>"]
    for h in rep.get("hook_patterns", [])[:4]:
        lines.append(f"• {h['pattern']}")
    return "\n".join(lines)


def done_today(ch_id: int) -> bool:
    with conn() as c, c.cursor() as cur:
        cur.execute("select 1 from trend_reports where channel_id=%s and day=current_date", (ch_id,))
        return cur.fetchone() is not None
