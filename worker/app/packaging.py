"""Packaging agent: hook title + 1280x720 thumbnail for each video.

The model proposes several titles; code then rejects any that break the channel's
rules (invented numbers, shouting, false urgency, too long) before one is used.
Nothing is uploaded here - the Publisher uses the result after approval.
"""

import logging
import os
import re
from urllib.parse import urlparse

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from app import llm, trends
from app.agents import _media_dir, agent
from app.visual import font_path

log = logging.getLogger("packaging")

W, H = 1280, 720
GOLD, WHITE, NAVY, TEAL, GREEN = (255, 200, 61), (255, 255, 255), (12, 20, 44), (16, 110, 140), (34, 197, 94)
ACRONYMS = {"RBI", "SEBI", "UPI", "EMI", "SIP", "PPF", "NPS", "GST", "ITR", "TDS", "EPF",
            "VPF", "KYC", "PAN", "NEFT", "IMPS", "ATM", "FD", "RD", "HRA", "LTA", "AMFI",
            "IRDAI", "NRI", "AI", "ETF", "IPO", "CIBIL", "UPI", "NSE", "BSE", "FY"}
# Words that would suggest the government or a regulator made or backs the video.
ENDORSE = ("official", "govt approved", "government approved", "government-approved",
           "rbi approved", "rbi-approved", "sebi approved", "sebi-approved",
           "endorsed by", "on behalf of", "authorised by", "authorized by",
           "government backed", "government-backed", "sarkari")

# Website names, domains or URLs must never appear in a video, title or thumbnail.
SITE = re.compile(r"(https?://|www\.|\b[a-z0-9-]+\.(?:gov\.in|nic\.in|org\.in|co\.in|"
                  r"ac\.in|in|com|org|net|gov)\b)", re.I)

HYPE = ("guaranteed", "100%", "get rich", "secret", "you won't believe", "shocking",
        "banned", "scam alert", "must watch", "before it's too late", "no one tells")


def _nums(text: str) -> set[str]:
    return {m.replace(",", "").rstrip(".") for m in re.findall(r"\d[\d,]*\.?\d*(?![\dA-Za-z])", text or "")}


def _valid(title: str, allowed: set[str]) -> str:
    """'' if fine, else the reason it was rejected."""
    t = title.strip()
    if not (20 <= len(t) <= 68):
        return "length"
    low = t.lower()
    if any(h in low for h in HYPE):
        return "hype"
    if any(e in low for e in ENDORSE):
        return "implies official endorsement"
    if SITE.search(t):
        return "contains website info"
    for w in re.findall(r"[A-Za-z]{4,}", t):
        if w.isupper() and w not in ACRONYMS:
            return "shouting"
    if t.count("!") > 0 or t.count("?") > 1:
        return "punctuation"
    for n in _nums(t):
        if n.isdigit() and int(n) <= 12:
            continue
        if n not in allowed:
            return "number not in sources: " + n
    return ""


def _fit_font(draw, text, path, size, max_w):
    while size > 30:
        f = ImageFont.truetype(path, size)
        if draw.textlength(text, font=f) <= max_w:
            return f
        size -= 4
    return ImageFont.truetype(path, 30)


def make_thumbnail(out: str, hook: str, badge: str, channel: str, lang: str = "en") -> None:
    img = Image.new("RGB", (W, H), NAVY)
    px = ImageDraw.Draw(img)
    for y in range(H):  # navy -> teal vertical blend
        t = y / (H - 1)
        px.line([(0, y), (W, y)], fill=tuple(int(NAVY[i] * (1 - t * 0.7) + TEAL[i] * t * 0.7)
                                               for i in range(3)))
    glow = Image.new("RGB", (W, H), (0, 0, 0))
    ImageDraw.Draw(glow).ellipse([W * 0.45, -H * 0.2, W * 1.2, H * 0.9], fill=(30, 90, 150))
    img = Image.blend(img, Image.composite(glow, img, glow.convert("L")), 0.45)
    img = img.filter(ImageFilter.GaussianBlur(0.6))
    d = ImageDraw.Draw(img)
    d.polygon([(0, H), (W * 0.35, H), (0, H * 0.62)], fill=GREEN)  # corner accent

    fp = font_path(lang)
    words = hook.upper().split() if lang == "en" else hook.split()
    lines, cur = [], ""
    for w_ in words:
        trial = (cur + " " + w_).strip()
        if cur and (len(trial) > 14 or len(lines) >= 2):
            lines.append(cur)
            cur = w_
        else:
            cur = trial
    lines.append(cur)
    lines = lines[:3]

    y = 150
    for i, ln in enumerate(lines):
        f = _fit_font(d, ln, fp, 190 if len(lines) < 3 else 150, W - 140)
        colour = GOLD if (i == len(lines) - 1 or any(c.isdigit() for c in ln)) else WHITE
        x = 70
        for dx, dy in ((-4, 0), (4, 0), (0, -4), (0, 4), (5, 5)):  # outline + shadow
            d.text((x + dx, y + dy), ln, font=f, fill=(0, 0, 0))
        d.text((x, y), ln, font=f, fill=colour)
        y += int(f.size * 1.12)

    if badge:  # empty by default: no site info on thumbnails
        bf = ImageFont.truetype(fp, 34)
        label = badge.upper()
        bw = int(d.textlength(label, font=bf)) + 56
        d.rounded_rectangle([60, 40, 60 + bw, 100], radius=14, fill=GREEN)
        d.text((88, 48), label, font=bf, fill=(8, 24, 16))
    cf = ImageFont.truetype(fp, 34)
    d.text((W - 60 - d.textlength(channel, font=cf), H - 70), channel, font=cf,
           fill=(200, 215, 235))
    img.convert("RGB").save(out, "JPEG", quality=95, optimize=True)


@agent("package")
async def package(ctx) -> dict:
    ch, scr = ctx["channel"], ctx["out"].get("script", {})
    res = ctx["out"].get("research", {})
    facts = res.get("facts", [])
    corpus = " ".join(f.get("fact", "") + " " + f.get("quote", "") for f in facts)
    corpus += " " + str(scr.get("title", ""))
    allowed = _nums(corpus)
    rep = trends.latest(ch["id"]) or {}
    formulas = "\n".join("- " + t for t in rep.get("title_formulas", [])[:5])
    hooks = "\n".join("- " + h.get("pattern", "") for h in rep.get("hook_patterns", [])[:5])

    prompt = f"""You are the Packaging agent for the YouTube channel "{ch['name']}"
(Indian personal finance, calm and trustworthy). Today's video:

Topic: {ctx['out'].get('scout', {}).get('topic', '')}
Working title: {scr.get('title', '')}
Verified facts you may reference:
{chr(10).join('- ' + f.get('fact', '') for f in facts[:10])}

Proven title shapes (learn the shape, never copy):
{formulas or '- (none yet)'}
Hook shapes:
{hooks or '- (none yet)'}

Write 6 title candidates and one thumbnail hook for each.
Rules:
- title 30 to 65 characters, names the concrete subject, makes one clear promise
- thumb_text is 2 to 4 words, plain, no emoji (e.g. "RBI NEW RULE", "EMI TRAP")
- numbers only if they appear in the verified facts above (or a list count 3-7)
- no hype, no "secret", no "guaranteed", no ALL-CAPS words except RBI, UPI, EMI etc.
- do not promise returns, do not imply advice, do not fake urgency
- the title must be true to the video; curiosity comes from specificity
- NO ENDORSEMENT - never say or imply that this video, channel or creator is official, government-run, or approved, endorsed or sponsored by the government, RBI, SEBI, the Income Tax Department or any regulator. Do not use the word 'official' to describe this video or channel. Cite sources plainly, e.g. 'according to an RBI press release' or 'the Income Tax Department's website says'.
- thumb_text must never contain the word OFFICIAL

Return JSON only:
{{"candidates": [{{"title": "...", "thumb_text": "...", "why": "..."}}], "best": 0}}"""
    data = await llm.generate_json(prompt, temperature=0.8)

    good, rejected = [], []
    for c in data.get("candidates", []):
        why = _valid(str(c.get("title", "")), allowed)
        (rejected if why else good).append({**c, "rejected": why} if why else c)
    if good:
        from app import guard
        fails = await guard.claim_fails([g["title"] for g in good], facts)
        rejected += [{**g, "rejected": "overclaims: " + fails[g["title"]]}
                     for g in good if g["title"] in fails]
        good = [g for g in good if g["title"] not in fails]
    if good:
        try:
            pick = data["candidates"][int(data.get("best", 0))]
            if pick not in good:
                pick = good[0]
        except Exception:
            pick = good[0]
    else:
        pick = {"title": str(scr.get("title", ""))[:68],
                "thumb_text": " ".join(str(scr.get("title", "")).split()[:3])}
        log.warning("all title candidates rejected: %s", [r["rejected"] for r in rejected])

    src = (ctx["out"].get("scout", {}) or {}).get("source_url") or \
          (facts[0].get("source") if facts else "")
    host = (urlparse(src).hostname or "").replace("www.", "")
    # A neutral citation, never "Official": the channel is not the government.
    badge = ""  # no site info in videos or thumbnails
    # Endorsement scan of everything the viewer reads or hears. A hit sends the video
    # to human review instead of shipping it.
    spoken = " ".join(str(s.get("narration", "")) for s in scr.get("sections", []))
    text_all = " ".join([spoken, str(scr.get("description", "")), str(pick.get("title", ""))]).lower()
    strong = ("govt approved", "government approved", "government-approved",
              "rbi approved", "rbi-approved", "sebi approved", "sebi-approved",
              "endorsed by the", "government backed", "government-backed",
              "official channel", "official video", "officially approved",
              "our official", "this official", "approved by the government",
              "approved by rbi", "approved by sebi", "in partnership with the")
    hits = sorted({p for p in strong if p in text_all})
    if hits:
        fa = ctx["out"].setdefault("fact", {})
        fa["risk"] = "medium"
        fa.setdefault("fixes", []).append("implies official endorsement: " + ", ".join(hits))
        log.warning("endorsement wording found: %s", hits)
    pick["thumb_text"] = str(pick.get("thumb_text") or "").replace("OFFICIAL", "").strip()
    thumb = os.path.join(_media_dir(ctx), "thumbnail.jpg")
    make_thumbnail(thumb, str(pick.get("thumb_text") or "MONEY RULE"), badge,
                   ch["name"], "ta" if ch.get("language") == "ta" else "en")
    if not good:  # fallback title must pass the claim check too
        from app import guard
        f2 = await guard.claim_fails([pick["title"]], facts)
        pick["claim_issue"] = f2.get(pick["title"], "")
    return {"title": pick["title"], "thumb_text": pick.get("thumb_text", ""),
            "claim_issue": pick.get("claim_issue", ""),
            "thumbnail": thumb, "alternatives": [g["title"] for g in good if g is not pick][:5],
            "rejected": [{"title": r["title"], "why": r["rejected"]} for r in rejected]}
