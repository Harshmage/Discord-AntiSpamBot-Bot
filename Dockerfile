FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot ./bot
COPY scripts ./scripts

RUN useradd --uid 1000 --create-home bot && mkdir /data && chown bot /data
USER bot

CMD ["python", "-m", "bot"]
