import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from scripts.install_frontend import MARKER, dependency_fingerprint, ensure_dependencies
from scripts import install_backend


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def npm_workspace(tmp_path, monkeypatch):
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "package.json").write_text('{"name":"fixture","version":"1.0.0"}')
    (frontend / "package-lock.json").write_text('{"lockfileVersion":3,"packages":{}}')
    commands = tmp_path / "bin"
    commands.mkdir()
    npm = commands / "npm"
    npm.write_text('''#!/bin/sh
printf '%s:%s\n' "$PWD" "$*" >> "$NPM_TEST_LOG"
if [ "$1" = ci ]; then
  mkdir -p node_modules
  if [ "${NPM_TEST_FAIL:-0}" != 0 ]; then exit 17; fi
  if [ "${NPM_TEST_CHANGE_LOCK:-0}" != 0 ]; then printf '{"changed":true}' > package-lock.json; fi
fi
''')
    npm.chmod(0o755)
    log = tmp_path / "npm.log"
    monkeypatch.setenv("PATH", str(commands) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("NPM_TEST_LOG", str(log))
    return frontend, log


def test_existing_modules_without_a_marker_are_installed_once(npm_workspace):
    frontend, log = npm_workspace
    (frontend / "node_modules").mkdir()
    assert ensure_dependencies(frontend) is True
    assert ensure_dependencies(frontend) is False
    assert log.read_text().splitlines() == [f"{frontend}:ci"]
    assert json.loads((frontend / "node_modules" / MARKER).read_text()) == dependency_fingerprint(frontend)


@pytest.mark.parametrize("filename", ["package-lock.json", "package.json"])
def test_changed_content_installs_again_even_with_same_timestamp(npm_workspace, filename):
    frontend, log = npm_workspace
    ensure_dependencies(frontend)
    path = frontend / filename
    stamp = path.stat()
    path.write_text(path.read_text() + "\n")
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    assert ensure_dependencies(frontend) is True
    assert len(log.read_text().splitlines()) == 2


def test_removed_modules_and_invalid_marker_force_reinstallation(npm_workspace):
    frontend, log = npm_workspace
    ensure_dependencies(frontend)
    marker = frontend / "node_modules" / MARKER
    marker.write_text("incomplete")
    assert ensure_dependencies(frontend) is True
    shutil.rmtree(frontend / "node_modules")
    assert ensure_dependencies(frontend) is True
    assert len(log.read_text().splitlines()) == 3


def test_failed_install_removes_stale_success_marker_and_retries(npm_workspace, monkeypatch):
    frontend, log = npm_workspace
    ensure_dependencies(frontend)
    lock = frontend / "package-lock.json"
    original = lock.read_text()
    lock.write_text(original + "\n")
    monkeypatch.setenv("NPM_TEST_FAIL", "1")
    with pytest.raises(subprocess.CalledProcessError):
        ensure_dependencies(frontend)
    assert not (frontend / "node_modules" / MARKER).exists()
    # Reverting the lock cannot resurrect the old installation marker.
    lock.write_text(original)
    monkeypatch.delenv("NPM_TEST_FAIL")
    assert ensure_dependencies(frontend) is True
    assert len(log.read_text().splitlines()) == 3


def test_changing_lock_during_install_never_records_success(npm_workspace, monkeypatch):
    frontend, _ = npm_workspace
    monkeypatch.setenv("NPM_TEST_CHANGE_LOCK", "1")
    with pytest.raises(RuntimeError, match="发生变化"):
        ensure_dependencies(frontend)
    assert not (frontend / "node_modules" / MARKER).exists()


def prepare_entrypoint_workspace(frontend, monkeypatch):
    workspace = frontend.parent
    scripts = workspace / "scripts"
    scripts.mkdir()
    for filename in ["start.sh", "dev.sh", "install_frontend.py", "install_backend.py"]:
        shutil.copyfile(ROOT / "scripts" / filename, scripts / filename)
    python = workspace / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text('''#!/bin/sh
if [ "$1" = -c ]; then exit 0; fi
if [ "$1" = -m ] && [ "$2" = uvicorn ]; then
  printf 'uvicorn\n' >> "$BACKEND_TEST_LOG"
  exit 0
fi
exec "$TEST_REAL_PYTHON" "$@"
''')
    python.chmod(0o755)
    (workspace / "requirements.txt").write_text("")
    (workspace / "requirements.lock").write_text("")
    (workspace / ".venv" / install_backend.MARKER).write_text(json.dumps(install_backend.dependency_fingerprint(workspace)))
    monkeypatch.setenv("TEST_REAL_PYTHON", sys.executable)
    monkeypatch.setenv("BACKEND_TEST_LOG", str(workspace / "backend.log"))
    return workspace


@pytest.mark.parametrize("entrypoint,command", [("start.sh", "run build"), ("dev.sh", "run dev")])
def test_both_startup_modes_check_dependencies_before_launch(npm_workspace, monkeypatch, entrypoint, command):
    frontend, log = npm_workspace
    workspace = prepare_entrypoint_workspace(frontend, monkeypatch)
    result = subprocess.run(["bash", str(workspace / "scripts" / entrypoint)], cwd=workspace,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert log.read_text().splitlines() == [f"{frontend}:ci", f"{frontend}:{command}"]
    assert (frontend / "node_modules" / MARKER).is_file()


@pytest.mark.parametrize("entrypoint", ["start.sh", "dev.sh"])
def test_failed_dependency_install_stops_startup(npm_workspace, monkeypatch, entrypoint):
    frontend, log = npm_workspace
    workspace = prepare_entrypoint_workspace(frontend, monkeypatch)
    monkeypatch.setenv("NPM_TEST_FAIL", "1")
    result = subprocess.run(["bash", str(workspace / "scripts" / entrypoint)], cwd=workspace,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert log.read_text().splitlines() == [f"{frontend}:ci"]
    assert not (frontend / "node_modules" / MARKER).exists()
    assert not (workspace / "backend.log").exists()


@pytest.fixture
def python_workspace(npm_workspace, monkeypatch):
    frontend, npm_log = npm_workspace
    workspace = prepare_entrypoint_workspace(frontend, monkeypatch)
    marker = workspace / ".venv" / install_backend.MARKER
    marker.unlink()
    uv = workspace / "bin" / "uv"
    uv.write_text('''#!/bin/sh
printf '%s\n' "$*" >> "$PYTHON_INSTALL_LOG"
if [ "${PYTHON_INSTALL_FAIL:-0}" != 0 ]; then exit 19; fi
if [ "${PYTHON_INSTALL_CHANGE_LOCK:-0}" != 0 ]; then printf '\n# changed' >> requirements.lock; fi
''')
    uv.chmod(0o755)
    log = workspace / "python-install.log"
    monkeypatch.setenv("PYTHON_INSTALL_LOG", str(log))
    return workspace, log, npm_log


def test_backend_old_timestamp_marker_is_not_trusted(python_workspace):
    workspace, log, _ = python_workspace
    (workspace / ".venv" / ".research-deps-installed").touch()
    assert install_backend.ensure_dependencies(workspace) is True
    assert install_backend.ensure_dependencies(workspace) is False
    assert len(log.read_text().splitlines()) == 1
    marker = workspace / ".venv" / install_backend.MARKER
    assert json.loads(marker.read_text()) == install_backend.dependency_fingerprint(workspace)


@pytest.mark.parametrize("filename", ["requirements.txt", "requirements.lock"])
def test_backend_changed_content_with_older_timestamp_installs(python_workspace, filename):
    workspace, log, _ = python_workspace
    install_backend.ensure_dependencies(workspace)
    manifest = workspace / filename
    manifest.write_text("# updated\n")
    os.utime(manifest, (1, 1))
    assert install_backend.ensure_dependencies(workspace) is True
    assert len(log.read_text().splitlines()) == 2


def test_backend_failed_install_clears_marker_and_retries_restored_lock(python_workspace, monkeypatch):
    workspace, log, _ = python_workspace
    install_backend.ensure_dependencies(workspace)
    lock = workspace / "requirements.lock"
    original = lock.read_text()
    lock.write_text("# changed\n")
    monkeypatch.setenv("PYTHON_INSTALL_FAIL", "1")
    with pytest.raises(subprocess.CalledProcessError):
        install_backend.ensure_dependencies(workspace)
    assert not (workspace / ".venv" / install_backend.MARKER).exists()
    lock.write_text(original)
    monkeypatch.delenv("PYTHON_INSTALL_FAIL")
    assert install_backend.ensure_dependencies(workspace) is True
    assert len(log.read_text().splitlines()) == 3


def test_backend_changes_during_install_do_not_record_success(python_workspace, monkeypatch):
    workspace, _, _ = python_workspace
    monkeypatch.setenv("PYTHON_INSTALL_CHANGE_LOCK", "1")
    with pytest.raises(RuntimeError, match="发生变化"):
        install_backend.ensure_dependencies(workspace)
    assert not (workspace / ".venv" / install_backend.MARKER).exists()


def test_backend_invalid_marker_forces_install(python_workspace):
    workspace, log, _ = python_workspace
    (workspace / ".venv" / install_backend.MARKER).write_text("interrupted")
    assert install_backend.ensure_dependencies(workspace) is True
    assert len(log.read_text().splitlines()) == 1


def test_backend_missing_interpreter_rejected(python_workspace):
    workspace, log, _ = python_workspace
    (workspace / ".venv" / "bin" / "python").unlink()
    with pytest.raises(RuntimeError, match="虚拟环境"):
        install_backend.ensure_dependencies(workspace)
    assert not log.exists()


def test_backend_without_uv_uses_venv_pip(python_workspace, monkeypatch):
    workspace, _, _ = python_workspace
    calls = []
    monkeypatch.setattr(install_backend.shutil, "which", lambda command: None)
    monkeypatch.setattr(install_backend.subprocess, "run", lambda command, **kwargs: calls.append(command))
    assert install_backend.ensure_dependencies(workspace) is True
    assert calls[1] == [str(workspace / ".venv" / "bin" / "python"), "-m", "pip", "install", "-r", str(workspace / "requirements.lock")]


@pytest.mark.parametrize("entrypoint", ["start.sh", "dev.sh"])
def test_both_modes_install_changed_backend_dependencies(python_workspace, entrypoint):
    workspace, log, npm_log = python_workspace
    result = subprocess.run(["bash", str(workspace / "scripts" / entrypoint)], cwd=workspace,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert len(log.read_text().splitlines()) == 1
    assert (workspace / ".venv" / install_backend.MARKER).is_file()
    assert npm_log.exists()


@pytest.mark.parametrize("entrypoint", ["start.sh", "dev.sh"])
def test_backend_install_failure_stops_both_modes(python_workspace, monkeypatch, entrypoint):
    workspace, log, npm_log = python_workspace
    monkeypatch.setenv("PYTHON_INSTALL_FAIL", "1")
    result = subprocess.run(["bash", str(workspace / "scripts" / entrypoint)], cwd=workspace,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert log.exists() and not npm_log.exists()
    assert not (workspace / ".venv" / install_backend.MARKER).exists()
    assert not (workspace / "backend.log").exists()
