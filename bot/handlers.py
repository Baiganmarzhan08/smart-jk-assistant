import logging
from asyncio import to_thread
from html import escape
from uuid import UUID, uuid4

from aiogram import Bot, F, Router
from aiogram.enums import ChatType, MessageEntityType, ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    LinkPreviewOptions,
    Message,
    ReplyKeyboardMarkup,
    ReplyParameters,
    User,
)
from aiohttp import ClientError
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from bot.assistant import MAX_QUESTION_LENGTH, AssistantUnavailable, GeminiAssistant
from bot.catalog import DEMO_CATALOG
from bot.document_store import list_documents
from bot.residents import (
    CATEGORIES, STATUSES, ResidenceProfile, ResidentInputError, create_ticket,
    own_tickets, residence_profile, select_residence,
)
from bot.voice import MAX_VOICE_BYTES, MAX_VOICE_SECONDS, VoiceTooLarge, download_voice


HELP_BUTTON = "Помощь"
SERVICES_BUTTON = "Услуги"
REQUEST_BUTTON = "Подготовить заявку"
MAX_DESCRIPTION_LENGTH = 1000
LOGGER = logging.getLogger(__name__)

MAIN_MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=SERVICES_BUTTON), KeyboardButton(text=REQUEST_BUTTON)],
        [KeyboardButton(text=HELP_BUTTON)],
    ],
    resize_keyboard=True,
)

REVIEW_BUTTONS = InlineKeyboardMarkup(
    inline_keyboard=[[
        InlineKeyboardButton(text="Подтвердить", callback_data="draft:confirm"),
        InlineKeyboardButton(text="Отменить", callback_data="draft:cancel"),
    ]],
)

CATEGORY_BUTTONS = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text=label, callback_data=f"ticket:category:{category}")]
    for category, label in CATEGORIES.items()
])

VOICE_BUTTONS = InlineKeyboardMarkup(inline_keyboard=[[
    InlineKeyboardButton(text="Верно", callback_data="voice:confirm"),
    InlineKeyboardButton(text="Исправить", callback_data="voice:edit"),
    InlineKeyboardButton(text="Отменить", callback_data="voice:cancel"),
]])


class Draft(StatesGroup):
    building = State()
    category = State()
    entrance = State()
    description = State()
    review = State()


class VoiceReview(StatesGroup):
    pending = State()


async def chat_profile(
    message: Message, database: Engine, resident_id: int | None = None,
) -> ResidenceProfile:
    if message.chat.type == ChatType.PRIVATE:
        return await to_thread(residence_profile, database, telegram_id=resident_id or message.chat.id)
    return await to_thread(residence_profile, database, group_chat_id=message.chat.id)


async def start(
    message: Message, command: CommandObject, state: FSMContext,
    assistant: GeminiAssistant | None, database: Engine | None,
) -> None:
    private = message.chat.type == ChatType.PRIVATE
    name = "Smart Residence"
    if database is not None:
        try:
            if private and command.args and command.args.startswith("jk_"):
                if await state.get_state() is not None:
                    await message.answer("Завершите текущий шаг или отправьте /cancel, затем откройте ссылку снова.")
                    return
                await to_thread(select_residence, database, message.chat.id, command.args[3:])
            profile = await chat_profile(message, database)
            name = profile.residence.name
        except ResidentInputError as error:
            await message.answer(str(error))
            return
        except SQLAlchemyError:
            await message.answer("Справочник ЖК пока недоступен. Попробуйте позже.")
            return
    greeting = f"Добро пожаловать в {name}! Выберите услугу или подготовьте заявку."
    if assistant is not None:
        how_to_ask = (
            "Задайте вопрос о ЖК обычным сообщением."
            if private
            else "Задайте вопрос через /ask@имя_бота, упоминание или ответ на сообщение бота."
        )
        greeting += (
            f"\n\n{how_to_ask} "
            "Вопрос будет передан Gemini. Не отправляйте персональные данные."
            "\nГолосовое сообщение Gemini расшифрует, а вы сможете проверить текст перед ответом."
        )
    await message.answer(greeting, reply_markup=MAIN_MENU if private else None)


async def help_command(message: Message) -> None:
    await message.answer(
        "/start — главное меню\n"
        "/help — помощь\n"
        "/services — каталог услуг вашего ЖК\n"
        "/residence — адрес, корпуса и контакты ЖК\n"
        "/documents — список документов ЖК\n"
        "/ask — задать вопрос о ЖК. В группе используйте /ask@имя_бота и текст вопроса\n"
        "/law — найти ответ в документах ЖК\n"
        "/request — оформить заявку в личном чате\n"
        "/status — статус своих заявок в личном чате\n"
        "/cancel — отменить черновик или расшифровку\n"
        "Голосовой вопрос до 60 секунд. В группе запишите его в ответ на сообщение бота."
    )


async def send_lines(message: Message, lines: list[str]) -> None:
    block = ""
    for line in lines:
        if len(block) + len(line) + 1 > 1900:
            await message.answer(block)
            block = ""
        block = f"{block}\n{line}" if block else line
    if block:
        await message.answer(block)


async def services(message: Message, database: Engine | None) -> None:
    if database is None:
        await message.answer(DEMO_CATALOG)
        return
    try:
        profile = await chat_profile(message, database)
    except (SQLAlchemyError, ResidentInputError):
        await message.answer("Каталог услуг пока недоступен.")
        return
    catalog = profile.catalog_lines() or ["Доступных услуг пока нет."]
    label = "Демонстрационный каталог" if profile.residence.is_demo else "Каталог услуг"
    await send_lines(message, [f"{label} · {profile.residence.name}", "", *catalog])


async def residence_command(message: Message, command: CommandObject, state: FSMContext, database: Engine | None) -> None:
    if database is None:
        await message.answer("Справочник ЖК пока недоступен.")
        return
    slug = (command.args or "").strip().lower()
    try:
        if slug and message.chat.type == ChatType.PRIVATE:
            if await state.get_state() is not None:
                await message.answer("Завершите текущий шаг или отправьте /cancel перед сменой ЖК.")
                return
            await to_thread(select_residence, database, message.chat.id, slug)
        profile = await chat_profile(message, database)
    except ResidentInputError as error:
        await message.answer(str(error))
        return
    except SQLAlchemyError:
        await message.answer("Справочник ЖК пока недоступен.")
        return
    await send_lines(message, [*profile.facts().splitlines(), f"Код ЖК: {profile.residence.slug}"])


async def documents_command(message: Message, database: Engine | None) -> None:
    if database is None:
        await message.answer("Каталог документов пока недоступен.")
        return
    try:
        profile = await chat_profile(message, database)
        documents = await to_thread(list_documents, database, residence_id=profile.residence.id)
    except (SQLAlchemyError, ResidentInputError) as error:
        LOGGER.warning("Не удалось получить список документов: %s", type(error).__name__)
        await message.answer("Не удалось открыть каталог документов. Попробуйте позже.")
        return
    if not documents:
        await message.answer("Публичных документов пока нет.")
        return

    lines = ["Последние документы ЖК"]
    for document in documents:
        title = escape(document.title)
        if document.source_url:
            title = f'<a href="{escape(document.source_url)}">{title}</a>'
        revision = f", редакция от {document.revision_date:%d.%m.%Y}" if document.revision_date else ""
        lines.append(f"• {title} ({document.file_format.upper()}{revision})")
    await message.answer(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


async def request_draft(message: Message, state: FSMContext, bot: Bot, database: Engine | None) -> None:
    if message.chat.type != ChatType.PRIVATE:
        identity = await bot.me()
        link = f"https://t.me/{identity.username}"
        if database is not None:
            try:
                profile = await chat_profile(message, database)
                link += f"?start=jk_{profile.residence.slug}"
            except (SQLAlchemyError, ResidentInputError):
                await message.answer("Справочник ЖК пока недоступен. Попробуйте позже.")
                return
        await message.answer(
            "Оформите заявку через /request в личном чате с ботом.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="Открыть личный чат", url=link),
            ]]),
        )
        return

    if database is not None:
        try:
            profile = await chat_profile(message, database)
        except (SQLAlchemyError, ResidentInputError):
            await message.answer("Не удалось открыть данные ЖК. Попробуйте позже.")
            return
        if not profile.buildings:
            await message.answer("В справочнике ЖК пока нет корпусов для оформления заявки.")
            return
        await state.clear()
        name = profile.residence.name + (" (учебный ЖК)" if profile.residence.is_demo else "")
        await state.set_data({
            "residence_id": profile.residence.id,
            "residence_name": name,
            "buildings": {item.label.casefold(): (item.id, item.label, item.entrances) for item in profile.buildings},
            "submission_key": str(uuid4()),
        })
        await state.set_state(Draft.building)
        await send_lines(message, [
            f"{name}. Введите обозначение корпуса. Для отмены используйте /cancel.",
            *(f"• {item.label}" for item in profile.buildings),
        ])
        return

    await state.clear()
    await state.set_state(Draft.description)
    await message.answer(
        f"Опишите проблему одним сообщением до {MAX_DESCRIPTION_LENGTH} символов. "
        "Для демонстрации используйте вымышленные данные. Для отмены введите /cancel."
    )


async def choose_building(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    building = data["buildings"].get((message.text or "").strip().casefold())
    if building is None:
        await message.answer("Введите обозначение корпуса из списка.")
        return
    building_id, label, entrances = building
    preview = await message.answer("Выберите категорию заявки.", reply_markup=CATEGORY_BUTTONS)
    await state.update_data(building_id=building_id, building_label=label, entrances=entrances, category_prompt=preview.message_id)
    await state.set_state(Draft.category)


async def choose_category(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    category = (callback.data or "").rsplit(":", 1)[-1]
    message = callback.message
    if (
        not isinstance(message, Message) or message.chat.type != ChatType.PRIVATE
        or message.message_id != data.get("category_prompt")
        or await state.get_state() != Draft.category.state or category not in CATEGORIES
    ):
        await callback.answer("Выбор категории уже закрыт.", show_alert=True)
        return
    limit = f" от 1 до {data['entrances']}" if data["entrances"] is not None else ""
    await message.edit_text(f"Категория: {CATEGORIES[category]}. Введите номер подъезда{limit}.")
    await state.update_data(category=category)
    await state.set_state(Draft.entrance)
    await callback.answer()


async def remind_category(message: Message) -> None:
    await message.answer("Выберите категорию кнопкой или отправьте /cancel.")


async def enter_entrance(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    value = (message.text or "").strip()
    maximum = data["entrances"] or 2_147_483_647
    if not (value.isascii() and value.isdigit() and len(value) <= 10 and 1 <= int(value) <= maximum):
        await message.answer(f"Введите номер подъезда от 1 до {maximum}." if data["entrances"] else "Введите положительный номер подъезда.")
        return
    await state.update_data(entrance=int(value))
    await state.set_state(Draft.description)
    await message.answer(
        f"Опишите проблему одним сообщением до {MAX_DESCRIPTION_LENGTH} символов. "
        "Укажите, что произошло и где. Для общей зоны номер квартиры не нужен."
    )


async def cancel_draft(message: Message, state: FSMContext) -> None:
    current = await state.get_state()
    if current is None:
        await message.answer("Активного черновика нет. Начните с /request.")
        return
    await state.clear()
    await message.answer("Расшифровка отменена." if current == VoiceReview.pending.state else "Черновик отменён.")


async def save_description(message: Message, state: FSMContext) -> None:
    description = (message.text or "").strip()
    if not 1 <= len(description) <= MAX_DESCRIPTION_LENGTH or "\0" in description:
        await message.answer(f"Пришлите текст длиной от 1 до {MAX_DESCRIPTION_LENGTH} символов.")
        return
    data = await state.get_data()
    title = "Проверьте описание"
    if "residence_id" in data:
        title = (
            f"Проверьте заявку для {data['residence_name']}\n"
            f"Корпус {data['building_label']}, подъезд {data['entrance']}\n"
            f"Категория: {CATEGORIES[data['category']]}"
        )
    preview = await message.answer(
        f"{title}\n\n{description}", reply_markup=REVIEW_BUTTONS,
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )
    await state.update_data(preview_id=preview.message_id, description=description)
    await state.set_state(Draft.review)


async def remind_review(message: Message) -> None:
    await message.answer("Выберите кнопку под черновиком или начните заново командой /request.")


async def finish_draft(callback: CallbackQuery, state: FSMContext, database: Engine | None) -> None:
    data = await state.get_data()
    preview = callback.message
    if (
        not isinstance(preview, Message)
        or preview.chat.type != ChatType.PRIVATE
        or preview.message_id != data.get("preview_id")
        or await state.get_state() != Draft.review.state
    ):
        await callback.answer("Черновик уже закрыт или заменён.", show_alert=True)
        return

    result = "Черновик отменён."
    if callback.data == "draft:confirm":
        if database is None:
            result = "Описание подтверждено. В учебном режиме без базы заявке не присваивается номер."
        else:
            try:
                ticket_id = await to_thread(
                    create_ticket, database,
                    submission_key=UUID(data["submission_key"]), telegram_id=callback.from_user.id,
                    residence_id=data["residence_id"], building_id=data["building_id"],
                    category=data["category"], entrance=data["entrance"], description=data["description"],
                )
            except ResidentInputError as error:
                await callback.answer(str(error), show_alert=True)
                return
            except SQLAlchemyError as error:
                LOGGER.warning("Ticket creation: %s", type(error).__name__)
                await callback.answer("Не удалось сохранить заявку. Попробуйте ещё раз.", show_alert=True)
                return
            result = f"Заявка №{ticket_id} зарегистрирована. Статус: Новая.\nПроверить: /status {ticket_id}"
    await preview.edit_text(result)
    await state.clear()
    await callback.answer()


async def ticket_status(message: Message, command: CommandObject, bot: Bot, database: Engine | None) -> None:
    if message.chat.type != ChatType.PRIVATE:
        username = (await bot.me()).username
        await message.answer("Статус своих заявок можно посмотреть в личном чате с ботом.", reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Открыть личный чат", url=f"https://t.me/{username}")]]
        ))
        return
    if database is None:
        await message.answer("Хранилище заявок пока недоступно.")
        return
    number = (command.args or "").strip()
    if number and not (number.isascii() and number.isdigit() and len(number) <= 10 and 1 <= int(number) <= 2_147_483_647):
        await message.answer("Введите /status или /status с номером заявки, например /status 12.")
        return
    try:
        tickets = await to_thread(own_tickets, database, message.chat.id, int(number) if number else None)
    except SQLAlchemyError:
        await message.answer("Не удалось получить статус. Попробуйте позже.")
        return
    if not tickets:
        await message.answer("Заявка с таким номером не найдена." if number else "У вас пока нет заявок.")
        return
    lines = [f"№{ticket.id} · {CATEGORIES[ticket.category]} · {STATUSES[ticket.status]}" for ticket in tickets]
    await message.answer("\n".join(lines))


def group_question(message: Message, identity: User) -> str | None:
    text = message.text or message.caption or ""
    username = f"@{identity.username}".casefold()
    mentions = [
        entity
        for entity in message.entities or message.caption_entities or ()
        if entity.type == MessageEntityType.MENTION and entity.extract_from(text).casefold() == username
    ]
    replied_to = message.reply_to_message
    author = replied_to.from_user if replied_to else None
    if not mentions and (author is None or author.id != identity.id):
        return None

    # Telegram supplies entity ranges in UTF-16 code units, including after emoji.
    encoded = text.encode("utf-16-le")
    for mention in sorted(mentions, key=lambda entity: entity.offset, reverse=True):
        start = mention.offset * 2
        end = (mention.offset + mention.length) * 2
        encoded = encoded[:start] + encoded[end:]
    return encoded.decode("utf-16-le").strip()


async def answer_question(
    message: Message, assistant: GeminiAssistant | None, question: str, *, documents_only: bool = False,
    database: Engine | None = None, resident_id: int | None = None,
) -> None:
    if assistant is None:
        answer = "Выберите команду из /help или подготовьте черновик через /request."
    elif not question:
        answer = (
            "Добавьте текст вопроса. Например: /law Что относится к общему имуществу кондоминиума?"
            if documents_only else "Добавьте текст вопроса о ЖК. Например: /ask Есть ли сантехник?"
        )
    elif len(question) > MAX_QUESTION_LENGTH:
        answer = f"Пришлите вопрос длиной от 1 до {MAX_QUESTION_LENGTH} символов."
    else:
        try:
            if database is None:
                reply = assistant.answer_from_documents if documents_only else assistant.answer
                answer = await reply(question)
            else:
                profile = await chat_profile(message, database, resident_id)
                if documents_only:
                    answer = await assistant.answer_from_documents(question, residence_id=profile.residence.id)
                else:
                    answer = await assistant.answer(question, profile=profile)
        except AssistantUnavailable:
            answer = "Не удалось получить ответ. Попробуйте позже или откройте /help."
        except (SQLAlchemyError, ResidentInputError):
            answer = "Справочник ЖК пока недоступен. Попробуйте позже."
    if message.chat.type == ChatType.PRIVATE:
        await message.answer(answer, link_preview_options=LinkPreviewOptions(is_disabled=True))
    else:
        await message.answer(
            answer,
            reply_parameters=ReplyParameters(message_id=message.message_id, allow_sending_without_reply=True),
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )


async def ask_command(
    message: Message, command: CommandObject, state: FSMContext, assistant: GeminiAssistant | None, database: Engine | None,
) -> None:
    current = await state.get_state()
    if current == VoiceReview.pending.state:
        await remind_voice_review(message)
        return
    if current is not None:
        await message.answer("Завершите черновик или отправьте /cancel, затем задайте вопрос.")
        return
    await answer_question(
        message, assistant, (command.args or "").strip(), documents_only=command.command.lower() == "law",
        database=database,
    )


async def fallback_text(
    message: Message, bot: Bot, state: FSMContext, assistant: GeminiAssistant | None, database: Engine | None,
) -> None:
    if message.chat.type == ChatType.PRIVATE:
        question = (message.text or "").strip()
    else:
        question = group_question(message, await bot.me())
    if question is not None:
        if await state.get_state() == VoiceReview.pending.state:
            await remind_voice_review(message)
            return
        await answer_question(message, assistant, question, database=database)


async def remind_voice_review(message: Message) -> None:
    await message.answer("Проверьте расшифровку с помощью кнопок или отправьте /cancel.")


async def handle_voice(message: Message, bot: Bot, state: FSMContext, assistant: GeminiAssistant | None) -> None:
    voice = message.voice
    if voice is None:
        return
    if message.chat.type != ChatType.PRIVATE and group_question(message, await bot.me()) is None:
        return
    current = await state.get_state()
    if current == VoiceReview.pending.state:
        await remind_voice_review(message)
        return
    if current is not None:
        await message.answer("Завершите черновик текстом или отправьте /cancel, затем запишите вопрос.")
        return
    if assistant is None:
        await message.answer("Распознавание голоса пока недоступно. Отправьте вопрос текстом.")
        return

    size_message = f"Запишите голосовое до {MAX_VOICE_SECONDS} секунд и 2 МБ."
    if not 0 <= voice.duration <= MAX_VOICE_SECONDS or (voice.file_size or 0) > MAX_VOICE_BYTES:
        await message.answer(size_message)
        return
    try:
        audio = await download_voice(bot, voice.file_id)
    except VoiceTooLarge:
        await message.answer(size_message)
        return
    except (TelegramAPIError, ClientError, TimeoutError):
        await message.answer("Не удалось скачать голосовое. Отправьте его ещё раз или задайте вопрос текстом.")
        return
    if not audio.startswith(b"OggS"):
        await message.answer("Запишите голосовое микрофоном в Telegram или отправьте вопрос текстом.")
        return
    try:
        transcript = await assistant.transcribe(audio)
    except AssistantUnavailable:
        await message.answer("Не удалось распознать голосовое. Запишите его ещё раз или отправьте вопрос текстом.")
        return

    preview = await message.answer(
        f"Проверьте расшифровку:\n\n{transcript}",
        reply_markup=VOICE_BUTTONS,
        link_preview_options=LinkPreviewOptions(is_disabled=True),
        reply_parameters=ReplyParameters(message_id=message.message_id, allow_sending_without_reply=True),
    )
    await state.set_data({"preview_id": preview.message_id, "transcript": transcript})
    await state.set_state(VoiceReview.pending)


async def finish_voice(
    callback: CallbackQuery, bot: Bot, state: FSMContext, assistant: GeminiAssistant | None, database: Engine | None,
) -> None:
    preview = callback.message
    data = await state.get_data()
    transcript = data.get("transcript")
    if (
        not isinstance(preview, Message)
        or await state.get_state() != VoiceReview.pending.state
        or preview.message_id != data.get("preview_id")
        or not transcript
    ):
        await callback.answer("Расшифровка закрыта или принадлежит другому участнику.", show_alert=True)
        return

    status = "Расшифровка отменена."
    if callback.data == "voice:edit":
        status = "Отправьте исправленный вопрос текстом."
        if preview.chat.type != ChatType.PRIVATE:
            identity = await bot.me()
            status = f"Отправьте исправленный вопрос через /ask@{identity.username} и текст вопроса."
    elif callback.data == "voice:confirm":
        status = "Расшифровка подтверждена."
    await callback.answer()
    await preview.edit_text(status)
    await state.clear()
    if callback.data == "voice:confirm":
        await answer_question(preview, assistant, transcript, database=database, resident_id=callback.from_user.id)


async def unknown_command(message: Message) -> None:
    await message.answer("Команда не найдена. Список команд доступен в /help.")


async def group_chat_id(message: Message) -> None:
    await message.answer(f"Идентификатор этой группы: {message.chat.id}")


def create_router() -> Router:
    router = Router(name="smart_residence")
    private = F.chat.type == ChatType.PRIVATE
    router.message.filter(
        F.chat.type.in_({ChatType.PRIVATE, ChatType.GROUP, ChatType.SUPERGROUP}),
        F.from_user.is_bot == False,  # MagicFilter builds a filter from this comparison.
    )
    router.message.register(start, CommandStart())
    router.message.register(help_command, Command("help"))
    router.message.register(services, Command("services"))
    router.message.register(residence_command, Command("residence"))
    router.message.register(group_chat_id, ~private, Command("chatid"))
    router.message.register(documents_command, Command("documents"))
    router.message.register(ask_command, ~F.forward_origin, Command("ask", "law"))
    router.message.register(request_draft, Command("request"))
    router.message.register(ticket_status, Command("status"))
    router.message.register(cancel_draft, private, Command("cancel"))
    router.message.register(cancel_draft, StateFilter(VoiceReview.pending), Command("cancel"))
    router.message.register(unknown_command, private, F.text.startswith("/"))
    router.message.register(help_command, private, F.text == HELP_BUTTON)
    router.message.register(services, private, F.text == SERVICES_BUTTON)
    router.message.register(request_draft, private, F.text == REQUEST_BUTTON)
    router.message.register(handle_voice, F.voice, ~F.forward_origin)
    router.message.register(choose_building, private, StateFilter(Draft.building))
    router.message.register(remind_category, private, StateFilter(Draft.category))
    router.message.register(enter_entrance, private, StateFilter(Draft.entrance))
    router.message.register(save_description, private, StateFilter(Draft.description))
    router.message.register(remind_review, private, StateFilter(Draft.review))
    router.message.register(fallback_text, F.text, ~F.text.startswith("/"), ~F.forward_origin)
    router.callback_query.register(finish_draft, F.data.in_({"draft:confirm", "draft:cancel"}))
    router.callback_query.register(choose_category, F.data.startswith("ticket:category:"))
    router.callback_query.register(finish_voice, F.data.in_({"voice:confirm", "voice:edit", "voice:cancel"}))
    return router
