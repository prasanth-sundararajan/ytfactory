"""Doctor agent: classifies failures, applies safe remedies, retries, and reports.

Guard rails: the Doctor only changes RUNTIME state (waits, clears cool-downs,
retries, marks stuck jobs, frees media). It never edits source code and never
bypasses the research / fact-check gates. Anything it cannot fix is diagnosed
by the model and sent to Telegram in plain words.
"""

import asyncio
import json
import logging
import traceback

from app import llm
from app.db import conn

log = logging.getLogger("healer")

notify = None  # async callable(text); set from main once the bot exists

# category -> (message fragments, retry delays in seconds)
RULES = [
    ("llm_unavailable", ("all providers failed", "high demand", "quota", "rate limit",
                         "overloaded", "429", "resource_exhausted"), [60, 180, 420]),
    ("network", ("timeout", "timed out", "connecterror", "connection reset",
                 "remoteprotocolerror", "readerror", "502", "503", "504",
                 "temporary failure"), [10, 30, 90]),
    ("database", ("psycopg", "could not connect", "connection refused"), [10, 30]),
    ("voice", ("edge-tts", "noaudioreceived", "wsserver", "no audio"), [15, 60]),
    ("render", ("ffmpeg", "libx264", "invalid data", "filtergraph", "moov atom"), [5]),
    ("search", ("no results", "search providers failed"), [20]),
]
NO_RETRY = (("disk_full", ("no space left",)),)


def classify(exc: Exception) -> tuple[str, list[int]]:
    msg = (type(exc).__name__ + " " + str(exc)).lower()
    for cat, frags in NO_RETRY:
        if any(f in msg for f in frags):
            return cat, []
    for cat, frags, delays in RULES:
        if any(f in msg for f in frags):
            return cat, delays
    return "unknown", [10]


def _log(agent_id: str, cat: str, err: str, action: str, resolved: bool = False) -> None:
    try:
        with conn() as c, c.cursor() as cur:
            cur.execute(
                "insert into incidents (agent, category, error, action, resolved) "
                "values (%s,%s,%s,%s,%s)", (agent_id, cat, err[:600], action, resolved))
    except Exception as e:  # logging must never break a run
        log.warning("incident log failed: %s", e)


def prepare(cat: str) -> str:
    """Safe remedies applied before a retry. Returns what was done."""
    if cat == "llm_unavailable":
        llm._cooldown.clear()
        return "cleared model cool-downs, waiting for capacity"
    if cat == "disk_full":
        return "disk full - needs manual cleanup"
    return "waiting then retrying"


def handle(agent_id: str, exc: Exception, attempt: int) -> int | None:
    """Seconds to wait before retrying, or None to give up."""
    cat, delays = classify(exc)
    err = f"{type(exc).__name__}: {str(exc)[:300]}"
    if attempt < len(delays):
        action = prepare(cat)
        _log(agent_id, cat, err, f"retry {attempt + 1}/{len(delays)}: {action}")
        log.warning("doctor: %s failed (%s) - %s", agent_id, cat, action)
        return delays[attempt]
    _log(agent_id, cat, err, "gave up")
    return None


async def report(agent_id: str, exc: Exception, video_id: int) -> None:
    """Called once when retries are exhausted: diagnose and tell the owner."""
    cat, _ = classify(exc)
    tb = "".join(traceback.format_exception(exc))[-1800:]
    diagnosis = ""
    try:
        d = await llm.generate_json(
            "You are the Doctor agent of a Python video pipeline (FFmpeg, edge-tts, "
            "Postgres, LLM APIs). Diagnose this failure for the owner in plain words.\n"
            f"Agent: {agent_id}\nCategory guess: {cat}\n\n{tb}\n\n"
            'Return JSON only: {"cause": "one sentence", "fix": "one or two concrete '
            'steps the owner can take", "safe_to_retry": true}', temperature=0.2)
        diagnosis = f"\nCause: {d.get('cause', '')}\nFix: {d.get('fix', '')}"
    except Exception:
        diagnosis = "\n(model unavailable for diagnosis)"
    text = (f"\U0001fa7a <b>Doctor</b>: video {video_id} stopped at <b>{agent_id}</b> "
            f"({cat}) after retries.\n<code>{str(exc)[:220]}</code>{diagnosis}")
    if notify:
        try:
            await notify(text)
        except Exception as e:
            log.warning("notify failed: %s", e)


def sweep_stuck(minutes: int = 90) -> list[int]:
    """Mark videos stuck in draft with no agent activity as failed."""
    with conn() as c, c.cursor() as cur:
        cur.execute(
            """update videos v set status='failed', updated_at=now()
               where v.status in ('draft','drafting')
                 and v.updated_at < now() - make_interval(mins => %s)
                 and not exists (select 1 from agent_runs r where r.video_id = v.id
                     and coalesce(r.finished_at, r.started_at) > now() - interval '30 minutes')
               returning v.id""", (minutes,))
        return [r[0] for r in cur.fetchall()]


async def watchdog():
    """Every 10 minutes: free stuck jobs and tell the owner."""
    while True:
        try:
            ids = await asyncio.to_thread(sweep_stuck)
            for i in ids:
                _log("watchdog", "stuck", f"video {i} had no activity", "marked failed", True)
                if notify:
                    await notify(f"\U0001fa7a <b>Doctor</b>: video {i} was stuck with no "
                                 "activity and was marked failed. Run /new to retry.")
        except Exception as e:
            log.warning("watchdog error: %s", e)
        await asyncio.sleep(600)


def recent(limit: int = 8) -> str:
    with conn() as c, c.cursor() as cur:
        cur.execute("select to_char(ts,'DD Mon HH24:MI'), agent, category, action "
                    "from incidents order by id desc limit %s", (limit,))
        rows = cur.fetchall()
    if not rows:
        return "<b>Doctor</b>: no incidents recorded."
    return "<b>Doctor · recent incidents</b>\n" + "\n".join(
        f"• {t} {a}/{c}: {act}" for t, a, c, act in rows)
