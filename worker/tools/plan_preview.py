"""Dry-run the storyboard plan for a saved script (no media generated).

Shows what the renderer would put on screen for every beat: a data slide with
its number, or a keyword card with its word.

Usage (inside the worker container):

    python /app/tools/plan_preview.py 16
"""

import json
import os
import sys

# Allow `python /app/tools/plan_preview.py` from inside the container.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, render  # noqa: E402


def main() -> None:
    video_id = int(sys.argv[1]) if len(sys.argv) > 1 else 16
    video = db.get_video(video_id)
    if not video:
        raise SystemExit("no video %s" % video_id)
    stored = video.get("script") or {}
    if isinstance(stored, str):
        stored = json.loads(stored)
    script = stored.get("script") or {}
    channel = db.get_channel(video["slug"])
    niche = channel.get("niche", "")
    lang = channel.get("language", "en")

    for i, sec in enumerate(script.get("sections") or []):
        print("\n== %d. %s" % (i + 1, sec.get("heading", "")))
        segs = render.split_segments(str(sec.get("narration", "")), 30.0)
        for j, seg in enumerate(segs):
            plan = render.plan_segment_visual(seg, sec, niche, lang, i * 100 + j)
            what = plan.get("highlight") or plan.get("keyword") or ""
            print("  [%-4s] %-24s | %s"
                  % (plan["type"], what[:24], seg["text"][:70]))


main()
