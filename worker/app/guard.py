"""Guardrail gate: the last check before anything reaches Telegram.

Two layers:
  claim_check()  - LLM judge: does a title/hook promise more than the verified facts say?
                   Used by Packaging, the Shorts Editor and the Thumbnail agent when choosing.
  final_gate()   - code re-check of everything a viewer sees or hears: main title, Short
                   titles, thumbnail text, headings, narration. Any issue -> the preview is
                   sent WITHOUT the Approve button and the video is marked needs_review.
"""

import logging

from app import llm, packaging

log = logging.getLogger("guard")


async def claim_fails(titles: list[str], facts: list[dict]) -> dict[str, str]:
    """For agents choosing a title: {title: reason}; {} if the check cannot run."""
    return (await claim_check(titles, facts)) or {}


async def full_gate(out: dict) -> list[str]:
    """Final gate: code checks + claim check on every title and thumbnail text.
    Fails CLOSED: if the claim check cannot run, the video is not approvable."""
    problems = final_gate(out)
    pkg = out.get("package", {}) or {}
    titles = [pkg.get("title") or (out.get("script", {}) or {}).get("title", "")]
    titles += [sh.get("title", "") for sh in (out.get("render", {}) or {}).get("shorts", []) or []]
    titles += [v.get("text", "") for v in (out.get("thumbnail", {}) or {}).get("variants", []) or []]
    res = await claim_check(titles, (out.get("research", {}) or {}).get("facts", []))
    if res is None:
        problems.append("Claim check could not run (models busy) - send /show again later")
    else:
        problems += [f"Overclaims: '{t}' - {why}" for t, why in res.items()]
    return problems


async def claim_check(titles: list[str], facts: list[dict]) -> dict[str, str] | None:
    """Return {title: reason} for titles that overclaim; {} = all fine; None = check failed."""
    titles = [t for t in titles if t]
    if not titles:
        return {}
    listing = "\n".join(f"{i}. {t}" for i, t in enumerate(titles))
    fact_text = "\n".join("- " + f.get("fact", "") for f in facts[:12]) or "- (none)"
    prompt = f"""You check YouTube titles for an Indian personal-finance channel.
Verified facts (the ONLY things the video can claim):
{fact_text}

Titles:
{listing}

A title FAILS if it promises an outcome or claim the facts do not support, for example:
avoiding tax notices, preventing penalties, saving a specific amount, "errors in your
form", guaranteed results, or anything the facts do not literally say. Describing what
the video explains or helps you check is fine ("How to check X", "What Y means").
Return JSON only: {{"fails": [{{"index": 0, "reason": "..."}}]}}"""
    try:
        d = await llm.generate_json(prompt, temperature=0.0)
    except Exception as e:
        log.warning("claim check unavailable: %s", e)
        return None
    out = {}
    for f in d.get("fails", []):
        try:
            out[titles[int(f["index"])]] = str(f.get("reason", "overclaims"))[:160]
        except Exception:
            continue
    return out


def final_gate(out: dict) -> list[str]:
    """Code-only re-check of all visible text. Returns a list of problems."""
    scr, pkg = out.get("script", {}) or {}, out.get("package", {}) or {}
    facts = (out.get("research", {}) or {}).get("facts", [])
    allowed = packaging._nums(" ".join(f.get("fact", "") + " " + f.get("quote", "") for f in facts))
    problems = []

    def check_title(label, t):
        why = packaging._valid(str(t or ""), allowed)
        if why and why != "length":
            problems.append(f"{label}: {why} ({t})")

    check_title("Title", pkg.get("title") or scr.get("title"))
    for sh in (out.get("render", {}) or {}).get("shorts", []) or []:
        check_title(f"Short {sh.get('index')} title", sh.get("title"))
        if sh.get("claim_issue"):
            problems.append(f"Short {sh.get('index')} title overclaims: {sh['claim_issue']}")
    if pkg.get("claim_issue"):
        problems.append(f"Title overclaims: {pkg['claim_issue']}")

    th = out.get("thumbnail", {}) or {}
    for v in th.get("variants", []) or [{"text": pkg.get("thumb_text", "")}]:
        txt = str(v.get("text") or "")
        if packaging.SITE.search(txt) or any(e in txt.lower() for e in packaging.ENDORSE):
            problems.append(f"Thumbnail text not allowed: {txt}")
    if th.get("badge"):
        problems.append("Thumbnail shows a source badge (site info)")

    heads = " ".join(str(s.get("heading", "")) for s in scr.get("sections", []))
    spoken = " ".join(str(s.get("narration", "")) for s in scr.get("sections", []))
    sites = sorted({m.group(0) for m in packaging.SITE.finditer(heads + " " + spoken)})
    if sites:
        problems.append("Website info in narration/headings: " + ", ".join(sites))
    low = (heads + " " + spoken).lower()
    for p in ("government approved", "rbi-approved", "rbi approved", "sebi approved",
              "official channel", "official video", "endorsed by the", "approved by the government"):
        if p in low:
            problems.append(f"Endorsement wording in narration: '{p}'")
    return problems


async def repair(ctx: dict, rounds: int = 2) -> list[str]:
    """Fix Short titles and thumbnail variants that fail the gate, then re-check.
    Returns the problems that remain (the main title is never changed silently)."""
    import asyncio
    from app import cutshorts

    out = ctx["out"]
    problems = await full_gate(out)
    for _ in range(rounds):
        bad = {p.split("'")[1] for p in problems if p.startswith("Overclaims: '")}
        if not bad:
            break
        scr, pkg, r = out.get("script", {}), out.get("package", {}), out.get("render", {})
        main_t = pkg.get("title") or scr.get("title", "")
        fixed = False
        for sh in r.get("shorts", []) or []:
            if sh.get("title") in bad and sh.get("beats"):
                new_t = cutshorts.safe_title(main_t, int(sh.get("index", 1)))
                beats = cutshorts.beat_times(out["visual"]["groups"], out["voice"]["durs"])
                lang = "ta" if ctx["channel"].get("language") == "ta" else "en"
                res = await asyncio.to_thread(
                    cutshorts._render_one, int(sh["index"]), tuple(sh["beats"]), beats,
                    out["voice"]["blocks"], r["master"], out["voice"]["workdir"], new_t, lang,
                    str(scr.get("disclaimer_short") or ""), str(scr.get("disclaimer") or ""))
                sh.update({k: res[k] for k in ("title", "master", "preview", "duration")})
                log.info("repaired short %s title -> %s", sh["index"], new_t)
                fixed = True
        th = out.get("thumbnail", {}) or {}
        keep = [v for v in th.get("variants", []) or [] if v.get("text") not in bad]
        if th.get("variants") and len(keep) != len(th["variants"]):
            th["variants"] = keep
            th["path"] = keep[0]["path"] if keep else pkg.get("thumbnail", "")
            fixed = True
        if not fixed:
            break
        problems = await full_gate(out)
    return problems
