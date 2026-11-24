import json
import pathlib
from contextlib import contextmanager

import psycopg

from app.config import CFG

SCHEMA_PATH = "/app/sql/schema.sql"


@contextmanager
def conn():
    c = psycopg.connect(CFG.database_url, autocommit=True)
    try:
        yield c
    finally:
        c.close()


def init_db() -> None:
    sql = pathlib.Path(SCHEMA_PATH).read_text(encoding="utf-8")
    with conn() as c, c.cursor() as cur:
        cur.execute(sql)


def seed_agents() -> int:
    cfg = json.load(open(CFG.config_path, encoding="utf-8"))
    rows = [
        (a["id"], a["name"], a["role"], a.get("phase", 1), i + 1)
        for i, a in enumerate(cfg["agents"])
    ]
    with conn() as c, c.cursor() as cur:
        cur.executemany(
            """insert into agents (id, name, role, phase, seq)
               values (%s, %s, %s, %s, %s)
               on conflict (id) do update set
                 name = excluded.name,
                 role = excluded.role,
                 phase = excluded.phase,
                 seq = excluded.seq""",
            rows,
        )
    return len(rows)


def seed_channels() -> int:
    path = pathlib.Path(CFG.config_path).with_name("channels.json")
    cfg = json.load(open(path, encoding="utf-8"))
    rows = [
        (c["slug"], c["name"], c.get("language", "en"), c.get("niche", ""))
        for c in cfg["channels"]
    ]
    with conn() as c, c.cursor() as cur:
        cur.executemany(
            """insert into channels (slug, name, language, niche)
               values (%s, %s, %s, %s)
               on conflict (slug) do update set
                 name = excluded.name,
                 language = excluded.language,
                 niche = excluded.niche""",
            rows,
        )
    return len(rows)


def _channel_config(slug: str) -> dict:
    """Per-channel settings that live in channels.json, not in Postgres.

    official_sources and disclaimer must reach the agents, otherwise the
    official-only gate silently disables itself.
    """
    path = pathlib.Path(CFG.config_path).with_name("channels.json")
    try:
        cfg = json.load(open(path, encoding="utf-8"))
    except Exception:
        return {}
    for c in cfg.get("channels", []):
        if c.get("slug") == slug:
            return c
    return {}


def get_channel(slug: str) -> dict:
    with conn() as c, c.cursor() as cur:
        cur.execute(
            "select id, slug, name, language, niche from channels where slug = %s", (slug,)
        )
        row = cur.fetchone()
        if not row:
            raise KeyError("unknown channel: " + slug)
        cols = [d.name for d in cur.description]
        ch = dict(zip(cols, row))

    cfg = _channel_config(slug)
    for key in ("official_sources", "disclaimer", "niche"):
        if cfg.get(key):
            ch[key] = cfg[key]
    ch["official_sources"] = [str(d).lower().strip() for d in ch.get("official_sources") or []]
    return ch


def list_channels() -> list[dict]:
    with conn() as c, c.cursor() as cur:
        cur.execute("select id, slug, name, language from channels order by id")
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def create_video(channel_id: int, kind: str) -> int:
    with conn() as c, c.cursor() as cur:
        cur.execute(
            "insert into videos (channel_id, kind, status) values (%s, %s, 'drafting') returning id",
            (channel_id, kind),
        )
        return cur.fetchone()[0]


def save_script(video_id: int, data: dict) -> None:
    script = data.get("script", {}) or {}
    title = (script.get("title") or "")[:200]
    with conn() as c, c.cursor() as cur:
        cur.execute(
            """update videos set script = %s, title = %s, status = 'scripted',
                               updated_at = now()
               where id = %s""",
            (json.dumps(data, ensure_ascii=False), title, video_id),
        )


def set_video_status(video_id: int, status: str) -> None:
    with conn() as c, c.cursor() as cur:
        cur.execute(
            "update videos set status = %s, updated_at = now() where id = %s",
            (status, video_id),
        )


def set_published(video_id: int, yt_video_id: str) -> None:
    with conn() as c, c.cursor() as cur:
        cur.execute(
            """update videos set status='published', yt_video_id=%s,
                                 published_at=now(), updated_at=now()
               where id=%s""", (yt_video_id, video_id))


def recent_topics(channel_id: int, limit: int = 8) -> list[str]:
    with conn() as c, c.cursor() as cur:
        cur.execute(
            """select title from videos
               where channel_id = %s and title is not null
               order by id desc limit %s""",
            (channel_id, limit),
        )
        return [r[0] for r in cur.fetchall()]


def record_start(agent_id: str, video_id: int) -> int:
    with conn() as c, c.cursor() as cur:
        cur.execute(
            "insert into agent_runs (agent_id, video_id, status) values (%s, %s, 'running') returning id",
            (agent_id, video_id),
        )
        return cur.fetchone()[0]


def record_finish(run_id: int, status: str, note: str | None = None,
                  payload: dict | None = None) -> None:
    with conn() as c, c.cursor() as cur:
        cur.execute(
            """update agent_runs
               set status = %s, finished_at = now(),
                   duration_s = extract(epoch from now() - started_at),
                   note = %s, payload = %s
               where id = %s""",
            (status, (note or "")[:500],
             json.dumps(payload, ensure_ascii=False) if payload else None, run_id),
        )


BOARD_SQL = """
select a.id, a.name, a.role,
       coalesce(r.status, 'pending') as status,
       r.finished_at, r.duration_s
from agents a
left join lateral (
    select status, finished_at, duration_s
    from agent_runs ar
    where ar.agent_id = a.id
    order by ar.id desc
    limit 1
) r on true
order by a.seq
"""


def board() -> list[dict]:
    with conn() as c, c.cursor() as cur:
        cur.execute(BOARD_SQL)
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def counts() -> dict:
    out: dict[str, int] = {}
    for row in board():
        out[row["status"]] = out.get(row["status"], 0) + 1
    return out


def get_video(video_id: int) -> dict | None:
    with conn() as c, c.cursor() as cur:
        cur.execute(
            """select v.id, v.title, v.status, v.kind, v.script,
                      ch.slug, ch.name as channel_name, ch.language
               from videos v left join channels ch on ch.id = v.channel_id
               where v.id = %s""",
            (video_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))


def pending_videos(limit: int = 10) -> list[dict]:
    """Videos that are rendered and waiting for a human decision."""
    with conn() as c, c.cursor() as cur:
        cur.execute(
            """select v.id, v.title, v.status, ch.slug
               from videos v left join channels ch on ch.id = v.channel_id
               where v.status in ('ready', 'needs_review')
               order by v.id desc limit %s""",
            (limit,),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
