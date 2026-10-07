FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

# Deno: the JavaScript runtime yt-dlp needs to solve YouTube's challenges
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

RUN useradd --create-home app \
 && python -m venv /opt/venv \
 && chown -R app /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    JOBS_DIR=/tmp/jobs \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py entrypoint.sh ./
COPY static static

USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')"
ENTRYPOINT ["sh", "/app/entrypoint.sh"]
