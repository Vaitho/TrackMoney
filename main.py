import logging

from telegram import Update
from telegram.ext import AIORateLimiter, Application

import browser
import db
import handlers
import jobs
from concurrency import PerChatUpdateProcessor
from config import settings

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)  # evita di stampare il token negli URL
logging.getLogger("apscheduler").setLevel(logging.WARNING)  # altrimenti una riga al minuto


async def post_init(app: Application) -> None:
    await db.connect()
    await app.bot.set_my_commands(handlers.MENU)


async def post_shutdown(app: Application) -> None:
    await browser.shutdown()
    await db.close()


def build_app() -> Application:
    app = (
        Application.builder()
        .token(settings.bot_token)
        .rate_limiter(AIORateLimiter())
        .concurrent_updates(PerChatUpdateProcessor(16))
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    handlers.register(app)
    jobs.register(app.job_queue)
    return app


def main() -> None:
    allowed = settings.allowed_ids  # se ALLOWED_USERS è scritto male, il bot non parte (meglio che restare aperto)
    logging.getLogger(__name__).info(
        "Utenti autorizzati: %s", ", ".join(map(str, sorted(allowed))) if allowed else "tutti (ALLOWED_USERS è vuoto)"
    )
    # I messaggi modificati non vengono nemmeno richiesti a Telegram.
    build_app().run_polling(allowed_updates=[Update.MESSAGE, Update.CALLBACK_QUERY])


if __name__ == "__main__":
    main()
