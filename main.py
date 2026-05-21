import asyncio
import logging
import signal

import uvicorn
from aiogram import Bot
from fastapi import FastAPI

import alice
import bot as bot_module
import db
from config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def create_app(telegram_bot: Bot) -> FastAPI:
    app = FastAPI(title="Alisa SOS", docs_url=None, redoc_url=None)
    app.state.bot = telegram_bot
    app.include_router(alice.router)

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    return app


async def run_bot(dp, telegram_bot: Bot) -> None:
    logger.info("Starting Telegram bot polling...")
    await dp.start_polling(telegram_bot, allowed_updates=["message", "callback_query"])


async def run_server(app: FastAPI) -> None:
    config = uvicorn.Config(
        app,
        host=settings.host,
        port=settings.port,
        log_level="info",
    )
    server = uvicorn.Server(config)
    logger.info("Starting FastAPI server on %s:%d", settings.host, settings.port)
    await server.serve()


async def main() -> None:
    await db.init(settings.db_path)
    telegram_bot = Bot(token=settings.telegram_bot_token)
    dp = bot_module.create_dispatcher()
    app = create_app(telegram_bot)

    loop = asyncio.get_running_loop()

    polling_task = loop.create_task(run_bot(dp, telegram_bot))
    server_task = loop.create_task(run_server(app))

    def _stop(sig, frame):  # noqa: ARG001
        logger.info("Shutting down...")
        polling_task.cancel()
        server_task.cancel()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    try:
        await asyncio.gather(polling_task, server_task)
    except asyncio.CancelledError:
        pass
    finally:
        await telegram_bot.session.close()
        await db.close()
        logger.info("Stopped.")


if __name__ == "__main__":
    asyncio.run(main())
