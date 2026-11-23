"""Narration via edge-tts, with word-level timings straight from the service.

edge-tts emits WordBoundary events alongside the audio, so captions get exact
timing without running speech recognition - no model download, no CPU burn, and
it works for Tamil as well as English.
"""

import asyncio
import logging
import subprocess

import edge_tts

log = logging.getLogger("voice")

VOICES = {
    "en": "en-IN-PrabhatNeural",   # Indian English, male, calm
    "ta": "ta-IN-PallaviNeural",   # Tamil, female, natural
}
FALLBACK = {"en": "en-IN-NeerjaNeural", "ta": "ta-IN-ValluvarNeural"}


class VoiceError(RuntimeError):
    pass


def probe_duration(path: str) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", path],
        capture_output=True, text=True,
    )
    try:
        return round(float(out.stdout.strip()), 3)
    except Exception:
        return 0.0


async def _one(text: str, voice: str, out_path: str) -> list[dict]:
    comm = edge_tts.Communicate(text, voice, rate="+0%")
    words: list[dict] = []
    wrote = 0
    with open(out_path, "wb") as f:
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
                wrote += len(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                start = chunk["offset"] / 1e7
                words.append({
                    "w": chunk["text"],
                    "s": round(start, 3),
                    "e": round(start + chunk["duration"] / 1e7, 3),
                })
    if wrote < 4000:
        raise VoiceError("audio stream was empty")
    return words


async def synth(text: str, lang: str, out_path: str) -> dict:
    """Synthesise one block of narration. Returns duration + word timings."""
    text = (text or "").strip()
    if not text:
        raise VoiceError("nothing to speak")

    voices = [VOICES.get(lang, VOICES["en"]), FALLBACK.get(lang, FALLBACK["en"])]
    last = None
    for voice in voices:
        for attempt in range(2):
            try:
                words = await _one(text, voice, out_path)
                dur = probe_duration(out_path)
                if dur <= 0:
                    raise VoiceError("ffprobe could not read duration")
                if words:
                    last_word_end = words[-1]["e"]
                    # The service sometimes reports slightly less than the file.
                    if last_word_end > dur:
                        dur = round(last_word_end + 0.15, 3)
                return {"path": out_path, "voice": voice, "duration": dur,
                        "words": words}
            except Exception as e:
                last = e
                log.warning("tts %s attempt %d failed: %s", voice, attempt + 1, str(e)[:120])
                await asyncio.sleep(2 + attempt * 3)
    raise VoiceError("all voices failed: " + str(last)[:200])


async def synth_many(items: list[tuple[str, str]], lang: str) -> list[dict]:
    """Synthesise sequentially - the service dislikes parallel bursts."""
    out = []
    for text, path in items:
        out.append(await synth(text, lang, path))
    return out
