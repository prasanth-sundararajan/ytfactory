"""Daily schedule: produce one video each morning, publish at the best evening slot.

Times are IST and come from env (defaults chosen from a 90-day scan of 293 top Indian
finance videos: the 20:00-22:00 IST slot had the highest median views/day):
  PRODUCE_TIME_IST   when the pipeline starts             (default 06:30)
  PUBLISH_TIME_IST   when the approved video goes public   (default 20:00, "now" = immediately)
  SHORT_TIMES_IST    when the two Shorts go public, next day (default 12:30,16:30)
  DAILY_VIDEO        "1" to produce daily, "0" to pause    (default 1)
"""

import datetime as dt
import os

from app.db import conn

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def parse(hhmm: str) -> tuple[int, int]:
    h, m = (hhmm or "0:0").strip().split(":")
    return int(h), int(m)


def enabled() -> bool:
    return os.environ.get("PUBLISH_TIME_IST", "20:00").strip().lower() not in ("", "now")


def main_slot(now: dt.datetime | None = None) -> dt.datetime:
    """Next publish slot at least 20 minutes away (YouTube needs time to process)."""
    now = (now or dt.datetime.now(dt.timezone.utc)).astimezone(IST)
    h, m = parse(os.environ.get("PUBLISH_TIME_IST", "20:00"))
    slot = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if slot < now + dt.timedelta(minutes=20):
        slot += dt.timedelta(days=1)
    return slot


def short_slots(main_at: dt.datetime) -> list[dt.datetime]:
    """Shorts go public the day after the full video, so they can link to it."""
    day = (main_at.astimezone(IST) + dt.timedelta(days=1)).date()
    out = []
    for t in os.environ.get("SHORT_TIMES_IST", "12:30,16:30").split(","):
        if t.strip():
            h, m = parse(t)
            out.append(dt.datetime(day.year, day.month, day.day, h, m, tzinfo=IST))
    return out


def iso(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fmt(t: dt.datetime) -> str:
    return t.astimezone(IST).strftime("%a %d %b, %I:%M %p IST").replace(" 0", " ")


def describe() -> str:
    if not enabled():
        return "Publishes immediately on approval"
    m = main_slot()
    s = ", ".join(x.strftime("%I:%M %p").lstrip("0") for x in short_slots(m))
    return f"Goes public {fmt(m)}; Shorts next day {s}"


def produced_today(slug: str) -> bool:
    start = dt.datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0)
    with conn() as c, c.cursor() as cur:
        cur.execute("""select count(*) from videos v join channels ch on ch.id = v.channel_id
                       where ch.slug = %s and v.created_at >= %s
                         and v.status not in ('blocked', 'failed', 'rejected')""", (slug, start))
        return cur.fetchone()[0] > 0


def pending_approval(hours: int = 30) -> list[int]:
    with conn() as c, c.cursor() as cur:
        cur.execute("""select id from videos where status in ('ready', 'needs_review')
                       and yt_video_id is null
                       and created_at > now() - make_interval(hours => %s)
                       order by id""", (hours,))
        return [r[0] for r in cur.fetchall()]
