FROM python:3.12-slim

WORKDIR /app

COPY requirements-bot.txt .
RUN pip install --no-cache-dir -r requirements-bot.txt

COPY bot/ bot/
COPY config.yaml .

RUN mkdir -p data

CMD ["python", "-m", "bot.main"]
