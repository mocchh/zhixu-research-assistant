#!/usr/bin/env bash
# Build and test against a disposable real app, never the user's running server.
set -euo pipefail
cd "$(dirname "$0")/.."
root="$PWD"
python="$root/.venv/bin/python"
report="${E2E_REPORT_DIR:-$root/frontend/test-results/e2e}"
if [[ "$report" != /* ]]; then report="$root/frontend/$report"; fi
export E2E_REPORT_DIR="$report"
if [[ ! -x "$python" ]]; then
  echo "Create .venv with Python 3.12 and install requirements.lock first." >&2
  exit 1
fi
mkdir -p "$report"
workspace=$(mktemp -d "${TMPDIR:-/tmp}/research-agent-e2e.XXXXXX")
workspace=$("$python" -c 'import pathlib,sys; print(pathlib.Path(sys.argv[1]).resolve())' "$workspace")
touch "$workspace/.e2e-isolated"
mkdir -m 700 "$workspace/firefox-home"
mkdir -p -m 700 "$workspace/firefox-home/Library/Application Support" "$workspace/firefox-home/Library/Caches" "$workspace/firefox-home/Downloads" "$workspace/firefox-home/Documents"
export E2E_FIREFOX_USER_HOME="$workspace/firefox-home"
server_pid=""
cleanup() {
  local result=$?
  trap - EXIT INT TERM
  if [[ -n "$server_pid" ]]; then
    kill "$server_pid" 2>/dev/null || true
    wait "$server_pid" 2>/dev/null || true
  fi
  [[ ! -f "$workspace/server.log" ]] || cp "$workspace/server.log" "$report/server.log"
  [[ ! -f "$workspace/server.json" ]] || cp "$workspace/server.json" "$report/server.json"
  if [[ -f "$workspace/.e2e-isolated" && "$workspace" == *research-agent-e2e.* ]]; then
    # Firefox can briefly finish background profile writes after closing.
    # Retry removal only for the marked workspace owned by this invocation.
    "$python" - "$workspace" <<'PY' || result=1
import errno, shutil, sys, time
for attempt in range(10):
    try:
        shutil.rmtree(sys.argv[1])
        break
    except FileNotFoundError:
        break
    except OSError as exc:
        if exc.errno not in (errno.ENOTEMPTY, errno.EBUSY) or attempt == 9:
            raise
        time.sleep(.2)
PY
  fi
  "$python" - "$workspace" "$report/isolation.json" <<'PY'
import json, sys
from pathlib import Path
workspace, output = map(Path, sys.argv[1:])
output.write_text(json.dumps({"temporary_workspace": str(workspace),
    "temporary_workspace_removed": not workspace.exists()}, indent=2), encoding="utf-8")
PY
  exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
(cd frontend && npm run typecheck:e2e && npm run build)
"$python" scripts/e2e_server.py --workspace "$workspace" --port "${E2E_PORT:-0}" >"$workspace/server.log" 2>&1 &
server_pid=$!
export E2E_BASE_URL
E2E_BASE_URL=$("$python" - "$workspace/server.json" "$server_pid" <<'PY'
import json, os, sys, time, urllib.request
from pathlib import Path
metadata, pid = Path(sys.argv[1]), int(sys.argv[2])
for attempt in range(160):
    try:
        os.kill(pid, 0)
        info = json.loads(metadata.read_text())
        with urllib.request.urlopen(info['url'] + '/api/health', timeout=.5) as response:
            if response.status == 200:
                assert not json.load(response)['model_configured']
                print(info['url'])
                break
    except (OSError, ValueError, KeyError):
        time.sleep(.25)
else:
    raise SystemExit('Isolated E2E service did not become ready; see test-results/e2e/server.log')
PY
)
export E2E_FIXTURE_DIR="$workspace/fixtures"
echo "E2E: isolated real service at $E2E_BASE_URL; temporary data removed on exit."
(cd frontend && npx playwright test "$@")
