#!/bin/bash
# Подвійний клік у Finder → запускає Cast4K і відкриває його в браузері.
set -e
cd "$(dirname "$0")"

if ! command -v ffmpeg >/dev/null 2>&1; then
  if command -v brew >/dev/null 2>&1; then
    echo "Встановлюю ffmpeg (один раз)…"
    brew install ffmpeg
  else
    echo "⚠️  Потрібен ffmpeg для MKV. Встанови Homebrew (https://brew.sh), потім: brew install ffmpeg"
  fi
fi

if [ ! -x .venv/bin/python ]; then
  echo "Перший запуск: готую оточення…"
  python3 -m venv .venv
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -r requirements.txt
fi

exec .venv/bin/python -m cast4k "$@"
