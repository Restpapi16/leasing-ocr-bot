FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

# Переменные окружения задаются при запуске контейнера
ENV TELEGRAM_TOKEN=""
ENV OPENAI_API_KEY=""
ENV OPENAI_MODEL="gpt-4o"

CMD ["python", "bot.py"]
