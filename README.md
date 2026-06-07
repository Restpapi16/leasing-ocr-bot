# 📄 Лизинг-OCR Telegram Бот

Telegram-бот для распознавания текста писем по лизинговым заявкам.
Принимает фото документа → извлекает полный текст через **OpenAI Vision API** → возвращает текст и ключевые реквизиты прямо в чат.

---

## 🚀 Быстрый старт

### 1. Клонировать репозиторий
```bash
git clone https://github.com/Restpapi16/leasing-ocr-bot.git
cd leasing-ocr-bot
```

### 2. Установить зависимости
```bash
y
```

### 3. Настроить переменные окружения
```bash
cp .env.example .env
# Откройте .env и заполните TELEGRAM_TOKEN и OPENAI_API_KEY
```

Или задайте напрямую:
```bash
export TELEGRAM_TOKEN="токен_от_BotFather"
export OPENAI_API_KEY="sk-ваш_ключ"
export OPENAI_MODEL="gpt-4o"   # необязательно
```

### 4. Запустить
```bash
python bot.py
```

---

## 🔧 Получение токенов

### Telegram Bot Token
1. Откройте [@BotFather](https://t.me/BotFather) в Telegram
2. Отправьте `/newbot`, следуйте инструкциям
3. Скопируйте токен вида `123456:ABCdef...`

### OpenAI API Key
1. Зайдите на [platform.openai.com](https://platform.openai.com)
2. Перейдите в **API Keys** → **Create new secret key**
3. Скопируйте ключ (начинается с `sk-`)

---

## 📋 Команды бота

| Команда | Описание |
|---------|----------|
| `/start` | Приветствие и инструкция |
| `/help` | Подробная справка |
| *(фото)* | Распознавание письма |

---

## 🏗 Структура ответа

```
📄 РАСПОЗНАННЫЙ ТЕКСТ:
[полный текст письма сохраняя структуру]

📋 КЛЮЧЕВЫЕ РЕКВИЗИТЫ:
• Номер заявки: …
• Дата: …
• Лизингополучатель: …
• Лизингодатель: …
• Предмет лизинга: …
• Стоимость: …
• Срок: …
• Аванс: …
```

---

## ⚙️ Переменные окружения

| Переменная | Обязательная | По умолчанию | Описание |
|-----------|:---:|---|---|
| `TELEGRAM_TOKEN` | ✅ | — | Токен от @BotFather |
| `OPENAI_API_KEY` | ✅ | — | Ключ API OpenAI |
| `OPENAI_MODEL` | ❌ | `gpt-4o` | Модель OpenAI (с поддержкой Vision) |

---

## 🐳 Docker

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py .
CMD ["python", "bot.py"]
```

```bash
docker build -t leasing-ocr-bot .
docker run -d \
  -e TELEGRAM_TOKEN="your_token" \
  -e OPENAI_API_KEY="sk-your_key" \
  leasing-ocr-bot
```

---

## 💡 Советы по качеству распознавания

- Используйте модель `gpt-4o` — лучшее качество Vision
- Снимайте при хорошем освещении, без бликов
- Параметр `temperature=0.1` минимизирует «фантазии» модели
- Бот принимает как сжатые фото, так и изображения, отправленные как файл
