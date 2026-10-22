#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [ ! -x .venv/bin/python ]; then
  printf '请先运行 bash scripts/start.sh 安装依赖并构建。\n'
  exit 1
fi
.venv/bin/python scripts/install_backend.py
.venv/bin/python scripts/install_frontend.py
.venv/bin/python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000 --reload &
api_pid=$!
trap 'kill "$api_pid" 2>/dev/null || true' EXIT INT TERM
cd frontend
npm run dev
