FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# yt-dlp с конца 2025 года требует JS-рантайм (Deno) для нормальной работы с YouTube
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY music.py .

ENV PYTHONUNBUFFERED=1 \
    DB_FILE=/app/data/users.db
VOLUME ["/app/data"]

CMD ["python", "music.py"]
