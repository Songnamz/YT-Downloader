#!/bin/sh
# YouTube changes often; grab the newest yt-dlp on every start (never fatal).
if [ "${AUTO_UPDATE:-1}" = "1" ]; then
  pip install --quiet --no-cache-dir --upgrade "yt-dlp[default]" || echo "yt-dlp update failed, using bundled version"
fi
# One worker: jobs live in memory. Threads serve polling and file downloads.
exec gunicorn --workers 1 --threads 16 --timeout 120 --bind 0.0.0.0:8000 app:app
