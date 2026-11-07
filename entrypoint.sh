#!/bin/sh
# YouTube changes often; grab the newest yt-dlp on every start (never fatal).
# Time-boxed so a slow or unreachable PyPI can't hold up startup for long.
if [ "${AUTO_UPDATE:-1}" = "1" ]; then
  timeout "${AUTO_UPDATE_TIMEOUT:-90}" \
    pip install --quiet --no-cache-dir --timeout 15 --upgrade "yt-dlp[default]" \
    || echo "yt-dlp update failed or timed out, using bundled version"
fi
echo "yt-dlp $(yt-dlp --version)"
# One worker: jobs live in memory. Threads serve polling and file downloads.
exec gunicorn --workers 1 --threads 16 --timeout 120 --bind 0.0.0.0:8000 app:app
