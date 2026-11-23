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

    asyncio.create_task(ticker())
    await dp.start_polling(b)


if __name__ == "__main__":
    asyncio.run(main())
