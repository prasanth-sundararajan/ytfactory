import asyncio
import logging
import os

from aiogram import Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (CallbackQuery, FSInputFile, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

from app import llm, youtube
from app.config import CFG
from app.db import (board, counts, get_video, list_channels, pending_videos,
                    set_video_status)
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
        f"Privacy on upload: <b>{PRIVACY}</b>\n\n"
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
        await m.answer_video(
            FSInputFile(preview),
            caption=_caption(ctx)[:1024],
            reply_markup=approval_keyboard(video_id),
            supports_streaming=True,
        )
    except Exception as e:
        log.exception("could not send preview")
        await m.answer(
            "\u26a0\ufe0f Preview too large for Telegram (%s). "
            "The file is on the server; use /pending to retry." % str(e)[:150]
        )


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
        ctx = row["script"] or {}
        await deliver(m, vid, ctx)

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
            credits = [c for c in (render.get("credits") or []) if c]
            desc = scr.get("description") or ""
            if credits:
                desc += "\n\nImages:\n" + "\n".join("- " + c for c in credits[:12])
            res = youtube.upload(
                master,
                title=scr.get("title") or "Untitled",
                description=desc,
                privacy=PRIVACY,
                tags=scr.get("tags") or [],
                category_id=str(scr.get("category_id") or 27),
            )
            set_video_status(vid, "published")
            link = "https://youtu.be/" + str(res.get("id"))
            await cb.message.edit_caption(
                caption=f"\u2705 Uploaded ({PRIVACY})\n{link}\n\n"
                        f"<b>{scr.get('title', '')}</b>"
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
        await m.answer("Commands: /status  /channels  /new  /pending  /show  /ping")
