"""Content agents.

Scout -> Research (Serper) -> Brief -> Scriptwriter -> FactCheck -> (revise) -> FactCheck

Grounding rule: once Research succeeds, the Scriptwriter may only use facts from
the fact sheet, and FactCheck verifies every claim against that same sheet.
"""

import asyncio
import datetime
import json
import os
import re
from urllib.parse import urlparse

from app import llm, search
from app.db import recent_topics

LANG = {"en": "English", "ta": "Tamil"}

AGENTS = {}
ORDER = ["scout", "research", "brief", "script", "fact",
         "voice", "visual", "render", "qc"]


class Blocked(RuntimeError):
    """Raised when a channel requires official sources and none were found.

    Fail-safe: no official source means no video, rather than a plausible guess.
    """


def _allowed(url: str, domains: list[str]) -> bool:
    """True only if the URL host is, or is a subdomain of, an official domain."""
    if not domains:
        return True
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return any(host == d or host.endswith("." + d) for d in domains)


def _norm(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace - for verbatim matching."""
    t = (text or "").lower()
    t = re.sub(r"[^\w\u0b80-\u0bff%]+", " ", t, flags=re.UNICODE)
    return " ".join(t.split())


def _numbers(text: str) -> set[str]:
    """Digit groups in a string, commas removed, so 80,000 == 80000."""
    return {n.replace(",", "") for n in re.findall(r"\d[\d,]*(?:\.\d+)?", text or "")}


def _has_disclaimer(narration: str, spoken: str) -> bool:
    """True if the closing sentence is already there, whole or in part."""
    n = _norm(narration)
    s = _norm(spoken)
    if not s:
        return True
    if s in n:
        return True
    head = " ".join(s.split()[:4])
    return len(head) > 8 and head in n


STOP = {
    "this", "that", "your", "with", "from", "what", "when", "why", "how", "does",
    "have", "will", "they", "them", "their", "about", "into", "more", "most",
    "than", "then", "also", "only", "just", "very", "much", "many", "some",
    "such", "make", "makes", "made", "need", "needs", "want", "wants", "video",
    "watch", "today", "india", "indian", "personal", "finance", "money", "should",
    "could", "would", "there", "these", "those", "which", "while", "where",
}

# Official domains publish a lot of material that is official but useless here:
# tenders, procurement manuals, the organisation's own financial statements.
BAD_PATTERNS = (
    "tender", "procurement", "corrigendum", "recruitment", "vacancy", "auction",
    "financials", "financial-statement", "balance-sheet", "balancesheet",
    "annual-report", "annualreport", "annual_report", "quotation", "rfp",
    "expression-of-interest", "/eoi", "/bid", "notice-inviting", "vendor",
    "empanelment", "canteen", "housekeeping", "manpower", "nsearchives",
    "/corporate/", "announcement", "board-meeting", "shareholding",
    "trading-window", "insider", "scrutinizer", "agm", "postal-ballot",
)

# A page that actually explains something to a member of the public.
# These are structural words, not topic words: "tax" appears in every URL here
# and proves nothing about whether the page is readable guidance.
GUIDE_PATTERNS = (
    "help", "faq", "guide", "how-to", "howto", "know-your", "individual",
    "taxpayer", "investor", "education", "citizen", "scheme", "benefit",
    "eligib", "charges", "fees", "process", "apply", "register", "portal",
    "circular", "notification",
)

WPS = 2.45  # words per second for the TTS voices (~147 wpm)


def _doc_ok(url: str, title: str) -> bool:
    hay = (url + " " + title).lower()
    return not any(p in hay for p in BAD_PATTERNS)


def _is_guidance(url: str, title: str) -> bool:
    hay = (url + " " + title).lower()
    return any(p in hay for p in GUIDE_PATTERNS)


def _search_query(sc: dict) -> str:
    """Short Google-shaped query built from the scout's keywords."""
    kws = [str(k).strip() for k in (sc.get("keywords") or []) if str(k).strip()]
    raw = " ".join(kws[:4]) if len(kws) >= 2 else str(
        sc.get("search_query") or sc.get("topic") or ""
    )
    tokens = re.findall(r"[A-Za-z0-9\u0b80-\u0bff]+", raw)
    return " ".join(tokens[:7])


def _keywords(sc: dict) -> set[str]:
    """Topic vocabulary used to reject off-topic facts."""
    text = " ".join(
        [str(sc.get("topic", "")), str(sc.get("angle", ""))]
        + [str(k) for k in (sc.get("keywords") or [])]
    )
    out = set()
    # 3+ chars keeps short but decisive terms (NPS, HRA, TDS, tax) in play.
    for w in re.findall(r"[a-zA-Z\u0b80-\u0bff]{3,}", text.lower()):
        if w not in STOP:
            out.add(w)
    return out


def _score(fact: str, quote: str, title: str, keys: set[str]) -> int:
    """How many distinct topic keywords the fact actually touches."""
    if not keys:
        return 2
    hay = _norm(fact + " " + quote + " " + title)
    return sum(1 for k in keys if k[:5] in hay)


def agent(agent_id):
    def deco(fn):
        AGENTS[agent_id] = fn
        return fn
    return deco


def _lang(ctx) -> str:
    return LANG.get(ctx["channel"].get("language", "en"), "English")


def _today() -> str:
    return datetime.date.today().strftime("%d %B %Y")


def _fy() -> str:
    t = datetime.date.today()
    start = t.year if t.month >= 4 else t.year - 1
    return "FY %d-%02d" % (start, (start + 1) % 100)


FRESHNESS = """Today is {today}. The current Indian financial year is {fy}.
Never present a tax slab, rate, limit or rule from an earlier year as if it is current.
"""


@agent("scout")
async def scout(ctx) -> dict:
    ch = ctx["channel"]
    recent = recent_topics(ch["id"]) or ["(none yet - this is the first video)"]
    avoid = ctx.get("avoid") or []
    avoid_block = ""
    if avoid:
        avoid_block = (
            "\nREJECTED TOPICS - the previous attempt could not be verified against "
            "official sources. Pick something different and more concrete:\n"
            + chr(10).join("- " + t for t in avoid)
            + "\n"
        )
    prompt = f"""You are the Scout agent for a YouTube channel.

Channel: {ch['name']}
Language: {_lang(ctx)}
Niche: {ch['niche']}
Format: a 5-minute explainer video, plus two 60-second shorts cut from it.

Already covered (do not repeat):
{chr(10).join('- ' + t for t in recent)}
{avoid_block}
Pick ONE topic for today.

Rules:
- high search intent, evergreen or genuinely timely
- specific, not vague. Good: "Why your first year of SIP returns is misleading".
  Bad: "Mutual funds explained".
- explainable honestly in 5 minutes
- no get-rich-quick, no guaranteed returns, no medical, legal or gambling advice
- the topic must be documented on official websites, so keep it CONCRETE and
  NAMED: a rule, a scheme, a charge, a document, a process, a limit.
  Avoid abstract marketing phrases like "strategy for wealth".
- "search_query" must be 3 to 6 plain words a person would type into Google,
  with no punctuation, no quotes, no year unless essential.

Return JSON only, no prose:
{{"topic": "...", "angle": "...", "why_now": "...", "search_intent": "...",
  "search_query": "...", "keywords": ["...", "...", "...", "...", "..."]}}"""
    return await llm.generate_json(prompt, temperature=0.9)


def _pick_domains(official: list[str], query: str, offset: int = 0, k: int = 4) -> list[str]:
    """Core domains always, plus one domain whose name matches the query.

    The core domains are listed first in channels.json (incometax, rbi, sebi,
    amfi) because they are where consumer-facing rules actually live.
    """
    compact = re.sub(r"[^a-z0-9]", "", _norm(query))
    matched = []
    for d in official:
        stem = d.split(".")[0]
        if len(stem) >= 3 and any(stem[:n] in compact for n in range(3, len(stem) + 1)):
            matched.append(d)
    core = [d for d in official if d not in matched][:k]
    return (matched[:1] + core)[: k + 1]


@agent("research")
async def research(ctx) -> dict:
    """Real web search + fact sheet. Never invents; if search is off, says so."""
    sc = ctx["out"]["scout"]
    ch = ctx["channel"]
    hl = "ta" if ch.get("language") == "ta" else "en"

    official = [d.lower() for d in (ch.get("official_sources") or [])]
    strict = bool(official)

    # Short, Google-shaped query. A long marketing-style topic returns nothing
    # when combined with a site: filter.
    q = _search_query(sc)

    queries = [
        f"{q} site:{d}"
        for d in _pick_domains(official, q, offset=int(ctx.get("video_id", 0) or 0))
    ]
    if not queries:
        queries = [q]

    hits, errors, providers = [], [], set()
    for query in queries:
        r = await search.web_search(query, num=8, gl="in", hl=hl)
        if r.get("ok"):
            hits.extend(r.get("organic", []))
            if r.get("provider"):
                providers.add(r["provider"])
        else:
            errors.append(r.get("error", "unknown"))

    seen, evidence, skipped_docs = set(), [], 0
    for h in hits:
        url = h.get("url")
        if not url or url in seen:
            continue
        seen.add(url)
        if not _doc_ok(url, str(h.get("title", ""))):
            skipped_docs += 1
            continue
        evidence.append(h)
    if strict:
        evidence = [e for e in evidence if _allowed(e["url"], official)]
    evidence = evidence[:16]

    if not evidence:
        detail = "; ".join(errors)[:200] or "no usable results"
        if strict:
            raise Blocked(
                "no official source found for topic '%s'. Tried: %s. Detail: %s"
                % (sc["topic"], ", ".join(queries), detail)
            )
        return {
            "enabled": False,
            "facts": [],
            "sources": [],
            "gaps": [],
            "providers": sorted(providers),
            "official_only": strict,
            "official_domains": official,
            "error": detail,
        }

    # Snippets alone are often too thin to carry a fact sheet. Pull the actual
    # page text for the top official results so the extractor has real wording.
    contents = await asyncio.gather(
        *(search.fetch_text(e["url"]) for e in evidence[:6])
    )
    content_by_url = {
        e["url"]: c for e, c in zip(evidence[:6], contents) if c
    }

    def _body(e: dict) -> str:
        text = e.get("snippet", "")
        page = content_by_url.get(e["url"], "")
        if page:
            text += " " + page[:4000]
        return text

    block = "\n".join(
        f"[{i}] {e['title']}\n    {e['url']}\n    snippet: {e['snippet']}"
        + (f"\n    page text: {content_by_url[e['url']][:4000]}"
           if content_by_url.get(e["url"]) else "")
        for i, e in enumerate(evidence, 1)
    )

    prompt = f"""You are the Research agent. Below are results from OFFICIAL sources only.

Extract ONLY facts that are directly supported by these snippets or page texts.
Never add anything from your own knowledge. Never round or reinterpret a number.
If the snippet or page text does not contain a number, do not produce that number.

Topic: {sc['topic']}
Today: {_today()}
Allowed official domains: {", ".join(official) if official else "any"}

What counts as a useful fact here: a rule, limit, rate, charge, deadline,
document, eligibility condition or procedure that applies to an ordinary
person. Not the organisation's own accounts, purchases, staff or projects.

Search results:
{block}

Return JSON only:
{{"facts": [{{"fact": "one specific sentence", "source": "the url",
             "quote": "the exact words from that source's snippet or page text that support it"}}],
  "numbers": [{{"value": "...", "context": "...", "source": "url"}}],
  "gaps": ["what a 5-minute video needs but these results do not cover"]}}

Rules:
- 6 to 12 facts, most important first
- the "quote" MUST be copied word for word from that source's snippet or page
  text above. It is checked by a program: anything not found verbatim is discarded.
- drop any fact whose snippet or page text does not clearly support it
- if two sources disagree, include both and note the disagreement in the fact text
- if a number is not explicitly in the snippet or page text, do not include it"""
    data = await llm.generate_json(prompt, temperature=0.1)

    # --- mechanical verification, no model opinion involved -----------------
    snippet_by_url = {e["url"]: _body(e) for e in evidence}
    title_by_url = {e["url"]: e.get("title", "") for e in evidence}
    all_snippets = " \n ".join(_norm(_body(e)) for e in evidence)
    keys = _keywords(sc)

    kept, dropped_unofficial, dropped_unquoted, dropped_number, dropped_offtopic = [], 0, 0, 0, 0
    for f in data.get("facts", []):
        url = str(f.get("source", "")).strip()
        fact_text = str(f.get("fact", "")).strip()
        quote = str(f.get("quote", "")).strip()
        if strict and not _allowed(url, official):
            dropped_unofficial += 1
            continue
        if not fact_text:
            continue
        nq = _norm(quote)
        if len(nq) < 12 or nq not in all_snippets:
            dropped_unquoted += 1
            continue
        snippet = snippet_by_url.get(url, "")
        corpus = _numbers(quote) | _numbers(snippet)
        if _numbers(fact_text) - corpus:
            dropped_number += 1
            continue
        if _score(fact_text, quote, title_by_url.get(url, ""), keys) < 2:
            dropped_offtopic += 1
            continue
        kept.append(
            {
                "fact": fact_text,
                "source": url,
                "quote": quote,
                "domain": (urlparse(url).hostname or "").lower(),
                "guidance": _is_guidance(url, title_by_url.get(url, "")),
            }
        )

    data["facts"] = kept
    data["dropped_unofficial"] = dropped_unofficial
    data["dropped_unquoted"] = dropped_unquoted
    data["dropped_number"] = dropped_number
    data["dropped_offtopic"] = dropped_offtopic
    data["skipped_documents"] = skipped_docs
    data["official_only"] = strict
    data["official_domains"] = official
    data["enabled"] = True
    data["sources"] = [{"title": e["title"], "url": e["url"]} for e in evidence]
    data["queries"] = queries
    data["providers"] = sorted(providers)

    guided = [f for f in kept if f.get("guidance")]
    if len(kept) < 2 or (strict and len(guided) < 1):
        raise Blocked(
            "only %d verifiable fact(s) survived, %d of them from an explanatory page, "
            "for topic '%s' (dropped: %d unofficial, %d unquoted, %d unbacked numbers, "
            "%d off-topic, %d irrelevant documents). Refusing to write a script on "
            "evidence this thin."
            % (len(kept), len(guided), sc["topic"], dropped_unofficial,
               dropped_unquoted, dropped_number, dropped_offtopic, skipped_docs)
        )
    return data


@agent("brief")
async def brief(ctx) -> dict:
    sc = ctx["out"]["scout"]
    ch = ctx["channel"]
    res = ctx["out"].get("research", {})

    facts = ""
    if res.get("enabled"):
        facts = "\nVerified facts you may rely on:\n" + "\n".join(
            "- " + f.get("fact", "") for f in res.get("facts", [])
        )

    prompt = f"""You are the Brief agent. Turn this topic into a tight 5-minute video brief.

Channel: {ch['name']}
Language: {_lang(ctx)}
Topic: {sc['topic']}
Angle: {sc['angle']}
{facts}

Build a brief for a 300-second video:
- one promise the viewer gets in the first 20 seconds
- five beats that each take about 50 seconds
- a soft close that asks for a subscribe without hype
- every beat must be supportable by the facts above

Return JSON only:
{{"working_title": "...",
  "audience": "who exactly this is for, in one sentence",
  "promise": "what the viewer walks away knowing",
  "hook": "the first 2 sentences spoken on camera",
  "beats": [{{"at": "0:00", "goal": "..."}}, ... five items],
  "cta": "..."}}"""
    return await llm.generate_json(prompt, temperature=0.6)


@agent("script")
async def script(ctx) -> dict:
    br = ctx["out"]["brief"]
    ch = ctx["channel"]
    lang = _lang(ctx)
    res = ctx["out"].get("research", {})
    fact = ctx["out"].get("fact")
    disclaimer = ch.get("disclaimer", "")

    grounding = ""
    if res.get("enabled"):
        grounding = """
GROUNDING RULES - these override everything else:
- You may only state facts, numbers, rates, limits or rules that appear in the
  verified fact sheet below. Nothing else.
- This applies to every statement of fact: about products, laws, procedures,
  events, dates and behaviour - not only numbers.
- Do not add examples, analogies or comparisons that reference real companies,
  people, laws, news events or studies unless they are in the fact sheet.
- If you need a fact that is not in the sheet, either omit it or say that the
  viewer should check the official portal. Do not guess.
- When you use a number, use it exactly as written in the sheet.

Verified fact sheet (all from official sources):
""" + "\n".join(
            "- " + f.get("fact", "") + "  [" + f.get("source", "") + "]"
            for f in res.get("facts", [])
        )
    else:
        grounding = """
NO WEB SEARCH WAS AVAILABLE for this run.
Therefore: do not state any specific number, rate, slab or limit as fact.
Teach the method instead, and put every figure in the visual_note prefixed [VERIFY].
"""

    revision = ""
    if fact:
        revision = f"""
THIS IS A REVISION PASS. The fact checker rejected parts of your previous draft.

Previous draft:
{json.dumps(ctx['out'].get('script', {}), ensure_ascii=False)[:6000]}

Fact check findings:
{json.dumps(fact, ensure_ascii=False)[:4000]}

Rewrite so that every claim marked "contradicted" or "not_in_sources" is either
removed, corrected to match the fact sheet, or reframed as something the viewer
must confirm on the official portal. Keep the same six-section structure.
"""

    expand = ""
    if ctx.get("expand"):
        expand = f"""
THIS IS AN EXPANSION PASS. Your previous draft was only {ctx.get('last_words', 0)} words,
which is under three minutes. Keep the same structure and the same verified
facts, but explain each step more fully: the mechanism, the reason it matters,
and the mistake people make. Target about 600 words. Do not invent new facts.
"""

    trim = ""
    if ctx.get("trim"):
        prev = int((ctx["out"].get("script") or {}).get("total_seconds") or 0)
        trim = f"""
THIS IS A TRIM PASS. Your previous draft runs {prev} seconds, which is over the
hard five-minute limit. Rewrite it to 285 to 295 seconds.
Cut whole sentences that repeat a point. Keep the hook, keep every verified
fact, keep the closing disclaimer sentence. Do not add anything new.
"""

    prompt = f"""You are the Scriptwriter agent. Write the full narration script.

Channel: {ch['name']}
Language: {lang}
Working title: {br['working_title']}
Audience: {br['audience']}
Promise: {br['promise']}
Hook: {br['hook']}
Beats: {json.dumps(br['beats'], ensure_ascii=False)}

{FRESHNESS.format(today=_today(), fy=_fy())}
{grounding}
{revision}
{expand}
{trim}
Rules:
- write the narration in {lang}. If {lang} is Tamil, use natural spoken Tamil.
- conversational, second person, short sentences. It will be read by a TTS voice.
- length: 560 to 740 words. Five minutes is the CEILING, not a target. A tight
  four-minute video is better than a padded five-minute one. Never exceed 300
  seconds of spoken narration.
- no filler, no repetition, no "in this video we will" padding. Every sentence
  must add information or move the argument forward.
- exactly six CONTENT sections. Each has a heading, narration, and a visual note.
- each section also needs "visual_query": 3 to 6 plain English search words
  describing a photo that could illustrate it (for example "bank passbook and pen",
  "server room cables", "calculator tax form"). Concrete nouns, no abstract words,
  no brand names, no people's names.
- do NOT create a section for the disclaimer. It is drawn as a one-line overlay
  automatically at the start and the end, over running footage. Section 1 must
  start with the actual content hook.
- no emojis, no stage directions inside the narration, no markdown
- end with a calm call to action, no hype, no promises of returns
- you do not need to output seconds; they are computed from your word count

DISCLAIMER - mandatory, written in {lang}:
- "disclaimer" must be exactly this text:
  "{disclaimer}"
- "disclaimer_short" must be ONE line, under 70 characters, no full stop needed,
  readable in a single glance as an overlay at the start and end of the video
- "spoken_disclaimer" must be one short spoken sentence in {lang} saying this
  video is education and not advice
- the LAST section's narration must end with the spoken_disclaimer, word for word
- the description must end with the full disclaimer text

Also produce two 60-second shorts cut from this material.
Each must stand alone, hook in the first 3 seconds, about 145-155 words.

Return JSON only:
{{"title": "under 70 characters",
  "description": "3-4 sentences, ending with the exact disclaimer above",
  "disclaimer": "the exact disclaimer text",
  "disclaimer_short": "under 90 characters, for the on-screen card",
  "spoken_disclaimer": "one sentence, spoken at the end",
  "tags": ["...", up to 12],
  "sections": [{{"heading": "...", "narration": "...", "visual_note": "...", "visual_query": "...", "seconds": 50}}, six items],
  "shorts": [{{"title": "...", "hook": "...", "narration": "...", "visual_query": "...", "seconds": 60}}, two items],
  "total_seconds": 300,
  "word_count": 750}}"""
    data = await llm.generate_json(prompt, temperature=0.55)

    # Drop any section the model dedicated to the disclaimer: it is drawn as a
    # card by the renderer, so a spoken section would duplicate it.
    sections = [
        s for s in (data.get("sections") or [])
        if "disclaimer" not in str(s.get("heading", "")).lower()
    ]
    for s in sections:
        words = len(str(s.get("narration", "")).split())
        s["words"] = words
        s["seconds"] = max(8, round(words / WPS))
    data["sections"] = sections
    data["word_count"] = sum(s.get("words", 0) for s in sections)
    data["total_seconds"] = sum(s.get("seconds", 0) for s in sections)
    if not data.get("disclaimer"):
        data["disclaimer"] = disclaimer

    # One line, one glance, drawn over running footage at the head and tail.
    short = " ".join(str(data.get("disclaimer_short") or "").split())
    if not short:
        short = " ".join(disclaimer.split(".")[0].split())
    if len(short) > 70:
        short = short[:67].rstrip(" ,;:-.") + "..."
    data["disclaimer_short"] = short
    data["disclaimer_overlay"] = {
        "text": short,
        "start_seconds": 5,
        "end_seconds": 5,
        "over_video": True,
    }
    if not data.get("spoken_disclaimer") and disclaimer:
        data["spoken_disclaimer"] = (
            "இது கல்வி நோக்கத்திற்கான வீடியோ, ஆலோசனை அல்ல."
            if lang == "Tamil"
            else "This video is educational only and is not advice."
        )

    # The spoken disclaimer must physically be the last thing said.
    spoken = str(data.get("spoken_disclaimer", "")).strip()
    sections = data.get("sections") or []
    if spoken and sections:
        last = sections[-1]
        narration = str(last.get("narration", "")).strip()
        if not _has_disclaimer(narration, spoken):
            last["narration"] = (narration + " " + spoken).strip()

    # Shorts publish on their own, so they carry the same disclaimer.
    for sh in data.get("shorts") or []:
        nar = str(sh.get("narration", "")).strip()
        if not _has_disclaimer(nar, spoken):
            nar = (nar + " " + spoken).strip()
        sh["narration"] = nar
        sh["disclaimer"] = disclaimer
        sh["disclaimer_short"] = short
        sh["seconds"] = 60

    data["disclaimer_positions"] = ["start", "end"]
    data["max_seconds"] = 300
    data["over_limit"] = (data.get("total_seconds") or 0) > 300

    desc = str(data.get("description", "")).strip()
    if disclaimer and disclaimer not in desc:
        desc = (desc + "\n\n" + disclaimer).strip()
    data["description"] = desc
    return data


@agent("fact")
async def fact(ctx) -> dict:
    scr = ctx["out"]["script"]
    res = ctx["out"].get("research", {})
    body = "\n\n".join(
        f"[{s.get('heading', '')}] {s.get('narration', '')}" for s in scr.get("sections", [])
    )
    shorts = scr.get("shorts") or []
    if shorts:
        body += "\n\n--- SHORTS (these publish too, so check them as strictly) ---\n"
        body += "\n\n".join(
            f"[SHORT {i}] {s.get('hook', '')} {s.get('narration', '')}"
            for i, s in enumerate(shorts, 1)
        )

    if res.get("enabled"):
        sheet = "\n".join(
            "- " + f.get("fact", "") + "  [" + f.get("source", "") + "]"
            for f in res.get("facts", [])
        )
        prompt = f"""You are the FactCheck agent. You have a verified fact sheet gathered
from live web search. Compare the script against it.

Verified fact sheet:
{sheet}

Script:
{body}

For every claim a viewer could challenge, assign exactly one verdict:
- "supported"        - the fact sheet states this
- "not_in_sources"   - a checkable factual assertion the sheet does not contain
- "contradicted"     - conflicts with the fact sheet

ONLY list checkable factual assertions: numbers, rates, limits, dates, rules,
named laws, named products, named institutions, and statements about how a
mechanism objectively works.

Do NOT list any of these - they are not claims and must be omitted entirely:
- opinions or framing ("timing the market is stressful")
- advice or recommendations ("you should review your goals")
- generic definitions that any adult knows
- rhetorical questions, calls to action, transitions
- the spoken disclaimer sentence

Also redo any arithmetic yourself and report if the script's total is wrong.

Also check every proper noun. Any bank, company, scheme, form, law or person
named in the script must appear in the fact sheet. A name that is absent is
"not_in_sources". A name that is wrong or invented is "contradicted".

Return JSON only:
{{"claims": [{{"claim": "...", "verdict": "supported|not_in_sources|contradicted", "note": "..."}}],
  "risk": "low|medium|high",
  "fixes": ["concrete rewrite for anything not supported"]}}"""
        data = await llm.generate_json(prompt, temperature=0.1)
        verdicts = {str(c.get("verdict", "")).lower() for c in data.get("claims", [])}
        if "contradicted" in verdicts:
            data["risk"] = "high"
        elif "not_in_sources" in verdicts:
            data["risk"] = "medium"
        else:
            data["risk"] = "low"
        data["mode"] = "grounded"
        return data

    prompt = f"""You are the FactCheck agent. You have NO live web access, so you must
never claim that something was verified against a source. You only classify.

Today is {_today()}. Current Indian financial year is {_fy()}.

Script:
{body}

List every factual claim a viewer could challenge.

Verdicts:
- "common_knowledge" - widely known and stable
- "needs_source"     - plausible but must be checked before publishing
- "risky"            - wrong, outdated, or could mislead someone into losing money

Return JSON only:
{{"claims": [{{"claim": "...", "verdict": "common_knowledge|needs_source|risky", "note": "why"}}],
  "risk": "low|medium|high",
  "fixes": ["..."]}}"""
    data = await llm.generate_json(prompt, temperature=0.2)
    data["mode"] = "ungrounded"
    return data


# --------------------------------------------------------------------------- #
# Phase 2: voice, visuals, render, quality gate
# --------------------------------------------------------------------------- #

MEDIA_DIR = os.environ.get("MEDIA_DIR", "/app/work/media")


def _media_dir(ctx, suffix: str = "") -> str:
    d = os.path.join(MEDIA_DIR, f"v{ctx['video_id']}{suffix}")
    os.makedirs(d, exist_ok=True)
    return d


def _progress(ctx):
    """Optional sync progress sink (the CLI prints it, the bot ignores it)."""
    cb = ctx.get("progress")
    if not callable(cb):
        return None

    def note(msg: str) -> None:
        try:
            cb(msg)
        except Exception:
            pass

    return note


@agent("voice")
async def voice_agent(ctx) -> dict:
    from app import render as R

    scr = ctx["out"].get("script") or {}
    sections = scr.get("sections") or []
    if not sections:
        raise Blocked("script has no sections to narrate")
    lang = ctx["channel"].get("language", "en")
    workdir = _media_dir(ctx)
    n = await R.narrate(sections, lang, workdir, progress=_progress(ctx))
    if n["total"] < 20:
        raise Blocked("narration came back almost empty (%.1fs)" % n["total"])
    return {
        "workdir": workdir,
        "lang": lang,
        "total": n["total"],
        "sections": len(sections),
        "durs": n["durs"],
        "audios": n["audios"],
        "blocks": n["blocks"],
        "headings": n["headings"],
    }


@agent("visual")
async def visual_agent(ctx) -> dict:
    from app import render as R

    scr = ctx["out"]["script"]
    sections = scr.get("sections") or []
    v = ctx["out"]["voice"]
    niche = ctx["channel"].get("niche", "")
    prompts = R.plan_images(sections, niche)
    images = await R.illustrate(sections, niche, v["lang"], v["workdir"], 1920, 1080)
    sources = [im.get("source") for im in images]
    return {
        "prompts": prompts,
        "images": images,
        "count": len(images),
        "sources": sources,
        "pexels_images": sum(1 for s in sources if s == "pexels"),
        "openverse_images": sum(1 for s in sources if s == "openverse"),
        "commons_images": sum(1 for s in sources if s == "commons"),
        "stock_images": sum(1 for s in sources
                            if s in ("pexels", "openverse", "commons")),
        "fallback_images": sum(1 for s in sources if s == "generated"),
        "credits": [im.get("credit", "") for im in images if im.get("credit")],
    }


@agent("render")
async def render_agent(ctx) -> dict:
    from app import render as R

    scr = ctx["out"]["script"]
    ch = ctx["channel"]
    v = ctx["out"]["voice"]
    im = ctx["out"]["visual"]

    out = await asyncio.to_thread(
        R.assemble,
        ctx["video_id"], scr, ch, v["workdir"], v["blocks"], v["headings"],
        v["durs"], im["images"], 1920, 1080, v["total"], _progress(ctx),
    )

    shorts = []
    for i, sh in enumerate(scr.get("shorts") or [], 1):
        if not str(sh.get("narration", "")).strip():
            continue
        try:
            shorts.append(
                await R.render_short(sh, scr, ch, ctx["video_id"], i, MEDIA_DIR)
            )
        except Exception as e:  # a broken short must not lose the long video
            shorts.append({"index": i, "title": sh.get("title", ""),
                           "error": str(e)[:300]})
    out["shorts"] = shorts
    out["credits"] = (im.get("credits") or [])
    try:
        out["pruned"] = R.prune_media(MEDIA_DIR)
    except Exception:
        pass
    return out


@agent("qc")
async def qc_agent(ctx) -> dict:
    from app import render as R

    r = ctx["out"].get("render") or {}
    checks: list[dict] = []

    def chk(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": str(detail)[:120]})

    master = r.get("master")
    if not master or not os.path.isfile(master):
        return {"pass": False, "checks": [{"check": "master exists", "ok": False,
                                           "detail": "no file"}], "warnings": []}

    info = R.probe(master)
    chk("resolution", info["width"] == 1920 and info["height"] == 1080,
        "%sx%s" % (info["width"], info["height"]))
    chk("duration under 5 min", 0 < info["duration"] <= 300.5,
        "%.1fs" % info["duration"])
    chk("has audio", info["has_audio"], info["acodec"] or "none")
    chk("h264", info["vcodec"] == "h264", info["vcodec"] or "?")
    chk("file size", info["size"] > 200_000, "%.1f MB" % (info["size"] / 1e6))

    loud = R.loudness(master)
    chk("loudness -20..-9 LUFS", -20.0 <= loud <= -9.0, "%.1f LUFS" % loud)

    dark = R.dark_fraction(master)
    chk("no black stretch", dark < 0.5, "%.0f%% dark samples" % (dark * 100))

    warnings: list[str] = []
    shorts = r.get("shorts") or []
    for s in shorts:
        tag = "short %s" % s.get("index")
        if s.get("error"):
            warnings.append("%s failed to render: %s" % (tag, s["error"]))
            continue
        si = R.probe(s["master"])
        chk(tag + " 1080x1920", si["width"] == 1080 and si["height"] == 1920,
            "%sx%s" % (si["width"], si["height"]))
        chk(tag + " under 62s", 0 < si["duration"] <= 62.5,
            "%.1fs" % si["duration"])

    if not shorts:
        warnings.append("no shorts were rendered")
    if r.get("sections", 0) < 3:
        warnings.append("very few sections (%s)" % r.get("sections"))

    passed = all(c["ok"] for c in checks)
    return {"pass": passed, "checks": checks, "warnings": warnings,
            "duration": info["duration"], "size": info["size"],
            "loudness": loud}
