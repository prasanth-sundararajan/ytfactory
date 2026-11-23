"""CLI: python -m app.cli [channel-slug]"""

import argparse
import asyncio
import json

from app.db import list_channels
from app.pipeline import run_pipeline
from app.state import write_state


async def _deliver(video_id: int, ctx: dict) -> bool:
    """Send the rendered preview to the owner with approve/reject buttons."""
    from aiogram import Bot
    from aiogram.client.default import DefaultBotProperties
    from aiogram.types import FSInputFile

    from app.bot import _caption, approval_keyboard
    from app.config import CFG

    r = ctx["out"].get("render") or {}
    preview = r.get("preview")
    if not preview:
        return False
    b = Bot(CFG.telegram_token, default=DefaultBotProperties(parse_mode="HTML"))
    try:
        await b.send_video(
            CFG.owner_id, FSInputFile(preview),
            caption=_caption(ctx)[:1024],
            reply_markup=approval_keyboard(video_id),
            supports_streaming=True,
        )
        return True
    finally:
        await b.session.close()


async def _main(slug: str, show_json: bool, render: bool,
                deliver: bool = False) -> None:
    def progress(msg: str) -> None:
        print("   .", msg, flush=True)

    try:
        video_id, ctx = await run_pipeline(slug, render=render, progress=progress)
    except Exception as e:
        write_state()
        print("=" * 78)
        print("PIPELINE FAILED")
        print("=" * 78)
        print(type(e).__name__ + ": " + str(e)[:800])
        print("\nNothing was published. The video row is marked failed.")
        return
    write_state()

    if ctx.get("blocked"):
        print("=" * 78)
        print("BLOCKED - no official source")
        print("=" * 78)
        print(ctx["blocked"])
        return

    scr = ctx["out"].get("script", {})
    res = ctx["out"].get("research", {})
    fa = ctx["out"].get("fact", {})

    print("=" * 78)
    print("VIDEO ID:", video_id, "|", ctx["channel"]["name"], "| revisions:", ctx["revisions"])
    print("=" * 78)
    print("TITLE  :", scr.get("title"))
    print("WORDS  :", scr.get("word_count"), "| SECONDS:", scr.get("total_seconds"))
    print("TAGS   :", ", ".join(scr.get("tags", [])))

    if res.get("enabled"):
        print("PROVIDER:", ",".join(res.get("providers", [])) or "?",
              "| SOURCES:", len(res.get("sources", [])),
              "| FACTS:", len(res.get("facts", [])))
        print("OFFICIAL ONLY:", res.get("official_only"),
              "| dropped: %s unofficial, %s unquoted, %s unbacked numbers, %s off-topic"
              % (res.get("dropped_unofficial"), res.get("dropped_unquoted"),
                 res.get("dropped_number"), res.get("dropped_offtopic")))
        for f in res.get("facts", [])[:6]:
            print("   *", f.get("fact", "")[:96])
            print("     ", f.get("domain", ""), "|", f.get("source", "")[:70])
    else:
        print("SOURCES: none (", res.get("error", "?"), ")")

    print()
    for i, s in enumerate(scr.get("sections", []), 1):
        print(f"--- {i}. {s.get('heading')}  ({s.get('seconds')}s) ---")
        print(s.get("narration"))
        print(f"[visual: {s.get('visual_note')}]")
        print()

    for i, s in enumerate(scr.get("shorts", []), 1):
        print(f"--- SHORT {i}: {s.get('title')} ({s.get('seconds')}s) ---")
        print("hook:", s.get("hook"))
        print(s.get("narration"))
        print()

    print("=" * 78)
    print("DISCLAIMER (description):", scr.get("disclaimer"))
    print("DISCLAIMER (overlay)    :", scr.get("disclaimer_short"))
    ov = scr.get("disclaimer_overlay") or {}
    print("OVERLAY                 : %ss at start, %ss at end, over running video"
          % (ov.get("start_seconds"), ov.get("end_seconds")))
    print("SPOKEN (last line)      :", scr.get("spoken_disclaimer"))
    print("LENGTH                  : %ss of max %ss"
          % (scr.get("total_seconds"), scr.get("max_seconds", 300)))
    print("WRITTEN BY              :", (scr.get("_meta") or {}).get("model", "?"))
    print()
    print("FACTCHECK mode:", fa.get("mode"), "| risk:", fa.get("risk"))
    for c in fa.get("claims", []):
        print(f"  [{c.get('verdict')}] {c.get('claim')}")
        print(f"      -> {c.get('note')}")
    print()

    if ctx.get("media_error"):
        print("RENDER FAILED:", ctx["media_error"][:500])
    r = ctx["out"].get("render") or {}
    qc = ctx["out"].get("qc") or {}
    if r:
        print("MASTER :", r.get("master"))
        print("PREVIEW:", r.get("preview"))
        print("VIDEO  : %sx%s  %.1fs  %.1f MB  %s/%s"
              % (r.get("width"), r.get("height"), r.get("duration", 0),
                 r.get("size", 0) / 1e6, r.get("vcodec"), r.get("acodec")))
        print("VISUALS:", ctx["out"].get("visual", {}).get("sources"))
        for s in r.get("shorts") or []:
            if s.get("error"):
                print("SHORT %s FAILED: %s" % (s.get("index"), s["error"][:200]))
            else:
                print("SHORT %s: %sx%s %.1fs %.1f MB -> %s"
                      % (s.get("index"), s.get("width"), s.get("height"),
                         s.get("duration", 0), s.get("size", 0) / 1e6,
                         s.get("master")))
        if qc:
            print("QC     :", "PASS" if qc.get("pass") else "FAIL",
                  "| loudness", qc.get("loudness"), "LUFS")
            for c in qc.get("checks", []):
                print("   [%s] %s (%s)" % ("ok" if c["ok"] else "!!",
                                           c["check"], c["detail"]))
            for w in qc.get("warnings", []):
                print("   warning:", w)
        print()

    if deliver:
        try:
            sent = await _deliver(video_id, ctx)
            print("DELIVERED TO TELEGRAM" if sent else "NOTHING TO DELIVER")
        except Exception as e:
            print("TELEGRAM DELIVERY FAILED:", str(e)[:300])

    if show_json:
        print(json.dumps(ctx["out"], indent=2, ensure_ascii=False))


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m app.cli")
    p.add_argument("slug", nargs="?", default="money-clear")
    p.add_argument("--json", action="store_true", help="dump raw agent payloads")
    p.add_argument("--no-render", action="store_true",
                   help="script only, skip voice/visual/render/QC")
    p.add_argument("--deliver", action="store_true",
                   help="send the finished preview to Telegram for approval")
    p.add_argument("--send", type=int, metavar="VIDEO_ID",
                   help="re-send an already rendered video for approval")
    p.add_argument("--list", action="store_true", help="list channels")
    a = p.parse_args()

    if a.list:
        for c in list_channels():
            print(f"{c['slug']:<14} {c['language']}  {c['name']}")
        return

    if a.send:
        from app.db import get_video

        row = get_video(a.send)
        if not row:
            print("no such video")
            return
        ok = asyncio.run(_deliver(a.send, row["script"] or {}))
        print("SENT" if ok else "NOTHING TO SEND")
        return

    asyncio.run(_main(a.slug, a.json, not a.no_render, a.deliver))


if __name__ == "__main__":
    main()
