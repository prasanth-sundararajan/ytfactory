"""Visuals: one still per storyboard beat, with a guaranteed fallback.

Long videos plan each beat as a data slide or a key-phrase card, and only use a
stock photo when `PEXELS_API_KEY` is set: keyword-matched free libraries return
art scans and manuscripts for finance topics, which look worse than a clean
card. Shorts still try Openverse (CC0 / public domain) then Wikimedia Commons
(free licences only) before falling back to a generated card, so a render can
never fail because an image service is down.

Every stock image is explicitly CC0/public domain or comes with a credit line
that the publisher can print.
"""

import asyncio
import glob
import hashlib
import logging
import os
import re
import urllib.parse

import httpx
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from app import imagegen

log = logging.getLogger("visual")

OPENVERSE = "https://api.openverse.org/v1/images/"
COMMONS = "https://commons.wikimedia.org/w/api.php"
PEXELS = "https://api.pexels.com/v1/search"
UA = "YTFactory/1.0 (educational video renderer; contact prasanthjeeva334@gmail.com)"

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]
TAMIL_CANDIDATES = [
    "/usr/share/fonts/truetype/noto/NotoSansTamil-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansTamil-Regular.ttf",
]

# Commons has no "only free" switch, so the licence is checked here.
FREE_LICENCES = ("cc0", "public domain", "pdm", "no restrictions")


def font_path(lang: str = "en") -> str:
    pool = (TAMIL_CANDIDATES + FONT_CANDIDATES) if lang == "ta" else FONT_CANDIDATES
    for p in pool:
        if os.path.exists(p):
            return p
    for p in glob.glob("/usr/share/fonts/**/*.ttf", recursive=True):
        return p
    raise RuntimeError("no TTF font on the system")


def _fit(img: Image.Image, w: int, h: int) -> Image.Image:
    img = img.convert("RGB")
    sw, sh = img.size
    scale = max(w / sw, h / sh)
    img = img.resize((max(1, int(sw * scale)), max(1, int(sh * scale))), Image.LANCZOS)
    left = (img.width - w) // 2
    top = (img.height - h) // 2
    return img.crop((left, top, left + w, top + h))


def _gradient_card(text: str, out_path: str, w: int, h: int, seed: int,
                   lang: str = "en") -> None:
    """Deterministic backdrop used when every image source fails.

    Deliberately textless: the heading is drawn by the subtitle layer, which
    shapes Tamil correctly and never duplicates itself.
    """
    hsh = hashlib.sha256(f"{seed}:{text}".encode("utf-8")).digest()
    a = (30 + hsh[0] % 40, 40 + hsh[1] % 40, 60 + hsh[2] % 50)
    b = (12 + hsh[3] % 20, 18 + hsh[4] % 25, 30 + hsh[5] % 30)

    img = Image.new("RGB", (w, h), a)
    draw = ImageDraw.Draw(img)
    for y in range(h):
        t = y / max(1, h - 1)
        draw.line(
            [(0, y), (w, y)],
            fill=(int(a[0] * (1 - t) + b[0] * t),
                  int(a[1] * (1 - t) + b[1] * t),
                  int(a[2] * (1 - t) + b[2] * t)),
        )

    # Soft light blob for depth.
    blob = Image.new("L", (w, h), 0)
    bd = ImageDraw.Draw(blob)
    cx = int(w * (0.25 + (hsh[6] % 50) / 100))
    cy = int(h * (0.20 + (hsh[7] % 40) / 100))
    r = int(min(w, h) * 0.55)
    bd.ellipse([cx - r, cy - r, cx + r, cy + r], fill=90)
    blob = blob.filter(ImageFilter.GaussianBlur(int(min(w, h) * 0.12)))
    img = Image.composite(Image.new("RGB", (w, h), (255, 255, 255)), img,
                          blob.point(lambda v: v // 3))

    # A faint diagonal rule set keeps an empty frame from looking like a bug.
    over = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    od = ImageDraw.Draw(over)
    gap = max(40, int(min(w, h) * 0.09))
    for i in range(-h, w, gap):
        od.line([(i, h), (i + h, 0)], fill=(255, 255, 255, 10),
                width=max(2, int(min(w, h) * 0.002)))
    img = Image.alpha_composite(img.convert("RGBA"), over).convert("RGB")

    img.save(out_path, "JPEG", quality=90)


def _wrap_text(draw, text: str, font, max_w: float, max_lines: int = 4) -> list[str]:
    """Greedy word wrap that never exceeds max_lines."""
    words = str(text or "").split()
    lines: list[str] = []
    cur = ""
    for w in words:
        trial = (cur + " " + w).strip()
        if draw.textlength(trial, font=font) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
            if len(lines) >= max_lines:
                break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    if len(lines) == max_lines:
        used = sum(len(x.split()) for x in lines)
        if used < len(words):
            lines[-1] = lines[-1].rstrip(" ,.;:") + "\u2026"
    return lines


def _fit_font(draw, text: str, path: str, size: int, max_w: float,
              min_size: int = 24) -> ImageFont.FreeTypeFont:
    """Shrink the size until the text fits on one line."""
    size = max(min_size, size)
    while size > min_size:
        font = ImageFont.truetype(path, size)
        if draw.textlength(text, font=font) <= max_w:
            return font
        size -= max(2, size // 18)
    return ImageFont.truetype(path, min_size)


def _slide_card_impl(spec: dict, out_path: str, w: int, h: int, lang: str = "en",
                     transparent: bool = False) -> None:
    """A data slide (big number) or a quote card (the spoken line).

    The palette is deliberately mid-navy, not black: the render pass adds a
    vignette and the QC gate rejects frames that average near-black.
    """
    seed = int(spec.get("seed") or 7)
    hsh = hashlib.sha256(
        f"{seed}:{spec.get('headline', '')}".encode("utf-8")
    ).digest()
    top = (30 + hsh[0] % 20, 44 + hsh[1] % 22, 68 + hsh[2] % 26)
    bot = (18, 26, 44)
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0)) if transparent else Image.new("RGB", (w, h), top)
    draw = ImageDraw.Draw(img)
    for y in range(0 if transparent else h):
        t = y / max(1, h - 1)
        draw.line([(0, y), (w, y)], fill=(
            int(top[0] * (1 - t) + bot[0] * t),
            int(top[1] * (1 - t) + bot[1] * t),
            int(top[2] * (1 - t) + bot[2] * t),
        ))
    if transparent:  # glass panel so text stays readable on the moving background
        draw.rounded_rectangle([int(w * 0.045), int(h * 0.15), int(w * 0.955), int(h * 0.80)],
                               radius=int(h * 0.035), fill=(8, 14, 34, 120),
                               outline=(255, 255, 255, 46), width=2)
    # Accent bar alternates sides so consecutive cards do not look identical.
    bar_w = max(6, int(w * 0.012))
    if seed % 2:
        draw.rectangle([w - bar_w, 0, w, h], fill=(34, 197, 94))
    else:
        draw.rectangle([0, 0, bar_w, h], fill=(34, 197, 94))

    fp = font_path(lang)
    kick_f = ImageFont.truetype(fp, int(h * 0.030))
    small_f = ImageFont.truetype(fp, int(h * 0.024))

    x = int(w * 0.075)
    max_w = int(w * 0.82)
    center = bool(spec.get("center"))
    highlight = str(spec.get("highlight") or "").strip()
    keyword = str(spec.get("keyword") or "").strip()
    headline = str(spec.get("headline") or "").strip()

    def kicker(y0: int) -> int:
        if not headline:
            return y0
        for line in _wrap_text(draw, headline, kick_f, max_w, 1):
            tx = x
            if center:
                tx = max(x, int((w - draw.textlength(line, font=kick_f)) / 2))
            draw.text((tx, y0), line, font=kick_f, fill=(146, 162, 182))
            y0 += int(h * 0.048)
        return y0

    if highlight:
        hi_f = _fit_font(draw, highlight, fp, int(h * 0.150), max_w)
        block_h = int(h * 0.185) + (int(h * 0.048) if headline else 0)
        y = max(int(h * 0.20), int((h - block_h) / 2))
        draw.text((x, y), highlight, font=hi_f, fill=(52, 211, 153))
        y += int(h * 0.185)
        kicker(y)
    else:
        word = keyword or headline
        word_f = _fit_font(draw, word, fp, int(h * 0.125), max_w)
        step = int(h * 0.150)
        block_h = step + (int(h * 0.048) if headline else 0)
        y = max(int(h * 0.20), int((h - block_h) / 2))
        y = kicker(y)
        tx = x
        if center:
            tx = max(x, int((w - draw.textlength(word, font=word_f)) / 2))
        draw.text((tx, y), word, font=word_f, fill=(52, 211, 153))

    draw.text((x, h - int(h * 0.06)), "YT Factory", font=small_f,
              fill=(120, 136, 156))
    if transparent:
        img.save(out_path, "PNG")
    else:
        img.save(out_path, "JPEG", quality=92)


def slide_card(spec: dict, out_path: str, w: int, h: int, lang: str = "en",
               fg_path: str | None = None) -> None:
    """JPEG card (still fallback) plus, optionally, a transparent PNG foreground
    that the renderer lays over an animated background."""
    _slide_card_impl(spec, out_path, w, h, lang)
    if fg_path:
        _slide_card_impl(spec, fg_path, w, h, lang, transparent=True)


def data_banner(spec: dict, path: str, w: int, h: int, lang: str = "en") -> None:
    """Lay the number on a cartoon picture as a bottom banner, keeping the art visible."""
    base = Image.open(path).convert("RGB")
    ov = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    top = int(h * 0.55)
    for y in range(top, h):  # soft dark scrim, strongest at the bottom
        a = int(225 * ((y - top) / (h - top)) ** 0.85)
        d.line([(0, y), (w, y)], fill=(6, 12, 30, a))
    fp = font_path(lang)
    big = str(spec.get("highlight") or spec.get("keyword") or spec.get("headline") or "")
    f = _fit_font(d, big, fp, int(h * 0.19), int(w * 0.86))
    x, y = int(w * 0.06), int(h * 0.72)
    stroke = max(4, int(h * 0.006))
    head = str(spec.get("headline") or "")
    if head and head != big:
        kf = ImageFont.truetype(fp, int(h * 0.036))
        d.text((x, y - int(h * 0.062)), head, font=kf, fill=(255, 255, 255, 255),
               stroke_width=3, stroke_fill=(0, 0, 0, 255))
    d.text((x, y), big, font=f, fill=(255, 205, 60, 255),
           stroke_width=stroke, stroke_fill=(0, 0, 0, 255))
    base.paste(ov, (0, 0), ov)
    base.save(path, "JPEG", quality=93)


def _query(item: dict) -> str:
    """A short, searchable phrase: the visual note first, then the heading."""
    raw = str(item.get("query") or item.get("caption") or item.get("prompt") or "")
    raw = raw.replace("[VERIFY]", " ")
    words = [w for w in raw.replace("/", " ").split() if w.isalpha() or w.isdigit()]
    return " ".join(words[:8])


# Stock libraries index concrete subjects, not narration. Mapping the topic
# vocabulary to photographable objects is what turns "claim old tax regime"
# into "tax documents calculator", which actually returns a picture.
CONCEPTS = [
    ("mutual fund", "stock market chart"),
    ("capital gain", "stock market chart"),
    ("sip", "investment growth chart"),
    ("nps", "pension retirement savings"),
    ("pension", "retirement savings"),
    ("insurance", "insurance policy document"),
    ("credit card", "credit card payment"),
    ("loan", "loan documents signing"),
    ("rent", "rent agreement paperwork"),
    ("salary", "salary payslip office"),
    ("payslip", "salary payslip office"),
    ("itr", "tax form paperwork"),
    ("tax", "tax documents calculator"),
    ("gst", "invoice paperwork"),
    ("bank", "bank passbook"),
    ("budget", "budget planning notebook"),
    ("saving", "savings coins jar"),
    ("invest", "investment growth chart"),
    ("fraud", "phone scam warning"),
    ("claim", "documents signing pen"),
    ("form", "filling form pen"),
    ("deadline", "calendar deadline clock"),
    ("document", "documents paperwork desk"),
    ("account", "bank account passbook"),
    ("interest", "interest rate chart"),
    ("payment", "online payment phone"),
    ("salary slip", "salary payslip office"),
]


def _query_variants(item: dict) -> list[str]:
    """Search phrases from most to least specific, best first."""
    q = _query(item)
    head = _query({"query": item.get("headline") or item.get("caption") or ""})
    low = (q + " " + head).lower()

    out: list[str] = []
    hits: list[str] = []
    for key, stock in CONCEPTS:
        if key in low and stock not in hits:
            hits.append(stock)
    out.extend(hits[:2])

    words = q.split()
    if len(words) >= 2:
        out.append(" ".join(words[:4]))
    if len(words) >= 3:
        out.append(" ".join(words[:2]))
    if head:
        out.append(" ".join(head.split()[:3]))

    seen, uniq = set(), []
    for v in out:
        v = " ".join(v.split())
        if v and v not in seen:
            seen.add(v)
            uniq.append(v)
    return uniq[:3] or [q or "finance"]


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z]{4,}", (text or "").lower()))


def _relevant(query: str, haystack: str) -> bool:
    """Reject a photo whose title/tags share almost nothing with the query.

    Without this, a Commons search for 'old tax regime' happily returns a
    1789 French Revolution engraving, which is worse than a clean text card.
    """
    q = _tokens(query)
    if not q:
        return True
    need = max(1, min(2, len(q)))
    return len(q & _tokens(haystack)) >= need


async def _download(client, url: str, out_path: str, w: int, h: int) -> bool:
    try:
        r = await client.get(url, timeout=70)
    except Exception as e:
        log.info("download failed: %s", str(e)[:100])
        return False
    if r.status_code != 200 or len(r.content) < 15000:
        log.info("download returned %s / %d bytes", r.status_code, len(r.content))
        return False
    tmp = out_path + ".raw"
    try:
        with open(tmp, "wb") as f:
            f.write(r.content)
        with Image.open(tmp) as im:
            if im.width < 1280 or im.height < 720:  # no heavy upscaling to 1080p
                log.info("image too small: %dx%d", im.width, im.height)
                return False
            from PIL import ImageStat
            g = im.convert("L").resize((640, int(640 * im.height / im.width)))
            sharp = ImageStat.Stat(g.filter(ImageFilter.FIND_EDGES)).var[0]
            if sharp < 40:
                log.info("image too soft (%.0f)", sharp)
                return False
            _fit(im, w, h).save(out_path, "JPEG", quality=92)
        os.remove(tmp)
        return True
    except Exception as e:
        log.info("image unusable: %s", str(e)[:100])
        return False
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


async def _pexels(client, query: str, out_path: str, w: int, h: int,
                  used: set[str], key: str) -> dict | None:
    """Modern stock photography. Free key, no card, no cost per request."""
    if not key:
        return None
    params = {
        "query": query,
        "per_page": 15,
        "size": "large",
        "orientation": "landscape" if w >= h else "portrait",
    }
    try:
        r = await client.get(PEXELS, params=params,
                             headers={"Authorization": key})
        photos = r.json().get("photos", []) if r.status_code == 200 else []
    except Exception as e:
        log.info("pexels search failed: %s", str(e)[:100])
        return None

    for photo in photos:
        src = photo.get("src") or {}
        url = src.get("large2x") or src.get("original")
        if not url or url in used:
            continue
        if await _download(client, url, out_path, w, h):
            used.add(url)
            return {
                "credit": "%s - %s (Pexels licence)" % (
                    str(photo.get("alt") or "photo")[:70],
                    str(photo.get("photographer") or "Pexels")[:40],
                ),
                "source_url": photo.get("url") or url,
            }
    return None


async def _openverse(client, query: str, out_path: str, w: int, h: int,
                     used: set[str]) -> dict | None:
    # CC BY needs attribution, which the publisher prints in the description.
    # Share-alike licences are deliberately excluded.
    base = {
        "q": query,
        "license": "cc0,pdm,by",
        "page_size": 20,
        "mature": "false",
        "size": "large",
    }
    attempts = [
        dict(base, aspect_ratio="wide" if w >= h else "tall"),
        base,
    ]
    for params in attempts:
        try:
            r = await client.get(OPENVERSE, params=params)
            results = r.json().get("results", []) if r.status_code == 200 else []
        except Exception as e:
            log.info("openverse search failed: %s", str(e)[:100])
            return None

        # Keep the API's relevance order: sorting by pixel count pulls in
        # unrelated high-resolution scans.
        for item in results:
            url = item.get("url")
            if not url or url in used:
                continue
            if (item.get("width") or 0) < 1000 or (item.get("height") or 0) < 560:
                continue
            tags = " ".join(str(t.get("name") or "") for t in
                            (item.get("tags") or []) if isinstance(t, dict))
            hay = " ".join([str(item.get("title") or ""),
                            str(item.get("description") or ""), tags])
            if not _relevant(query, hay):
                continue
            if await _download(client, url, out_path, w, h):
                used.add(url)
                return {
                    "credit": "%s - %s (%s, Openverse)" % (
                        str(item.get("title") or "untitled")[:70],
                        str(item.get("creator") or "unknown")[:40],
                        str(item.get("license") or "cc0").upper(),
                    ),
                    "source_url": item.get("foreign_landing_url") or url,
                }
    return None


async def _commons(client, query: str, out_path: str, w: int, h: int,
                   used: set[str]) -> dict | None:
    params = {
        "action": "query",
        "format": "json",
        "generator": "search",
        "gsrsearch": query + " filetype:bitmap",
        "gsrnamespace": 6,
        "gsrlimit": 30,
        "prop": "imageinfo",
        "iiprop": "url|size|extmetadata",
        "iiurlwidth": 1920,
    }
    try:
        r = await client.get(COMMONS, params=params)
        pages = ((r.json().get("query") or {}).get("pages") or {})
    except Exception as e:
        log.info("commons search failed: %s", str(e)[:100])
        return None

    for page in pages.values():
        info = (page.get("imageinfo") or [{}])[0]
        meta = info.get("extmetadata") or {}
        lic = str((meta.get("LicenseShortName") or {}).get("value") or "").lower()
        if not any(tag in lic for tag in FREE_LICENCES):
            continue
        url = info.get("thumburl") or info.get("url")
        if not url or url in used:
            continue
        if (info.get("width") or 0) < 900:
            continue
        desc = re.sub(r"<[^>]+>", " ", str(
            (meta.get("ImageDescription") or {}).get("value") or ""))
        hay = " ".join([str(page.get("title") or ""), desc])
        if not _relevant(query, hay):
            continue
        if await _download(client, url, out_path, w, h):
            used.add(url)
            artist = str((meta.get("Artist") or {}).get("value") or "Wikimedia")
            artist = artist.split("<")[0][:40]
            return {
                "credit": "%s - %s (%s)" % (
                    str(page.get("title") or "").replace("File:", "")[:70],
                    artist, lic.upper()[:20]),
                "source_url": info.get("descriptionurl") or url,
            }
    return None


async def build_images(prompts: list[dict], out_dir: str, w: int, h: int,
                       lang: str = "en", concurrency: int = 3) -> list[dict]:
    """prompts: [{prompt, caption, query, seed}] -> [{path, source, credit}]."""
    os.makedirs(out_dir, exist_ok=True)
    sem = asyncio.Semaphore(concurrency)
    used: set[str] = set()
    results: list[dict] = [None] * len(prompts)  # type: ignore[list-item]

    async with httpx.AsyncClient(headers={"User-Agent": UA},
                                 follow_redirects=True) as client:
        key = (os.environ.get("PEXELS_API_KEY") or "").strip()

        async def one(i: int, item: dict) -> None:
            async with sem:
                path = os.path.join(out_dir, f"img_{i:02d}.jpg")
                seed = int(item.get("seed") or (i * 977 + 13))
                query = _query(item)
                found = await _pexels(client, query, path, w, h, used, key)
                source = "pexels"
                if not found:
                    found = await _openverse(client, query, path, w, h, used)
                    source = "openverse"
                if not found:
                    found = await _commons(client, query, path, w, h, used)
                    source = "commons"
                if not found:
                    source = "generated"
                    _gradient_card(item.get("caption") or query, path, w, h,
                                   seed, lang)
                results[i] = {
                    "path": path,
                    "source": source,
                    "query": query,
                    "prompt": item.get("prompt", ""),
                    "credit": (found or {}).get("credit", ""),
                    "source_url": (found or {}).get("source_url", ""),
                }
                log.info("image %d/%d via %s (%s)", i + 1, len(prompts),
                         source, query[:40])

        await asyncio.gather(*(one(i, p) for i, p in enumerate(prompts)))
    return results


async def build_visuals(plans: list[dict], out_dir: str, w: int, h: int,
                        lang: str = "en", concurrency: int = 3,
                        channel: dict | None = None) -> list[dict]:
    """Build one visual per narration beat.

    A plan with type "data" becomes a slide card (no network). An "image" plan
    searches free stock first, and falls back to a text slide if nothing fits -
    so the screen is never a blank gradient.
    """
    os.makedirs(out_dir, exist_ok=True)
    sem = asyncio.Semaphore(concurrency)
    used: set[str] = set()
    results: list[dict] = [None] * len(plans)  # type: ignore[list-item]

    # Image Studio: the creative director picks scenes + formats once per video.
    scenes: dict[int, dict] = {}
    if imagegen.enabled():
        scenes = await imagegen.plan_scenes(plans, channel or {"name": "Money Clear"})
        log.info("image studio: %d AI scenes planned (%s)", len(scenes),
                 ",".join(imagegen.providers()))

    async with httpx.AsyncClient(headers={"User-Agent": UA},
                                 follow_redirects=True) as client:
        key = (os.environ.get("PEXELS_API_KEY") or "").strip()

        async def one(i: int, item: dict) -> None:
            async with sem:
                path = os.path.join(out_dir, f"vis_{i:03d}.jpg")
                sc = scenes.get(i)
                if sc:
                    rec = await imagegen.create(
                        client, sc, path, w, h, "default",
                        int(item.get("seed") or i))
                    if rec and item.get("type") == "data":
                        data_banner(item, path, w, h, lang)
                    if rec:
                        results[i] = {
                            "path": path, "source": "ai",
                            "type": "data" if item.get("type") == "data" else "image",
                            "query": sc["subject"][:60], "format": sc["format"],
                            "headline": item.get("headline", ""),
                            "credit": "", "source_url": "",
                        }
                        log.info("visual %d/%d via ai/%s", i + 1, len(plans),
                                 sc["format"])
                        return
                if item.get("type") == "data":
                    slide_card(item, path, w, h, lang,
                               fg_path=path[:-4] + "_fg.png")
                    results[i] = {
                        "path": path, "source": "slide", "type": "data",
                        "fg": path[:-4] + "_fg.png", "seed": item.get("seed"),
                        "query": "", "headline": item.get("headline", ""),
                        "credit": "", "source_url": "",
                    }
                    return
                variants = _query_variants(item)
                query = variants[0]
                found = None
                source = "slide"
                # Pexels is the only keyless-quality source. Openverse and
                # Commons match keywords but return art scans and manuscripts,
                # so a clean card beats an unrelated picture every time.
                if key:
                    for q in variants:
                        found = await _pexels(client, q, path, w, h, used, key)
                        if found:
                            query = q
                            source = "pexels"
                            break
                if not found:
                    slide_card(item, path, w, h, lang,
                               fg_path=path[:-4] + "_fg.png")
                    source = "slide"
                results[i] = {
                    "path": path, "source": source, "type": "image",
                    "fg": (path[:-4] + "_fg.png") if source == "slide" else "",
                    "seed": item.get("seed"),
                    "query": query, "headline": item.get("headline", ""),
                    "credit": (found or {}).get("credit", ""),
                    "source_url": (found or {}).get("source_url", ""),
                }
                log.info("visual %d/%d via %s (%s)", i + 1, len(plans),
                         source, query[:40])

        await asyncio.gather(*(one(i, p) for i, p in enumerate(plans)))
    # One honest disclosure line in the description, not one per image.
    for r in results:
        if r and r.get("source") == "ai":
            r["credit"] = "Illustrations are AI-generated"
            break
    return results
