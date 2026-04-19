FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir .

RUN mkdir -p /data /logs

ENV SECRETARY_DATA_DIR=/data
ENV SECRETARY_LOG_DIR=/logs
ENV SECRETARY_DB_PATH=/data/secretary.sqlite3

CMD ["python", "-m", "tele_secretary", "bot"]
