import json
import logging
import time

import httpx

from app.config import CFG

log = logging.getLogger("llm")

GEMINI_CHAIN = [
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-flash-lite-latest",
    "gemini-3.5-flash",
]
GROQ_PREF = ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]
GROQ_SKIP = (
    "whisper", "tts", "guard", "embed", "vision", "compound",
    "orpheus", "allam", "safety", "moderation", "rerank",
)

# When a provider says "quota exceeded", stop retrying it for a while instead of
# paying that latency on every single agent call.
COOLDOWN_S = 1800
_cooldown: dict[str, float] = {}


def _cooling(key: str) -> bool:
    return _cooldown.get(key, 0) > time.time()


def _cool(key: str) -> None:
    _cooldown[key] = time.time() + COOLDOWN_S


def _is_quota(err: str) -> bool:
    e = err.lower()
    return any(s in e for s in ("quota", "429", "rate limit", "resource_exhausted"))


class LLMError(RuntimeError):
    pass


async def _gemini(client, model, prompt, system=None, json_mode=False, temperature=0.7):
    body = {"contents": [{"parts": [{"text": prompt}]}]}
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    gen = {"temperature": temperature}
    if json_mode:
        gen["responseMimeType"] = "application/json"
    body["generationConfig"] = gen

    r = await client.post(
        "https://generativelanguage.googleapis.com/v1beta/models/"
        + model
        + ":generateContent",
        params={"key": CFG.gemini_key},
        json=body,
        timeout=120,
    )
    d = r.json()
    if "error" in d:
        raise LLMError(str(d["error"].get("message", "unknown"))[:160])
    return d["candidates"][0]["content"]["parts"][0]["text"]


async def _groq(client, model, prompt, system=None, json_mode=False, temperature=0.7):
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": prompt})
    payload = {"model": model, "messages": msgs, "temperature": temperature}
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    r = await client.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": "Bearer " + CFG.groq_key},
        json=payload,
        timeout=120,
    )
    d = r.json()
    if "error" in d:
        raise LLMError(str(d["error"])[:160])
    return d["choices"][0]["message"]["content"]


async def _deepseek(client, prompt, system=None, json_mode=False, temperature=0.7):
    """DeepSeek uses the OpenAI chat format. Last-resort fallback."""
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": prompt})
    payload = {"model": CFG.deepseek_model, "messages": msgs,
               "temperature": temperature}
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    r = await client.post(
        "https://api.deepseek.com/chat/completions",
        headers={"Authorization": "Bearer " + CFG.deepseek_key},
        json=payload,
        timeout=180,
    )
    d = r.json()
    if "error" in d:
        raise LLMError(str(d["error"])[:160])
    return d["choices"][0]["message"]["content"]


async def _groq_chain(client):
    try:
        r = await client.get(
            "https://api.groq.com/openai/v1/models",
            headers={"Authorization": "Bearer " + CFG.groq_key},
            timeout=20,
        )
        ids = [m["id"] for m in r.json().get("data", [])]
        ids = [i for i in ids if not any(s in i.lower() for s in GROQ_SKIP)]
        picked = [m for m in GROQ_PREF if m in ids]
        picked += [i for i in ids if i not in picked]
        if picked:
            return picked
    except Exception as e:
        log.warning("groq model list failed: %s", e)
    return list(GROQ_PREF)


async def generate(prompt: str, system: str | None = None, json_mode: bool = False,
                   temperature: float = 0.7) -> dict:
    errors = []
    async with httpx.AsyncClient() as client:
        for model in GEMINI_CHAIN:
            key = "gemini/" + model
            if _cooling(key):
                errors.append(f"{key}: cooling down")
                continue
            try:
                text = await _gemini(client, model, prompt, system, json_mode, temperature)
                return {"text": text, "provider": "gemini", "model": model}
            except Exception as e:
                log.warning("gemini/%s failed: %s", model, str(e)[:160])
                errors.append(f"{key}: {str(e)[:120]}")
                if _is_quota(str(e)):
                    _cool(key)
        for model in await _groq_chain(client):
            key = "groq/" + model
            if _cooling(key):
                errors.append(f"{key}: cooling down")
                continue
            try:
                text = await _groq(client, model, prompt, system, json_mode, temperature)
                return {"text": text, "provider": "groq", "model": model}
            except Exception as e:
                log.warning("groq/%s failed: %s", model, str(e)[:160])
                errors.append(f"{key}: {str(e)[:120]}")
                if _is_quota(str(e)):
                    _cool(key)
        if CFG.deepseek_key:
            key = "deepseek/" + CFG.deepseek_model
            if _cooling(key):
                errors.append(f"{key}: cooling down")
            else:
                try:
                    text = await _deepseek(client, prompt, system, json_mode, temperature)
                    return {"text": text, "provider": "deepseek",
                            "model": CFG.deepseek_model}
                except Exception as e:
                    log.warning("%s failed: %s", key, str(e)[:160])
                    errors.append(f"{key}: {str(e)[:120]}")
                    if _is_quota(str(e)):
                        _cool(key)
    raise LLMError("all providers failed | " + " | ".join(errors[:6]))


def _extract_json(text: str) -> dict:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("```")[1]
        if t.startswith("json"):
            t = t[4:]
    dec = json.JSONDecoder()
    for i, ch in enumerate(t):
        if ch != "{":
            continue
        try:
            obj, _ = dec.raw_decode(t[i:])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    raise LLMError("no JSON object in response: " + text[:200])


async def generate_json(prompt: str, system: str | None = None,
                        temperature: float = 0.7) -> dict:
    res = await generate(prompt, system, json_mode=True, temperature=temperature)
    try:
        data = _extract_json(res["text"])
    except Exception:
        # One corrective retry: some models wrap JSON in prose or emit two objects.
        retry = prompt + (
            "\n\nIMPORTANT: return exactly one JSON object and nothing else. "
            "No explanation, no second object, no markdown."
        )
        res = await generate(retry, system, json_mode=True, temperature=0.2)
        data = _extract_json(res["text"])
    data["_meta"] = {"provider": res["provider"], "model": res["model"]}
    return data


async def health() -> list[dict]:
    out = []
    async with httpx.AsyncClient() as client:
        for model in GEMINI_CHAIN:
            try:
                await _gemini(client, model, "Reply with exactly: OK")
                out.append({"provider": "gemini", "model": model, "ok": True, "detail": ""})
            except Exception as e:
                out.append(
                    {"provider": "gemini", "model": model, "ok": False, "detail": str(e)[:60]}
                )
        for model in (await _groq_chain(client))[:2]:
            try:
                await _groq(client, model, "Reply with exactly: OK")
                out.append({"provider": "groq", "model": model, "ok": True, "detail": ""})
            except Exception as e:
                out.append(
                    {"provider": "groq", "model": model, "ok": False, "detail": str(e)[:60]}
                )
    if CFG.deepseek_key:
        async with httpx.AsyncClient() as client:
            try:
                await _deepseek(client, "Reply with exactly: OK")
                out.append({"provider": "deepseek", "model": CFG.deepseek_model,
                            "ok": True, "detail": ""})
            except Exception as e:
                out.append({"provider": "deepseek", "model": CFG.deepseek_model,
                            "ok": False, "detail": str(e)[:60]})
    return out
