import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramAPIError, TelegramUnauthorizedError
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.types import BotCommand
from aiogram.utils.token import TokenValidationError
from aiohttp import ClientSession
from sqlalchemy.engine import Engine, URL

from bot.assistant import GeminiAssistant
from bot.config import load_gemini_settings, load_token
from bot.database import create_database_engine, database_url
from bot.embeddings import GeminiEmbeddings
from bot.handlers import create_router
from bot.knowledge import KnowledgeBase


COMMANDS = (
    BotCommand(command="start", description="Главное меню"),
    BotCommand(command="help", description="Помощь"),
    BotCommand(command="services", description="Каталог услуг"),
    BotCommand(command="residence", description="Адрес, корпуса и контакты ЖК"),
    BotCommand(command="documents", description="Список документов ЖК"),
    BotCommand(command="ask", description="Задать вопрос о ЖК"),
    BotCommand(command="law", description="Найти ответ в документах ЖК"),
    BotCommand(command="request", description="Оформить заявку"),
    BotCommand(command="status", description="Статус своих заявок"),
    BotCommand(command="cancel", description="Отменить черновик или расшифровку"),
)


def create_dispatcher(assistant: GeminiAssistant | None = None, database: Engine | None = None) -> Dispatcher:
    dispatcher = Dispatcher(events_isolation=SimpleEventIsolation(), assistant=assistant, database=database)
    dispatcher.include_router(create_router())
    return dispatcher


async def run_bot(token: str, api_key: str, model: str, url: URL | None = None) -> None:
    database = create_database_engine(url) if url is not None else None
    try:
        async with Bot(token) as bot, ClientSession() as session:
            knowledge = KnowledgeBase(database, GeminiEmbeddings(session, api_key)) if database and api_key else None
            assistant = GeminiAssistant(session, api_key, model, knowledge) if api_key else None
            dispatcher = create_dispatcher(assistant, database)
            await bot.set_my_commands(list(COMMANDS))
            await dispatcher.start_polling(bot, close_bot_session=False)
    finally:
        if database is not None:
            await asyncio.to_thread(database.dispose)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        token = load_token()
        api_key, model = load_gemini_settings()
        url = database_url(required=False)
    except ValueError as error:
        raise SystemExit(str(error)) from None

    try:
        asyncio.run(run_bot(token, api_key, model, url))
    except (TokenValidationError, TelegramUnauthorizedError):
        raise SystemExit("Проверьте токен бота в .env.") from None
    except TelegramAPIError:
        raise SystemExit("Ошибка Telegram Bot API. Проверьте соединение с Telegram.") from None


if __name__ == "__main__":
    main()
