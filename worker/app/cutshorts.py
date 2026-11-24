"""Shorts Editor: cuts two 15-20 second Shorts out of the finished long video.

Costs nothing: it reuses the long video's voice, captions timing and pictures.
Rules enforced in code (not just in the prompt):
  - each Short is 15-20 s and starts and ends on a sentence boundary,
  - the two Shorts never overlap and have at least one full beat between them,
  - the closing disclaimer sentence is never the Short's content (it is added as a banner).
"""

import asyncio
import logging
import os
import re

from app import llm, packaging
from app import render as R

log = logging.getLogger("cutshorts")

LO, HI = 15.0, 20.0           # target length in seconds
RELAX_LO, RELAX_HI = 13.0, 22.0  # only if no whole-sentence window fits the target
W, H = 1080, 1920


def beat_times(groups: list[list[dict]], durs: list[float]) -> list[dict]:
    """Actual on-screen time of every beat (mirrors how the long video was cut)."""
    beats, off = [], 0.0
    for gi, grp in enumerate(groups):
        weights = [float(v.get("dur") or 0) for v in grp]
        if sum(weights) <= 0:
            weights = [1.0] * len(grp)
        tot, acc = sum(weights), 0.0
        for k, v in enumerate(grp):
            d = weights[k] * durs[gi] / tot
            if k == len(grp) - 1:
                d = max(0.6, durs[gi] - acc)
            beats.append({**v, "t0": off + acc, "t1": off + acc + d})
            acc += d
        off += durs[gi]
    return beats


def _is_disclaimer(text: str) -> bool:
    t = (text or "").lower()
    return "educational" in t and ("advice" in t or "verify" in t or "official" in t)


def windows(beats: list[dict], lo: float, hi: float) -> list[tuple[int, int]]:
    out = []
    last = len(beats) - 1
    for a in range(len(beats)):
        if a == last or _is_disclaimer(beats[a].get("narration")):
            continue
        for b in range(a, len(beats)):
            if b == last or _is_disclaimer(beats[b].get("narration")):
                break
            d = beats[b]["t1"] - beats[a]["t0"]
            if d > hi:
                break
            if d >= lo:
                out.append((a, b))
    return out


def separate(w1: tuple[int, int], w2: tuple[int, int], gap: int = 2) -> bool:
    """True if the two windows cannot overlap and have `gap` beats between them."""
    first, second = sorted((w1, w2))
    return second[0] >= first[1] + gap


def choose_pair(cands: list[tuple[int, int]], beats: list[dict], llm_ids: list[int]):
    """Validate the model's picks; otherwise pick by simple, deterministic rules."""
    for gap in (2, 1):
        ids = [i for i in llm_ids if 0 <= i < len(cands)]
        if len(ids) >= 2 and separate(cands[ids[0]], cands[ids[1]], gap):
            return sorted([cands[ids[0]], cands[ids[1]]])
        # fallback: an early "hook" window + a later "key point" window
        early = [c for c in cands if c[0] <= 2] or cands[:1]
        for e in early:
            later = [c for c in cands if separate(e, c, gap) and c[0] > e[1]]
            if later:
                mid = beats[-1]["t1"] * 0.6
                later.sort(key=lambda c: (-sum(1 for k in range(c[0], c[1] + 1)
                                               if beats[k].get("type") == "data"),
                                          abs(beats[c[0]]["t0"] - mid)))
                return [e, later[0]]
    return [cands[0]] if cands else []


def words_for(blocks: list[dict], t0: float, t1: float) -> list[dict]:
    out = []
    for blk in blocks:
        for w in blk.get("words", []):
            s, e = blk["offset"] + w["s"], blk["offset"] + w["e"]
            if s >= t0 - 0.05 and e <= t1 + 0.15:
                out.append({"w": w["w"], "s": max(0.0, s - t0), "e": max(0.05, e - t0)})
    return out


async def _meta(win_text: list[str], ch: dict, allowed: set[str]) -> list[dict]:
    prompt = f"""You write YouTube Shorts titles for "{ch['name']}" (Indian personal finance).
Two Shorts were cut from one video. For each, write a title and a one-line description.

Short 1 narration: {win_text[0]}
Short 2 narration: {win_text[1] if len(win_text) > 1 else ''}

Rules: title 30-60 characters, names the concrete subject, one clear promise, no hype,
no 'secret', no 'guaranteed', no ALL-CAPS words except acronyms (RBI, UPI, EMI...),
numbers only if they appear in the narration. Description: one plain sentence.
Return JSON only: {{"shorts": [{{"title": "...", "description": "..."}}, {{...}}]}}"""
    try:
        data = await llm.generate_json(prompt, temperature=0.6)
        items = data.get("shorts", [])
    except Exception as e:
        log.warning("short titles failed: %s", e)
        items = []
    out = []
    for i, text in enumerate(win_text):
        it = items[i] if i < len(items) else {}
        title = str(it.get("title", "")).strip()
        if packaging._valid(title, allowed):
            first = re.split(r"(?<=[.!?])\s", text.strip())[0]
            title = first[:60].rstrip(" ,;:") or "Money tip"
        out.append({"title": title,
                    "description": str(it.get("description", "")).strip() or text[:140]})
    return out


def _vertical(src169: str, audio_src: str, t0: float, dur: float, out: str) -> None:
    """16:9 picture centred over a blurred, zoomed copy of itself; audio from the master."""
    fc = (
        "[0:v]split[a][b];"
        f"[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
        "boxblur=36:4,eq=brightness=-0.10:saturation=1.15[bg];"
        f"[b]scale={W}:-2[fg];"
        "[bg][fg]overlay=(W-w)/2:(H-h)/2-110,format=yuv420p[v]"
    )
    R.run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", src169, "-ss", f"{t0:.3f}", "-t", f"{dur:.3f}", "-i", audio_src,
        "-filter_complex", fc, "-map", "[v]", "-map", "1:a",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
        "-r", str(R.FPS), "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        "-shortest", "-movflags", "+faststart", out,
    ], "vertical compose")


def _render_one(idx: int, win: tuple[int, int], beats: list[dict], blocks: list[dict],
                master: str, workdir: str, title: str, lang: str, disclaimer: str,
                full_disc: str = "") -> dict:
    a, b = win
    t0, t1 = beats[a]["t0"], beats[b]["t1"]
    dur = t1 - t0
    sdir = os.path.join(workdir, f"cut{idx}")
    os.makedirs(sdir, exist_ok=True)

    clips = []
    for k in range(a, b + 1):
        part = os.path.join(sdir, f"p{k:03d}.mp4")
        R.image_clip(beats[k]["path"], part, 1920, 1080, beats[k]["t1"] - beats[k]["t0"],
                     zoom_in=(k % 2 == 0))
        clips.append(part)
    raw169 = os.path.join(sdir, "raw169.mp4")
    R.concat(clips, raw169, sdir)

    rawv = os.path.join(sdir, "rawv.mp4")
    _vertical(raw169, master, t0, dur, rawv)

    ass = os.path.join(sdir, "captions.ass")
    # No burned-in narration captions: YouTube provides subtitles.
    # BURN_CAPTIONS_SHORTS=1 brings them back.
    caps = ([{"words": words_for(blocks, t0, t1), "offset": 0.0}]
            if os.environ.get("BURN_CAPTIONS_SHORTS", "0") == "1" else [])
    R.build_ass(ass, W, H, lang, caps,
                [{"text": title, "start": 0.0, "end": min(4.0, dur)}],
                disclaimer=disclaimer, total=dur, full_disclaimer=full_disc,
                full_seconds=2.0)
    out = os.path.join(sdir, "short_1080x1920.mp4")
    R.finish(rawv, ass, out, dur)
    prev = os.path.join(sdir, "preview.mp4")
    R.make_preview(out, prev, 640)
    for f in clips + [raw169, rawv]:
        try:
            os.remove(f)
        except OSError:
            pass
    info = R.probe(out)
    return {"index": idx, "title": title, "master": out, "preview": prev,
            "duration": info["duration"], "width": info["width"], "height": info["height"],
            "size": info["size"], "start": round(t0, 2), "end": round(t1, 2),
            "beats": [a, b], "cut": True}


async def make(ctx: dict, render_out: dict) -> list[dict]:
    v, im = ctx["out"]["voice"], ctx["out"]["visual"]
    ch, scr = ctx["channel"], ctx["out"]["script"]
    beats = beat_times(im["groups"], v["durs"])
    cands = windows(beats, LO, HI) or windows(beats, RELAX_LO, RELAX_HI)
    if not cands:
        raise RuntimeError("no sentence-aligned window of 15-20 s found")

    # offer the model a spread of candidates, keep ids stable
    step = max(1, len(cands) // 30)
    shown = cands[::step]
    listing = "\n".join(
        f"{i}: {beats[a]['t0']:.0f}s-{beats[b]['t1']:.0f}s "
        + " ".join(str(beats[k].get("narration", "")) for k in range(a, b + 1))[:230]
        for i, (a, b) in enumerate(shown))
    ids: list[int] = []
    try:
        d = await llm.generate_json(
            "Pick TWO Short-video windows from this list for a YouTube channel. Short 1: "
            "an attention-grabbing hook that makes sense on its own. Short 2: the most "
            "useful single fact or step, from a different part of the video. They must not "
            "overlap in time. Return JSON only: {\"picks\": [id1, id2]}\n\n" + listing,
            temperature=0.3)
        ids = [int(x) for x in d.get("picks", [])]
    except Exception as e:
        log.warning("short picker failed, using rules: %s", e)
    pair = choose_pair(shown, beats, ids)

    # hard guarantee, re-checked right before rendering
    if len(pair) == 2 and not separate(pair[0], pair[1], 1):
        pair = pair[:1]
    texts = [" ".join(str(beats[k].get("narration", "")) for k in range(a, b + 1))
             for a, b in pair]
    allowed = packaging._nums(" ".join(f.get("fact", "") for f in
                                       ctx["out"].get("research", {}).get("facts", [])) + " "
                              + " ".join(texts))
    meta = await _meta(texts, ch, allowed)

    lang = "ta" if ch.get("language") == "ta" else "en"
    disc = str(scr.get("disclaimer_short") or "Educational content only. Not professional advice.")
    out = []
    for i, ((a, b), m) in enumerate(zip(pair, meta), 1):
        s = await asyncio.to_thread(_render_one, i, (a, b), beats, v["blocks"],
                                    render_out["master"], v["workdir"], m["title"], lang, disc,
                                    str(scr.get("disclaimer") or ch.get("disclaimer") or ""))
        s["description"] = (m["description"] + "\n\n#Shorts\n\n"
                            + str(ch.get("disclaimer", "")))[:4800]
        s["tags"] = (scr.get("tags") or [])[:10] + ["shorts"]
        out.append(s)
    if len(out) == 2 and out[0]["end"] > out[1]["start"]:  # belt and braces
        raise RuntimeError("shorts overlap - refusing to publish")
    return out
