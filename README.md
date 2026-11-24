# YT Factory

An automated YouTube content factory for finance and AI explainer channels.
It discovers a topic, grounds it in **official sources only**, writes a 5-minute
script plus two shorts, narrates and renders them, runs a quality gate, and
sends the result to Telegram for approval before anything is uploaded.

Nothing publishes without a human click.

## Pipeline

```
scout -> research -> brief -> script -> fact -> voice -> storyboard -> visual -> render -> qc -> approve -> upload
```

| Agent | Role |
|-------|------|
| Scout | Trend and topic discovery |
| Research | Web search + fact sheet from official domains only |
| Brief | Audience angle and outline |
| Scriptwriter | 5-minute script + two 60-second shorts |
| FactCheck | Claim and number verification against the fact sheet |
| Voice | TTS narration (English / Tamil) |
| VisualDirector | Storyboard: one beat per sentence, each planned as a data slide or a related image |
| Renderer | FFmpeg 1080p master + 1080x1920 shorts |
| QC | Resolution, duration, audio, loudness and black-frame checks |
| Publisher | Telegram approval, then YouTube upload |

## Storyboard

The narration is split into sentence beats with timings taken from the voice
track, and every beat is planned before any media is fetched:

- a beat that states a rate, limit or amount becomes a **data slide** (section
  heading + the number, e.g. `₹50,000`, `15%`, `36 months`);
- every other beat becomes a **key-phrase card**: the section heading plus a
  short phrase from that beat (`Old Tax Regime`, `tax exemptions`), so the text
  on screen changes with the narration while the captions carry the full line;
- if `PEXELS_API_KEY` is set, beats first try a relevant stock photo and fall
  back to the card. Openverse and Wikimedia Commons are not used for beats:
  they match keywords but return art scans and manuscripts.

Captions are burned from word-level timings. `edge-tts` 7.3 defaults to
sentence-level events, so the voice module explicitly requests
`boundary="WordBoundary"` and falls back to spreading words inside each
sentence window if the service ignores it.

The plan is written to `work/media/v<id>/storyboard.json` and
`storyboard.md` (human-readable), then the renderer cuts each section into
clips whose length matches the beat, so the picture changes with the voice.

## Stack

- Python 3.12, `aiogram` Telegram bot
- PostgreSQL 16 + Redis 7 (search cache)
- `edge-tts` narration with word-level timings
- FFmpeg 7 for clips, captions (libass), loudness normalisation
- Pillow for generated fallback cards
- Gemini (primary) and Groq (fallback) for LLM calls
- Serper (Google) with DuckDuckGo fallback for search
- YouTube Data API v3 for uploads

## Layout

```
bin/            ytf CLI, OAuth helpers
config/         agents.json, channels.json
worker/app/     agents, pipeline, render, voice, visual, search, llm, db, bot
worker/sql/     database schema
docker-compose.yml
```

## Configuration

Copy the values into a `.env` file next to `docker-compose.yml`:

```
TELEGRAM_BOT_TOKEN=
TELEGRAM_OWNER_ID=
GEMINI_API_KEY=
GROQ_API_KEY=
SERPER_API_KEY=
POSTGRES_PASSWORD=
YT_CLIENT_ID=
YT_CLIENT_SECRET=
YT_REFRESH_TOKEN=
# optional
PEXELS_API_KEY=
YT_PRIVACY=private
```

`.env` is git-ignored. Runtime media (`work/`), the Postgres data directory
(`data/`) and logs are also ignored.

## Run

```bash
docker compose up -d --build
```

The worker seeds the database, starts the Telegram bot and sends a startup
message to the owner.

### Telegram commands

- `/status` - agent pipeline board
- `/channels` - configured channels
- `/new [slug]` - run the full pipeline and send the preview for approval
- `/pending` - videos waiting for approval
- `/show <id>` - re-send a rendered video
- `/ping` - test every LLM provider

### CLI

```bash
python -m app.cli money-clear            # script + render + preview
python -m app.cli money-clear --no-render  # script only
python -m app.cli --list                 # list channels
python -m app.cli --send 12              # re-send a video for approval
```

## Safety rules

- Research may only use pages from the channel's `official_sources` list.
- Every fact must be backed by a verbatim quote from the fetched page text.
- Numbers are mechanically checked against the source text.
- FactCheck classifies every claim and sets a risk level; medium/high risk
  stops the video at `needs_review`.
- Uploads default to `private` and require an explicit approval click.
