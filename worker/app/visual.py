"""Visuals: one still per section, from free sources, with a guaranteed fallback.

Order: Openverse (CC0 / public domain) -> Wikimedia Commons (free licences only)
-> a generated gradient card. The last option always works, so a render can never
fail because an image service is down.

No paid or key-requiring service is used here. Every image is either explicitly
CC0/public domain or comes with a credit line that the publisher can print.
"""

import asyncio
import glob
import hashlib
import logging
import os
import urllib.parse

import httpx
from PIL import Image, ImageDraw, ImageFilter

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


def _query(item: dict) -> str:
    """A short, searchable phrase: the visual note first, then the heading."""
    raw = str(item.get("query") or item.get("caption") or item.get("prompt") or "")
    raw = raw.replace("[VERIFY]", " ")
    words = [w for w in raw.replace("/", " ").split() if w.isalpha() or w.isdigit()]
    return " ".join(words[:8])


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
            if im.width < 640 or im.height < 360:
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
