#!/usr/bin/env python3
"""Exercise fresh installs and schema upgrades in a disposable source copy.

Only package-manager traffic goes outside localhost. The copy excludes documents,
settings, .env, existing dependencies, and the user's database.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request


ROOT = Path(__file__).resolve().parents[1]


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def seed_legacy(directory: Path) -> dict:
    """Construct the schemas preceding the additive migrations, with saved data."""
    import pymupdf
    from backend.storage import Store

    store = Store(directory)
    pdf = store.files / "upgrade-fixture.pdf"
    with pymupdf.open() as document:
        for number in (1, 2):
            document.new_page().insert_text((72, 72), f"Installation upgrade fixture page {number}")
        document.save(pdf)
    paper = store.create_paper(pdf.name, pdf, digest(pdf), "upload", 2)
    for number in (1, 2):
        store.add_page(paper["id"], {"page": number, "width": 595, "height": 842,
            "warnings": [], "blocks": [{"text": f"Installation upgrade fixture page {number}",
            "bbox": [72, 60, 320, 80], "kind": "paragraph", "section": "Introduction", "order_index": 0}]})
    store.update_paper(paper["id"], status="ready", title="Preserved legacy title", tags=["legacy"])
    evidence = store.blocks([paper["id"]])[0]
    session = store.create_session("Preserved conversation", [paper["id"]], [
        {"id": "legacy-answer", "role": "assistant", "content": "Saved answer", "sources": [evidence], "status": "completed"}])
    note = store.create_note("First title", "First version", [paper["id"]], [evidence])
    note = store.save_note(note["id"], 1, "Preserved note", "Second version", [paper["id"]], [evidence])
    store.save_reading_position(paper["id"], 0, 2, 0.4)
    store.save_translation(paper["id"], 1, "zh", evidence["text"], "已保存的旧版译文")
    options = {"target_language": "zh", "start_page": 1, "end_page": 2}
    job = store.create_job("translate", paper["id"], options)
    store.update_job(job, status="paused", result=options)
    removed = {"papers": ["parser_version", "title_custom", "tags_custom"],
               "pages": ["page_label"], "jobs": ["options", "last_page"]}
    with store.connect() as db:
        for table, columns in removed.items():
            for column in columns:
                db.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return {"paper_id": paper["id"], "session_id": session["id"], "note_id": note["id"],
            "job_id": job, "job_options": options, "pdf": str(pdf), "pdf_sha256": digest(pdf),
            "removed_columns": removed}


def snapshot(database: Path) -> dict:
    with sqlite3.connect(database) as db:
        db.row_factory = sqlite3.Row
        tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return {table: [dict(row) for row in db.execute(f'SELECT * FROM "{table}" ORDER BY rowid')] for table in tables}


def verify_legacy(directory: Path, before: dict, fixture: dict) -> dict:
    from backend.storage import Store

    store = Store(directory)
    after = snapshot(store.path)
    for table, rows in before.items():
        assert len(rows) == len(after[table]), f"row count changed: {table}"
        for old, new in zip(rows, after[table], strict=True):
            assert old == {key: new[key] for key in old}, f"saved data changed: {table}"
    assert store.job(fixture["job_id"])["options"] == fixture["job_options"]
    assert store.job(fixture["job_id"])["last_page"] == 0
    assert store.paper(fixture["paper_id"])["parser_version"] == ""
    assert store.page(fixture["paper_id"], 1)["page_label"] == ""
    assert digest(Path(fixture["pdf"])) == fixture["pdf_sha256"]
    # Running the upgrade a second time must be harmless.
    Store(directory)
    assert snapshot(store.path) == after
    with store.connect() as db:
        assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    return {"passed": True, "preserved_rows": {table: len(rows) for table, rows in before.items()},
            "migrated_columns": fixture["removed_columns"], "pdf_sha256": fixture["pdf_sha256"], "idempotent": True}


def request(base: str, path: str) -> dict | str:
    with urllib.request.urlopen(base + path, timeout=5) as response:
        payload = response.read().decode()
        return json.loads(payload) if "json" in response.headers.get("Content-Type", "") else payload


@contextmanager
def server(workspace: Path, data: Path, env: dict, log: Path):
    with socket.socket() as endpoint:
        endpoint.bind(("127.0.0.1", 0))
        port = endpoint.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    process = None
    try:
        with log.open("w") as output:
            process = subprocess.Popen([str(workspace / ".venv/bin/python"), "-m", "uvicorn", "backend.app:app",
                "--host", "127.0.0.1", "--port", str(port), "--no-access-log"], cwd=workspace,
                env={**env, "RESEARCH_DATA_DIR": str(data)}, stdout=output, stderr=subprocess.STDOUT)
            for _ in range(300):
                if process.poll() is not None:
                    raise RuntimeError(f"server exited: {log}")
                try:
                    health = request(base, "/api/health")
                    break
                except OSError:
                    time.sleep(0.1)
            else:
                raise RuntimeError(f"server startup timed out: {log}")
            assert health["model_configured"] is False
            assert "<html" in request(base, "/").lower()
            yield base, health
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def probe_upgrade(output: Path) -> None:
    # Executed by the newly installed Python, not by the caller's environment.
    sys.path.insert(0, str(ROOT))
    data = ROOT / "upgrade-data"
    fixture = seed_legacy(data)
    before = snapshot(data / "research.sqlite3")
    report = verify_legacy(data, before, fixture)
    with server(ROOT, data, os.environ.copy(), output.with_suffix(".server.log")) as (base, health):
        assert request(base, "/api/stats")["papers"] == 1
        assert request(base, f"/api/notes/{fixture['note_id']}")["content"] == "Second version"
        assert request(base, f"/api/sessions/{fixture['session_id']}")["messages"][0]["content"] == "Saved answer"
        report.update({"http_boot_passed": True, "health": health})
    report["restart_preserved_data"] = verify_legacy(data, before, fixture)["passed"]
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")


def run_validation(output: Path, workspace: Path | None = None) -> int:
    automatic_workspace = workspace is None
    workspace = workspace or Path(tempfile.mkdtemp(prefix="research-install-v6-"))
    workspace.mkdir(parents=True, exist_ok=True)
    if any(workspace.iterdir()):
        raise ValueError("安装验收目录必须为空，避免覆盖已有环境或用户数据。")
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {"started_at": datetime.now(timezone.utc).isoformat(), "workspace": str(workspace),
              "source": str(ROOT), "passed": False, "phases": [], "external_model_requests": 0,
              "dependency_environment": "new venv and node_modules; fresh isolated uv/npm caches"}
    env = {key: value for key, value in os.environ.items() if key in {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "SYSTEMROOT"}}
    env.update({"PYTHON_DOTENV_DISABLED": "1", "LLM_API_KEY": "", "LLM_BASE_URL": "http://127.0.0.1:1/v1",
        "LLM_MODEL": "disabled-install-test", "RESEARCH_DATA_DIR": str(workspace / "fresh-data"),
        "MPLCONFIGDIR": str(workspace / "matplotlib"), "UV_CACHE_DIR": str(workspace / ".uv-cache"),
        "UV_DEFAULT_INDEX": "https://pypi.org/simple", "PIP_INDEX_URL": "https://pypi.org/simple",
        "NPM_CONFIG_CACHE": str(workspace / ".npm-cache"), "NPM_CONFIG_REGISTRY": "https://registry.npmjs.org",
        "NPM_CONFIG_AUDIT": "false", "NPM_CONFIG_FUND": "false"})

    def record():
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")

    def command(label: str, arguments: list[str], expected=0):
        log = output.with_name(output.stem + "-" + label + ".log")
        started = time.monotonic()
        with log.open("w") as handle:
            result = subprocess.run(arguments, cwd=workspace, env=env, stdout=handle, stderr=subprocess.STDOUT)
        passed = result.returncode == 0 if expected == 0 else result.returncode != 0
        report["phases"].append({"phase": label, "passed": passed, "returncode": result.returncode,
            "expected": "success" if expected == 0 else "failure", "elapsed_seconds": round(time.monotonic() - started, 2), "log": str(log)})
        record()
        if not passed:
            raise RuntimeError(f"{label} failed; see {log}")

    try:
        for name in ("backend", "scripts", "tests", "frontend"):
            shutil.copytree(ROOT / name, workspace / name, ignore=shutil.ignore_patterns("node_modules", "dist", ".vite", "__pycache__", "*.tsbuildinfo", "test-results", "playwright-report"), dirs_exist_ok=True)
        for name in ("requirements.txt", "requirements.lock"):
            shutil.copyfile(ROOT / name, workspace / name)
        assert not (workspace / ".env").exists() and not (workspace / "data").exists()
        command("create-venv", [shutil.which("python3.12") or "python3.12", "-m", "venv", str(workspace / ".venv")])
        python = str(workspace / ".venv/bin/python")
        command("python-install", [python, "scripts/install_backend.py"])
        command("python-lock-check", [python, "-m", "pip", "check"])
        command("python-locked-versions", [python, "-c", "from pathlib import Path; from importlib.metadata import version; import json, sys; locked = dict(line.split('==', 1) for line in Path('requirements.lock').read_text().splitlines() if line and not line.startswith('#')); installed = {name: version(name) for name in locked}; assert installed == locked, (installed, locked); import pymupdf, rapidocr_onnxruntime, onnxruntime, matplotlib; print(json.dumps({'python': sys.version, 'packages': installed}, indent=2))"])
        command("frontend-install", [python, "scripts/install_frontend.py"])
        command("frontend-build", ["npm", "--prefix", "frontend", "run", "build"])
        with server(workspace, workspace / "fresh-data", env, output.with_name(output.stem + "-fresh-server.log")) as (base, health):
            assert request(base, "/api/stats")["papers"] == 0
            report["fresh_boot"] = {"passed": True, "health": health}
        marker = workspace / ".venv/.research-deps-installed.json"
        before = marker.stat().st_mtime_ns
        command("python-unchanged", [python, "scripts/install_backend.py"])
        assert marker.stat().st_mtime_ns == before
        lock = workspace / "requirements.lock"
        original = lock.read_bytes()
        lock.write_text("this is not a valid requirement @ :\n")
        os.utime(lock, (1, 1))
        command("python-invalid-old-timestamp", [python, "scripts/install_backend.py"], expected=1)
        assert not marker.exists()
        lock.write_bytes(original)
        os.utime(lock, (1, 1))
        command("python-retry", [python, "scripts/install_backend.py"])
        assert marker.exists()
        front_marker = workspace / "frontend/node_modules/.research-deps-installed.json"
        before = front_marker.stat().st_mtime_ns
        command("frontend-unchanged", [python, "scripts/install_frontend.py"])
        assert front_marker.stat().st_mtime_ns == before
        front_lock = workspace / "frontend/package-lock.json"
        original_front = front_lock.read_bytes()
        front_lock.write_text("{ invalid lock")
        os.utime(front_lock, (1, 1))
        command("frontend-invalid-old-timestamp", [python, "scripts/install_frontend.py"], expected=1)
        assert not front_marker.exists()
        front_lock.write_bytes(original_front)
        os.utime(front_lock, (1, 1))
        command("frontend-retry", [python, "scripts/install_frontend.py"])
        assert front_marker.exists()
        command("frontend-build-after-retry", ["npm", "--prefix", "frontend", "run", "build"])
        upgrade_output = output.with_name(output.stem + "-upgrade.json")
        command("legacy-upgrade", [python, "scripts/validate_install.py", "--probe-upgrade", str(upgrade_output)])
        report["legacy_upgrade"] = json.loads(upgrade_output.read_text())
        command("fresh-environment-regression", [python, "-m", "pytest", "tests/test_startup_dependencies.py", "tests/test_install_upgrade.py", "tests/test_api.py", "-q"])
        report["passed"] = True
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
    finally:
        if report["passed"] and automatic_workspace:
            shutil.rmtree(workspace)
            report["workspace_removed"] = not workspace.exists()
        else:
            report["workspace_removed"] = False
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        record()
    print(json.dumps({"passed": report["passed"], "report": str(output), "workspace": str(workspace)}, ensure_ascii=False))
    return 0 if report["passed"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "test-results/install-v6-validation.json")
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--probe-upgrade", type=Path)
    args = parser.parse_args()
    if args.probe_upgrade:
        probe_upgrade(args.probe_upgrade.resolve())
        return 0
    return run_validation(args.output.resolve(), args.workspace.resolve() if args.workspace else None)


if __name__ == "__main__":
    raise SystemExit(main())
