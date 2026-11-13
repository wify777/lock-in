#!/bin/bash
# Двойной щелчок в Finder запускает Lock In и открывает его в браузере.
cd "$(dirname "$0")" || exit 1
if ! command -v python3 >/dev/null 2>&1; then
  echo "Нужен Python 3. Установи его: https://www.python.org/downloads/"
  read -r -p "Нажми Enter, чтобы закрыть..." _
  exit 1
fi
exec python3 lockin_server.py --open
