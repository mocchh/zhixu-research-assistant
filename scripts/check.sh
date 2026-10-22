#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
all=0
unit_only=0
eval_args=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    --all) all=1; shift ;;
    --unit-only) unit_only=1; shift ;;
    --database|--cache-dir)
      if [ "$#" -lt 2 ]; then printf '缺少 %s 的参数。\n' "$1" >&2; exit 2; fi
      eval_args+=("$1" "$2"); shift 2 ;;
    --help|-h)
      printf '用法：bash scripts/check.sh [--all | --unit-only] [--database PATH] [--cache-dir PATH]\n默认：单元/接口测试、构建、原PDF验收。\n--all：再运行22题、17题与12题开发回归、导入压力测试、真实浏览器回归。\n--unit-only：只运行单元/接口测试与构建，不依赖原始PDF。\n'
      exit 0 ;;
    *) printf '未知参数：%s\n' "$1" >&2; exit 2 ;;
  esac
done
if [ "$all" -eq 1 ] && [ "$unit_only" -eq 1 ]; then
  printf '%s\n' '--all 和 --unit-only 不能同时使用。' >&2
  exit 2
fi
if [ ! -x .venv/bin/python ] || [ ! -d frontend/node_modules ]; then
  printf '请先运行 bash scripts/start.sh 安装依赖。\n' >&2
  exit 1
fi
check_workspace=$(mktemp -d "${TMPDIR:-/tmp}/research-check.XXXXXX")
trap 'rm -rf "$check_workspace"' EXIT
export RESEARCH_DATA_DIR="$check_workspace/data"
export MPLCONFIGDIR="$check_workspace/matplotlib"
export LLM_API_KEY=''
export PYTHON_DOTENV_DISABLED=1
mkdir -p test-results
failed=0
run_check() {
  if "$@"; then return 0; else failed=1; fi
}
run_check .venv/bin/python -m pytest tests backend/test_*.py -q --junitxml=test-results/backend.xml
run_check npm --prefix frontend test
run_check npm --prefix frontend run build
if [ "$unit_only" -eq 0 ]; then
  # macOS ships Bash 3.2; + expansion is safe for an empty array under set -u.
  run_check .venv/bin/python scripts/evaluate_retrieval.py --output test-results/retrieval.json ${eval_args[@]+"${eval_args[@]}"}
fi
if [ "$all" -eq 1 ]; then
  run_check .venv/bin/python scripts/evaluate_retrieval.py --fixture tests/fixtures/retrieval_holdout.json --output test-results/retrieval-holdout.json ${eval_args[@]+"${eval_args[@]}"}
  run_check .venv/bin/python scripts/evaluate_retrieval.py --fixture tests/fixtures/retrieval_unseen_v2.json --match-protocol table_cells_v2 --output test-results/retrieval-known17.json ${eval_args[@]+"${eval_args[@]}"}
  run_check .venv/bin/python scripts/evaluate_retrieval.py --fixture tests/fixtures/retrieval_unseen_v3.json --match-protocol table_cells_v2 --output test-results/retrieval-known12.json ${eval_args[@]+"${eval_args[@]}"}
  run_check .venv/bin/python scripts/stress_import.py --output test-results/import-stress.json
  run_check npm --prefix frontend run test:e2e
fi
exit "$failed"
