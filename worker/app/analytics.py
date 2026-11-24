"""YouTube Analytics collector + daily snapshot store (growth agent, phase 1)."""

import datetime
import logging

import httpx

from app import youtube
from app.db import conn

log = logging.getLogger("analytics")
REPORTS = "https://youtubeanalytics.googleapis.com/v2/reports"
VIDEO_METRICS = ("views,estimatedMinutesWatched,averageViewPercentage,"
                 "subscribersGained,likes,comments,shares")


def _report(token: str, **params) -> dict:
    r = httpx.get(REPORTS, params={"ids": "channel==MINE", **params},
                  headers={"Authorization": "Bearer " + token}, timeout=60)
    d = r.json()
    if "error" in d:
        raise youtube.YouTubeError("analytics: " + str(d["error"])[:200])
    return d


def _rows(d: dict) -> list[dict]:
    cols = [c["name"] for c in d.get("columnHeaders", [])]
    return [dict(zip(cols, r)) for r in d.get("rows", [])]


def collect(slug: str, days: int = 7) -> dict:
    """Store a channel-per-day series and a lifetime snapshot of each published video."""
    token = youtube.access_token(slug)
    today = datetime.date.today()
    start = today - datetime.timedelta(days=days)

    with conn() as c, c.cursor() as cur:
        cur.execute("select id from channels where slug = %s", (slug,))
        row = cur.fetchone()
        if not row:
            raise youtube.YouTubeError("unknown channel " + slug)
        ch_id = row[0]

        series = _rows(_report(
            token, startDate=start.isoformat(), endDate=today.isoformat(),
            metrics="views,estimatedMinutesWatched,subscribersGained,subscribersLost",
            dimensions="day", sort="day"))
        for r in series:
            cur.execute(
                """insert into channel_daily
                   (channel_id, day, views, watch_min, subs_gained, subs_lost)
                   values (%s,%s,%s,%s,%s,%s)
                   on conflict (channel_id, day) do update set views=excluded.views,
                   watch_min=excluded.watch_min, subs_gained=excluded.subs_gained,
                   subs_lost=excluded.subs_lost""",
                (ch_id, r["day"], r["views"], r["estimatedMinutesWatched"],
                 r["subscribersGained"], r["subscribersLost"]))

        cur.execute("select yt_video_id from videos where channel_id=%s "
                    "and yt_video_id is not null", (ch_id,))
        ids = [x[0] for x in cur.fetchall()]
        n = 0
        if ids:
            rows = _rows(_report(
                token, startDate="2020-01-01", endDate=today.isoformat(),
                metrics=VIDEO_METRICS, dimensions="video",
                filters="video==" + ",".join(ids[:200]), maxResults=200))
            for r in rows:
                cur.execute(
                    """insert into metrics_daily
                       (yt_video_id, day, views, watch_min, avg_view_pct,
                        subs_gained, likes, comments, shares)
                       values (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       on conflict (yt_video_id, day) do update set
                       views=excluded.views, watch_min=excluded.watch_min,
                       avg_view_pct=excluded.avg_view_pct,
                       subs_gained=excluded.subs_gained, likes=excluded.likes,
                       comments=excluded.comments, shares=excluded.shares""",
                    (r["video"], today, r["views"], r["estimatedMinutesWatched"],
                     r["averageViewPercentage"], r["subscribersGained"],
                     r["likes"], r["comments"], r["shares"]))
                n += 1
    return {"channel": slug, "days": len(series), "videos": n}


def stats_text(slug: str) -> str:
    with conn() as c, c.cursor() as cur:
        cur.execute(
            """select coalesce(sum(views),0), coalesce(sum(watch_min),0),
                      coalesce(sum(subs_gained),0), coalesce(sum(subs_lost),0)
               from channel_daily d join channels ch on ch.id=d.channel_id
               where ch.slug=%s and d.day > current_date - 7""", (slug,))
        v, w, g, l = cur.fetchone()
        cur.execute(
            """select v.title, m.views, m.avg_view_pct, m.subs_gained
               from metrics_daily m join videos v on v.yt_video_id=m.yt_video_id
               join channels ch on ch.id=v.channel_id
               where ch.slug=%s and m.day=(select max(day) from metrics_daily)
               order by m.views desc limit 5""", (slug,))
        top = cur.fetchall()
    lines = [f"<b>{slug}</b> · last 7 days",
             f"Views {v:,}  Watch {w:,.0f} min  Subs +{g} / -{l}"]
    for t, views, pct, subs in top:
        lines.append(f"• {(t or '?')[:48]} — {views:,} views, "
                     f"{(pct or 0):.0f}% avg, +{subs} subs")
    if not top:
        lines.append("No published videos with data yet.")
    return "\n".join(lines)
