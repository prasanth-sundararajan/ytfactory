"""Re-render an existing video from its saved script (no LLM, no research).

Useful when iterating on the visual or render stages: the narration is spoken
again and the master, preview and storyboard are rebuilt from the script that
is already stored in the database.

Usage (inside the worker container):

    python /app/tools/rerender.py 16
"""

import asyncio
import json
import os
import sys

# Allow `python /app/tools/rerender.py` from inside the container.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, render  # noqa: E402

MEDIA = "/app/work/media"


async def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("usage: rerender.py <video_id>")
    video_id = int(sys.argv[1])
    video = db.get_video(video_id)
    if not video:
        raise SystemExit("no video %s" % video_id)

    # db.save_script stores the whole agent context, so the script is nested.
    stored = video.get("script") or {}
    if isinstance(stored, str):
        stored = json.loads(stored)
    script = stored.get("script") or {}
    if not script.get("sections"):
        raise SystemExit("video %s has no script" % video_id)

    channel = db.get_channel(video["slug"])
    print("RE-RENDER video", video_id, "|", video.get("title"), "|",
          channel["slug"])
    out = await render.render_long(script, channel, video_id, MEDIA,
                                   progress=lambda m: print("  .", m))
    print("MASTER :", out["master"])
    print("PREVIEW:", out["preview"])
    print("VIDEO  : %sx%s %.1fs %.1f MB"
          % (out["width"], out["height"], out["duration"],
             out["size"] / 1e6))
    print("BEATS  : %d | data slides %d"
          % (out["beats"], out["data_slides"]))
    print("STORY  :", out["storyboard"])
    print("SOURCES:", sorted(set(out["sources"])))
    print("QC     : loudness %.1f LUFS | dark samples %.0f%%"
          % (render.loudness(out["master"]),
             render.dark_fraction(out["master"]) * 100))


asyncio.run(main())
