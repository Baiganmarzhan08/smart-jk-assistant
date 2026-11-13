import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.types import User
from sqlalchemy.exc import SQLAlchemyError

from bot.assistant import AssistantUnavailable
from bot.document_store import DocumentSummary
from bot.main import create_dispatcher
from bot.models import Building, Residence, Service
from bot.residents import ResidenceProfile
from bot.voice import MAX_VOICE_BYTES, MAX_VOICE_SECONDS


RESIDENT = {"id": 42, "is_bot": False, "first_name": "Resident"}
BOT_USER = {"id": 123, "is_bot": True, "first_name": "Smart", "username": "smart_demo_bot"}
PRIVATE = {"id": 42, "type": "private", "first_name": "Resident"}
GROUP = {"id": -100111, "type": "supergroup", "title": "Smart Residence"}
VOICE = {"file_id": "voice-id", "file_unique_id": "unique-id", "duration": 5, "file_size": 100}


class Chat:
    def __init__(self, dispatcher, bot, api):
        self.dispatcher = dispatcher
        self.bot = bot
        self.api = api
        self.update_id = 0

    async def send(self, text, chat=PRIVATE, **extra):
        self.update_id += 1
        await self.dispatcher.feed_raw_update(self.bot, {
            "update_id": self.update_id,
            "message": {
                "message_id": self.update_id,
                "date": 1700000000,
                "chat": chat,
                "from": RESIDENT,
                "text": text,
                **extra,
            },
        })

    async def click(self, data, preview_id=999, chat=PRIVATE, user=RESIDENT):
        self.update_id += 1
        await self.dispatcher.feed_raw_update(self.bot, {
            "update_id": self.update_id,
            "callback_query": {
                "id": str(self.update_id),
                "from": user,
                "chat_instance": "test",
                "data": data,
                "message": {
                    "message_id": preview_id,
                    "date": 1700000000,
                    "chat": chat,
                    "from": BOT_USER,
                    "text": "Проверьте описание",
                },
            },
        })

    def last_method(self):
        return self.api.call_args.args[1]


@pytest.fixture
def chat_session(monkeypatch):
    @asynccontextmanager
    async def open_chat(assistant=None, database=None):
        dispatcher = create_dispatcher(assistant, database)
        async with Bot("123:test-token") as bot:
            api = AsyncMock(return_value=SimpleNamespace(message_id=999))
            monkeypatch.setattr(bot, "me", AsyncMock(return_value=User.model_validate(BOT_USER)))
            monkeypatch.setattr(bot.session, "make_request", api)
            try:
                yield Chat(dispatcher, bot, api)
            finally:
                await dispatcher.fsm.close()

    return open_chat


@pytest.mark.parametrize(("text", "expected"), [
    ("/start", "Smart Residence"),
    ("/help", "/services"),
    ("/services", "Демонстрационный каталог"),
    ("/documents", "Каталог документов пока недоступен"),
    ("/request", "Опишите проблему"),
    ("Услуги", "Демонстрационный каталог"),
    ("Когда починят лифт?", "/help"),
    ("/status", "Хранилище заявок пока недоступно"),
])
def test_private_routes(chat_session, text, expected):
    async def scenario():
        async with chat_session() as chat:
            await chat.send(text)
            method = chat.last_method()
            assert isinstance(method, SendMessage)
            assert expected in method.text
            if text == "/start":
                assert method.reply_markup.keyboard[0][1].text == "Подготовить заявку"

    asyncio.run(scenario())


@pytest.mark.parametrize(("action", "expected"), [
    ("draft:confirm", "Описание подтверждено"),
    ("draft:cancel", "Черновик отменён"),
])
def test_draft_review(chat_session, action, expected):
    async def scenario():
        async with chat_session() as chat:
            await chat.send("/request")
            await chat.send(" " * 1001)
            assert "Пришлите текст" in chat.last_method().text
            await chat.send("а" * 1001)
            assert "Пришлите текст" in chat.last_method().text
            await chat.send("В вымышленном подъезде не горит свет")
            assert "не горит свет" in chat.last_method().text
            await chat.click(action, preview_id=998)
            assert chat.last_method().show_alert
            await chat.click(action)
            edit = chat.api.call_args_list[-2].args[1]
            assert isinstance(edit, EditMessageText)
            assert expected in edit.text
            await chat.click(action)
            assert isinstance(chat.last_method(), AnswerCallbackQuery)
            assert chat.last_method().show_alert

    asyncio.run(scenario())


def test_cancel_command(chat_session):
    async def scenario():
        async with chat_session() as chat:
            await chat.send("/request")
            await chat.send("/cancel")
            assert "Черновик отменён" in chat.last_method().text
            await chat.send("Описание после отмены")
            assert "/help" in chat.last_method().text

    asyncio.run(scenario())


def test_group_routing(chat_session):
    async def scenario():
        async with chat_session() as chat:
            await chat.send("Обычное сообщение", GROUP)
            await chat.send("/help@another_bot", GROUP)
            chat.api.assert_not_called()

            await chat.send("/request@smart_demo_bot", GROUP)
            assert "личном чате" in chat.last_method().text
            await chat.send(
                "🏠 @Smart_Demo_Bot вопрос",
                GROUP,
                entities=[{"type": "mention", "offset": 3, "length": 15}],
            )
            assert "/help" in chat.last_method().text
            await chat.send("Ответ боту", GROUP, reply_to_message={
                "message_id": 900,
                "date": 1700000000,
                "chat": GROUP,
                "from": BOT_USER,
                "text": "Сообщение бота",
            })
            assert "/help" in chat.last_method().text
            assert chat.api.await_count == 3

    asyncio.run(scenario())


def test_documents_lists_titles_safely_and_handles_db_errors(chat_session, monkeypatch):
    async def scenario():
        database = object()
        source = "https://example.com/?a=1&b=2"
        sample = DocumentSummary(1, "Правила <ЖК>", "pdf", None, source, True)
        async with chat_session(database=database) as chat:
            monkeypatch.setattr("bot.handlers.chat_profile", AsyncMock(return_value=demo_profile()))
            monkeypatch.setattr("bot.handlers.list_documents", lambda engine, **kwargs: [sample])
            await chat.send("/documents@smart_demo_bot", GROUP)
            assert "Правила &lt;ЖК&gt;" in chat.last_method().text
            assert "?a=1&amp;b=2" in chat.last_method().text

            def fail(engine, **kwargs):
                raise SQLAlchemyError("private error details")

            monkeypatch.setattr("bot.handlers.list_documents", fail)
            await chat.send("/documents")
            assert "Не удалось открыть каталог" in chat.last_method().text

    asyncio.run(scenario())


def demo_profile():
    return ResidenceProfile(
        Residence(id=1, name="Smart Residence", slug="smart-residence", is_demo=True),
        [Building(id=11, residence_id=1, label="А", entrances=3, apartments=96)],
        [Service(name="Сантехник", description="Устранение протечек.", price="3000 ₸")],
    )


def test_real_request_flow_requires_confirmation_and_returns_number(chat_session, monkeypatch):
    async def scenario():
        profile = AsyncMock(return_value=demo_profile())
        monkeypatch.setattr("bot.handlers.chat_profile", profile)
        submitted = []

        def save(engine, **kwargs):
            submitted.append(kwargs)
            return 512

        monkeypatch.setattr("bot.handlers.create_ticket", save)
        async with chat_session(database=object()) as chat:
            await chat.send("/request")
            assert "Введите обозначение корпуса" in chat.last_method().text
            await chat.send("Неизвестный корпус")
            assert "из списка" in chat.last_method().text
            await chat.send("А")
            await chat.click("ticket:category:lighting")
            await chat.send("4")
            assert "от 1 до 3" in chat.last_method().text
            await chat.send("2")
            await chat.send("В подъезде не горит свет")
            assert "Корпус А, подъезд 2" in chat.last_method().text
            assert not submitted
            await chat.click("draft:confirm")
            edited = chat.api.call_args_list[-2].args[1]
            assert isinstance(edited, EditMessageText)
            assert "Заявка №512 зарегистрирована" in edited.text
            assert submitted[0]["telegram_id"] == RESIDENT["id"]
            assert (submitted[0]["category"], submitted[0]["building_id"], submitted[0]["entrance"]) == ("lighting", 11, 2)
            await chat.click("draft:confirm")
            assert len(submitted) == 1

    asyncio.run(scenario())


def test_status_checks_current_resident_and_hides_ticket_data_in_group(chat_session, monkeypatch):
    async def scenario():
        calls = []

        def lookup(engine, resident_id, number):
            calls.append((resident_id, number))
            return []

        monkeypatch.setattr("bot.handlers.own_tickets", lookup)
        async with chat_session(database=object()) as chat:
            await chat.send("/status@smart_demo_bot 12", GROUP)
            assert "личном чате" in chat.last_method().text
            assert not calls
            await chat.send("/status 12")
            assert "не найдена" in chat.last_method().text
            assert calls == [(RESIDENT["id"], 12)]

    asyncio.run(scenario())


def test_group_uses_selected_residence_catalog_and_private_link(chat_session, monkeypatch):
    async def scenario():
        monkeypatch.setattr("bot.handlers.chat_profile", AsyncMock(return_value=demo_profile()))
        async with chat_session(database=object()) as chat:
            await chat.send("/services@smart_demo_bot", GROUP)
            assert "3000 ₸" in chat.last_method().text
            await chat.send("/residence@smart_demo_bot", GROUP)
            assert "Квартир всего: 96" in chat.last_method().text
            await chat.send("/request@smart_demo_bot", GROUP)
            assert chat.last_method().reply_markup.inline_keyboard[0][0].url.endswith("?start=jk_smart-residence")

    asyncio.run(scenario())


def test_gemini_keeps_commands_and_drafts_separate(chat_session):
    async def scenario():
        assistant = SimpleNamespace(answer=AsyncMock(return_value="В каталоге есть сантехник."))
        async with chat_session(assistant) as chat:
            await chat.send("/start")
            assert "Gemini" in chat.last_method().text
            await chat.send("  Есть сантехник?  ")
            assistant.answer.assert_awaited_once_with("Есть сантехник?")
            assert chat.last_method().text == "В каталоге есть сантехник."
            assistant.answer.reset_mock()

            await chat.send("/services")
            await chat.send("Вопрос из группы", GROUP)
            await chat.send("/help@smart_demo_bot", GROUP)
            await chat.send("/request")
            await chat.send("/ask Содержимое черновика")
            assert "/cancel" in chat.last_method().text
            await chat.send("Текст черновика")
            assert "Проверьте описание" in chat.last_method().text
            assistant.answer.assert_not_awaited()

    asyncio.run(scenario())


@pytest.mark.parametrize(("text", "question", "answer"), [
    ("/ask@smart_demo_bot Есть ли электрик?", "Есть ли электрик?", "Да, в каталоге есть электрик."),
    ("/ask@smart_demo_bot Сантехник қызметі бар ма?", "Сантехник қызметі бар ма?", "Иә, бар."),
    ("/ask@smart_demo_bot Сәлем! У вас есть клининг?", "Сәлем! У вас есть клининг?", "Да, есть."),
])
def test_group_ask_routes_only_question_text(chat_session, text, question, answer):
    async def scenario():
        assistant = SimpleNamespace(answer=AsyncMock(return_value=answer))
        async with chat_session(assistant) as chat:
            await chat.send(text, GROUP)
            assistant.answer.assert_awaited_once_with(question)
            assert chat.last_method().text == answer
            assert chat.last_method().reply_parameters.message_id == chat.update_id

    asyncio.run(scenario())


def test_group_mentions_replies_and_unaddressed_text(chat_session):
    async def scenario():
        assistant = SimpleNamespace(answer=AsyncMock(return_value="Ответ"))
        async with chat_session(assistant) as chat:
            await chat.send("Просто сообщение", GROUP)
            await chat.send("/ask@another_bot Что-то?", GROUP)
            await chat.send("@another_bot привет", GROUP, entities=[
                {"type": "mention", "offset": 0, "length": 12},
            ])
            await chat.send("@smart_demo_bot сообщение из пересылки", GROUP, entities=[
                {"type": "mention", "offset": 0, "length": 15},
            ], forward_origin={"type": "hidden_user", "date": 1700000000, "sender_user_name": "Resident"})
            chat.api.assert_not_called()
            assistant.answer.assert_not_awaited()

            await chat.send("🏠 @Smart_Demo_Bot, есть сантехник?", GROUP, entities=[
                {"type": "mention", "offset": 3, "length": 15},
            ])
            assistant.answer.assert_awaited_once_with("🏠 , есть сантехник?")
            assert chat.last_method().reply_parameters.message_id == chat.update_id

            assistant.answer.reset_mock()
            await chat.send("А сколько стоит вызов сантехника?", GROUP, reply_to_message={
                "message_id": 900,
                "date": 1700000000,
                "chat": GROUP,
                "from": BOT_USER,
                "text": "Предыдущее сообщение",
            })
            assistant.answer.assert_awaited_once_with("А сколько стоит вызов сантехника?")

    asyncio.run(scenario())


def test_empty_group_ask_uses_local_hint(chat_session):
    async def scenario():
        assistant = SimpleNamespace(answer=AsyncMock())
        async with chat_session(assistant) as chat:
            await chat.send("/ask@smart_demo_bot", GROUP)
            assert "Добавьте текст вопроса" in chat.last_method().text
            assistant.answer.assert_not_awaited()

    asyncio.run(scenario())


def test_law_command_routes_to_documents(chat_session):
    async def scenario():
        assistant = SimpleNamespace(answer=AsyncMock(), answer_from_documents=AsyncMock(return_value="Дереккөз"))
        async with chat_session(assistant) as chat:
            await chat.send("/law@smart_demo_bot Ортақ мүлік деген не?", GROUP)
            assert chat.last_method().text == "Дереккөз"
            assistant.answer_from_documents.assert_awaited_once_with("Ортақ мүлік деген не?")
            assistant.answer.assert_not_awaited()

    asyncio.run(scenario())


def test_gemini_failure_and_question_length(chat_session):
    async def scenario():
        assistant = SimpleNamespace(answer=AsyncMock(side_effect=AssistantUnavailable))
        async with chat_session(assistant) as chat:
            await chat.send("а" * 1501)
            assert "1500" in chat.last_method().text
            assistant.answer.assert_not_awaited()
            await chat.send("Когда починят лифт?")
            assert "Не удалось получить ответ" in chat.last_method().text
            assistant.answer.assert_awaited_once()

    asyncio.run(scenario())


@pytest.fixture
def voice_download(monkeypatch):
    def install(chat, data=b"OggS sample"):
        async def write_audio(file, *, destination, seek):
            assert file == VOICE["file_id"]
            destination.write(data)

        download = AsyncMock(side_effect=write_audio)
        monkeypatch.setattr(chat.bot, "download", download)
        return download

    return install


def test_group_voice_is_addressed_and_only_sender_can_confirm(chat_session, voice_download):
    async def scenario():
        assistant = SimpleNamespace(
            transcribe=AsyncMock(return_value="Сәлем! Есть ли сантехник?"),
            answer=AsyncMock(return_value="Да, стоимость уточняется."),
        )
        async with chat_session(assistant) as chat:
            download = voice_download(chat)
            await chat.send(None, GROUP, voice=VOICE)
            download.assert_not_awaited()
            chat.api.assert_not_called()

            await chat.send(None, GROUP, voice=VOICE, reply_to_message={
                "message_id": 900, "date": 1700000000, "chat": GROUP, "from": BOT_USER, "text": "Вопрос?",
            })
            assistant.transcribe.assert_awaited_once_with(b"OggS sample")
            assert "Сәлем! Есть ли сантехник?" in chat.last_method().text
            assistant.answer.assert_not_awaited()

            await chat.click("voice:confirm", chat=GROUP, user={**RESIDENT, "id": 43})
            assert chat.last_method().show_alert
            assistant.answer.assert_not_awaited()

            await chat.click("voice:confirm", chat=GROUP)
            assistant.answer.assert_awaited_once_with("Сәлем! Есть ли сантехник?")
            assert "стоимость уточняется" in chat.last_method().text
            await chat.click("voice:confirm", chat=GROUP)
            assert chat.last_method().show_alert
            assistant.answer.assert_awaited_once()

    asyncio.run(scenario())


@pytest.mark.parametrize("button", ["voice:edit", "voice:cancel"])
def test_private_voice_can_be_replaced_or_cancelled(chat_session, voice_download, button):
    async def scenario():
        assistant = SimpleNamespace(transcribe=AsyncMock(return_value="Неверный текст"), answer=AsyncMock(return_value="Да."))
        async with chat_session(assistant) as chat:
            voice_download(chat)
            await chat.send(None, voice=VOICE)
            assert "Проверьте расшифровку" in chat.last_method().text
            await chat.click(button)
            assistant.answer.assert_not_awaited()
            await chat.send("Исправленный вопрос")
            assistant.answer.assert_awaited_once_with("Исправленный вопрос")

    asyncio.run(scenario())


@pytest.mark.parametrize("metadata", [
    {"duration": MAX_VOICE_SECONDS + 1},
    {"file_size": MAX_VOICE_BYTES + 1},
])
def test_voice_metadata_limits(chat_session, voice_download, metadata):
    async def scenario():
        assistant = SimpleNamespace(transcribe=AsyncMock())
        async with chat_session(assistant) as chat:
            download = voice_download(chat)
            await chat.send(None, voice={**VOICE, **metadata})
            assert "до 60 секунд" in chat.last_method().text
            download.assert_not_awaited()
            assistant.transcribe.assert_not_awaited()

    asyncio.run(scenario())


def test_voice_stream_size_limit(chat_session, voice_download):
    async def scenario():
        assistant = SimpleNamespace(transcribe=AsyncMock())
        async with chat_session(assistant) as chat:
            download = voice_download(chat, b"x" * (MAX_VOICE_BYTES + 1))
            await chat.send(None, voice={**VOICE, "file_size": None})
            assert "до 60 секунд" in chat.last_method().text
            download.assert_awaited_once()
            assistant.transcribe.assert_not_awaited()

    asyncio.run(scenario())


def test_voice_download_and_transcription_failures(chat_session, voice_download):
    async def scenario():
        assistant = SimpleNamespace(transcribe=AsyncMock(side_effect=AssistantUnavailable))
        async with chat_session(assistant) as chat:
            download = voice_download(chat)
            write_audio = download.side_effect
            download.side_effect = TimeoutError
            await chat.send(None, voice=VOICE)
            assert "Не удалось скачать" in chat.last_method().text
            assistant.transcribe.assert_not_awaited()

            download.side_effect = write_audio
            await chat.send(None, voice=VOICE)
            assert "Не удалось распознать" in chat.last_method().text
            assistant.transcribe.assert_awaited_once()

    asyncio.run(scenario())
