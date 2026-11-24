# Growth Agent — Improvement Plan

A daily agent that looks at how every channel is doing, finds **one new, testable
way** to get more views and subscribers, runs it as an experiment, measures it,
and feeds what worked back into Scout / Brief / Script.

Same rule as the rest of YT Factory: **nothing changes on YouTube without a
human click in Telegram.**

---

## 1. Where we are today (gaps found in the code)

| Gap | Where | Why it blocks growth |
|-----|-------|----------------------|
| `analyst` agent is declared but never implemented | `config/agents.json` | No feedback loop: Scout picks topics blind |
| YouTube video id is not saved after upload | `bot.py` `_approve` → only `set_video_status(vid, "published")` | Can't link a video to its views / subs |
| Shorts are rendered but never uploaded | `bot.py` uploads `render["master"]` only | Shorts are the cheapest subscriber source |
| No custom thumbnail | `youtube.py` has no `thumbnails.set` | Thumbnail + title drive most of CTR |
| No schedule — every video needs `/new` | `main.py` only runs `ticker()` | Irregular uploads hurt reach |
| Uploads are `private` by default | `YT_PRIVACY` | Fine for safety, but metrics stay at zero until switched |
| One `YT_REFRESH_TOKEN` for 4 channels | `config.py` | Can only upload to / read one channel |
| Scout has no market signal | `agents.scout` | Topics come from the LLM only, not from search demand or competitors |

The OAuth URL already asks for `yt-analytics.readonly` and `youtube.readonly`,
so the analytics data is reachable once the token is refreshed.

---

## 2. The daily loop

```
 06:00 IST  ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐
 ──────────▶│ COLLECT  │──▶│ DIAGNOSE │──▶│ DISCOVER │──▶│ PROPOSE  │──▶ Telegram
            └──────────┘   └──────────┘   └──────────┘   └──────────┘      │
                 ▲                                                        approve
                 │         ┌──────────┐   ┌──────────┐                     │
                 └─────────│  LEARN   │◀──│ MEASURE  │◀──── APPLY ◀────────┘
                           └──────────┘   └──────────┘
```

1. **Collect** — pull yesterday's numbers for every published video and channel.
2. **Diagnose** — find the weakest link: impressions, CTR, retention, or sub conversion.
3. **Discover** — look outside: trending searches, competitor uploads, comments.
4. **Propose** — choose ONE experiment aimed at the weakest link (never repeat a
   tested idea unless the result was unclear).
5. **Apply** — after approval: change a title or thumbnail, add a pinned comment,
   schedule a Short, change Scout's topic weights, and so on.
6. **Measure** — after 48 h / 7 d, compare against the baseline.
7. **Learn** — write the result to the playbook, which is added to Scout,
   Brief and Script prompts.

---

## 3. What the agent measures

From the **YouTube Analytics API** (`reports.query`, `ids=channel==MINE`):

| Metric | Funnel stage | Diagnosis when low |
|--------|-------------|--------------------|
| `impressions` (via Reporting API) / `views` | Reach | Topic has no demand, or SEO is weak |
| impression CTR | Click | Title / thumbnail problem |
| `averageViewPercentage`, `averageViewDuration` | Watch | Hook / pacing problem |
| `audienceWatchRatio` (retention curve) | Watch | Exact second where viewers leave |
| `subscribersGained / views` | Convert | Missing CTA, no series, no reason to return |
| `insightTrafficSourceType` | Reach | Search vs Browse vs Shorts feed vs Suggested |
| `likes`, `comments`, `shares` | Engage | Community signal |

From the **Data API**: per-video stats, titles, tags, and the newest comments.

The **weakest-link rule**: compare each metric with the channel's own 30-day
median (and after ~20 videos, with the niche benchmark). The stage with the
largest negative gap gets the experiment.

---

## 4. The experiment library (seed ideas)

The agent starts with these and can add its own ideas (LLM + web research).
Each idea has a target stage, a way to apply it, and a success metric.

### Reach (views / impressions)
- **Search-demand topics**: Scout ranks candidate topics by Google/YouTube autocomplete
  volume (Serper `autocomplete` + YouTube search suggestion endpoint) before choosing.
- **Timely hooks**: watch official sources (RBI policy dates, Budget, ITR deadline,
  AMFI monthly data, new AI model launches) and schedule videos ahead of them.
- **Shorts → long funnel**: upload the two Shorts already rendered, 24 h and 48 h after
  the long video, with the long video linked as "related video".
- **Series & playlists**: group videos by theme ("Tax 2026-27", "AI for freelancers")
  and add each upload to its playlist automatically.
- **Upload time test**: rotate publish hour (e.g. 07:00 / 12:30 / 19:00 IST) and keep
  the winner per channel.
- **Description and chapter SEO**: add timestamps from the storyboard beats, put the
  first 2 lines around the main keyword, add 3 hashtags.

### Click (CTR)
- **Custom thumbnails**: render a 1280×720 thumbnail (big number + 3-word hook) with
  the existing Pillow card renderer, upload with `thumbnails.set`.
- **Title A/B**: generate 3 titles; after 48 h, if CTR < channel median, swap to
  title B, measure again (YouTube's built-in "Test & compare" can't be called
  through the API, so do it manually in sequence).
- **Number-first titles** for finance ("₹50,000 deduction most people miss").

### Watch (retention)
- **Hook rewrite**: if retention drops > 30 % in the first 30 s, Brief must open with the
  payoff, not the context.
- **Pattern breaks**: a new visual every 6–8 s (the storyboard already does this —
  tune the beat length from retention data).
- **Shorter / longer test**: 4 min vs 7 min for the same topic type.

### Convert (subscribers)
- **Specific CTA**: "Next week: how to claim HRA without rent receipts — subscribe so you
  don't miss it" instead of a generic subscribe line.
- **End screen + pinned comment** linking the next video in the series.
- **Reply to comments** within 24 h (agent drafts, human approves in Telegram).
- **Comment-driven topics**: questions from comments go straight to Scout's queue.

### Not allowed (hard rules in the prompt and in code)
No sub-for-sub, bought views, bots, fake comments, misleading thumbnails,
re-uploading other creators' content, "guaranteed returns" claims, or anything that
breaks YouTube's spam / deceptive-practice policies. These can get the channel removed.

---

## 5. Build plan (phased)

### Phase 0 — Make growth measurable (1–2 days)
- `schema.sql`: add `videos.yt_video_id`, `videos.published_at`, `videos.parent_id`
  (Short → long link).
- `bot.py` `_approve`: save `res["id"]` into `yt_video_id`.
- `config.py` / `channels.json`: one refresh token per channel
  (`YT_REFRESH_TOKEN_<SLUG>`), `youtube.py` takes the channel slug.
- Decide the privacy policy: keep `private` for review, but switch to `public`
  (or scheduled `publishAt`) after approval — metrics need public videos.

### Phase 1 — Analyst agent (2–3 days)
- New `worker/app/analytics.py`: `channel_daily()`, `video_metrics(ids)`,
  `retention(video_id)`, `traffic_sources()`, `recent_comments()`.
- New table `metrics_daily(video_id, day, views, watch_min, avg_view_pct,
  subs_gained, likes, comments, shares, ctr, impressions)`.
- Implement the `analyst` agent in `agents.py`, saving a daily snapshot.
- Telegram `/stats [slug]` → 7-day summary per channel.

### Phase 2 — Growth agent + experiments (3–4 days)
- New tables:
  - `experiments(id, channel_id, idea_key, hypothesis, stage, action jsonb,
    baseline jsonb, result jsonb, status, started_at, measured_at)`
  - `playbook(channel_id, lesson, evidence, confidence, created_at)`
- New `worker/app/growth.py`:
  - `diagnose()` → weakest stage per channel
  - `discover()` → Serper trends + competitor top videos + comment questions
  - `propose()` → LLM picks ONE experiment from the library or invents one,
    must not repeat a tested `idea_key`
  - `apply()` → executes the action through `youtube.py` (title update,
    thumbnail, playlist, pinned comment draft, Shorts schedule, Scout weights)
  - `measure()` → closes experiments at 48 h and 7 d, writes the verdict
- Telegram: morning report with buttons **Run experiment / Skip / Suggest another**.

### Phase 3 — Daily scheduler (1 day)
- In `main.py`, add `growth_daily()` next to `ticker()` (asyncio loop or APScheduler):
  - 06:00 IST: collect → diagnose → propose → Telegram report
  - 06:30 IST: `/new` for each active channel (optional auto-produce, still needs approval)
  - every 6 h: measure open experiments
- Guard against duplicate runs with a Redis lock.

### Phase 4 — Close the loop into content (2 days)
- Scout prompt gets: top 5 playbook lessons + best-performing topics + comment
  questions + search-demand scores.
- Brief / Script prompts get: hook and CTA lessons, ideal length for this channel.
- Thumbnail generator + `thumbnails.set` upload.
- Shorts upload with schedule and link to the parent video.

### Phase 5 — Weekly strategy review (later)
- Sunday report: what moved views / subs, which experiments won, next week's
  series plan, channels to pause or double down on.

---

## 6. Morning Telegram report (example)

```
📈 Money Clear — daily growth (23 Nov)
Views 1,240 (+18%)   Subs +14 (+5)   CTR 4.1%   Avg view 38%

Weakest link: CLICK (CTR 4.1% vs 30-day median 5.6%)
Last experiment: "Specific CTA" → subs/1k views 9.8 → 11.3  ✅ kept

Today's idea: Number-first title on video #42
  now:  "Understanding Section 80C Deductions"
  test: "₹1.5 Lakh Tax Saving Most Salaried People Miss (80C)"
  measure: CTR after 48 h vs 4.1%

[ ▶ Run ]  [ ⏭ Skip ]  [ 💡 Another idea ]
```

---

## 7. API quota and cost

- Data API: 10,000 units/day by default. Upload = 1,600, title update = 50,
  thumbnail = 50, comment list = 1, stats read = 1. Budget: ≤ 2 uploads + all
  reads + a few updates per channel per day fits easily; check this before adding
  more channels.
- Analytics API: separate quota, reads are cheap.
- LLM: one Gemini call for diagnosis + one for the proposal per channel per day.

---

## 8. Success metrics for the agent itself

- Every published video has a `yt_video_id` and daily metrics (100 %).
- ≥ 5 experiments closed per channel per month, each with a clear verdict.
- Playbook lessons are used in Scout / Script prompts (logged in `agent_runs`).
- 90-day target: subs per 1k views and CTR above the first-month baseline.

## 9. Suggested order of work

1. Phase 0 (save the video id, per-channel tokens) — everything else depends on it.
2. Phase 1 Analyst + `/stats`.
3. Upload the Shorts + custom thumbnails (biggest quick wins).
4. Phase 2 Growth agent + Phase 3 scheduler.
5. Phase 4 feedback into the prompts.
