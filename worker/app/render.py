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

VSTOP = set("""
the a an and or but if then than that this these those is are was were be been
being of to in on for with from by as at into over under about after before
between during without you your we our they their it its he she his her i me my
us them what which who whom how why when where can could should would will
shall may might must do does did done have has had not no nor so very too also
just only more most much many some any all each every both few other such own
same out up down off again further once here there according per cent
""".split())

# Words that are technically content but make weak card headlines.
_WEAK = set("""
understanding specific certain various several important essential really
actually simply exactly properly correctly smoothly significantly
straightforward additional alternative general overall entire whole
distinguish summarize summarise exhausting disciplined verify examine ensure
check review understand section
""".split())

NUM_ANY = re.compile(r"(?<![A-Za-z0-9])\d[\d,]*(?:\.\d+)?(?![A-Za-z0-9])")
UNIT_AFTER = re.compile(
    r"\s*(%|per\s+cent|lakhs?|crores?|rupees?|months?|years?|days?|weeks?|"
    r"hours?|minutes?)\b", re.I)
CUR_BEFORE = re.compile(r"(?:\u20b9|rs\.?)\s*$", re.I)
BIG_UNIT = ("%", "per cent", "lakh", "lakhs", "crore", "crores")


def _indian_group(digits: str) -> str:
    """50000 -> 50,000; 1500000 -> 15,00,000."""
    if len(digits) <= 3:
        return digits
    head, tail = digits[:-3], digits[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ",".join(parts) + "," + tail


_UNITS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
    "seventy": 70, "eighty": 80, "ninety": 90,
}
_SCALES = {"hundred": 100, "thousand": 1000, "lakh": 100000,
           "lakhs": 100000, "crore": 10000000, "crores": 10000000}
_WORD_NUM = re.compile(
    r"\b((?:(?:%s)[\s-]+)+(?:hundred|thousand|lakhs?|crores?))\b"
    % "|".join(_UNITS), re.I)


def _parse_word_number(phrase: str) -> int:
    total, current = 0, 0
    for word in re.split(r"[\s-]+", phrase.lower()):
        if word in _UNITS:
            current += _UNITS[word]
        elif word in _SCALES:
            scale = _SCALES[word]
            if scale == 100:
                current = (current or 1) * 100
            else:
                total += (current or 1) * scale
                current = 0
    return total + current


def _word_numbers(text: str) -> str:
    """'fifty thousand rupees' -> '50000 rupees', so the slide can show it."""
    return _WORD_NUM.sub(lambda m: str(_parse_word_number(m.group(1))),
                         text or "")


def _highlight(text: str) -> str:
    """Pick the number a viewer should read off the screen, with its unit.

    Identifiers such as 80CCD or ITR-2 are ignored, and a number carrying a
    currency mark or a unit (%, lakh, months) beats a bare one.
    """
    t = _word_numbers(text or "")
    best: tuple[int, str, str, bool] | None = None
    for m in NUM_ANY.finditer(t):
        unit = ""
        score = 0
        cur = bool(CUR_BEFORE.search(t[:m.start()]))
        if cur:
            score += 2
        u = UNIT_AFTER.match(t[m.end():])
        if u:
            unit = re.sub(r"\s+", " ", u.group(1).lower())
            score += 2
            if unit in BIG_UNIT:
                score += 2
        digits = m.group(0).replace(",", "").strip()
        # A year ("Income Tax Act, 1961") is not a figure to put on a slide,
        # and a bare one- or two-digit number is usually a list marker.
        if len(digits) == 4 and digits.isdigit() and 1900 <= int(digits) <= 2099:
            continue
        if score == 0 and not (digits.isdigit() and len(digits) >= 4):
            continue
        if best is None or score > best[0]:
            best = (score, m.group(0).strip(), unit, cur)
    if not best:
        return ""
    _, val, unit, cur = best
    digits = val.replace(",", "")
    if digits.isdigit() and len(digits) >= 4:
        val = _indian_group(digits)
    if cur or unit in ("rupee", "rupees"):
        return "\u20b9" + val
    if unit == "%":
        return val + "%"
    return (val + " " + unit).strip()


def _visual_query(text: str) -> str:
    words = [w for w in re.findall(r"[a-zA-Z]{3,}", (text or "").lower())
             if w not in VSTOP]
    return " ".join(words[:6])


def _keyword(text: str, heading: str = "") -> str:
    """The one word a viewer should take from this beat.

    Captions carry the full sentence, so the card shows a single strong term
    ("deductions", "NPS", "exemptions") rather than a fragment of the line.
    Terms that also appear in the section heading, identifiers and numbers are
    preferred; adverbs and filler words are skipped.
    """
    head = set(re.findall(r"[a-z0-9()]{4,}", (heading or "").lower()))
    best: tuple[int, str] | None = None
    for m in re.finditer(r"[A-Za-z0-9][A-Za-z0-9()]{2,}", text or ""):
        raw = m.group(0)
        low = re.sub(r"[^a-z0-9]", "", raw.lower())
        if not low or low in VSTOP or low in _WEAK:
            continue
        if len(low) < 4 and not (raw.isupper() or any(c.isdigit() for c in raw)):
            continue
        if low.endswith("ly") and len(low) > 5:
            continue
        score = len(low)
        if low in head:
            score += 4
        if any(c.isdigit() for c in low):
            score += 4
        if raw.isupper() and len(raw) >= 3:
            score += 3
        if raw[0].isupper() and m.start() > 0:
            score += 1
        if best is None or score > best[0]:
            best = (score, raw)
    return best[1] if best else ""


def split_segments(text: str, total_dur: float, max_words: int = 42) -> list[dict]:
    """Split narration into sentence beats with approximate times.

    Times are proportional to word count and always sum to total_dur, so the
    beat visuals line up with the section audio.
    """
    raw = [p.strip() for p in re.split(r"(?<=[.!?])\s+", text or "") if p.strip()]
    if not raw:
        raw = [str(text or "").strip()]
    beats: list[str] = []
    for p in raw:
        words = p.split()
        if len(words) > max_words:
            for k in range(0, len(words), max_words):
                beats.append(" ".join(words[k:k + max_words]))
        else:
            beats.append(p)
    total_words = sum(len(b.split()) for b in beats) or 1
    out, t = [], 0.0
    for b in beats:
        d = total_dur * len(b.split()) / total_words
        out.append({"text": b, "start": t, "dur": d})
        t += d
    return out


def plan_segment_visual(seg: dict, section: dict, niche: str, lang: str,
                        index: int) -> dict:
    """Choose a data slide or an image for one narration beat."""
    text = str(seg.get("text") or "").strip()
    heading = str(section.get("heading") or "").strip()
    highlight = _highlight(text)
    seed = 1000 + index * 137
    if highlight and len(text.split()) >= 5:
        # The captions already carry the sentence, so a data slide stays clean:
        # section headline, the number, and nothing competing with the voice.
        return {
            "type": "data", "headline": heading, "highlight": highlight,
            "body": "", "caption": heading, "query": "",
            "dur": seg.get("dur", 6.0), "seed": seed,
        }
    query = _visual_query(text)
    if len(query.split()) < 2:
        query = str(section.get("visual_query") or heading or "")
    # If no relevant stock photo exists, this becomes a keyword card carrying
    # the one term this beat is about, so the screen changes with the voice.
    keyword = _keyword(text, heading) or heading
    return {
        "type": "image", "headline": heading,
        "keyword": keyword,
        "body": "", "caption": heading, "query": query.lower(),
        "center": (seed % 3 == 2),
        "dur": seg.get("dur", 6.0), "seed": seed,
    }


def build_storyboard(sections: list[dict], blocks: list[dict],
                     durs: list[float], niche: str, lang: str) -> list[dict]:
    """Plan the whole video beat by beat before anything is rendered.

    Each beat carries its narration, absolute start/end, and whether the screen
    should show a data slide or a related image (with the search query).
    """
    story: list[dict] = []
    offset = 0.0
    for i, sec in enumerate(sections):
        segs = []
        if blocks and i < len(blocks):
            segs = blocks[i].get("segments") or []
        if not segs:
            total = durs[i] if durs and i < len(durs) else 30.0
            segs = split_segments(str(sec.get("narration", "")), total)
        beats: list[dict] = []
        t = offset
        for j, seg in enumerate(segs):
            plan = plan_segment_visual(seg, sec, niche, lang, i * 100 + j)
            plan["start"] = round(t, 2)
            plan["end"] = round(t + plan["dur"], 2)
            plan["narration"] = seg["text"]
            beats.append(plan)
            t += plan["dur"]
        story.append({
            "section": i + 1,
            "heading": sec.get("heading", ""),
            "start": round(offset, 2),
            "end": round(t, 2),
            "beats": beats,
        })
        offset = t
    return story


def save_storyboard(story: list[dict], workdir: str) -> str:
    """Write the storyboard as JSON (machine) and Markdown (human)."""
    json_path = os.path.join(workdir, "storyboard.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(story, f, ensure_ascii=False, indent=2)

    lines = ["# Storyboard", ""]
    for sec in story:
        lines.append(
            f"## {sec['section']}. {sec['heading']}  "
            f"({sec['start']}s \u2013 {sec['end']}s)"
        )
        lines.append("")
        for b in sec["beats"]:
            tag = "DATA" if b.get("type") == "data" else "IMAGE"
            lines.append(
                f"- **{b['start']}s\u2013{b['end']}s** [{tag}] "
                f"{b.get('headline', '')}"
            )
            if b.get("highlight"):
                lines.append(f"  - highlight: `{b['highlight']}`")
            if b.get("keyword"):
                lines.append(f"  - keyword: `{b['keyword']}`")
            if b.get("query"):
                lines.append(f"  - search: `{b['query']}`")
            if b.get("visual"):
                lines.append(
                    f"  - visual: `{os.path.basename(b['visual'])}` "
                    f"({b.get('visual_source', '?')})"
                )
            lines.append(f"  - says: {b.get('narration', '')}")
            lines.append("")
    with open(os.path.join(workdir, "storyboard.md"), "w",
              encoding="utf-8") as f:
        f.write("\n".join(lines))
    return json_path


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


def _wrap_heading(text: str, width: int) -> str:
    """Escape for ASS and, on narrow screens, break into centred lines."""
    if width <= 0:
        return _ass_escape(text)
    import textwrap
    lines = textwrap.wrap(str(text), width) or [""]
    return "\\N".join(_ass_escape(x) for x in lines[:3])


def build_ass(path: str, w: int, h: int, lang: str,
              caption_blocks: list[dict], headings: list[dict],
              disclaimer: str = "", total: float = 0.0,
              start_s: float = 5.0, end_s: float = 5.0,
              full_disclaimer: str = "", full_seconds: float = 5.0) -> None:
    """captions + headings + the disclaimer banner, all in one subtitle track.

    The disclaimer lives here rather than in a drawtext filter because libass
    shapes complex scripts (Tamil) correctly while drawtext does not on every
    build. It is one line, drawn over the running video, at the start and end.
    """
    font_name = "Noto Sans Tamil" if lang == "ta" else "Noto Sans"
    vertical = h > w
    cap_size = int(h * (0.036 if vertical else 0.050))
    head_size = int(h * (0.034 if vertical else 0.042))
    margin_v = int(h * 0.075)
    disc_size = _one_line_size(disclaimer or " ", lang, int(h * 0.030),
                               w * 0.86)
    full_size = int(h * (0.031 if vertical else 0.036))

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
Style: Full,{font_name},{full_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&HB4000000,-1,0,0,0,100,100,0,0,4,{int(h*0.012)},0,5,{int(w*0.07)},{int(w*0.07)},0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [header]
    max_chars = (16 if lang == "ta" else 21) if vertical else (34 if lang == "ta" else 46)
    max_words = (3 if lang == "ta" else 4) if vertical else (6 if lang == "ta" else 8)

    for hd in headings:
        lines.append(
            f"Dialogue: 0,{_ass_time(hd['start'])},{_ass_time(hd['end'])},Head,,0,0,0,,"
            f"{_wrap_heading(hd['text'], 22 if vertical else 0)}\n"
        )

    if disclaimer and total > 0:
        text = _ass_escape(disclaimer)
        a0, a1 = 0.4, min(start_s, max(0.6, total - 0.2))
        b0 = max(a1 + 0.1, total - end_s)
        b1 = max(b0 + 0.1, total - 0.15)
        if not full_disclaimer.strip():  # the full end card replaces the start banner
            lines.append(
                f"Dialogue: 0,{_ass_time(a0)},{_ass_time(a1)},Disc,,0,0,0,,{text}\n"
            )
        if full_disclaimer.strip():
            import textwrap
            card_s = max(a1 + 0.3, total - full_seconds)  # last N seconds (5 long, 2 Shorts)
            wrapped = "\\N".join(_ass_escape(x) for x in textwrap.wrap(
                " ".join(full_disclaimer.split()), 27 if vertical else 62))
            lines.append(
                f"Dialogue: 1,{_ass_time(card_s)},{_ass_time(max(card_s + 0.5, total - 0.05))},"
                f"Full,,0,0,0,,{wrapped}\n"
            )
        else:
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


PALETTES = [  # four-colour loops for the animated card background
    ("0x12306e", "0x11a3c9", "0x7c3aed", "0x12306e"),
    ("0x2a1065", "0xc026d3", "0x2563eb", "0x2a1065"),
    ("0x0b3b4f", "0x10b981", "0x2563eb", "0x0b3b4f"),
    ("0x1e2a78", "0x3b82f6", "0x06b6d4", "0x1e2a78"),
]


def _orb(path: str, rgb: tuple, size: int = 900) -> None:
    """A soft glowing circle (transparent PNG) that drifts over the background."""
    from PIL import Image
    g = Image.radial_gradient("L").resize((size, size))
    alpha = g.point(lambda v: max(0, int((255 - min(255, v * 1.45)) * 0.45)))
    im = Image.new("RGBA", (size, size), rgb + (0,))
    im.putalpha(alpha)
    im.save(path, "PNG")


def animated_card_clip(fg: str, out: str, w: int, h: int, dur: float, seed: int,
                       workdir: str) -> None:
    """Moving gradient + two drifting light orbs, with the card sliding up and fading in."""
    pal = PALETTES[int(seed or 0) % len(PALETTES)]
    o1, o2 = os.path.join(workdir, "orb_a.png"), os.path.join(workdir, "orb_b.png")
    if not os.path.exists(o1):
        _orb(o1, (120, 220, 255))
    if not os.path.exists(o2):
        _orb(o2, (255, 190, 90))
    bg = (f"gradients=s={w}x{h}:c0={pal[0]}:c1={pal[1]}:c2={pal[2]}:c3={pal[3]}:"
          f"nb_colors=4:speed=0.035:rate={FPS}:d={dur:.3f}")
    fc = (
        "[1:v]format=rgba,fade=t=in:st=0:d=0.45:alpha=1[fg];"
        "[2:v]format=rgba[o1];[3:v]format=rgba[o2];"
        "[0:v]format=rgba[bg];"
        "[bg][o1]overlay=format=auto:x='W*0.10+W*0.16*sin(t*0.5)':y='H*0.05+H*0.14*cos(t*0.4)':shortest=1[b1];"
        "[b1][o2]overlay=format=auto:x='W*0.55+W*0.14*cos(t*0.45)':y='H*0.40+H*0.12*sin(t*0.55)':shortest=1[b2];"
        "[b2][fg]overlay=format=auto:x=0:y='38*pow(max(0,1-t/0.5),2)':shortest=1,"
        "eq=contrast=1.05:saturation=1.15:brightness=0.02,vignette=PI/7,format=yuv420p"
    )
    run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", bg,
        "-loop", "1", "-framerate", str(FPS), "-t", f"{dur:.3f}", "-i", fg,
        "-loop", "1", "-framerate", str(FPS), "-t", f"{dur:.3f}", "-i", o1,
        "-loop", "1", "-framerate", str(FPS), "-t", f"{dur:.3f}", "-i", o2,
        "-filter_complex", fc, "-an", "-t", f"{dur:.3f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
        "-pix_fmt", "yuv420p", "-r", str(FPS), "-movflags", "+faststart", out,
    ], "animated card")


def image_clip(image: str, out: str, w: int, h: int, dur: float,
               zoom_in: bool, fg: str = "", seed: int = 0) -> None:
    """A silent clip for one visual: animated card if a foreground exists, else Ken Burns."""
    dur = max(0.4, float(dur or 0))
    if fg and os.path.exists(fg):
        try:
            animated_card_clip(fg, out, w, h, dur, seed, os.path.dirname(out))
            return
        except Exception as e:  # never lose a clip over an effect
            log.warning("animated card failed, using still: %s", str(e)[:120])
    ow, oh = int(w * 1.28), int(h * 1.28)
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
        f"eq=contrast=1.04:saturation=1.06:brightness=-0.01,"
        f"vignette=PI/5,"
        f"format=yuv420p"
    )
    run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-loop", "1", "-framerate", str(FPS), "-i", image,
        "-t", f"{dur:.3f}",
        "-vf", vf,
        "-an",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
        "-pix_fmt", "yuv420p", "-r", str(FPS),
        "-movflags", "+faststart",
        out,
    ], "image clip")


def mux_audio(video: str, audio: str, out: str) -> None:
    """Attach the section narration to a silent section video."""
    run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", video, "-i", audio,
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
        "-shortest", "-movflags", "+faststart",
        out,
    ], "mux audio")


def section_clip_multi(visuals: list[dict], audio: str, out: str, w: int,
                       h: int, total_dur: float, workdir: str,
                       section_idx: int) -> None:
    """Build one section from several beat visuals, then attach its audio."""
    n = max(1, len(visuals))
    # Durations come from the storyboard beats. If a caller hands over bare
    # visuals without them, split the section evenly instead of writing 0-second
    # clips (which produce an empty, streamless mp4).
    weights = [float(v.get("dur") or 0) for v in visuals]
    if sum(weights) <= 0:
        weights = [1.0] * n
    total_vis = sum(weights)
    parts: list[str] = []
    acc = 0.0
    for k, v in enumerate(visuals):
        d = weights[k] * total_dur / total_vis
        if k == n - 1:
            d = max(0.6, total_dur - acc)
        part = os.path.join(workdir, f"s{section_idx}_p{k:02d}.mp4")
        image_clip(v["path"], part, w, h, d,
                   zoom_in=((section_idx + k) % 2 == 0),
                   fg=v.get("fg") or "", seed=int(v.get("seed") or k))
        parts.append(part)
        acc += d
    raw = os.path.join(workdir, f"s{section_idx}_raw.mp4")
    concat(parts, raw, workdir)
    mux_audio(raw, audio, out)
    for p in parts:
        try:
            os.remove(p)
        except OSError:
            pass
    try:
        os.remove(raw)
    except OSError:
        pass


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
        blocks.append({
            "words": v["words"], "offset": offset,
            "segments": split_segments(str(sec.get("narration", "")),
                                       v["duration"]),
        })
        headings.append({"text": str(sec.get("heading", "")), "start": offset,
                         "end": offset + min(4.5, v["duration"])})
        offset += v["duration"]
    return {"blocks": blocks, "headings": headings, "durs": durs,
            "audios": audios, "total": round(offset, 3)}


async def illustrate(story: list[dict], niche: str, lang: str, workdir: str,
                     w: int, h: int, concurrency: int = 4) -> list[list[dict]]:
    """Stage 2: build one visual per storyboard beat, grouped back per section.

    Data beats become slide cards; image beats search free stock and fall back
    to a slide. The returned groups are what Assemble cuts on, so the picture
    changes with the narration rather than once per section.
    """
    plans = [b for sec in story for b in sec.get("beats", [])]
    if not plans:
        raise RenderError("storyboard has no beats")
    visuals = await visual.build_visuals(plans, workdir, w, h, lang,
                                         concurrency=concurrency)
    groups: list[list[dict]] = []
    k = 0
    for sec in story:
        n = len(sec.get("beats", []))
        group = visuals[k:k + n]
        groups.append(group)
        for beat, vis in zip(sec.get("beats", []), group):
            # build_visuals returns the media record only; carry the beat's
            # timing and plan forward so the cutter knows how long to hold it.
            for field in ("dur", "start", "end", "highlight", "narration"):
                if field in beat and field not in vis:
                    vis[field] = beat[field]
            beat["visual"] = vis.get("path", "")
            beat["visual_source"] = vis.get("source", "")
        k += n
    return groups


def assemble(video_id: int, script: dict, channel: dict, workdir: str,
             blocks: list[dict], headings: list[dict], durs: list[float],
             groups: list[list[dict]], w: int, h: int, total: float,
             progress=None) -> dict:
    """Stage 3: beat visuals -> section clips -> concat -> captions -> master."""
    lang = channel.get("language", "en")
    clips = []
    for i, group in enumerate(groups):
        clip = os.path.join(workdir, f"sec_{i:02d}.mp4")
        audio = os.path.join(workdir, f"a{i:02d}.mp3")
        if progress:
            progress(f"clip {i+1}/{len(groups)}: {len(group)} visual(s)")
        if len(group) == 1:
            section_clip(group[0]["path"], audio, clip, w, h, durs[i],
                         zoom_in=(i % 2 == 0))
        else:
            section_clip_multi(group, audio, clip, w, h, durs[i], workdir, i)
        clips.append(clip)

    raw = os.path.join(workdir, "raw.mp4")
    concat(clips, raw, workdir)

    disc = str(script.get("disclaimer_short")
               or "Educational content only. Not professional advice.")
    ass_path = os.path.join(workdir, "captions.ass")
    build_ass(ass_path, w, h, lang, blocks, headings,
              disclaimer=disc, total=total,
              full_disclaimer=str(script.get("disclaimer") or ""))

    master = os.path.join(workdir, "master_1080p.mp4")
    if progress:
        progress("final pass: captions, disclaimer, loudness")
    finish(raw, ass_path, master, total)

    preview = os.path.join(workdir, "preview_480p.mp4")
    make_preview(master, preview)

    # Intermediates are large and useless once the master exists.
    for i in range(len(groups)):
        for name in (f"sec_{i:02d}.mp4", f"a{i:02d}.mp3"):
            try:
                os.remove(os.path.join(workdir, name))
            except OSError:
                pass
    try:
        os.remove(raw)
    except OSError:
        pass

    flat = [v for g in groups for v in g]
    info = probe(master)
    return {
        "master": master, "preview": preview,
        "duration": info["duration"], "width": info["width"],
        "height": info["height"], "vcodec": info["vcodec"],
        "acodec": info["acodec"], "size": info["size"],
        "overlay": disc, "sections": len(groups),
        "beats": len(flat),
        "data_slides": sum(1 for v in flat if v.get("type") == "data"),
        "sources": [v.get("source") for v in flat],
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

    story = build_storyboard(sections, n["blocks"], n["durs"],
                             channel.get("niche", ""), lang)
    save_storyboard(story, workdir)
    if progress:
        beats = sum(len(s["beats"]) for s in story)
        progress(f"storyboard: {beats} beats planned")
    groups = await illustrate(story, channel.get("niche", ""), lang, workdir, w, h)
    save_storyboard(story, workdir)  # rewrite with the chosen visual per beat
    out = await asyncio.to_thread(
        assemble, video_id, script, channel, workdir, n["blocks"],
        n["headings"], n["durs"], groups, w, h, n["total"], progress,
    )
    out["storyboard"] = os.path.join(workdir, "storyboard.json")
    return out


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
              disclaimer=disc, total=dur,
              full_disclaimer=str(script.get("disclaimer") or ""),
              full_seconds=2.0)

    master = os.path.join(workdir, "short_1080x1920.mp4")
    await asyncio.to_thread(finish, clip, ass_path, master, dur)
    preview = os.path.join(workdir, "preview.mp4")
    await asyncio.to_thread(make_preview, master, preview, 640)

    info = probe(master)
    return {"index": index, "title": short.get("title", ""), "master": master,
            "preview": preview, "duration": info["duration"],
            "width": info["width"], "height": info["height"], "size": info["size"]}
