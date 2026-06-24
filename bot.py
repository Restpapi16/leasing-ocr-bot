import os
import base64
import logging
import httpx
from io import BytesIO
from typing import Optional
from dotenv import load_dotenv
load_dotenv()

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, constants
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    filters,
    ContextTypes,
)
from openai import AsyncOpenAI

# ─── Конфигурация ────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
OPENAI_API_KEY   = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL     = os.getenv("OPENAI_MODEL", "gpt-4o")

AMO_SUBDOMAIN    = os.getenv("AMO_SUBDOMAIN", "")
AMO_ACCESS_TOKEN = os.getenv("AMO_ACCESS_TOKEN", "")

# ─── ID полей AmoCRM ─────────────────────────────────────────────────────────
COMPANY_FIELD_INN   = 711641
COMPANY_FIELD_AGENT = 711655

DEAL_PIPELINE_STATUS_ID = 70009922
DEAL_TAG = "Лизинг_OCR"

# ─── Состояния диалога ───────────────────────────────────────────────────────
(
    WAIT_PHOTO,
    WAIT_OCR_CONFIRM,
    WAIT_EDIT_COMPANY,
    WAIT_EDIT_INN,
    WAIT_AGENT_CHOICE,
    WAIT_PHONE,
    WAIT_NAME,
    WAIT_CONFIRM,
) = range(8)

# ─── Сценарии ─────────────────────────────────────────────────────────────────
SCENARIOS = {
    "5800": {
        "description": "РБ Лизинг — заявка от сети продаж",
        "system_prompt": (
            "Ты — ассистент по обработке лизинговых заявок.\n"
            "С фото нужно извлечь СТРОГО следующие поля в формате JSON:\n"
            "{\n"
            '  "company_name": "Наименование клиента",\n'
            '  "inn": "ИНН клиента",\n'
            '  "activity": "Основной вид деятельности",\n'
            '  "revenue_segment": "Выручка в млн руб. / Сегмент",\n'
            '  "leasing_type": "Вид лизинга",\n'
            '  "leasing_subject": "Предмет лизинга",\n'
            '  "cost": "Стоимость",\n'
            '  "term_months": "Срок лизинга в мес",\n'
            '  "advance_pct": "Аванс лизингополучателя в %",\n'
            '  "payment_type": "Тип платежей",\n'
            '  "full_text": "ВЕСЬ текст с фото дословно"\n'
            "}\n\n"
            "Если поле не найдено — ставь null.\n"
            "Отвечай ТОЛЬКО валидным JSON, без markdown-блоков."
        ),
    },
}

# ─── Логирование ─────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── OpenAI ──────────────────────────────────────────────────────────────────
openai_client: Optional[AsyncOpenAI] = None

def get_openai_client() -> AsyncOpenAI:
    global openai_client
    if openai_client is None:
        if not OPENAI_API_KEY:
            raise RuntimeError("OPENAI_API_KEY не задан.")
        openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)
    return openai_client

# ─── AmoCRM helpers ───────────────────────────────────────────────────────────

def _amo_headers() -> dict:
    return {
        "Authorization": f"Bearer {AMO_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }


async def _create_company(
    company_name: str,
    inn: Optional[str],
    phone: Optional[str] = None,
    agent_name: Optional[str] = None,
) -> int:
    """Создаёт новую компанию, заполняя все поля за один запрос."""
    base_url = f"https://{AMO_SUBDOMAIN}.amocrm.ru/api/v4"

    custom_fields: list = []

    if inn and COMPANY_FIELD_INN:
        custom_fields.append(
            {"field_id": COMPANY_FIELD_INN, "values": [{"value": inn}]}
        )

    if agent_name and COMPANY_FIELD_AGENT:
        custom_fields.append(
            {"field_id": COMPANY_FIELD_AGENT, "values": [{"value": agent_name}]}
        )

    # Телефон — стандартное поле AmoCRM, заполняется через field_code="PHONE"
    if phone:
        custom_fields.append(
            {
                "field_code": "PHONE",
                "values": [{"value": phone, "enum_code": "WORK"}],
            }
        )

    payload: dict = {"name": company_name}
    if custom_fields:
        payload["custom_fields_values"] = custom_fields

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{base_url}/companies",
            headers=_amo_headers(),
            json=[payload],
        )
        resp.raise_for_status()
        company_id = resp.json()["_embedded"]["companies"][0]["id"]
        logger.info("Компания создана: id=%s", company_id)
        return company_id


async def _create_deal(
    deal_name: str,
    company_id: int,
    full_text: str,
) -> int:
    base_url = f"https://{AMO_SUBDOMAIN}.amocrm.ru/api/v4"
    deal_payload: dict = {
        "name": deal_name,
        "status_id": DEAL_PIPELINE_STATUS_ID,
        "_embedded": {
            "companies": [{"id": company_id}],
            "tags": [{"name": DEAL_TAG}],
        },
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{base_url}/leads",
            headers=_amo_headers(),
            json=[deal_payload],
        )
        resp.raise_for_status()
        deal_id = resp.json()["_embedded"]["leads"][0]["id"]
        logger.info("Сделка создана (id=%s)", deal_id)

        await client.post(
            f"{base_url}/leads/notes",
            headers=_amo_headers(),
            json=[{
                "entity_id": deal_id,
                "note_type": "common",
                "params": {"text": f"📄 OCR-текст с фото:\n\n{full_text}"},
            }],
        )

    return deal_id

# ─── OCR ─────────────────────────────────────────────────────────────────────

async def _ocr_photo(image_b64: str, mime: str, system_prompt: str) -> dict:
    import json
    client = get_openai_client()
    response = await client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {
                    "url": f"data:{mime};base64,{image_b64}",
                    "detail": "high",
                }},
                {"type": "text", "text": "Распознай поля по инструкции."},
            ]},
        ],
        max_tokens=4096,
        temperature=0.1,
    )
    raw = response.choices[0].message.content.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw)

# ─── Вспомогательные сообщения ────────────────────────────────────────────────

async def _show_ocr_confirm(message, company_name: str, inn: str) -> None:
    """Показывает результат OCR и просит пользователя подтвердить или исправить."""
    await message.reply_text(
        f"🔍 <b>Распознано:</b>\n"
        f"• <b>Компания:</b> {company_name}\n"
        f"• <b>ИНН:</b> {inn}\n\n"
        "Всё верно?",
        parse_mode=constants.ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Верно", callback_data="ocr_confirm_ok"),
        ], [
            InlineKeyboardButton("✏️ Изменить компанию", callback_data="ocr_edit_company"),
            InlineKeyboardButton("✏️ Изменить ИНН",     callback_data="ocr_edit_inn"),
        ]]),
    )

async def _ask_phone(message) -> None:
    await message.reply_text(
        "📞 Введите <b>номер телефона</b> агента:\n"
        "<i>Например: +79001234567</i>\n\n"
        "Или нажмите /cancel для отмены.",
        parse_mode=constants.ParseMode.HTML,
    )

async def _ask_name(message) -> None:
    await message.reply_text(
        "👤 Введите <b>ФИО</b> агента:\n"
        "<i>Например: Иванов Иван Иванович</i>\n\n"
        "Или нажмите /cancel для отмены.",
        parse_mode=constants.ParseMode.HTML,
    )

async def _ask_confirm(message, phone: str, name: str) -> None:
    await message.reply_text(
        f"🔎 Проверьте введённые данные:\n\n"
        f"📞 <b>Телефон:</b> {phone}\n"
        f"👤 <b>ФИО:</b> {name}\n\n"
        "Всё верно?",
        parse_mode=constants.ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Да, отправить", callback_data="confirm_yes"),
            InlineKeyboardButton("✏️ Исправить",    callback_data="confirm_edit"),
        ], [
            InlineKeyboardButton("🔄 Начать заново", callback_data="confirm_reset"),
        ]]),
    )

# ─── Handlers ─────────────────────────────────────────────────────────────────

async def handle_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("🔑 Пришлите ваш уникальный код для начала работы.")

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("🔑 Пришлите ваш уникальный код для начала работы.")

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("🚫 Операция отменена.")
    return ConversationHandler.END

async def handle_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    scenario = SCENARIOS.get(text)
    if not scenario:
        await update.message.reply_text("🔑 Пришлите ваш уникальный код для начала работы.")
        return ConversationHandler.END
    context.user_data["scenario_code"] = text
    context.user_data["scenario"] = scenario
    await update.message.reply_text(
        f"✅ Сценарий <b>{text}</b>: {scenario['description']}\n\n"
        "📸 Отправьте фото документа.",
        parse_mode=constants.ParseMode.HTML,
    )
    return WAIT_PHOTO

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.message
    if message.photo:
        file_obj = await context.bot.get_file(message.photo[-1].file_id)
        mime = "image/jpeg"
    elif message.document and message.document.mime_type.startswith("image/"):
        file_obj = await context.bot.get_file(message.document.file_id)
        mime = message.document.mime_type
    else:
        await message.reply_text("⚠️ Пожалуйста, отправьте фото.")
        return WAIT_PHOTO

    status_msg = await message.reply_text("⏳ Распознаю текст…")
    buf = BytesIO()
    await file_obj.download_to_memory(buf)
    image_b64 = base64.b64encode(buf.getvalue()).decode()

    try:
        ocr_data = await _ocr_photo(image_b64, mime, context.user_data["scenario"]["system_prompt"])
    except Exception as exc:
        logger.exception("Ошибка OCR")
        await status_msg.edit_text(f"❌ Ошибка распознавания: {exc}")
        return ConversationHandler.END

    context.user_data["ocr_data"] = ocr_data
    await status_msg.delete()

    company_name = ocr_data.get("company_name") or "—"
    inn = ocr_data.get("inn") or "—"

    # Просим пользователя подтвердить или исправить распознанные данные
    await _show_ocr_confirm(message, company_name, inn)
    return WAIT_OCR_CONFIRM


# ─── Подтверждение / правка OCR-данных ───────────────────────────────────────

async def handle_ocr_confirm_ok(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Пользователь подтвердил — переходим к вопросу про агента."""
    await update.callback_query.answer()
    await update.callback_query.edit_message_reply_markup(reply_markup=None)

    ocr_data = context.user_data.get("ocr_data", {})
    company_name = ocr_data.get("company_name") or "—"
    inn = ocr_data.get("inn") or "—"

    await update.callback_query.message.reply_text(
        f"✅ Данные подтверждены:\n"
        f"• <b>Компания:</b> {company_name}\n"
        f"• <b>ИНН:</b> {inn}\n\n"
        "❓ <b>Имеется ли номер телефона и ФИО агента?</b>",
        parse_mode=constants.ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("Да", callback_data="agent_yes"),
            InlineKeyboardButton("Нет", callback_data="agent_no"),
        ]]),
    )
    return WAIT_AGENT_CHOICE


async def handle_ocr_edit_company(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Пользователь хочет исправить название компании."""
    await update.callback_query.answer()
    await update.callback_query.edit_message_reply_markup(reply_markup=None)
    current = context.user_data.get("ocr_data", {}).get("company_name") or "—"
    await update.callback_query.message.reply_text(
        f"✏️ Текущее наименование: <b>{current}</b>\n\n"
        "Введите правильное <b>наименование компании</b>:\n"
        "Или нажмите /cancel для отмены.",
        parse_mode=constants.ParseMode.HTML,
    )
    return WAIT_EDIT_COMPANY


async def handle_ocr_edit_inn(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Пользователь хочет исправить ИНН."""
    await update.callback_query.answer()
    await update.callback_query.edit_message_reply_markup(reply_markup=None)
    current = context.user_data.get("ocr_data", {}).get("inn") or "—"
    await update.callback_query.message.reply_text(
        f"✏️ Текущий ИНН: <b>{current}</b>\n\n"
        "Введите правильный <b>ИНН</b>:\n"
        "Или нажмите /cancel для отмены.",
        parse_mode=constants.ParseMode.HTML,
    )
    return WAIT_EDIT_INN


async def handle_edit_company_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Получили новое название компании — сохраняем и снова показываем данные для проверки."""
    new_company = update.message.text.strip()
    context.user_data.setdefault("ocr_data", {})["company_name"] = new_company
    inn = context.user_data["ocr_data"].get("inn") or "—"
    await update.message.reply_text(
        f"✅ Наименование обновлено: <b>{new_company}</b>",
        parse_mode=constants.ParseMode.HTML,
    )
    await _show_ocr_confirm(update.message, new_company, inn)
    return WAIT_OCR_CONFIRM


async def handle_edit_inn_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Получили новый ИНН — сохраняем и снова показываем данные для проверки."""
    new_inn = update.message.text.strip()
    context.user_data.setdefault("ocr_data", {})["inn"] = new_inn
    company_name = context.user_data["ocr_data"].get("company_name") or "—"
    await update.message.reply_text(
        f"✅ ИНН обновлён: <b>{new_inn}</b>",
        parse_mode=constants.ParseMode.HTML,
    )
    await _show_ocr_confirm(update.message, company_name, new_inn)
    return WAIT_OCR_CONFIRM


# ─── Агент ────────────────────────────────────────────────────────────────────

async def handle_agent_no(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    await update.callback_query.edit_message_reply_markup(reply_markup=None)
    await update.callback_query.message.reply_text("👌 Агент не указан. Создаю запись в AmoCRM…")
    return await _push_to_amo(update.callback_query.message, context, phone=None, name=None)

async def handle_agent_yes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    await update.callback_query.edit_message_reply_markup(reply_markup=None)
    await _ask_phone(update.callback_query.message)
    return WAIT_PHONE

async def handle_phone_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["agent_phone"] = update.message.text.strip()
    await _ask_name(update.message)
    return WAIT_NAME

async def handle_name_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["agent_name"] = update.message.text.strip()
    await _ask_confirm(
        update.message,
        phone=context.user_data["agent_phone"],
        name=context.user_data["agent_name"],
    )
    return WAIT_CONFIRM

async def handle_confirm_yes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    await update.callback_query.edit_message_reply_markup(reply_markup=None)
    await update.callback_query.message.reply_text("👌 Данные подтверждены. Создаю запись в AmoCRM…")
    return await _push_to_amo(
        update.callback_query.message,
        context,
        phone=context.user_data.get("agent_phone"),
        name=context.user_data.get("agent_name"),
    )

async def handle_confirm_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    await update.callback_query.edit_message_reply_markup(reply_markup=None)
    context.user_data.pop("agent_phone", None)
    context.user_data.pop("agent_name", None)
    await _ask_phone(update.callback_query.message)
    return WAIT_PHONE

async def handle_confirm_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    await update.callback_query.edit_message_reply_markup(reply_markup=None)
    scenario = context.user_data.get("scenario")
    scenario_code = context.user_data.get("scenario_code")
    context.user_data.clear()
    context.user_data["scenario"] = scenario
    context.user_data["scenario_code"] = scenario_code
    await update.callback_query.message.reply_text(
        "🔄 Данные сброшены.\n\n📸 Отправьте фото документа заново."
    )
    return WAIT_PHOTO

# ─── Отправка в АМО ───────────────────────────────────────────────────────────

async def _push_to_amo(
    message,
    context: ContextTypes.DEFAULT_TYPE,
    phone: Optional[str],
    name: Optional[str],
) -> int:
    ocr_data = context.user_data.get("ocr_data", {})
    company_name = ocr_data.get("company_name") or "Без названия"
    inn = ocr_data.get("inn")
    full_text = ocr_data.get("full_text") or str(ocr_data)

    try:
        # Создаём компанию сразу со всеми полями за один запрос
        company_id = await _create_company(
            company_name=company_name,
            inn=inn,
            phone=phone,
            agent_name=name,
        )
        await _create_deal(
            deal_name=company_name,
            company_id=company_id,
            full_text=full_text,
        )
        await message.reply_text(
            f"✅ <b>Готово!</b>\n\n"
            f"🏢 Компания: <b>{company_name}</b>\n"
            f"📋 Сделка создана.",
            parse_mode=constants.ParseMode.HTML,
        )
    except Exception as exc:
        logger.exception("Ошибка при отправке в AmoCRM")
        await message.reply_text(f"❌ Ошибка AmoCRM: {exc}")

    context.user_data.clear()
    return ConversationHandler.END

# ─── Запуск ───────────────────────────────────────────────────────────────────

def main() -> None:
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN не задан.")
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY не задан.")
    if not AMO_SUBDOMAIN or not AMO_ACCESS_TOKEN:
        raise RuntimeError("AMO_SUBDOMAIN / AMO_ACCESS_TOKEN не заданы.")

    logger.info("Запуск бота (модель: %s)…", OPENAI_MODEL)
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[
            MessageHandler(
                filters.TEXT & ~filters.COMMAND & filters.Regex(r"^\d{4,6}$"),
                handle_code,
            )
        ],
        states={
            WAIT_PHOTO: [
                MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_photo)
            ],
            WAIT_OCR_CONFIRM: [
                CallbackQueryHandler(handle_ocr_confirm_ok,     pattern="^ocr_confirm_ok$"),
                CallbackQueryHandler(handle_ocr_edit_company,   pattern="^ocr_edit_company$"),
                CallbackQueryHandler(handle_ocr_edit_inn,       pattern="^ocr_edit_inn$"),
            ],
            WAIT_EDIT_COMPANY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_edit_company_input)
            ],
            WAIT_EDIT_INN: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_edit_inn_input)
            ],
            WAIT_AGENT_CHOICE: [
                CallbackQueryHandler(handle_agent_yes, pattern="^agent_yes$"),
                CallbackQueryHandler(handle_agent_no,  pattern="^agent_no$"),
            ],
            WAIT_PHONE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_phone_input)
            ],
            WAIT_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_name_input)
            ],
            WAIT_CONFIRM: [
                CallbackQueryHandler(handle_confirm_yes,   pattern="^confirm_yes$"),
                CallbackQueryHandler(handle_confirm_edit,  pattern="^confirm_edit$"),
                CallbackQueryHandler(handle_confirm_reset, pattern="^confirm_reset$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        conversation_timeout=600,
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(conv)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_unknown))

    logger.info("Бот запущен.")
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
