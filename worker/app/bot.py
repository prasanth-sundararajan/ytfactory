import asyncio
import logging
import os

from aiogram import Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (CallbackQuery, FSInputFile, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

from app import analytics, healer, llm, news, trends, youtube
from app import schedule as SCH
from app import guard
from app.config import CFG
from app.db import (board, counts, get_video, list_channels, pending_videos,
                    set_published, set_video_status)
from app.pipeline import run_pipeline, summary_text

log = logging.getLogger("bot")

MARKS = {
    "pending": "\u25cb",
    "ready": "\u25d4",
    "running": "\u25d0",
    "done": "\u25cf",
    "failed": "\u2716",
    "blocked": "\u2298",
    "skipped": "\u2013",
}

# Uploads stay private unless YT_PRIVACY is explicitly changed. A private upload
# is still a real publish step (quota is spent), but nothing is public by accident.
PRIVACY = os.environ.get("YT_PRIVACY", "private")


def board_text() -> str:
    rows = board()
    lines = ["<b>YT FACTORY</b> \u00b7 agent pipeline", ""]
    for i, r in enumerate(rows, 1):
        mark = MARKS.get(r["status"], "?")
        name = r["name"]
        pad = " " * max(0, 14 - len(name))
        lines.append(f"{i:>2}. {mark} <code>{name}{pad}</code> {r['status'].upper()}")
    c = counts()
    summary = " \u00b7 ".join(f"{k} {v}" for k, v in sorted(c.items()))
    lines += ["", f"<b>{len(rows)} agents</b> \u2014 {summary}"]
    return "\n".join(lines)


def approval_keyboard(video_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="\u2705 Approve & upload",
                             callback_data=f"ap:{video_id}"),
        InlineKeyboardButton(text="\u274c Reject",
                             callback_data=f"rj:{video_id}"),
    ]])


def reject_keyboard(video_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="\u274c Reject", callback_data=f"rj:{video_id}")]])


async def _gated(video_id: int, ctx: dict) -> tuple[str, InlineKeyboardMarkup]:
    """Caption + buttons after the final guardrail gate (fails closed)."""
    problems = await guard.full_gate(ctx["out"])
    if not problems:
        return _caption(ctx)[:1024], approval_keyboard(video_id)
    set_video_status(video_id, "needs_review")
    warn = "\u26d4 <b>Blocked by guardrails - not approvable</b>\n" + "\n".join(
        "\u2022 " + p for p in problems[:6]) + "\n\n"
    return (warn + _caption(ctx))[:1024], reject_keyboard(video_id)


def _caption(ctx: dict) -> str:
    scr = ctx["out"].get("script") or {}
    r = ctx["out"].get("render") or {}
    fa = ctx["out"].get("fact") or {}
    qc = ctx["out"].get("qc") or {}
    shorts = [s for s in (r.get("shorts") or []) if not s.get("error")]
    return (
        f"<b>{scr.get('title', 'untitled')}</b>\n\n"
        f"{r.get('duration', 0):.0f}s \u00b7 {r.get('width')}\u00d7{r.get('height')} "
        f"\u00b7 {r.get('size', 0) / 1e6:.1f} MB\n"
        f"FactCheck: {fa.get('risk', '?')} \u00b7 "
        f"QC: {'pass' if qc.get('pass') else 'review'}\n"
        f"Shorts rendered: {len(shorts)}\n"
        f"<b>{SCH.describe() if SCH.enabled() else 'Privacy on upload: ' + PRIVACY}</b>\n\n"
        f"<i>{scr.get('disclaimer_short', '')}</i>"
    )


async def deliver(m: Message, video_id: int, ctx: dict) -> None:
    """Send the preview and the approve/reject buttons."""
    r = ctx["out"].get("render") or {}
    preview = r.get("preview")
    if not preview or not os.path.isfile(preview):
        await m.answer("\u26a0\ufe0f No video file to show (render failed).")
        return
    try:
        cap, kb = await _gated(video_id, ctx)
        await m.answer_video(
            FSInputFile(preview),
            caption=cap,
            reply_markup=kb,
            supports_streaming=True,
        )
        for sh in (r.get("shorts") or []):
            if sh.get("error") or not sh.get("preview") or not os.path.isfile(sh["preview"]):
                continue
            await m.answer_video(
                FSInputFile(sh["preview"]),
                caption=f"Short {sh.get('index')}: {sh.get('title', '')} "
                        f"({sh.get('duration', 0):.0f}s, {sh.get('start', 0):.0f}s-"
                        f"{sh.get('end', 0):.0f}s of the full video)"[:1024],
                supports_streaming=True)
        await _send_thumbs(lambda f, caption: m.answer_photo(f, caption=caption), ctx)
    except Exception as e:
        log.exception("could not send preview")
        await m.answer(
            "\u26a0\ufe0f Preview too large for Telegram (%s). "
            "The file is on the server; use /pending to retry." % str(e)[:150]
        )


async def _send_thumbs(send_photo, ctx: dict) -> None:
    t = ctx["out"].get("thumbnail") or {}
    for i, v in enumerate(t.get("variants") or [], 1):
        if os.path.isfile(v.get("path", "")):
            tag = "uploads with the video" if i == 1 else "alternative for an A/B test"
            await send_photo(FSInputFile(v["path"]),
                             caption=f"Thumbnail {i}: {v.get('text', '')} ({tag})"[:1024])


async def deliver_to_owner(b, video_id: int, ctx: dict) -> None:
    """Send the preview + approve buttons to the owner (used by the daily job)."""
    r = ctx["out"].get("render") or {}
    preview = r.get("preview")
    if not preview or not os.path.isfile(preview):
        await b.send_message(CFG.owner_id, f"\u26a0\ufe0f Video {video_id}: no preview file.")
        return
    cap, kb = await _gated(video_id, ctx)
    await b.send_video(CFG.owner_id, FSInputFile(preview), caption=cap,
                       reply_markup=kb, supports_streaming=True)
    for sh in (r.get("shorts") or []):
        if sh.get("error") or not sh.get("preview") or not os.path.isfile(sh["preview"]):
            continue
        await b.send_video(CFG.owner_id, FSInputFile(sh["preview"]),
                           caption=f"Short {sh.get('index')}: {sh.get('title', '')}"[:1024],
                           supports_streaming=True)
    await _send_thumbs(lambda f, caption: b.send_photo(CFG.owner_id, f, caption=caption), ctx)


async def _produce(m: Message, note: Message, slug: str) -> None:
    """Run the full pipeline without blocking the polling loop."""
    register.busy = True
    try:
        video_id, ctx = await run_pipeline(slug)
        from app.state import write_state

        write_state()
        await note.edit_text(summary_text(ctx))
        if not ctx.get("blocked"):
            await deliver(m, video_id, ctx)
    except Exception as e:
        log.exception("pipeline failed")
        try:
            await note.edit_text("\u274c " + str(e)[:400])
        except Exception:
            pass
    finally:
        register.busy = False


def register(dp: Dispatcher) -> None:
    register.busy = False

    async def owner_only(handler, event, data):
        user = getattr(event, "from_user", None)
        if not user or user.id != CFG.owner_id:
            log.warning("ignored update from %s", getattr(user, "id", "?"))
            return None
        return await handler(event, data)

    dp.message.middleware(owner_only)
    dp.callback_query.middleware(owner_only)

    @dp.message(Command("start", "help"))
    async def _start(m: Message):
        await m.answer(
            "<b>YT Factory</b>\n\n"
            "/status \u2014 agent pipeline\n"
            "/channels \u2014 the four channels\n"
            "/new [slug] \u2014 script, render and send for approval\n"
            "/pending \u2014 videos waiting for your approval\n"
            "/ping \u2014 test every LLM provider\n\n"
            "Nothing publishes without your approval."
        )

    @dp.message(Command("status"))
    async def _status(m: Message):
        await m.answer(board_text())

    @dp.message(Command("channels"))
    async def _channels(m: Message):
        rows = list_channels()
        lines = ["<b>Channels</b>", ""]
        for c in rows:
            lines.append(f"\u00b7 <code>{c['slug']:<12}</code> {c['name']} ({c['language']})")
        lines += ["", "Run one with <code>/new money-clear</code>"]
        await m.answer("\n".join(lines))

    @dp.message(Command("new"))
    async def _new(m: Message):
        parts = (m.text or "").split(maxsplit=1)
        slug = parts[1].strip() if len(parts) > 1 else "money-clear"
        if register.busy:
            await m.answer("\u23f3 A video is already being produced. "
                           "One at a time keeps the server responsive.")
            return
        note = await m.answer(
            f"Running pipeline for <b>{slug}</b>\u2026\n"
            "Research \u2192 Script \u2192 FactCheck \u2192 voice \u2192 visuals \u2192 render\n"
            "<i>This takes several minutes. You will get the video here.</i>"
        )
        asyncio.create_task(_produce(m, note, slug))

    @dp.message(Command("pending"))
    async def _pending(m: Message):
        rows = pending_videos(10)
        if not rows:
            await m.answer("Nothing is waiting for approval.")
            return
        lines = ["<b>Waiting for approval</b>", ""]
        for r in rows:
            lines.append(f"#{r['id']} \u00b7 {r['status']} \u00b7 {r['title'] or 'untitled'}")
            lines.append(f"     <code>{r['slug']}</code>")
        lines += ["", "Use /show &lt;id&gt; to get the video again."]
        await m.answer("\n".join(lines))

    @dp.message(Command("show"))
    async def _show(m: Message):
        parts = (m.text or "").split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip().isdigit():
            await m.answer("Usage: <code>/show 12</code>")
            return
        vid = int(parts[1].strip())
        row = get_video(vid)
        if not row:
            await m.answer("No such video.")
            return
        # videos store the agents output dict; deliver() expects it under "out"
        ctx = {"out": row["script"] or {}}
        await deliver(m, vid, ctx)

    @dp.message(Command("doctor"))
    async def _doctor(m: Message):
        await m.answer(healer.recent())

    @dp.message(Command("news"))
    async def _news(m: Message):
        note = await m.answer("Reading RBI / PIB announcements\u2026")
        try:
            r = await news.run("money-clear", force=True)
            cands = r.get("candidates") or []
            if not cands:
                await note.edit_text("No suitable official announcement today. "
                                     "Scout will use an evergreen topic.")
            else:
                await note.edit_text("<b>Official news candidates (tried in order)</b>\n"
                                     + "\n".join(f"{i}. {c['topic']}"
                                                 for i, c in enumerate(cands, 1)))
        except Exception as e:
            await note.edit_text("News pick failed: " + str(e)[:300])

    @dp.message(Command("trends"))
    async def _trends(m: Message):
        parts = (m.text or "").split()
        slug = parts[1] if len(parts) > 1 else "money-clear"
        note = await m.answer("Scanning YouTube trends\u2026 (about a minute)")
        try:
            rep = await trends.run(slug)
            await note.edit_text(trends.report_text(slug, rep, len(rep.get("top_videos", []))))
        except Exception as e:
            await note.edit_text("Trend scan failed: " + str(e)[:300])

    @dp.message(Command("stats"))
    async def _stats(m: Message):
        parts = (m.text or "").split()
        slugs = parts[1:] or [c["slug"] for c in list_channels()]
        note = await m.answer("Collecting analytics\u2026")
        out = []
        for s in slugs:
            try:
                await asyncio.to_thread(analytics.collect, s)
                out.append(analytics.stats_text(s))
            except Exception as e:
                out.append(f"<b>{s}</b>: {str(e)[:160]}")
        await note.edit_text("\n\n".join(out))

    @dp.message(Command("ping"))
    async def _ping(m: Message):
        note = await m.answer("Testing providers\u2026")
        rows = await llm.health()
        lines = ["<b>Provider health</b>", ""]
        for r in rows:
            mark = "\u2705" if r["ok"] else "\u274c"
            lines.append(f"{mark} {r['provider']} \u00b7 <code>{r['model']}</code>")
            if not r["ok"]:
                lines.append(f"     <i>{r['detail']}</i>")
        await note.edit_text("\n".join(lines))

    @dp.callback_query(F.data.startswith("ap:"))
    async def _approve(cb: CallbackQuery):
        vid = int(cb.data.split(":")[1])
        row = get_video(vid)
        if not row:
            await cb.answer("No such video", show_alert=True)
            return
        data = row["script"] or {}
        problems = await guard.full_gate(data)
        if problems:  # an old preview can never bypass the gate
            await cb.answer("Blocked by guardrails", show_alert=True)
            await cb.message.answer("\u26d4 Upload blocked:\n" + "\n".join(
                "\u2022 " + p for p in problems[:8]))
            return
        scr = data.get("script") or {}
        render = data.get("render") or {}
        master = render.get("master")
        if not master or not os.path.isfile(master):
            await cb.answer("Video file is missing", show_alert=True)
            return

        await cb.answer("Uploading\u2026")
        try:
            await cb.message.edit_caption(caption="\u23f3 Uploading to YouTube\u2026")
        except Exception:
            pass
        try:
            pkg = data.get("package") or {}
            credits = [c for c in (render.get("credits") or []) if c]
            desc = scr.get("description") or ""
            if credits:
                desc += "\n\nImages:\n" + "\n".join("- " + c for c in credits[:12])
            main_at = SCH.main_slot() if SCH.enabled() else None
            short_at = SCH.short_slots(main_at) if main_at else []
            res = youtube.upload(
                master,
                title=pkg.get("title") or scr.get("title") or "Untitled",
                description=desc,
                privacy=PRIVACY,
                tags=scr.get("tags") or [],
                category_id=str(scr.get("category_id") or 27),
                slug=row.get("slug"),
                publish_at=SCH.iso(main_at) if main_at else None,
            )
            set_published(vid, str(res.get("id")))
            thumb_note = ("\nGoes public: " + SCH.fmt(main_at)) if main_at else ""
            thumb = (data.get("thumbnail") or {}).get("path") or pkg.get("thumbnail")
            if thumb and os.path.isfile(thumb):
                try:
                    await asyncio.to_thread(youtube.set_thumbnail, str(res.get("id")),
                                            thumb, row.get("slug"))
                except Exception as te:
                    thumb_note = "\nThumbnail not set: " + str(te)[:120]
            shorts_note = ""
            for sh in (render.get("shorts") or []):
                if sh.get("error") or not sh.get("master") or not os.path.isfile(sh["master"]):
                    continue
                try:
                    link_desc = (str(sh.get("description") or "")
                                 .replace("#Shorts", "Full video: https://youtu.be/"
                                          + str(res.get("id")) + "\n\n#Shorts", 1))
                    r2 = await asyncio.to_thread(
                        youtube.upload, sh["master"], title=str(sh.get("title"))[:95],
                        description=link_desc, privacy=PRIVACY,
                        tags=sh.get("tags") or ["shorts"], category_id="27",
                        slug=row.get("slug"),
                        publish_at=(SCH.iso(short_at[int(sh.get("index", 1)) - 1])
                                    if len(short_at) >= int(sh.get("index", 1)) else None))
                    shorts_note += f"\nShort {sh.get('index')}: https://youtu.be/{r2.get('id')}"
                    if len(short_at) >= int(sh.get("index", 1)):
                        shorts_note += " (public " + SCH.fmt(short_at[int(sh.get("index", 1)) - 1]) + ")"
                except Exception as se:
                    shorts_note += f"\nShort {sh.get('index')} failed: " + str(se)[:100]
            link = "https://youtu.be/" + str(res.get("id"))
            await cb.message.edit_caption(
                caption=f"\u2705 Uploaded ({PRIVACY})\n{link}\n\n"
                        f"<b>{pkg.get('title') or scr.get('title', '')}</b>" + thumb_note + shorts_note
            )
        except Exception as e:
            log.exception("upload failed")
            await cb.message.edit_caption(
                caption="\u274c Upload failed: " + str(e)[:300]
            )

    @dp.callback_query(F.data.startswith("rj:"))
    async def _reject(cb: CallbackQuery):
        vid = int(cb.data.split(":")[1])
        set_video_status(vid, "rejected")
        await cb.answer("Rejected")
        try:
            await cb.message.edit_caption(
                caption="\u274c Rejected. Nothing was uploaded."
            )
        except Exception:
            pass

    @dp.message(F.text & ~F.text.startswith("/"))
    async def _chat(m: Message):
        await m.answer("Commands: /status  /channels  /new  /pending  /show  /stats  /trends  /news  /doctor  /ping")
