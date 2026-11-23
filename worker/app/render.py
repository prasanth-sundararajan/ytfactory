"""FFmpeg assembly: stills -> Ken Burns clips -> captions -> 1080p master.

Everything is deterministic and local. No paid service is involved at this stage.
"""

import asyncio
import json
import logging
import os
import re
import subprocess

from app import visual, voice

log = logging.getLogger("render")

FPS = 25
CRF_MASTER = 20
LOUDNESS = "loudnorm=I=-14:TP=-1.5:LRA=11"


class RenderError(RuntimeError):
    pass


def run(cmd: list[str], what: str = "ffmpeg") -> str:
    log.info("%s: %s", what, " ".join(cmd[:6]) + (" ..." if len(cmd) > 6 else ""))
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        tail = (p.stderr or "")[-1200:]
        raise RenderError(f"{what} failed ({p.returncode}): {tail}")
    return p.stdout


def probe(path: str) -> dict:
    p = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", path],
        capture_output=True, text=True,
    )
    if p.returncode != 0:
        raise RenderError("ffprobe failed: " + (p.stderr or "")[-400:])
    d = json.loads(p.stdout or "{}")
    v = next((s for s in d.get("streams", []) if s.get("codec_type") == "video"), {})
    a = next((s for s in d.get("streams", []) if s.get("codec_type") == "audio"), {})
    return {
        "duration": round(float(d.get("format", {}).get("duration") or 0), 3),
        "width": int(v.get("width") or 0),
        "height": int(v.get("height") or 0),
        "vcodec": v.get("codec_name"),
        "acodec": a.get("codec_name"),
        "fps": v.get("r_frame_rate"),
        "has_audio": bool(a),
        "size": int(d.get("format", {}).get("size") or 0),
    }


# --------------------------------------------------------------------------- #
# measurements
# --------------------------------------------------------------------------- #

def loudness(path: str) -> float:
    """Integrated loudness in LUFS (EBU R128). Returns 0.0 if unreadable."""
    p = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", path,
         "-af", "ebur128=framelog=quiet", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    vals = re.findall(r"I:\s*(-?\d+(?:\.\d+)?)\s*LUFS", p.stderr or "")
    try:
        return round(float(vals[-1]), 1)
    except Exception:
        return 0.0


def dark_fraction(path: str, samples: int = 10) -> float:
    """Share of sampled frames that are essentially black.

    A section whose image failed to decode shows up here as a run of black
    frames, which is the failure viewers notice first.
    """
    import shutil
    import tempfile

    from PIL import Image

    dur = probe(path)["duration"]
    if dur <= 0:
        return 0.0
    tmp = tempfile.mkdtemp()
    values: list[float] = []
    try:
        for i in range(samples):
            t = dur * (i + 0.5) / samples
            frame = os.path.join(tmp, "f%02d.jpg" % i)
            subprocess.run(
                ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                 "-ss", "%.2f" % t, "-i", path, "-frames:v", "1",
                 "-vf", "scale=32:18", "-q:v", "5", frame],
                capture_output=True, text=True,
            )
            if not os.path.exists(frame):
                continue
            with Image.open(frame) as im:
                px = list(im.convert("L").getdata())
            values.append(sum(px) / max(1, len(px)))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if not values:
        return 0.0
    return sum(1 for v in values if v < 18) / len(values)


# --------------------------------------------------------------------------- #
# captions
# --------------------------------------------------------------------------- #

def _ass_time(t: float) -> str:
    t = max(0.0, t)
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _ass_escape(text: str) -> str:
    return (text or "").replace("\\", "＼").replace("{", "(").replace("}", ")")


def _group(words: list[dict], max_chars: int, max_words: int) -> list[list[dict]]:
    lines, cur = [], []
    for w in words:
        trial = cur + [w]
        text = " ".join(x["w"] for x in trial)
        if cur and (len(text) > max_chars or len(cur) >= max_words):
            lines.append(cur)
            cur = [w]
        else:
            cur = trial
    if cur:
        lines.append(cur)
    return lines


def _one_line_size(text: str, lang: str, size: int, max_w: float) -> int:
    """Shrink a font until the text fits on a single line."""
    try:
        from PIL import ImageFont

        path = visual.font_path(lang)
        for s in range(size, 15, -1):
            if ImageFont.truetype(path, s).getlength(text) <= max_w:
                return s
    except Exception:
        pass
    return max(16, size)


def build_ass(path: str, w: int, h: int, lang: str,
              caption_blocks: list[dict], headings: list[dict],
              disclaimer: str = "", total: float = 0.0,
              start_s: float = 5.0, end_s: float = 5.0) -> None:
    """captions + headings + the disclaimer banner, all in one subtitle track.

    The disclaimer lives here rather than in a drawtext filter because libass
    shapes complex scripts (Tamil) correctly while drawtext does not on every
    build. It is one line, drawn over the running video, at the start and end.
    """
    font_name = "Noto Sans Tamil" if lang == "ta" else "Noto Sans"
    cap_size = int(h * 0.050)
    head_size = int(h * 0.042)
    margin_v = int(h * 0.075)
    disc_size = _one_line_size(disclaimer or " ", lang, int(h * 0.030),
                               w * 0.86)

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{font_name},{cap_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,0,0,0,0,100,100,0,0,1,3,0,2,{int(w*0.06)},{int(w*0.06)},{margin_v},1
Style: Head,{font_name},{head_size},&H00E8E8E8,&H00FFFFFF,&H00101010,&HA0000000,-1,0,0,0,100,100,0,0,1,2,0,8,{int(w*0.06)},{int(w*0.06)},{int(h*0.055)},1
Style: Disc,{font_name},{disc_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H8C000000,-1,0,0,0,100,100,0,0,4,{int(h*0.011)},0,8,{int(w*0.05)},{int(w*0.05)},{int(h*0.145)},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [header]
    max_chars = 34 if lang == "ta" else 46
    max_words = 6 if lang == "ta" else 8

    for hd in headings:
        lines.append(
            f"Dialogue: 0,{_ass_time(hd['start'])},{_ass_time(hd['end'])},Head,,0,0,0,,"
            f"{_ass_escape(hd['text'])}\n"
        )

    if disclaimer and total > 0:
        text = _ass_escape(disclaimer)
        a0, a1 = 0.4, min(start_s, max(0.6, total - 0.2))
        b0 = max(a1 + 0.1, total - end_s)
        b1 = max(b0 + 0.1, total - 0.15)
        lines.append(
            f"Dialogue: 0,{_ass_time(a0)},{_ass_time(a1)},Disc,,0,0,0,,{text}\n"
        )
        lines.append(
            f"Dialogue: 0,{_ass_time(b0)},{_ass_time(b1)},Disc,,0,0,0,,{text}\n"
        )

    for blk in caption_blocks:
        for grp in _group(blk["words"], max_chars, max_words):
            if not grp:
                continue
            start = blk["offset"] + grp[0]["s"]
            end = blk["offset"] + grp[-1]["e"] + 0.12
            text = _ass_escape(" ".join(x["w"] for x in grp))
            lines.append(
                f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Cap,,0,0,0,,{text}\n"
            )

    with open(path, "w", encoding="utf-8") as f:
        f.writelines(lines)


# --------------------------------------------------------------------------- #
# clips
# --------------------------------------------------------------------------- #

def section_clip(image: str, audio: str, out: str, w: int, h: int,
                 dur: float, zoom_in: bool) -> None:
    ow, oh = int(w * 1.28), int(h * 1.28)
    # Spread the whole 14% push across the section so slow sections do not freeze
    # after a few seconds and long ones do not jitter.
    step = 0.14 / max(1.0, dur * FPS)
    if zoom_in:
        zexpr = f"min(1+{step:.6f}*on,1.14)"
    else:
        zexpr = f"max(1.14-{step:.6f}*on,1.0)"
    vf = (
        f"scale={ow}:{oh}:force_original_aspect_ratio=increase,"
        f"crop={ow}:{oh},"
        f"zoompan=z='{zexpr}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
        f"d=1:s={w}x{h}:fps={FPS},"
        # A light, consistent grade so mixed stock still looks like one series.
        f"eq=contrast=1.04:saturation=1.06:brightness=-0.01,"
        f"vignette=PI/5,"
        f"format=yuv420p"
    )
    run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-loop", "1", "-framerate", str(FPS), "-i", image,
        "-i", audio,
        "-t", f"{dur:.3f}",
        "-vf", vf,
        "-af", "apad",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
        "-pix_fmt", "yuv420p", "-r", str(FPS),
        "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
        "-movflags", "+faststart",
        out,
    ], "section clip")


def concat(clips: list[str], out: str, workdir: str) -> None:
    lst = os.path.join(workdir, "concat.txt")
    with open(lst, "w", encoding="utf-8") as f:
        for c in clips:
            f.write("file '" + c.replace("'", "'\\''") + "'\n")
    run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", lst,
        "-c", "copy", "-movflags", "+faststart", out,
    ], "concat")


def finish(raw: str, ass_path: str, out: str, dur: float) -> None:
    """Burn the subtitle track (captions, headings, disclaimer) and normalise audio."""
    vf = (
        f"ass={ass_path},"
        f"fade=t=in:st=0:d=0.5,fade=t=out:st={max(0.0, dur-0.7):.2f}:d=0.7,"
        f"format=yuv420p"
    )
    run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", raw,
        "-vf", vf,
        "-af", LOUDNESS,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(CRF_MASTER),
        "-pix_fmt", "yuv420p", "-r", str(FPS),
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        "-movflags", "+faststart",
        out,
    ], "final pass")


def prune_media(root: str, days: int = 14, keep: int = 12) -> int:
    """Delete old render directories, keeping the newest few regardless of age."""
    import shutil
    import time

    if not os.path.isdir(root):
        return 0
    entries = [e for e in os.scandir(root)
               if e.is_dir() and e.name.startswith("v")]
    entries.sort(key=lambda e: e.stat().st_mtime, reverse=True)
    now = time.time()
    removed = 0
    for i, e in enumerate(entries):
        if i < keep:
            continue
        if now - e.stat().st_mtime < days * 86400:
            continue
        shutil.rmtree(e.path, ignore_errors=True)
        removed += 1
    return removed


def make_preview(src: str, out: str, h: int = 480) -> None:
    run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", src,
        "-vf", f"scale=-2:{h}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "30",
        "-c:a", "aac", "-b:a", "96k",
        "-movflags", "+faststart",
        out,
    ], "preview")


# --------------------------------------------------------------------------- #
# orchestration
# --------------------------------------------------------------------------- #

def _slug(text: str, n: int = 40) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", (text or "section")).strip("-").lower()
    return (s[:n] or "section")


def plan_images(sections: list[dict], niche: str) -> list[dict]:
    out = []
    for i, s in enumerate(sections):
        note = str(s.get("visual_note") or "").strip()
        heading = str(s.get("heading") or "").strip()
        query = str(s.get("visual_query") or "").strip() or heading or note
        base = note or heading
        prompt = (
            f"{base}. Subject: {heading}. Context: a video about {niche}. "
            f"Wide cinematic composition"
        )
        out.append({"prompt": prompt, "caption": heading or note,
                    "query": query, "seed": 1000 + i * 137})
    return out


async def narrate(sections: list[dict], lang: str, workdir: str,
                  progress=None) -> dict:
    """Stage 1: speak every section, keep word timings."""
    blocks, headings, durs, audios = [], [], [], []
    offset = 0.0
    for i, sec in enumerate(sections):
        audio = os.path.join(workdir, f"a{i:02d}.mp3")
        if progress:
            progress(f"voice {i+1}/{len(sections)}: {str(sec.get('heading', ''))[:46]}")
        v = (await voice.synth_many([(str(sec.get("narration", "")), audio)], lang))[0]
        durs.append(v["duration"])
        audios.append(audio)
        blocks.append({"words": v["words"], "offset": offset})
        headings.append({"text": str(sec.get("heading", "")), "start": offset,
                         "end": offset + min(4.5, v["duration"])})
        offset += v["duration"]
    return {"blocks": blocks, "headings": headings, "durs": durs,
            "audios": audios, "total": round(offset, 3)}


async def illustrate(sections: list[dict], niche: str, lang: str, workdir: str,
                     w: int, h: int) -> list[dict]:
    """Stage 2: one still per section."""
    return await visual.build_images(plan_images(sections, niche), workdir, w, h, lang)


def assemble(video_id: int, script: dict, channel: dict, workdir: str,
             blocks: list[dict], headings: list[dict], durs: list[float],
             images: list[dict], w: int, h: int, total: float,
             progress=None) -> dict:
    """Stage 3: clips -> concat -> captions, disclaimer, loudness -> master."""
    lang = channel.get("language", "en")
    clips = []
    for i, im in enumerate(images):
        clip = os.path.join(workdir, f"sec_{i:02d}.mp4")
        audio = os.path.join(workdir, f"a{i:02d}.mp3")
        if progress:
            progress(f"clip {i+1}/{len(images)}")
        section_clip(im["path"], audio, clip, w, h, durs[i], zoom_in=(i % 2 == 0))
        clips.append(clip)

    raw = os.path.join(workdir, "raw.mp4")
    concat(clips, raw, workdir)

    disc = str(script.get("disclaimer_short")
               or "Educational content only. Not professional advice.")
    ass_path = os.path.join(workdir, "captions.ass")
    build_ass(ass_path, w, h, lang, blocks, headings,
              disclaimer=disc, total=total)

    master = os.path.join(workdir, "master_1080p.mp4")
    if progress:
        progress("final pass: captions, disclaimer, loudness")
    finish(raw, ass_path, master, total)

    preview = os.path.join(workdir, "preview_480p.mp4")
    make_preview(master, preview)

    # Intermediates are large and useless once the master exists.
    for i in range(len(images)):
        for name in (f"sec_{i:02d}.mp4", f"a{i:02d}.mp3"):
            try:
                os.remove(os.path.join(workdir, name))
            except OSError:
                pass
    try:
        os.remove(raw)
    except OSError:
        pass

    info = probe(master)
    return {
        "master": master, "preview": preview,
        "duration": info["duration"], "width": info["width"],
        "height": info["height"], "vcodec": info["vcodec"],
        "acodec": info["acodec"], "size": info["size"],
        "overlay": disc, "sections": len(images),
        "sources": [im.get("source") for im in images],
    }


async def render_long(script: dict, channel: dict, video_id: int, work_root: str,
                      progress=None) -> dict:
    """Convenience wrapper used by the CLI when a one-shot render is wanted."""
    sections = script.get("sections") or []
    if not sections:
        raise RenderError("script has no sections")
    lang = channel.get("language", "en")
    w, h = 1920, 1080
    workdir = os.path.join(work_root, f"v{video_id}")
    os.makedirs(workdir, exist_ok=True)

    if progress:
        progress(f"narration: {len(sections)} sections")
    n = await narrate(sections, lang, workdir, progress)
    if progress:
        progress(f"total narration {n['total']}s")
    imgs = await illustrate(sections, channel.get("niche", ""), lang, workdir, w, h)
    return await asyncio.to_thread(
        assemble, video_id, script, channel, workdir, n["blocks"],
        n["headings"], n["durs"], imgs, w, h, n["total"], progress,
    )


async def render_short(short: dict, script: dict, channel: dict, video_id: int,
                       index: int, work_root: str) -> dict:
    """One 1080x1920 short, built from its own narration."""
    lang = channel.get("language", "en")
    w, h = 1080, 1920
    workdir = os.path.join(work_root, f"v{video_id}_s{index}")
    os.makedirs(workdir, exist_ok=True)

    narration = str(short.get("narration") or "").strip()
    if not narration:
        raise RenderError("short has no narration")

    audio = os.path.join(workdir, "a.mp3")
    v = (await voice.synth_many([(narration, audio)], lang))[0]
    dur = min(v["duration"], 62.0)

    prompt = (f"{short.get('title', '')}. {short.get('hook', '')}. "
              f"Context: a short vertical video about {channel.get('niche', '')}. "
              f"Vertical cinematic composition, strong central subject")
    imgs = await visual.build_images(
        [{"prompt": prompt, "caption": str(short.get("title", "")),
          "query": str(short.get("visual_query") or short.get("title") or ""),
          "seed": 5000 + index * 311}], workdir, w, h, lang, concurrency=1
    )

    clip = os.path.join(workdir, "clip.mp4")
    await asyncio.to_thread(section_clip, imgs[0]["path"], audio, clip,
                            w, h, dur, (index % 2 == 0))

    ass_path = os.path.join(workdir, "captions.ass")
    disc = str(short.get("disclaimer_short") or script.get("disclaimer_short") or
               "Educational content only. Not professional advice.")
    build_ass(ass_path, w, h, lang,
              [{"words": v["words"], "offset": 0.0}],
              [{"text": str(short.get("title", "")), "start": 0.0,
                "end": min(4.0, dur)}],
              disclaimer=disc, total=dur)

    master = os.path.join(workdir, "short_1080x1920.mp4")
    await asyncio.to_thread(finish, clip, ass_path, master, dur)
    preview = os.path.join(workdir, "preview.mp4")
    await asyncio.to_thread(make_preview, master, preview, 640)

    info = probe(master)
    return {"index": index, "title": short.get("title", ""), "master": master,
            "preview": preview, "duration": info["duration"],
            "width": info["width"], "height": info["height"], "size": info["size"]}
