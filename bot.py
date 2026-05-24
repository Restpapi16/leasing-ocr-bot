#!/usr/bin/env python3
"""
Telegram-бот для OCR писем по лизинговым заявкам.
Принимает фото письма → извлекает текст через OpenAI Vision API → отвечает текстом.
"""

import os
import base64
import logging
from io import BytesIO

from telegram import Update, constants
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from openai import AsyncOpenAI

# ─── Конфигурация ────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL   = os.getenv("OPENAI_MODEL", "gpt-4o")  # модель с поддержкой Vision

# ─── Логирование ─────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── Промпт для анализа ──────────────────────────────────────────────────────
SYSTEM_PROMPT = """Ты — ассистент, специализирующийся на обработке деловой корреспонденции,
связанной с лизинговыми заявками.

Твоя задача:
1. Полностью и точно распознать весь текст письма на изображении.
2. Сохранить оригинальную структуру документа: заголовки, абзацы, таблицы, реквизиты.
3. Выделить ключевые реквизиты лизинга, если они присутствуют:
   - Номер и дата заявки
   - Лизингополучатель / лизингодатель
   - Предмет лизинга (марка, модель, VIN / серийный номер)
   - Стоимость и валюта
   - Срок лизинга
   - Аванс, остаточная стоимость, ставка удорожания
4. Если изображение нечёткое или текст частично нечитаем — укажи это явно.
5. Отвечай строго на русском языке.

Сначала выведи раздел «📄 РАСПОЗНАННЫЙ ТЕКСТ:», а затем раздел «📋 КЛЮЧЕВЫЕ РЕКВИЗИТЫ:»."""

USER_PROMPT = "Распознай и передай полный текст письма с изображения."

# ─── Клиент OpenAI ───────────────────────────────────────────────────────────
openai_client: AsyncOpenAI | None = None


def get_openai_client() -> AsyncOpenAI:
    global openai_client
    if openai_client is None:
        if not OPENAI_API_KEY:
            raise RuntimeError("OPENAI_API_KEY не задан.")
        openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)
    return openai_client


# ─── Handlers ────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Приветственное сообщение."""
    await update.message.reply_text(
        "👋 Привет!\n\n"
        "Отправьте мне *фото письма* по лизинговой заявке — я распознаю текст "
        "и выделю ключевые реквизиты.\n\n"
        "📌 *Советы для точного результата:*\n"
        "• Снимайте при хорошем освещении\n"
        "• Держите камеру ровно над документом\n"
        "• Избегайте бликов и теней",
        parse_mode=constants.ParseMode.MARKDOWN,
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "ℹ️ *Как пользоваться ботом:*\n\n"
        "1. Сфотографируйте письмо по лизинговой заявке.\n"
        "2. Отправьте фото в этот чат.\n"
        "3. Получите распознанный текст и ключевые реквизиты.\n\n"
        "🔒 Изображения не сохраняются. Обработка — через OpenAI Vision API.",
        parse_mode=constants.ParseMode.MARKDOWN,
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Основной обработчик фото."""
    message = update.message
    await message.reply_chat_action(constants.ChatAction.TYPING)

    # Берём фото наивысшего разрешения
    photo = message.photo[-1]
    logger.info("Получено фото: file_id=%s, size=%dx%d", photo.file_id, photo.width, photo.height)

    # Уведомляем пользователя
    status_msg = await message.reply_text("⏳ Распознаю текст письма…")

    try:
        # Скачиваем файл
        file = await context.bot.get_file(photo.file_id)
        buf = BytesIO()
        await file.download_to_memory(buf)
        image_b64 = base64.b64encode(buf.getvalue()).decode()

        # Запрос к OpenAI Vision
        client = get_openai_client()
        response = await client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{image_b64}",
                                "detail": "high",
                            },
                        },
                        {"type": "text", "text": USER_PROMPT},
                    ],
                },
            ],
            max_tokens=4096,
            temperature=0.1,
        )

        result_text = response.choices[0].message.content.strip()
        logger.info("Ответ OpenAI получен, длина: %d символов", len(result_text))

        await status_msg.delete()
        await _send_long_message(message, result_text)

    except Exception as exc:
        logger.exception("Ошибка при обработке фото")
        await status_msg.edit_text(
            f"❌ Произошла ошибка при обработке изображения:\n`{exc}`\n\n"
            "Попробуйте ещё раз или проверьте настройки бота.",
            parse_mode=constants.ParseMode.MARKDOWN,
        )


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик документов (фото, отправленное как файл)."""
    doc = update.message.document
    if not doc.mime_type or not doc.mime_type.startswith("image/"):
        await update.message.reply_text(
            "⚠️ Пожалуйста, отправьте изображение (фото или файл-картинку)."
        )
        return

    await update.message.reply_chat_action(constants.ChatAction.TYPING)
    status_msg = await update.message.reply_text("⏳ Распознаю текст письма…")

    try:
        file = await context.bot.get_file(doc.file_id)
        buf = BytesIO()
        await file.download_to_memory(buf)
        image_b64 = base64.b64encode(buf.getvalue()).decode()

        mime = doc.mime_type
        client = get_openai_client()
        response = await client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
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
                        {"type": "text", "text": USER_PROMPT},
                    ],
                },
            ],
            max_tokens=4096,
            temperature=0.1,
        )

        result_text = response.choices[0].message.content.strip()
        await status_msg.delete()
        await _send_long_message(update.message, result_text)

    except Exception as exc:
        logger.exception("Ошибка при обработке документа")
        await status_msg.edit_text(
            f"❌ Ошибка: `{exc}`",
            parse_mode=constants.ParseMode.MARKDOWN,
        )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ответ на любой текст (не фото)."""
    await update.message.reply_text(
        "📸 Отправьте, пожалуйста, *фото письма* по лизинговой заявке.",
        parse_mode=constants.ParseMode.MARKDOWN,
    )


# ─── Утилиты ─────────────────────────────────────────────────────────────────

async def _send_long_message(message, text: str) -> None:
    """Разбивает длинный текст на части <= 4096 символов."""
    LIMIT = 4000
    if len(text) <= LIMIT:
        await message.reply_text(text)
        return

    parts = []
    while text:
        if len(text) <= LIMIT:
            parts.append(text)
            break
        split_at = text.rfind("\n\n", 0, LIMIT)
        if split_at == -1:
            split_at = text.rfind("\n", 0, LIMIT)
        if split_at == -1:
            split_at = LIMIT
        parts.append(text[:split_at])
        text = text[split_at:].lstrip()

    for i, part in enumerate(parts, 1):
        prefix = f"[Часть {i}/{len(parts)}]\n" if len(parts) > 1 else ""
        await message.reply_text(prefix + part)


# ─── Запуск ───────────────────────────────────────────────────────────────────

def main() -> None:
    if not TELEGRAM_TOKEN:
        raise RuntimeError(
            "Переменная окружения TELEGRAM_TOKEN не задана.\n"
            "Создайте бота через @BotFather и задайте токен."
        )
    if not OPENAI_API_KEY:
        raise RuntimeError(
            "Переменная окружения OPENAI_API_KEY не задана."
        )

    logger.info("Запуск бота (модель: %s)…", OPENAI_MODEL)

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.IMAGE, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Бот запущен. Ожидаю сообщений…")
    app.run_polling(allowed_updates=["message"])


if __name__ == "__main__":
    main()
