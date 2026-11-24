# YT Factory - Project Skills

What this project can do, how to operate it, and the rules it must never break.
Written from the running VPS build (`/opt/ytfactory`, Nov 2026). Items marked
**(untested)** were built but have not run end to end yet.

## 1. Purpose

An automated YouTube channel factory. For each video it finds a topic, grounds it in
official sources, writes a 5-minute script, narrates it, illustrates every slide,
renders 1080p, cuts two Shorts, checks quality, and sends it to Telegram. **Nothing
uploads until the owner taps Approve.**

Live channel: **Money Clear** (English finance). Other configured channels (AI Brief,
Panam Pesu, Tamil Tech) share one YouTube login and are **not safe to run** until each
has its own `YT_REFRESH_TOKEN_<SLUG>`.

## 2. Niche scope: finance, money and the share market

Share Market / Stock Market content comes under the Finance / Money niche.
Examples:

- Stock market analysis
- Investing for beginners
- Mutual funds
- SIP investing
- Trading strategies
- Options & Futures education
- Personal finance
- Retirement planning
- Dividend investing

For YouTube:

- ✅ High CPM/RPM (advertisers pay more)
- ✅ Good affiliate opportunities (brokers, financial products)
- ✅ Good for authority-building and revenue
- ✅ Suits educational, analytical, and data-driven creators

**Guard rails that come with the niche (do not skip):**

- Educational only. No buy/sell/hold calls, no target prices, no tips, no "guaranteed"
  or "sure-shot" returns, no get-rich claims.
- Trading, options and futures videos explain how things work and what the risks are.
  They never tell a viewer what to trade. Unregistered investment advice is regulated
  in India (SEBI), so keep the disclaimer on every video.
- Affiliate links (brokers, financial products) need a clear disclosure in the
  description and on screen where required. Never present an affiliate product as
  advice. Check current SEBI/ASCI rules before adding any.
- Every fact still comes from the channel's `official_sources` list (RBI, SEBI, AMFI,
  NSE, BSE, incometax.gov.in, etc.) with a word-for-word quote.

## 3. Pipeline and agents

```
News -> Scout -> Research -> Brief -> Script -> FactCheck -> Packaging
      -> Voice -> Visual (Image Studio) -> Render (+ Shorts Editor) -> QC
      -> Telegram approve -> Publisher
Background: Trends (00:10 UTC), Analyst (00:30 UTC), Doctor (always)
```

| Agent | Job |
|---|---|
| News | Reads RBI press releases, RBI notifications and PIB feeds; ranks up to 5 topic candidates daily (00:05 UTC) |
| Scout | Uses the next unused news candidate; otherwise an evergreen topic for ONE kind of person (first-job employee, freelancer, parent, retiree...) |
| Research | Searches official domains only, reads pages, keeps facts that have exact quotes. Blocks the topic if evidence is thin; Scout then tries the next (5 attempts) |
| Brief | Outline plus a hook built from Trend Analyst patterns |
| Script | 5-minute narration (hard limit 300 s) |
| FactCheck | Claim and number checks against the fact sheet; revision pass if risky |
| Packaging | 6 title candidates, validated in code (no invented numbers, no hype, no shouting); makes the 1280x720 thumbnail |
| Voice | edge-tts narration with word-level timings |
| Visual | Storyboard, one picture per beat, via Image Studio (below) |
| Render | FFmpeg 1080p master, animated card backgrounds, captions, loudness |
| Shorts Editor | Cuts two 15-20 s vertical Shorts from the full video (below) |
| QC | Resolution, duration, audio, loudness, black-frame checks |
| Trends | Scans YouTube charts worldwide plus finance searches; stores topic ideas, hook patterns, title formulas |
| Analyst | Pulls channel and video analytics into Postgres |
| Doctor | Classifies failures, retries safely, reports to Telegram, frees stuck jobs |

Declared but not built: Leads (redirect links and attribution).

## 4. Safety rules (never weaken)

1. Research uses the channel's official-source domains only.
2. Every fact needs a verbatim quote from the fetched page.
3. Numbers are mechanically checked against source text.
4. A topic with too few verified facts, or no explanatory official page, is **blocked**,
   not relaxed. A news announcement alone does not count as an explainer page.
5. FactCheck risk medium or high stops the video at `needs_review`.
6. The Doctor changes runtime state only (waits, retries, cool-downs). It never edits
   source code and never retries a deliberate safety block.
7. Titles must not contain numbers absent from the sources.
8. Image prompts ban text, logos, real people, real banknotes and return promises.
9. Human approval before every upload.
10. **No implied government endorsement.** The channel is not the government. Never use
    "Official" as a label for the video, channel, thumbnail or title; never say or imply
    the content is approved, endorsed, sponsored or run by the government, RBI, SEBI, the
    Income Tax Department or any regulator. Cite plainly ("according to an RBI press
    release"). Enforced in code: titles with "official", "RBI-approved", "government
    approved", "endorsed by" and similar are rejected; the thumbnail badge reads
    "Source: <domain>"; endorsement phrases in the narration or description send the
    video to human review; images ban government emblems, seals and stamps.

## 5. Image Studio (`imagegen.py`)

- Creative Director (one LLM call per video) gives **every beat** a format and a concrete
  scene; gaps are filled locally so no slide is left without a picture.
- Style: bright friendly 2D cartoon, rich blue with gold, orange, pink accents.
- Eight formats on a routine, never the same one twice in a row: HOOK, CONCEPT, PROCESS,
  WARNING, EVERYDAY, COMPARE, CLOSEUP, SUMMARY.
- Number slides: the cartoon fills the screen, the number sits in a bottom banner.
- Provider order: Gemini (only if `IMAGE_GEMINI=1`, needs billing) -> **Cloudflare
  Workers AI FLUX-1-schnell (free daily allowance, ~130-170 images/day)** -> OpenAI
  (`gpt-image-2`, medium, paid fallback) -> Together if keyed -> animated text cards.
- Cloudflare FLUX takes only `prompt` + `steps` (no size, seed or negative prompt) and
  returns a square image that is cropped to 16:9, so a composition suffix keeps objects in
  the middle band, coins symbol-free, devices unbranded. It still sometimes draws $ signs
  or stray text: check slides in review.
- 429s are retried with back-off before a provider is cooled down. Results are cached.
- The description carries one line: "Illustrations are AI-generated".
- Cost guess for 35-45 images at medium: about $1.50-1.80 per video **(unverified,
  check the OpenAI usage page)**. Set `OPENAI_IMAGE_QUALITY=low` to cut it.

Fallback cards use an animated aurora gradient (FFmpeg `gradients` + drifting orbs) with
a glass panel that fades and slides in.

## 5b. Thumbnail agent (`thumbnail.py`)

- Runs after Packaging. A Thumbnail Director proposes **3 concepts** (2-4 word hook text,
  one gold accent word, a cartoon scene, why it earns a click honestly).
- Text is checked in code: 1-4 words, no endorsement words ("official", "RBI-approved"...),
  no hype, numbers only from verified facts.
- Layout: text panel on the left, cartoon art on the right (never overlapping a face),
  `Source: <domain>` badge, channel name. Lines are balanced ("CHECK / FORM 16").
- Art uses **OpenAI first, Cloudflare backup** (`THUMB_PROVIDERS`): Cloudflare kept drawing
  Apple logos, play buttons and $ figures. About 3 images, roughly $0.13 per video (estimate).
- Concept 1 uploads with the video; 2 and 3 are sent to Telegram for manual A/B tests in
  YouTube Studio. If no art can be made, Packaging's plain thumbnail is used.

## 6. Shorts Editor (`cutshorts.py`)

- Cuts two Shorts of **15-20 s** from the finished video; no new image or voice cost.
- Whole-sentence windows only; the disclaimer sentence is never content.
- **The two Shorts never overlap**: at least one full beat between them, enforced in the
  picker, again before rendering, and as a final end-vs-start check.
- Vertical 1080x1920: the 16:9 picture centred over a blurred copy, wrapped title,
  four-word captions, disclaimer banner.
- Approve uploads the main video, then both Shorts with a link to the full video and
  `#Shorts` **(untested against the real YouTube API)**.
- Falls back to the old written 60 s Shorts if cutting fails.

## 6b. Daily schedule (`schedule.py`)

Times chosen from a 90-day scan of 293 top Indian finance videos (YouTube Data API):
the 20:00-22:00 IST publish slot had the highest median views/day (about 11,000), ahead
of the crowded 18:00-20:00 slot. Shorts sample was small (75); 12:00-14:00 IST led.

| Step | Default (IST) | Env |
|---|---|---|
| News, trends, analytics | 05:35-06:00 | - |
| Produce one video | 06:30 | `PRODUCE_TIME_IST`, pause with `DAILY_VIDEO=0` |
| You approve in Telegram | any time | - |
| Full video goes public | 20:00 (next day if approved after 19:40) | `PUBLISH_TIME_IST` (`now` = immediate) |
| Shorts go public | next day 12:30 and 16:30 | `SHORT_TIMES_IST` |

Uploads use YouTube scheduled publishing (private + `publishAt`). The daily job skips if a
video was already made today or one is still waiting for approval (sends a reminder).

## 7. Telegram commands

`/new [slug]` full run - `/pending` - `/show <id>` - `/status` - `/channels` -
`/stats [slug]` analytics - `/trends [slug]` scan now - `/news` today's candidates -
`/doctor` recent incidents - `/ping` model health.

## 8. LLM and search order

- LLM: Gemini (several models) -> Groq -> DeepSeek (last fallback). DeepSeek cannot make
  images. Free Gemini often returns "high demand"; the chain absorbs it.
- Web search: **Tavily** (free, 1,000 credits/month, `site:` becomes a domain filter) ->
  **DuckDuckGo** (free, no key, often refuses the VPS with HTTP 202) -> **Serper** (last,
  while credits last). Results are cached in Redis.

## 8b. Disclaimer

- Money Clear's exact disclaimer (in `config/channels.json`) ends every description.
- On screen it appears only as a full-text card at the very end: **5 seconds on the full
  video, 2 seconds on each Short**. There is no start banner.
- The narration ends with a short spoken disclaimer sentence.

## 8c. Captions

- Full video: narration captions are **not** burned in; YouTube provides subtitles.
  Section headings and the disclaimer card stay. `BURN_CAPTIONS_LONG=1` restores them.
- Shorts: captions are **not** burned in either; YouTube subtitles are used.
  `BURN_CAPTIONS_SHORTS=1` restores them. The title and disclaimer card stay.

## 9. Operations (VPS)

- Host: `/opt/ytfactory`, Docker Compose services `postgres`, `redis`, `worker`.
- Code is mounted from `./worker`; **restart the worker to load code changes**:
  `docker compose restart worker`. To reload `.env` use
  `docker compose up -d --force-recreate worker`.
- A restart **kills any CLI run in progress** (`python -m app.cli money-clear`). Wait
  for it to finish first.
- Run a video from the shell: `docker exec -d -w /app ytf-worker sh -c "python -m app.cli money-clear > /app/data/run.log 2>&1"`.
- Media: `/opt/ytfactory/work/media/v<id>/` (master, thumbnail, `vis_*.jpg`, `cut1`, `cut2`).
- Env var names (values live only in `.env`, never in git or chat): `TELEGRAM_BOT_TOKEN`,
  `TELEGRAM_OWNER_ID`, `GEMINI_API_KEY`, `GROQ_API_KEY`, `DEEPSEEK_API_KEY`,
  `OPENAI_API_KEY`, `SERPER_API_KEY`, `YT_CLIENT_ID`, `YT_CLIENT_SECRET`,
  `YT_REFRESH_TOKEN`, `POSTGRES_PASSWORD`, optional `PEXELS_API_KEY`,
  `YT_PRIVACY` (now `public`), `IMAGE_GEMINI`, `OPENAI_IMAGE_MODEL`,
  `OPENAI_IMAGE_QUALITY`, `IMAGE_MAX_PER_VIDEO`.
- Daily YouTube quota 10,000 units: an upload costs 1,600 (main + 2 Shorts = 4,800);
  the trend scan costs about 1,000 and now skips if it already ran today.

## 10. Known gaps and risks

- No video has been uploaded yet; thumbnail upload and video-id saving are **untested**.
- No daily scheduler; someone sends `/new`. Per-channel YouTube logins are missing.
- `YT_PRIVACY=public` means every approved video goes live immediately.
- The worker logs print the Gemini API key inside request URLs; rotate that key.
- Several keys were exposed in chat during setup; rotate before sharing logs.
- Title and fact claims such as "2% per month" must be checked against the official
  source before approving.
- The VPS code is ahead of GitHub (uncommitted). Commit from the VPS or sync it back.
- Monetization needs 1,000 subscribers and 4,000 watch hours (or the Shorts route).
- Slide template redesign (title, big number, progress bar, source tag) is not done.
- The same cartoon character does not stay consistent across slides.
