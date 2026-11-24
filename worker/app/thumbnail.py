"""Thumbnail agent: thumbnail ideas + finished 1280x720 thumbnails.

  1. Thumbnail Director (LLM) proposes 3 concepts: hook text, cartoon scene, accent word.
  2. Each concept's text is checked in code (length, no endorsement words, numbers only
     from the verified facts, no shouting except the hook itself).
  3. Each concept gets a cartoon picture from the Image Studio providers (Cloudflare first,
     OpenAI fallback), composed with a left-side text panel.
  4. Concept 1 is the one uploaded; the others are kept for manual A/B tests in Studio.

If no picture can be made, it falls back to the plain Packaging thumbnail.
"""

import logging
import os
from urllib.parse import urlparse

import httpx
from PIL import Image, ImageDraw, ImageFont

from app import imagegen, llm, packaging
from app.agents import _media_dir, agent
from app.visual import font_path

log = logging.getLogger("thumbnail")
W, H = 1280, 720
GOLD, WHITE, BLACK = (255, 205, 60), (255, 255, 255), (0, 0, 0)

ART_W = 760  # right-hand art block; the left 520 px is the text panel
STYLE = ("Bold YouTube thumbnail art, bright friendly 2D cartoon, saturated colours, strong "
         "contrast, one large expressive subject filling a square frame, simple background. "
         "Every laptop, phone and screen is plain and unbranded with no icons; papers are blank. "
         "No text, no letters, no numbers, no logos, no app or play-button icons, no real people, "
         "no government emblems or seals, no banknotes, no watermark.")


def _check(text: str, allowed: set[str]) -> str:
    """'' if the thumbnail text is acceptable, else why not."""
    t = " ".join(text.split())
    words = t.split()
    if not 1 <= len(words) <= 4:
        return "must be 1-4 words"
    low = t.lower()
    if any(e in low for e in packaging.ENDORSE):
        return "implies official endorsement"
    if packaging.SITE.search(t):
        return "contains website info"
    if any(h in low for h in packaging.HYPE):
        return "hype"
    for n in packaging._nums(t):
        if not (n.isdigit() and int(n) <= 12) and n not in allowed:
            return "number not in sources: " + n
    return ""


def compose(art: str, out: str, text: str, accent: str, badge: str, channel: str,
            lang: str = "en") -> None:
    """Text panel on the left, cartoon art on the right, blended at the seam."""
    img = Image.new("RGB", (W, H), (10, 22, 56))
    d = ImageDraw.Draw(img)
    for y in range(H):  # navy -> deep blue panel gradient
        t = y / (H - 1)
        d.line([(0, y), (W, y)], fill=(int(10 + 8 * t), int(22 + 20 * t), int(56 + 40 * t)))
    pic = Image.open(art).convert("RGB").resize((ART_W, H))
    x0 = W - ART_W
    mask = Image.new("L", (ART_W, H), 255)
    md = ImageDraw.Draw(mask)
    for x in range(110):  # soft left edge of the art
        md.line([(x, 0), (x, H)], fill=int(255 * x / 110))
    img.paste(pic, (x0, 0), mask)
    d = ImageDraw.Draw(img)
    fp = font_path(lang)

    words = (text.upper() if lang == "en" else text).split()
    lines, cur = [], []
    for w_ in words:  # at most ~10 characters per line
        if cur and len(" ".join(cur + [w_])) > 10:
            lines.append(cur)
            cur = [w_]
        else:
            cur.append(w_)
    lines = (lines + [cur])[:3]
    if len(lines) > 1 and len(" ".join(lines[-1])) <= 2:
        if len(lines[-2]) > 1:  # "CHECK FORM / 16" -> "CHECK / FORM 16"
            lines[-1] = [lines[-2].pop()] + lines[-1]
        else:
            lines[-2] = lines[-2] + lines[-1]
            lines = lines[:-1]
    acc = (accent or "").upper()
    max_w = x0 + 40 - 48
    size = 140
    while size > 56:  # one size for all lines, the largest that fits
        f = ImageFont.truetype(fp, size)
        if all(d.textlength(" ".join(ln), font=f) <= max_w for ln in lines):
            break
        size -= 6
    f = ImageFont.truetype(fp, size)
    y = int(H * 0.52 - len(lines) * size * 0.56)
    space = d.textlength(" ", font=f)
    for ln in lines:
        x = 48
        for w_ in ln:  # colour only the accent word (and numbers) gold
            gold = (acc and w_.strip("?!.,") == acc) or any(ch.isdigit() for ch in w_)
            d.text((x, y), w_, font=f, fill=GOLD if gold else WHITE, stroke_width=7,
                   stroke_fill=BLACK)
            x += d.textlength(w_, font=f) + space
        y += int(size * 1.1)

    if badge:
        bf = ImageFont.truetype(fp, 26)
        bw = int(d.textlength(badge, font=bf)) + 32
        d.rounded_rectangle([40, 30, 40 + bw, 74], radius=12, fill=(34, 197, 94))
        d.text((56, 37), badge, font=bf, fill=(8, 24, 16))
    cf = ImageFont.truetype(fp, 28)
    d.text((48, H - 56), channel, font=cf, fill=(220, 230, 245), stroke_width=2,
           stroke_fill=BLACK)
    img.save(out, "JPEG", quality=95, optimize=True)


@agent("thumbnail")
async def thumbnail(ctx) -> dict:
    ch, out = ctx["channel"], ctx["out"]
    scr, pkg = out.get("script", {}), out.get("package", {})
    facts = out.get("research", {}).get("facts", [])
    allowed = packaging._nums(" ".join(f.get("fact", "") + " " + f.get("quote", "")
                                       for f in facts))
    title = pkg.get("title") or scr.get("title", "")

    prompt = f"""You are the Thumbnail Director for "{ch['name']}" (Indian personal finance,
calm and trustworthy). Video title: {title}
Topic: {out.get('scout', {}).get('topic', '')}
Verified facts:
{chr(10).join('- ' + f.get('fact', '') for f in facts[:8])}

Propose 3 different thumbnail concepts. Each has:
- "text": 2 to 4 punchy words that match the title's promise (e.g. "CHECK FORM 16",
  "EMI TRAP?", "UPI LIMIT"). No word "official", no hype, numbers only if in the facts.
- "accent": the one word from "text" to colour gold.
- "scene": one cartoon scene in 12-25 words showing an emotion or situation that makes
  people curious (e.g. "a surprised young Indian employee staring at a long salary slip
  with a magnifying glass"). Cartoon characters are fine; no real people, no logos, no
  government symbols, no text in the picture.
- "why": one line on why it earns a click honestly.
The thumbnail must never promise more than the video delivers.
Return JSON only: {{"concepts": [{{"text": "...", "accent": "...", "scene": "...", "why": "..."}}]}}"""
    data = await llm.generate_json(prompt, temperature=0.8)

    concepts, rejected = [], []
    for c in data.get("concepts", [])[:3]:
        why = _check(str(c.get("text", "")), allowed)
        (rejected if why else concepts).append({**c, "rejected": why} if why else c)

    from app import guard
    fails = await guard.claim_fails([c["text"] for c in concepts], facts)
    rejected += [{**c, "rejected": "overclaims: " + fails[c["text"]]}
                 for c in concepts if c["text"] in fails]
    concepts = [c for c in concepts if c["text"] not in fails]
    host = ""
    for f in facts:
        host = (urlparse(f.get("source", "")).hostname or "").replace("www.", "")
        if host:
            break
    badge = ""  # no site info in videos or thumbnails
    lang = "ta" if ch.get("language") == "ta" else "en"
    mdir = _media_dir(ctx)

    variants = []
    async with httpx.AsyncClient() as client:
        for i, c in enumerate(concepts, 1):
            art = os.path.join(mdir, f"thumb_art_{i}.jpg")
            order = [x.strip() for x in os.environ.get("THUMB_PROVIDERS", "openai,cloudflare").split(",")]
            raw = await imagegen._generate(client, f"{c['scene']}. {STYLE}",
                                           ctx["video_id"] * 10 + i, order)
            if not raw or not imagegen._finish(raw, art, ART_W, H):
                continue
            path = os.path.join(mdir, f"thumbnail_v{i}.jpg")
            compose(art, path, c["text"], c.get("accent", ""), badge, ch["name"], lang)
            variants.append({"path": path, "text": c["text"], "scene": c["scene"],
                             "why": c.get("why", "")})

    if not variants:  # no picture: keep the plain Packaging thumbnail
        log.warning("thumbnail art failed; using packaging thumbnail")
        return {"path": pkg.get("thumbnail", ""), "variants": [], "rejected": rejected,
                "fallback": True}
    return {"path": variants[0]["path"], "text": variants[0]["text"], "variants": variants,
            "rejected": rejected, "badge": badge, "fallback": False}
