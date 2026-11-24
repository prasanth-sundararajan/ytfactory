"""Image Studio: the channel's AI image creator, run like a small creative department.

  Creative Director (LLM, one call per video)
      reads the whole storyboard, picks the scenes that deserve a picture and, for
      each, a FORMAT from the format book plus one concrete visual metaphor.
  Format book
      six fixed shot formats so the channel looks consistent and varied on a routine.
  Brand kit
      one style preamble per channel (palette, medium, hard bans).
  Producer
      builds the final prompt, calls the first configured provider, checks the
      result and caches it. Any failure falls back to the existing card/stock path,
      so a render can never be blocked by an image service.

Images are stylised illustrations only: no real people, faces, logos, banknotes or
text (text is added in code). That keeps them honest and policy-safe.
"""

import asyncio
import base64
import hashlib
import io
import json
import logging
import os
import time

import httpx
from PIL import Image, ImageEnhance, ImageFilter, ImageStat

from app import llm

log = logging.getLogger("imagegen")

MAX_PER_VIDEO = int(os.environ.get("IMAGE_MAX_PER_VIDEO", "60"))
CACHE_DIR = os.environ.get("IMAGE_CACHE_DIR", "/app/data/imgcache")

BRAND = {
    "default": {
        "medium": "bright friendly 2D cartoon illustration, bold clean shapes, smooth flat "
                  "colours with soft shading, like a modern animated explainer video",
        "palette": "vibrant rich-blue background with gold, orange and pink accents, high "
                   "contrast and clearly readable",
        "mood": "cheerful, clear and trustworthy",
        "bans": ("no text, no letters, no numbers, no logos, no watermark, no real people or "
                 "celebrities, no photographic realism, no real banknote designs, no government "
                 "emblems, seals, stamps or national symbols, no website addresses, nothing that "
                 "looks like an "
                 "official document or government building sign"),
    },
}

# The routine: each format is a reusable shot recipe.
FORMATS = {
    "HOOK": "one large expressive hero object centred, dramatic light, lots of clean space",
    "CONCEPT": "clear scene explaining one idea with two or three simple objects, tidy layout",
    "PROCESS": "left-to-right scene of three connected steps shown as simple icons joined by "
               "arrows or a path",
    "WARNING": "caution scene: a damaged or tilting object with a warning-triangle shape, "
               "orange-red accent",
    "EVERYDAY": "a friendly cartoon character (a young Indian salaried professional) in an "
                "everyday money situation, expressive and relatable",
    "COMPARE": "split scene comparing two options side by side, each with one clear object",
    "CLOSEUP": "close-up of one key object with rich cartoon detail, simple background",
    "SUMMARY": "calm balanced composition: a shield, checklist or upward path, optimistic and "
               "reassuring",
}
ORDER_HINT = ("HOOK first, SUMMARY last, in between rotate CONCEPT, EVERYDAY, PROCESS, "
              "COMPARE, CLOSEUP, WARNING (only for risks and mistakes)")

# Square output is cropped to 16:9 (top and bottom lost) and the model takes no negative
# prompt, so the rules are restated positively for it.
CF_SUFFIX = (" Composition: a wide horizontal scene; place every important object inside the "
             "middle horizontal band with empty background above and below. Money appears only "
             "as plain gold coins with no symbols. Every device is plain and unbranded. "
             "Signs, papers and screens are blank.")

_cooldown: dict[str, float] = {}


# ---------------------------------------------------------------- providers --

def providers() -> list[str]:
    out = []
    if os.environ.get("IMAGE_PROVIDER") == "stub":
        return ["stub"]
    # Order of preference. Gemini and OpenAI image generation are paid APIs.
    if os.environ.get("IMAGE_GEMINI") == "1" and os.environ.get("GEMINI_API_KEY"):
        out.append("gemini")  # needs billing on the Google project
    if os.environ.get("CLOUDFLARE_ACCOUNT_ID") and os.environ.get("CLOUDFLARE_API_TOKEN"):
        out.append("cloudflare")  # free daily allowance: first choice
    if os.environ.get("OPENAI_API_KEY"):
        out.append("openai")      # paid: fallback when Cloudflare fails
    if os.environ.get("TOGETHER_API_KEY"):
        out.append("together")
    return out


def enabled() -> bool:
    return bool(providers())


async def _cloudflare(client, prompt, seed):
    acct, tok = os.environ["CLOUDFLARE_ACCOUNT_ID"], os.environ["CLOUDFLARE_API_TOKEN"]
    r = await client.post(
        f"https://api.cloudflare.com/client/v4/accounts/{acct}/ai/run/"
        "@cf/black-forest-labs/flux-1-schnell",
        headers={"Authorization": "Bearer " + tok},
        json={"prompt": prompt + CF_SUFFIX, "steps": 6}, timeout=120)  # no seed/size
    d = r.json()
    if not d.get("success"):
        raise RuntimeError("cloudflare: " + str(d.get("errors"))[:160])
    return base64.b64decode(d["result"]["image"])


async def _together(client, prompt, seed):
    r = await client.post(
        "https://api.together.xyz/v1/images/generations",
        headers={"Authorization": "Bearer " + os.environ["TOGETHER_API_KEY"]},
        json={"model": "black-forest-labs/FLUX.1-schnell-Free", "prompt": prompt,
              "width": 1344, "height": 768, "steps": 4, "n": 1, "seed": seed,
              "response_format": "b64_json"}, timeout=120)
    d = r.json()
    if "error" in d:
        raise RuntimeError("together: " + str(d["error"])[:160])
    return base64.b64decode(d["data"][0]["b64_json"])


async def _gemini(client, prompt, seed):
    r = await client.post(
        "https://generativelanguage.googleapis.com/v1beta/models/"
        "gemini-2.5-flash-image:generateContent",
        params={"key": os.environ["GEMINI_API_KEY"]}, timeout=120,
        json={"contents": [{"parts": [{"text": prompt}]}],
              "generationConfig": {"responseModalities": ["IMAGE"]}})
    d = r.json()
    if "error" in d:
        raise RuntimeError("gemini: " + str(d["error"].get("message"))[:160])
    for p in d["candidates"][0]["content"]["parts"]:
        if "inlineData" in p:
            return base64.b64decode(p["inlineData"]["data"])
    raise RuntimeError("gemini: no image returned")


async def _openai(client, prompt, seed):
    r = await client.post(
        "https://api.openai.com/v1/images/generations",
        headers={"Authorization": "Bearer " + os.environ["OPENAI_API_KEY"]},
        json={"model": os.environ.get("OPENAI_IMAGE_MODEL", "gpt-image-2"),
              "prompt": prompt, "size": "1536x1024", "n": 1,
              "quality": os.environ.get("OPENAI_IMAGE_QUALITY", "medium")},
        timeout=180)
    d = r.json()
    if "error" in d:
        raise RuntimeError("openai: " + str(d["error"].get("message"))[:160])
    item = d["data"][0]
    if item.get("b64_json"):
        return base64.b64decode(item["b64_json"])
    if item.get("url"):
        return (await client.get(item["url"], timeout=60)).content
    raise RuntimeError("openai: no image returned")


async def _stub(client, prompt, seed):
    """Offline test provider: a gradient with a shape. Never used in production."""
    from PIL import ImageDraw
    im = Image.new("RGB", (1344, 768), (14, 30, 62))
    d = ImageDraw.Draw(im)
    for y in range(768):
        d.line([(0, y), (1344, y)], fill=(14, 30 + y // 12, 62 + y // 9))
    d.ellipse([480, 160, 864, 544], fill=(255, 200, 61))
    d.rectangle([300, 520, 1044, 560], fill=(34, 197, 94))
    for _ in range(3000):  # texture so the sharpness gate sees detail
        x, y = (seed * 31 + _ * 17) % 1344, (seed * 13 + _ * 29) % 768
        d.point((x, y), fill=(255, 255, 255))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


_CALL = {"gemini": _gemini, "openai": _openai, "cloudflare": _cloudflare,
         "together": _together, "stub": _stub}


async def _generate(client, prompt: str, seed: int,
                    order: list[str] | None = None) -> bytes | None:
    active = providers()
    names = [n for n in (order or []) if n in active] + [n for n in active
                                                          if n not in (order or [])]
    for name in names:
        if _cooldown.get(name, 0) > time.time():
            continue
        for attempt in range(4):
            try:
                return await _CALL[name](client, prompt, seed)
            except Exception as e:
                msg = str(e)[:160]
                low = msg.lower()
                log.warning("image provider %s failed (try %d): %s", name, attempt + 1, msg)
                if any(k in low for k in ("rate", "429", "too many", "timeout", "overloaded")) \
                        and attempt < 3:
                    await asyncio.sleep(15 * (attempt + 1))
                    continue
                if any(k in low for k in ("quota", "billing", "auth", "limit", "insufficient")):
                    _cooldown[name] = time.time() + 3600
                break
    return None


# ----------------------------------------------------------------- producer --

def build_prompt(fmt: str, subject: str, channel_slug: str = "default") -> str:
    b = BRAND.get(channel_slug, BRAND["default"])
    return (f"{subject}. {FORMATS.get(fmt, FORMATS['CONCEPT'])}. "
            f"Style: {b['medium']}; {b['palette']}; {b['mood']}. "
            f"Wide 16:9 composition, subject centred with safe margins. {b['bans']}.")


def _finish(raw: bytes, out: str, w: int, h: int) -> bool:
    """Decode, quality-check, crop to 16:9, upscale, gentle polish."""
    try:
        im = Image.open(io.BytesIO(raw)).convert("RGB")
    except Exception:
        return False
    if im.width < 1000:
        return False
    st = ImageStat.Stat(im.convert("L"))
    if st.stddev[0] < 18:  # near-flat image
        return False
    target = w / h
    if im.width / im.height < target:  # taller than 16:9: crop top/bottom
        nh = int(im.width / target)
        top = (im.height - nh) // 2
        im = im.crop((0, top, im.width, top + nh))
    else:
        nw = int(im.height * target)
        left = (im.width - nw) // 2
        im = im.crop((left, 0, left + nw, im.height))
    im = im.resize((w, h), Image.LANCZOS)
    im = im.filter(ImageFilter.UnsharpMask(radius=1.6, percent=70, threshold=3))
    im = ImageEnhance.Contrast(im).enhance(1.05)
    im.save(out, "JPEG", quality=93, optimize=True)
    return True


def _fallback_scene(beat: dict, fmt: str) -> dict:
    text = str(beat.get("narration") or beat.get("headline") or "")[:140]
    return {"format": fmt, "subject": "a clear cartoon scene illustrating: " + text}


async def plan_scenes(beats: list[dict], channel: dict) -> dict[int, dict]:
    """Creative Director: a format + visual metaphor for EVERY beat (data slides too).

    Whatever the model skips is filled in locally so no slide is left without a picture.
    """
    cand = list(enumerate(beats))[:MAX_PER_VIDEO]
    if not cand:
        return {}
    listing = "\n".join(f"{i}. [{b.get('headline', '')}] {str(b.get('narration', ''))[:130]}"
                        for i, b in cand)
    prompt = f"""You are the Creative Director of the YouTube channel "{channel['name']}"
(Indian personal finance explainers). Every narration beat gets its own cartoon background
picture that illustrates what is being said at that moment.

Beats (index. [section] narration):
{listing}

For EVERY beat give a format from: {', '.join(FORMATS)}.
Routine: {ORDER_HINT}. Never use the same format twice in a row.
"subject": ONE concrete cartoon scene in 12-25 words that matches that beat's meaning
(e.g. "a smiling cartoon employee holding a phone showing a green tick beside a shield").
Make neighbouring beats visually different from each other.
Rules: cartoon characters are fine, but never a real or famous person, no bank or
government logo, no official seal, emblem or stamp, no real banknote, no text or numbers anywhere in the picture, nothing that
promises returns or guarantees.

Return JSON only:
{{"scenes": [{{"index": 0, "format": "HOOK", "subject": "..."}}]}}"""
    try:
        data = await llm.generate_json(prompt, temperature=0.7)
    except Exception as e:
        log.warning("creative director failed: %s", e)
        data = {}
    names = list(FORMATS)
    out: dict[int, dict] = {}
    for s_ in data.get("scenes", []):
        try:
            i, fmt = int(s_["index"]), str(s_.get("format", "CONCEPT")).upper()
        except Exception:
            continue
        if i < 0 or i >= len(cand) or not s_.get("subject"):
            continue
        out[i] = {"format": fmt if fmt in FORMATS else "CONCEPT",
                  "subject": str(s_["subject"])[:240]}
    last = None
    for i, beat in cand:  # fill gaps and break back-to-back repeats
        sc = out.get(i) or _fallback_scene(beat, names[i % len(names)])
        if sc["format"] == last:
            sc["format"] = next(n for n in names[i % len(names):] + names if n != last)
        out[i] = sc
        last = sc["format"]
    return out


async def create(client, scene: dict, out: str, w: int, h: int,
                 channel_slug: str, seed: int) -> dict | None:
    """Make one image for a scene. Returns a media record or None (caller falls back)."""
    prompt = build_prompt(scene["format"], scene["subject"], channel_slug)
    key = hashlib.sha256(f"{prompt}:{seed}".encode()).hexdigest()[:20]
    os.makedirs(CACHE_DIR, exist_ok=True)
    cached = os.path.join(CACHE_DIR, key + ".jpg")
    if not os.path.exists(cached):
        raw = await _generate(client, prompt, seed % 100000)
        if not raw or not await asyncio.to_thread(_finish, raw, cached, w, h):
            return None
    with open(cached, "rb") as src, open(out, "wb") as dst:
        dst.write(src.read())
    return {"credit": "", "source_url": "", "format": scene["format"],
            "prompt": prompt, "ai": True}
