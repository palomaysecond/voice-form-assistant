#!/bin/bash
# App Service (Linux) startup command. The Python image has no ffmpeg, which
# the app needs to convert browser WebM/Opus and M4A uploads to 16 kHz WAV.
if ! command -v ffmpeg >/dev/null 2>&1; then
  apt-get update && apt-get install -y --no-install-recommends ffmpeg
fi
# One worker (F1 has 1 GB RAM), a few threads so /health answers during a long request.
gunicorn --bind=0.0.0.0:${PORT:-8000} --timeout 600 --workers 1 --threads 4 app:app
