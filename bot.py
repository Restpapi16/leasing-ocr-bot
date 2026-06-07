#!/usr/bin/env python3
"""
Telegram-бот для OCR писем по лизинговым заявкам + интеграция с AmoCRM.
Сценарии запускаются по коду (например, 5800).
"""

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

# AmoCRM
AMO_SUBDOMAIN    = os.getenv("AMO_SUBDOMAIN", "")          # yourcompany (без .amocrm.ru)
AMO_ACCESS_TOKEN = os.getenv("AMO_ACCESS_TOKEN", "")       # Long-lived OAuth2 access token

# ─── ID кастомных полей AmoCRM (заполни сам!) ──────────────────────────────────
# Поля КОМПАНИИ (custom fields)
COMPANY_FIELD_INN   = 711641   # TODO: вставь ID кастомного поля ИНН компании
COMPANY_FIELD_AGENT = 711655   # TODO: вставь ID кастомного поля ФИО агента компании
# Телефон — СТАНДАРТНОЕ поле AmoCRM, записывается через ключ "phone" в теле запроса

# ─── Состояния ConversationHandler ───────────────────────────────────────────
WAIT_PHOTO = 1
WAIT_AGENT = 2

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
    # Пример добавления нового сценария:
    # "1234": {
    #     "description": "Другой сценарий",
    #     "system_prompt": "...",
    # },
}

# ─── Логирование ─────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── OpenAI клиент ───────────────────────────────────────────────────────────
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


async def _find_or_create_company(company_name: str, inn: Optional[str]) -> int:
    """
    Ищет компанию по названию. Если не найдена — создаёт.
    Возвращает ID компании в AmoCRM.
    Контакт НЕ создаётся (по ТЗ).
    """
    base_url = f"https://{AMO_SUBDOMAIN}.amocrm.ru/api/v4"

    async with httpx.AsyncClient() as client:
        # Поиск по названию
        resp = await client.get(
            f"{base_url}/companies",
            headers=_amo_headers(),
            params={"query": company_name, "limit": 5},
        )
        if resp.status_code == 200:
            items = resp.json().get("_embedded", {}).get("companies", [])
            if items:
                company_id = items[0]["id"]
                logger.info("Компания найдена: id=%s", company_id)
                # Обновляем кастомное поле ИНН если есть
                if inn and COMPANY_FIELD_INN:
                    await client.patch(
                        f"{base_url}/companies/{company_id}",
                        headers=_amo_headers(),
                        json={
                            "custom_fields_values": [
                                {"field_id": COMPANY_FIELD_INN, "values": [{"value": inn}]}
                            ]
                        },
                    )
                return company_id

        # Компания не найдена — создаём
        payload: dict = {"name": company_name}
        if inn and COMPANY_FIELD_INN:
            payload["custom_fields_values"] = [
                {"field_id": COMPANY_FIELD_INN, "values": [{"value": inn}]}
            ]

        resp = await client.post(
            f"{base_url}/companies",
            headers=_amo_headers(),
            json=[payload],
        )
        resp.raise_for_status()
        company_id = resp.json()["_embedded"]["companies"][0]["id"]
        logger.info("Компания создана: id=%s", company_id)
        return company_id


async def _update_company_phone(company_id: int, phone: str) -> None:
    """
    Записывает телефон в СТАНДАРТНОЕ поле компании.
    В AmoCRM это ключ "phone" верхнего уровня, а НЕ custom_fields_values.
    """
    base_url = f"https://{AMO_SUBDOMAIN}.amocrm.ru/api/v4"
    payload = {
        "phone": [
            {"value": phone, "enum_code": "WORK"}
        ]
    }
    async with httpx.AsyncClient() as client:
        resp = await client.patch(
            f"{base_url}/companies/{company_id}",
            headers=_amo_headers(),
            json=payload,
        )
        resp.raise_for_status()
        logger.info("Телефон компании обновлён: %s", phone)


async def _create_deal(
    deal_name: str,
    company_id: int,
    full_text: str,
    agent_phone: Optional[str],
    agent_name: Optional[str],
    inn: Optional[str],
) -> int:
    """
    Создаёт сделку в AmoCRM и привязывает компанию.
    Весь текст OCR идёт в примечание сделки.
    Возвращает ID созданной сделки.
    """
    base_url = f"https://{AMO_SUBDOMAIN}.amocrm.ru/api/v4"

    deal_payload: dict = {
        "name": deal_name,
        "_embedded": {
            "companies": [{"id": company_id}],
        },
    }

    async with httpx.AsyncClient() as client:
        # Создаём сделку
        resp = await client.post(
            f"{base_url}/leads",
            headers=_amo_headers(),
            json=[deal_payload],
        )
        resp.raise_for_status()
        deal_id = resp.json()["_embedded"]["leads"][0]["id"]
        logger.info("Сделка создана: id=%s", deal_id)

        # Добавляем примечание со всем текстом OCR
        note_payload = {
            "entity_id": deal_id,
            "note_type": "common",
            "params": {"text": f"📄 OCR-текст с фото:\n\n{full_text}"},
        }
        await client.post(
            f"{base_url}/leads/notes",
            headers=_amo_headers(),
            json=[note_payload],
        )

        # ФИО агента — кастомное поле компании
        if agent_name and COMPANY_FIELD_AGENT:
            await client.patch(
                f"{base_url}/companies/{company_id}",
                headers=_amo_headers(),
                json={
                    "custom_fields_values": [
                        {"field_id": COMPANY_FIELD_AGENT, "values": [{"value": agent_name}]}
                    ]
                },
            )

    # Телефон — стандартное поле, выносим в отдельный метод
    if agent_phone:
        await _update_company_phone(company_id, agent_phone)

    return deal_id


# ─── OCR через OpenAI Vision ─────────────────────────────────────────────────

async def _ocr_photo(image_b64: str, mime: str, system_prompt: str) -> dict:
    """
    Отправляет фото в OpenAI Vision.
    Возвращает dict с распознанными полями (JSON от GPT).
    """
    import json
    client = get_openai_client()
    response = await client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{mime};base64,{image_b64}",
                            "detail": "high",
                        },
                    },
                    {"type": "text", "text": "Распознай поля по инструкции."},
                ],
            },
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


# ─── ConversationHandler шаги ─────────────────────────────────────────────────

async def handle_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    scenario = SCENARIOS.get(text)
    if not scenario:
        await update.message.reply_text(
            f"❌ Неизвестный код сценария: <b>{text}</b>\n\n"
            "Введите корректный код (например, <b>5800</b>).",
            parse_mode=constants.ParseMode.HTML,
        )
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
        photo = message.photo[-1]
        file_obj = await context.bot.get_file(photo.file_id)
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

    scenario = context.user_data["scenario"]
    try:
        ocr_data = await _ocr_photo(image_b64, mime, scenario["system_prompt"])
    except Exception as exc:
        logger.exception("Ошибка OCR")
        await status_msg.edit_text(f"❌ Ошибка распознавания: {exc}")
        return ConversationHandler.END

    context.user_data["ocr_data"] = ocr_data
    await status_msg.delete()

    company_name = ocr_data.get("company_name") or "—"
    inn = ocr_data.get("inn") or "—"

    await message.reply_text(
        f"🔍 Распознано:\n"
        f"• <b>Компания:</b> {company_name}\n"
        f"• <b>ИНН:</b> {inn}\n\n"
        "❓ <b>Имеется ли номер телефона и ФИО агента?</b>\n\n"
        "Нажмите <b>Нет</b> или напишите данные в формате:\n"
        "<code>+79001234567 Иванов Иван Иванович</code>",
        parse_mode=constants.ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Нет", callback_data="agent_no")]
        ]),
    )
    return WAIT_AGENT


async def handle_agent_no(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    await update.callback_query.edit_message_reply_markup(reply_markup=None)
    await update.callback_query.message.reply_text("👌 Агент не указан. Создаю запись в AmoCRM…")
    return await _push_to_amo(update.callback_query.message, context, phone=None, name=None)


async def handle_agent_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    parts = text.split(None, 1)
    phone = parts[0] if parts else None
    name = parts[1] if len(parts) > 1 else None
    await update.message.reply_text("👌 Данные получены. Создаю запись в AmoCRM…")
    return await _push_to_amo(update.message, context, phone=phone, name=name)


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
        company_id = await _find_or_create_company(company_name, inn)
        deal_id = await _create_deal(
            deal_name=company_name,
            company_id=company_id,
            full_text=full_text,
            agent_phone=phone,
            agent_name=name,
            inn=inn,
        )
        await message.reply_text(
            f"✅ <b>Готово!</b>\n\n"
            f"🏢 Компания: <b>{company_name}</b>\n"
            f"📋 Сделка ID: <b>{deal_id}</b>\n"
            f"🔗 <a href=\"https://{AMO_SUBDOMAIN}.amocrm.ru/leads/detail/{deal_id}\">Открыть в AmoCRM</a>",
            parse_mode=constants.ParseMode.HTML,
        )
    except Exception as exc:
        logger.exception("Ошибка при отправке в AmoCRM")
        await message.reply_text(f"❌ Ошибка AmoCRM: {exc}")

    context.user_data.clear()
    return ConversationHandler.END


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("🚫 Операция отменена.")
    return ConversationHandler.END


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    codes = ", ".join(f"<b>{k}</b>" for k in SCENARIOS)
    await update.message.reply_text(
        "👋 Привет!\n\n"
        f"Введите код сценария ({codes}) для начала работы.\n"
        "Для отмены — /cancel",
        parse_mode=constants.ParseMode.HTML,
    )


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
            WAIT_AGENT: [
                CallbackQueryHandler(handle_agent_no, pattern="^agent_no$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_agent_text),
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        conversation_timeout=600,
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(conv)

    logger.info("Бот запущен.")
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
