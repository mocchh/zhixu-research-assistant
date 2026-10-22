"""Actual process ownership protects crash recovery from duplicate startup."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from backend.app import create_app
from backend.workspace_lock import WorkspaceInUseError, WorkspaceLock
from scripts.runtime_harness import RuntimeServer, wait_for
from scripts.stress_import import make_pdf


ROOT = Path(__file__).resolve().parents[1]


def test_lock_is_nonblocking_and_reusable_without_deleting_its_file(tmp_path):
    lock = WorkspaceLock(tmp_path)
    with lock:
        before = lock.path.stat().st_ino
        tick = time.monotonic()
        with pytest.raises(WorkspaceInUseError, match="另一个应用实例"):
            WorkspaceLock(tmp_path).acquire()
        assert time.monotonic() - tick < 1
    assert lock.path.exists() and lock.path.stat().st_ino == before
    with lock:
        assert lock.descriptor is not None
    lock.release()


def test_different_data_directories_can_run_together(tmp_path):
    with WorkspaceLock(tmp_path / "one"), WorkspaceLock(tmp_path / "two"):
        pass


def test_crashed_process_releases_lock_without_deleting_lock_file(tmp_path):
    ready = tmp_path / "ready"
    code = "from pathlib import Path; import sys; from backend.workspace_lock import WorkspaceLock; lock=WorkspaceLock(Path(sys.argv[1])).acquire(); Path(sys.argv[2]).touch(); sys.stdin.readline()"
    child = subprocess.Popen([sys.executable, "-c", code, str(tmp_path), str(ready)],
                             cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        wait_for(lambda: ready.exists(), 5)
        with pytest.raises(WorkspaceInUseError):
            WorkspaceLock(tmp_path).acquire()
        child.kill()
        child.wait(timeout=5)
        with WorkspaceLock(tmp_path):
            assert (tmp_path / ".workspace.lock").exists()
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=5)


def test_startup_failure_releases_workspace_lock(tmp_path, monkeypatch):
    app = create_app(tmp_path)
    def failed_recovery():
        raise RuntimeError("simulated recovery failure")
    monkeypatch.setattr(app.state.runner, "recover", failed_recovery)
    with pytest.raises(RuntimeError, match="simulated recovery failure"):
        with TestClient(app):
            pass
    with TestClient(create_app(tmp_path, run_jobs=False)) as client:
        assert client.get("/api/health").status_code == 200


def test_constructed_apps_cannot_start_same_workspace_together(tmp_path):
    first = create_app(tmp_path, run_jobs=False)
    second = create_app(tmp_path, run_jobs=False)
    with TestClient(first):
        with pytest.raises(WorkspaceInUseError):
            with TestClient(second):
                pass
        with pytest.raises(WorkspaceInUseError):
            create_app(tmp_path, run_jobs=False)
    with TestClient(create_app(tmp_path, run_jobs=False)):
        pass


def test_shutdown_keeps_lock_until_worker_stops(tmp_path):
    app = create_app(tmp_path, run_jobs=False)
    worker_started, release_worker, closing = threading.Event(), threading.Event(), threading.Event()
    def worker():
        worker_started.set()
        assert release_worker.wait(5)
    def run_app():
        with TestClient(app):
            app.state.runner.pool.submit(worker)
            assert worker_started.wait(2)
            closing.set()
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(run_app)
        try:
            assert closing.wait(3)
            with pytest.raises(TimeoutError):
                running.result(timeout=.1)
            with pytest.raises(WorkspaceInUseError):
                WorkspaceLock(tmp_path).acquire()
        finally:
            release_worker.set()
        running.result(timeout=5)
    with WorkspaceLock(tmp_path):
        pass


def test_second_process_rejected_before_cleanup_or_recovery(tmp_path):
    workspace = tmp_path / "runtime"
    with RuntimeServer(workspace) as server:
        server.arm("parse")
        response = server.client.post("/api/papers/upload", files={
            "files": ("active.pdf", make_pdf(1, 4), "application/pdf")})
        assert response.status_code == 200
        pid = response.json()["papers"][0]["id"]
        server.reached("parse")
        before = server.client.get("/api/jobs").json()
        assert any(job["status"] == "processing" for job in before["jobs"])
        staging = workspace / "data/files" / f"{uuid4().hex}.pdf"
        staging.write_bytes(b"another upload has not committed yet")
        environment = {**os.environ, "RESEARCH_DATA_DIR": str(workspace / "data"),
                       "PYTHON_DOTENV_DISABLED": "1", "LLM_API_KEY": ""}
        other = subprocess.run([sys.executable, "-m", "uvicorn", "backend.app:app", "--host", "127.0.0.1", "--port", "0"],
                               cwd=ROOT, env=environment, capture_output=True, text=True, timeout=10)
        assert other.returncode != 0 and "另一个应用实例" in other.stderr
        assert staging.read_bytes() == b"another upload has not committed yet"
        assert server.client.get("/api/jobs").json() == before
        assert server.client.get("/api/health").status_code == 200
        (workspace / "parse.release").touch()
        wait_for(lambda: next((p for p in server.client.get("/api/papers").json()["papers"]
                               if p["id"] == pid and p["status"] == "ready"), None))
        assert staging.is_file()
        server.stop(kill=True)
        server.start()
        assert not staging.exists()
        paper = server.client.get(f"/api/papers/{pid}").json()["paper"]
        assert paper["status"] == "ready" and paper["processed_pages"] == 4
