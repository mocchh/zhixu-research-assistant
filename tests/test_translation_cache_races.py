"""Whole-page cache races use real SQLite and local provider stubs only."""
import asyncio
import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor

import pymupdf
import pytest
from fastapi.testclient import TestClient

from backend import intelligence
from backend.app import create_app
from backend.tasks import page_text


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "")
    app = create_app(tmp_path / "data", run_jobs=False)
    store = app.state.store
    path = store.files / "source.pdf"
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((72, 72), "Reparsed source with corrected content.")
        pdf.save(path)
    paper = store.create_paper(path.name, path, hashlib.sha256(path.read_bytes()).hexdigest(), "upload", 1)
    pid = paper["id"]
    store.add_page(pid, {"page": 1, "width": 595, "height": 842, "blocks": [
        {"text": "Old extracted source.", "bbox": [72, 60, 450, 100]}]})
    store.update_paper(pid, status="ready")
    app.state.config.save("fake-only", "http://127.0.0.1:1/v1", "local-test")
    with TestClient(app) as client:
        yield client, app, pid


@pytest.mark.parametrize("change", ["reparse", "delete"])
def test_late_whole_page_response_cannot_replace_current_cache(workspace, monkeypatch, change):
    client, app, pid = workspace
    store = app.state.store
    old_source = page_text(store.page(pid, 1))
    entered, release = threading.Event(), threading.Event()
    calls = []

    async def translate(text, language, settings):
        calls.append(text)
        if text == old_source:
            entered.set()
            assert await asyncio.to_thread(release.wait, 5)
            return "Late old translation"
        return "Current translation"

    monkeypatch.setattr(intelligence, "translate_text", translate)
    body = {"paper_id": pid, "page": 1, "target_language": "zh"}
    with ThreadPoolExecutor(max_workers=1) as pool:
        old_request = pool.submit(client.post, "/api/translate", json=body)
        try:
            assert entered.wait(5)
            if change == "delete":
                assert client.delete(f"/api/papers/{pid}").status_code == 200
            else:
                result = client.post(f"/api/papers/{pid}/reparse")
                assert result.status_code == 200, result.text
                app.state.runner._execute(result.json()["job_id"], "parse", pid, {})
                new_source = page_text(store.page(pid, 1))
                assert new_source != old_source and "corrected content" in new_source
                current = client.post("/api/translate", json=body)
                assert current.status_code == 200, current.text
                assert current.json()["translation"] == "Current translation"
        finally:
            release.set()
        old_result = old_request.result(timeout=5)
    assert old_result.status_code == 409, old_result.text
    assert "未保存" in old_result.json()["detail"]
    assert store.translation(pid, 1, "zh", old_source) is None
    with store.connect() as db:
        rows = db.execute("SELECT source_hash,text FROM translations").fetchall()
    if change == "delete":
        assert rows == [] and len(calls) == 1
    else:
        assert len(rows) == 1 and len(calls) == 2
        assert rows[0]["source_hash"] == hashlib.sha256(new_source.encode()).hexdigest()
        assert rows[0]["text"] == "Current translation"
        cached = client.post("/api/translate", json=body)
        assert cached.json()["cached"] and len(calls) == 2


def test_storage_rejects_missing_page_and_inactive_job_without_evicting_cache(workspace):
    _, app, pid = workspace
    store = app.state.store
    source = page_text(store.page(pid, 1))
    assert store.save_translation(pid, 1, "zh", source, "Current translation")
    job = store.create_job("translate", pid)
    store.update_job(job, status="cancelled")
    assert not store.save_translation(pid, 1, "zh", source, "Cancelled task", job_id=job)
    assert store.translation(pid, 1, "zh", source) == "Current translation"
    store.create_reparse(pid)
    assert store.page(pid, 1) is None
    assert not store.save_translation(pid, 1, "zh", source, "Missing page")
    with store.connect() as db:
        rows = db.execute("SELECT text FROM translations").fetchall()
    # Reparse preserves the old hash-keyed cache for unchanged source; the late
    # response must not modify it while the page itself is unavailable.
    assert [row["text"] for row in rows] == ["Current translation"]
