import asyncio
import logging
from logging.handlers import RotatingFileHandler

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeChat

import csat
import notify
import sessions
import storage
import triage
from api_client import PrintBoxAPIClient
from config import settings

# Managed directly here (not via shell redirection like `> bot.log`) so the
# file is actually bounded - 5MB x 5 backups = 25MB max, regardless of how
# long the process runs.
_LOG_FILE = "support_bot.log"
_LOG_MAX_BYTES = 5 * 1024 * 1024
_LOG_BACKUP_COUNT = 5


def _configure_logging() -> None:
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_handler = RotatingFileHandler(
        _LOG_FILE, maxBytes=_LOG_MAX_BYTES, backupCount=_LOG_BACKUP_COUNT, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logging.basicConfig(level=logging.INFO, handlers=[file_handler, console_handler])


_configure_logging()
logger = logging.getLogger(__name__)

_SESSION_SCAN_INTERVAL_SECONDS = 60


async def _expire_idle_sessions(bot: Bot, dp: Dispatcher) -> None:
    while True:
        await asyncio.sleep(_SESSION_SCAN_INTERVAL_SECONDS)
        for telegram_id in sessions.pop_expired(settings.session_timeout_minutes):
            try:
                chat_id = int(telegram_id)
                key = StorageKey(bot_id=bot.id, chat_id=chat_id, user_id=chat_id)
                await FSMContext(storage=dp.storage, key=key).clear()
                await bot.send_message(
                    chat_id,
                    "⌛ Сессия закрыта из-за неактивности. Если вопрос остался — напишите /start.",
                )
            except Exception:
                logger.exception("failed to expire session for %s", telegram_id)


async def _publish_commands(bot: Bot) -> None:
    """Everyone sees /start; only listed admins get /admin in their command
    menu. Without per-chat scopes the command is hidden by a check inside the
    handler but still advertised to every user."""
    await bot.set_my_commands(
        [BotCommand(command="start", description="Начать заново")],
        scope=BotCommandScopeAllPrivateChats(),
    )
    for raw_id in settings.admin_telegram_ids.split(","):
        admin_id = raw_id.strip()
        if not admin_id:
            continue
        try:
            await bot.set_my_commands(
                [
                    BotCommand(command="start", description="Начать заново"),
                    BotCommand(command="admin", description="Админ-панель"),
                ],
                scope=BotCommandScopeChat(chat_id=int(admin_id)),
            )
        except Exception:
            # A never-started chat rejects this - not worth failing startup over.
            logger.warning("could not set admin commands for %s", admin_id)


async def main() -> None:
    storage.init_db()

    bot = Bot(token=settings.support_bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(triage.router)
    dp.include_router(notify.router)
    dp.include_router(csat.router)

    await _publish_commands(bot)

    api = PrintBoxAPIClient()
    expiry_task = asyncio.create_task(_expire_idle_sessions(bot, dp))
    try:
        await dp.start_polling(bot, api=api)
    finally:
        expiry_task.cancel()
        await api.aclose()


if __name__ == "__main__":
    asyncio.run(main())
