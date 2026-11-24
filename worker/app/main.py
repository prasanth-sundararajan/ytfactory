import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

from app import bot as botmod
from app.config import CFG
from app.db import init_db, seed_agents, seed_channels
from app.state import write_state

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
log = logging.getLogger("main")


async def ticker():
    while True:
        try:
            write_state()
        except Exception as e:
            log.warning("state write failed: %s", e)
        await asyncio.sleep(60)


async def analyst_daily():
    """Collect analytics once a day at 00:30 UTC (06:00 IST)."""
    import datetime
    from app import analytics
    from app.db import list_channels
    last = None
    while True:
        now = datetime.datetime.utcnow()
        if last != now.date() and (now.hour, now.minute) >= (0, 30):
            last = now.date()
            for ch in list_channels():
                try:
                    r = await asyncio.to_thread(analytics.collect, ch["slug"])
                    log.info("analyst %s", r)
                except Exception as e:
                    log.warning("analyst %s failed: %s", ch["slug"], e)
        await asyncio.sleep(300)


async def daily_producer(b):
    """One video a day at PRODUCE_TIME_IST, delivered to Telegram for approval."""
    import datetime
    import os
    from app import bot as botmod, schedule as SCH
    from app.pipeline import run_pipeline, summary_text
    last = None
    while True:
        try:
            now = datetime.datetime.now(SCH.IST)
            h, m = SCH.parse(os.environ.get("PRODUCE_TIME_IST", "06:30"))
            due = (os.environ.get("DAILY_VIDEO", "1") == "1" and last != now.date()
                   and (now.hour, now.minute) >= (h, m))
            if due and not botmod.register.busy:
                last = now.date()
                pend = await asyncio.to_thread(SCH.pending_approval)
                if await asyncio.to_thread(SCH.produced_today, "money-clear"):
                    log.info("daily: already produced today")
                elif pend:
                    await b.send_message(CFG.owner_id,
                        "\u23f8 Daily video skipped: video(s) "
                        + ", ".join(str(x) for x in pend)
                        + " still waiting for approval. Use /pending.")
                else:
                    botmod.register.busy = True
                    try:
                        await b.send_message(CFG.owner_id, "\U0001f3ac Daily video started.")
                        vid, ctx = await run_pipeline("money-clear")
                        await b.send_message(CFG.owner_id, summary_text(ctx)[:4000])
                        if not ctx.get("blocked"):
                            await botmod.deliver_to_owner(b, vid, ctx)
                    finally:
                        botmod.register.busy = False
        except Exception as e:
            log.warning("daily producer error: %s", e)
        await asyncio.sleep(120)


async def news_daily():
    """Pick the day's topic from official news at 00:05 UTC (skips if done)."""
    import datetime
    from app import news
    last = None
    while True:
        now = datetime.datetime.now(datetime.timezone.utc)
        if last != now.date() and (now.hour, now.minute) >= (0, 5):
            last = now.date()
            try:
                await news.run("money-clear")
            except Exception as e:
                log.warning("news failed: %s", e)
        await asyncio.sleep(300)


async def trends_daily():
    """Scan trends once a day at 00:10 UTC (before the analyst and any /new)."""
    import datetime
    from app import trends
    last = None
    while True:
        now = datetime.datetime.now(datetime.timezone.utc)
        if last != now.date() and (now.hour, now.minute) >= (0, 10):
            last = now.date()
            try:
                from app.db import get_channel
                if not await asyncio.to_thread(trends.done_today, get_channel("money-clear")["id"]):
                    await trends.run("money-clear")
            except Exception as e:
                log.warning("trends failed: %s", e)
        await asyncio.sleep(300)


async def main():
    log.info("initialising database")
    init_db()
    n_agents = seed_agents()
    n_channels = seed_channels()
    log.info("seeded %d agents, %d channels", n_agents, n_channels)
    write_state()

    b = Bot(CFG.telegram_token, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()
    botmod.register(dp)
    await b.delete_webhook(drop_pending_updates=True)

    me = await b.get_me()
    await b.send_message(
        CFG.owner_id,
        "<b>YT Factory worker online</b>\n\n"
        f"Bot: @{me.username}\n"
        f"Agents: <b>{n_agents}</b>   Channels: <b>{n_channels}</b>\n"
        "Database: postgres \u2705   Queue: redis \u2705   YouTube \u2705\n\n"
        "Commands: /status  /ping  /new  /channels",
    )
    log.info("startup message sent to %s", CFG.owner_id)

    from app import healer
    healer.notify = lambda text: b.send_message(CFG.owner_id, text)
    asyncio.create_task(healer.watchdog())
    asyncio.create_task(ticker())
    asyncio.create_task(analyst_daily())
    asyncio.create_task(news_daily())
    asyncio.create_task(daily_producer(b))
    asyncio.create_task(trends_daily())
    await dp.start_polling(b)


if __name__ == "__main__":
    asyncio.run(main())
