import asyncio
import logging

from app import db, healer
from app import packaging  # noqa: F401  (registers the agent)
from app.agents import AGENTS, Blocked

log = logging.getLogger("pipeline")

MAX_REVISIONS = 1


async def _run_agent(agent_id: str, video_id: int, ctx: dict) -> dict:
    run_id = db.record_start(agent_id, video_id)
    attempt = 0
    while True:
        try:
            out = await AGENTS[agent_id](ctx)
            ctx["out"][agent_id] = out
            db.record_finish(run_id, "done", payload=out)
            log.info("agent %s done", agent_id)
            return out
        except Blocked as e:  # a deliberate safety stop - never retried
            db.record_finish(run_id, "failed", note=str(e))
            raise
        except Exception as e:
            delay = healer.handle(agent_id, e, attempt)
            if delay is None:
                db.record_finish(run_id, "failed", note=str(e))
                log.exception("agent %s failed", agent_id)
                await healer.report(agent_id, e, video_id)
                raise
            attempt += 1
            await asyncio.sleep(delay)


async def run_pipeline(slug: str, kind: str = "long",
                       render: bool = True, progress=None) -> tuple[int, dict]:
    """Scout -> Research -> Brief -> Script -> FactCheck -> media, self-healing."""
    ch = db.get_channel(slug)
    video_id = db.create_video(ch["id"], kind)
    ctx = {"channel": ch, "kind": kind, "out": {}, "revisions": 0,
           "video_id": video_id, "progress": progress}
    log.info("pipeline start channel=%s video=%s", slug, video_id)

    # Scout + Research, retried with a different topic if the topic cannot be
    # grounded in official sources. Better to pick again than to publish a guess.
    for attempt in range(5):
        try:
            await _run_agent("scout", video_id, ctx)
            await _run_agent("research", video_id, ctx)
            break
        except Blocked as e:
            log.warning("research blocked (attempt %d/5): %s", attempt + 1, e)
            ctx.setdefault("avoid", []).append(
                str(ctx["out"].get("scout", {}).get("topic", ""))
            )
            if attempt == 4:
                db.set_video_status(video_id, "blocked")
                ctx["blocked"] = str(e)
                return video_id, ctx

    try:
        for agent_id in ("brief", "script", "fact"):
            await _run_agent(agent_id, video_id, ctx)
    except Blocked as e:
        db.set_video_status(video_id, "blocked")
        ctx["blocked"] = str(e)
        log.warning("pipeline blocked: %s", e)
        return video_id, ctx

    while ctx["revisions"] < MAX_REVISIONS:
        fa = ctx["out"].get("fact", {})
        if fa.get("risk") == "low" or not fa.get("fixes"):
            break
        ctx["revisions"] += 1
        log.info("revision pass %d (risk=%s)", ctx["revisions"], fa.get("risk"))
        await _run_agent("script", video_id, ctx)
        await _run_agent("fact", video_id, ctx)

    # Five minutes is a ceiling. Only rescue a script that is genuinely too short
    # to carry a topic - roughly under two and a half minutes.
    scr = ctx["out"].get("script", {})
    if (scr.get("total_seconds") or 0) < 150:
        ctx["last_words"] = scr.get("word_count", 0)
        ctx["expand"] = True
        log.info("expansion pass (was %s words)", ctx["last_words"])
        await _run_agent("script", video_id, ctx)
        await _run_agent("fact", video_id, ctx)
        scr = ctx["out"].get("script", {})

    # ...and a ceiling means a ceiling. Trim once if the writer overshot.
    if (scr.get("total_seconds") or 0) > 300:
        ctx["trim"] = True
        log.info("trim pass (was %ss)", scr.get("total_seconds"))
        await _run_agent("script", video_id, ctx)
        await _run_agent("fact", video_id, ctx)

    try:
        await _run_agent("package", video_id, ctx)
    except Exception as e:  # packaging is optional; the video still ships
        log.warning("packaging skipped: %s", e)

    db.save_script(video_id, ctx["out"])
    final = ctx["out"].get("fact", {})
    db.set_video_status(
        video_id, "scripted" if final.get("risk") == "low" else "needs_review"
    )

    if render:
        try:
            for agent_id in ("voice", "visual", "render", "qc"):
                await _run_agent(agent_id, video_id, ctx)
        except Exception as e:
            log.exception("media stage failed")
            ctx["media_error"] = str(e)[:600]
        db.save_script(video_id, ctx["out"])

        qc = ctx["out"].get("qc") or {}
        if ctx.get("media_error"):
            db.set_video_status(video_id, "render_failed")
        elif qc.get("pass") and final.get("risk") == "low":
            db.set_video_status(video_id, "ready")
        else:
            db.set_video_status(video_id, "needs_review")

    log.info("pipeline done video=%s revisions=%d risk=%s status=%s",
             video_id, ctx["revisions"], final.get("risk"),
             "rendered" if ctx.get("out", {}).get("render") else "script only")
    return video_id, ctx


def summary_text(ctx: dict) -> str:
    if ctx.get("blocked"):
        return (
            "\u26d4 <b>Blocked - no official source</b>\n\n"
            + str(ctx["blocked"])[:600]
            + "\n\nNo official source means no script. Nothing was generated."
        )
    sc = ctx["out"].get("scout", {})
    br = ctx["out"].get("brief", {})
    scr = ctx["out"].get("script", {})
    fa = ctx["out"].get("fact", {})
    res = ctx["out"].get("research", {})

    if res.get("enabled"):
        research_line = (
            f"<b>Research</b>: {len(res.get('facts', []))} verified facts "
            f"from {len(res.get('sources', []))} results "
            f"({', '.join(res.get('providers', [])) or '?'})"
            + (" · official domains only" if res.get("official_only") else "")
        )
        domains = sorted({f.get("domain", "") for f in res.get("facts", []) if f.get("domain")})
        if domains:
            research_line += "\n<b>Domains</b>: " + ", ".join(domains[:6])
        dropped = (res.get("dropped_unofficial") or 0) + (res.get("dropped_unquoted") or 0) \
            + (res.get("dropped_number") or 0) + (res.get("dropped_offtopic") or 0)
        if dropped:
            research_line += f"\n<b>Rejected</b>: {dropped} unverified claim(s)"
    else:
        research_line = "<b>Research</b>: \u274c no search results - ungrounded"

    lines = [
        "<b>" + str(scr.get("title", br.get("working_title", "untitled"))) + "</b>",
        "",
        "<b>Topic</b>: " + str(sc.get("topic", "")),
        "<b>Angle</b>: " + str(sc.get("angle", "")),
        research_line,
        "<b>Promise</b>: " + str(br.get("promise", "")),
        "",
        "<b>Hook</b>",
        str(br.get("hook", "")),
        "",
        "<b>Sections</b>",
    ]
    for i, s in enumerate(scr.get("sections", []), 1):
        lines.append(f"{i}. {s.get('heading', '')} <i>({s.get('seconds', '?')}s)</i>")

    shorts = scr.get("shorts", [])
    if shorts:
        lines += ["", "<b>Shorts</b>"]
        for s in shorts:
            lines.append(f"\u00b7 {s.get('title', '')} <i>({s.get('seconds', 60)}s)</i>")

    lines += [
        "",
        f"<b>{scr.get('word_count', 0)} words</b> \u00b7 "
        f"<b>{scr.get('total_seconds', 0)}s</b> "
        f"(limit {scr.get('max_seconds', 300)}s)",
        f"<b>Written by</b>: "
        + str((scr.get("_meta") or {}).get("model", "?")),
        f"<b>Revisions</b>: {ctx.get('revisions', 0)}",
        "",
        f"<b>FactCheck</b>: {fa.get('risk', '?')} "
        f"({len(fa.get('claims', []))} claims, {fa.get('mode', '?')})",
    ]
    bad = [
        c for c in fa.get("claims", [])
        if str(c.get("verdict", "")).lower() in
        ("risky", "needs_source", "contradicted", "not_in_sources")
    ]
    for c in bad[:4]:
        lines.append(f"  \u00b7 <i>{c.get('verdict')}</i>: {str(c.get('claim', ''))[:90]}")

    if fa.get("risk") != "low":
        lines += ["", "\u26a0\ufe0f Not ready to publish until the flagged claims are fixed."]

    r = ctx["out"].get("render") or {}
    qc = ctx["out"].get("qc") or {}
    v = ctx["out"].get("voice") or {}
    im = ctx["out"].get("visual") or {}

    if ctx.get("media_error"):
        lines += ["", "\u274c <b>Render failed</b>", str(ctx["media_error"])[:300]]
    elif r:
        lines += [
            "",
            f"<b>Video</b>: {r.get('duration', 0):.0f}s \u00b7 "
            f"{r.get('width')}\u00d7{r.get('height')} \u00b7 "
            f"{r.get('size', 0) / 1e6:.1f} MB",
            f"<b>Narration</b>: {v.get('total', 0):.0f}s in "
            f"{v.get('sections', 0)} sections",
        ]
        if im:
            lines.append(
                f"<b>Visuals</b>: {im.get('stock_images', 0)} stock \u00b7 "
                f"{im.get('cards', 0)} designed cards"
            )
        shorts = r.get("shorts") or []
        if shorts:
            lines.append(
                "<b>Shorts</b>: "
                + ", ".join(
                    "failed" if s.get("error") else f"{s.get('duration', 0):.0f}s"
                    for s in shorts
                )
            )
        if qc:
            bad = [c for c in qc.get("checks", []) if not c.get("ok")]
            if qc.get("pass"):
                lines.append("<b>QC</b>: \u2705 pass")
            else:
                lines.append(f"<b>QC</b>: \u26a0\ufe0f {len(bad)} issue(s)")
                for c in bad[:4]:
                    lines.append(f"  \u00b7 {c.get('check')}: {c.get('detail')}")
        if fa.get("risk") == "low" and qc.get("pass"):
            lines += ["", "\u2705 Ready for your approval."]

    return "\n".join(lines)
