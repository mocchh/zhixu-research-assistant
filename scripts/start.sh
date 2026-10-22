#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -x .venv/bin/python ]; then
  if command -v python3.12 >/dev/null 2>&1; then
    python3.12 -m venv .venv
  elif command -v uv >/dev/null 2>&1; then
    uv venv --python 3.12 .venv
  else
    printf '请先安装 Python 3.12 或 uv，再运行此脚本。\n' >&2
    exit 1
  fi
fi
.venv/bin/python scripts/install_backend.py

.venv/bin/python scripts/install_frontend.py
(cd frontend && npm run build)
printf '\n知序已准备好。打开 http://127.0.0.1:8000\n按 Ctrl+C 停止；文献与进度保存在 data/。\n\n'
exec .venv/bin/python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000
