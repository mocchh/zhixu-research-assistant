"""Workspace API tests use only disposable SQLite databases."""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.storage import RevisionConflict, Store
from backend.tasks import TaskRunner, page_text
from backend.workspace_routes import create_workspace_router


def sample_page(number, text="Source text"):
    return {"page": number, "width": 600, "height": 800, "blocks": [
        {"text": text, "bbox": [10, 20, 400, 40], "kind": "paragraph", "section": "Introduction", "order_index": 0}
    ] if text else [], "warnings": []}


@pytest.fixture
def workspace(tmp_path):
    store = Store(tmp_path / "data")
    paper = store.create_paper("example.pdf", tmp_path / "example.pdf", "sample", "test", 3)
    for number in range(1, 4):
        store.add_page(paper["id"], sample_page(number, f"Original page {number}."))
    store.update_paper(paper["id"], status="ready")
    settings = {"api_key": "local-fake-key"}
    config = SimpleNamespace(get=lambda: settings)
    runner = TaskRunner(store, config)
    app = FastAPI()
    app.include_router(create_workspace_router(store, runner, config, run_jobs=False))
    with TestClient(app) as client:
        yield client, store, runner, paper["id"], settings
    runner.close()


def test_session_scope_and_sources_survive_restart_and_paper_deletion(workspace):
    client, store, _, paper, _ = workspace
    source = {"id": "block-1", "paper_id": paper, "title": "Snapshot title", "page": 2,
              "bbox": [10, 20, 400, 40], "text": "Original evidence", "version": 7}
    first = {"id": "user-1", "role": "user", "content": "Question", "paper_ids": [paper], "request_id": "request-1"}
    pending = {"id": "assistant-1", "role": "assistant", "status": "pending", "paper_ids": [paper], "request_id": "request-1"}
    created = client.post("/api/sessions", json={"title": "Scope snapshots", "paper_ids": [paper], "messages": [first, pending]})
    assert created.status_code == 201, created.text
    session = created.json()
    pending.update(content="Answer", status="completed", sources=[source], mode="extractive", warnings=["Check version"])
    body = {"revision": session["revision"], "title": session["title"], "paper_ids": ["next-paper"], "messages": [first, pending]}
    updated = client.put(f"/api/sessions/{session['id']}", json=body).json()
    assert updated["revision"] == 2
    assert updated["paper_ids"] == ["next-paper"]
    assert updated["messages"][1]["paper_ids"] == [paper]
    assert client.put(f"/api/sessions/{session['id']}", json=body).status_code == 409
    store.delete_paper(paper)
    reopened = Store(store.directory)
    restored = reopened.session(session["id"])
    assert restored["messages"][1]["sources"] == [source]
    assert restored["messages"][1]["request_id"] == "request-1"
    assert restored["messages"][1]["status"] == "completed"
    summaries = client.get("/api/sessions").json()["sessions"]
    assert summaries[0]["message_count"] == 2 and "messages" not in summaries[0]
    assert client.delete(f"/api/sessions/{session['id']}").status_code == 200
    assert client.get(f"/api/sessions/{session['id']}").status_code == 404


def test_session_competing_saves_one_wins(workspace):
    _, store, _, _, _ = workspace
    session = store.create_session()
    def save(title):
        try:
            return store.save_session(session["id"], 1, title, [], [])["title"]
        except RevisionConflict:
            return "conflict"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, ["First", "Second"]))
    assert results.count("conflict") == 1
    assert store.session(session["id"])["revision"] == 2


def test_session_validates_duplicate_ids_and_required_revision(workspace):
    client, _, _, _, _ = workspace
    message = {"id": "same", "role": "user", "content": "text"}
    assert client.post("/api/sessions", json={"messages": [message, message]}).status_code == 422
    session = client.post("/api/sessions", json={}).json()
    assert client.put(f"/api/sessions/{session['id']}", json={"title": "x", "paper_ids": [], "messages": []}).status_code == 422
    assert client.post("/api/sessions", json={"title": "  "}).status_code == 422


def test_notes_revisions_keep_immutable_evidence_and_delete_cascades(workspace):
    client, store, _, paper, _ = workspace
    original = {"title": "Research note", "content": "First", "kind": "review", "paper_ids": [paper], "sources": [{"text": "Original quote"}]}
    response = client.post("/api/notes", json=original)
    assert response.status_code == 201, response.text
    note = response.json()
    changed = {**original, "revision": 1, "content": "Second", "sources": [{"text": "New quote"}]}
    updated = client.put(f"/api/notes/{note['id']}", json=changed)
    assert updated.status_code == 200, updated.text
    assert [item["revision"] for item in updated.json()["versions"]] == [2, 1]
    assert client.put(f"/api/notes/{note['id']}", json=changed).status_code == 409
    store.delete_paper(paper)
    old = client.get(f"/api/notes/{note['id']}/versions/1").json()
    assert old["content"] == "First" and old["sources"] == original["sources"]
    assert Store(store.directory).note(note["id"])["content"] == "Second"
    assert client.get("/api/notes").json()["notes"][0]["revision"] == 2
    assert client.delete(f"/api/notes/{note['id']}").status_code == 200
    assert store.note_version(note["id"], 1) is None
    assert client.get(f"/api/notes/{note['id']}").status_code == 404


def test_reading_revision_range_and_restart(workspace):
    client, store, _, paper, _ = workspace
    path = f"/api/papers/{paper}/reading"
    initial = client.get(path).json()
    assert initial["page"] == 1 and initial["revision"] == 0
    saved = client.put(path, json={"page": 2, "scroll_ratio": .75, "revision": 0})
    assert saved.status_code == 200
    assert saved.json()["revision"] == 1
    assert Store(store.directory).reading_position(paper)["scroll_ratio"] == .75
    assert client.put(path, json={"page": 1, "revision": 0}).status_code == 409
    assert client.put(path, json={"page": 4, "revision": 1}).status_code == 400
    assert client.put(path, json={"page": 2, "revision": 1, "scroll_ratio": 2}).status_code == 422
    store.delete_paper(paper)
    assert client.get(path).status_code == 404
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM reading_positions").fetchone()[0] == 0


def test_translation_estimate_range_cache_and_configuration(workspace):
    client, store, _, paper, settings = workspace
    store.save_translation(paper, 2, "en", page_text(store.page(paper, 2)), "cached")
    path = f"/api/papers/{paper}"
    body = {"target_language": "en", "start_page": 2, "end_page": 3}
    result = client.post(path + "/translation-estimate", json=body)
    assert result.status_code == 200, result.text
    estimate = result.json()
    assert estimate["pages"] == 2 and estimate["cached_pages"] == 1
    assert estimate["pending_characters"] == len("Original page 3.")
    assert estimate["total_characters"] == len("Original page 2.Original page 3.")
    assert estimate["estimated_input_tokens"] > 0 and "price" not in estimate
    assert client.post(path + "/translation-estimate", json={"start_page": 3, "end_page": 2}).status_code == 400
    assert client.post(path + "/translation-estimate", json={"end_page": 4}).status_code == 400
    settings["api_key"] = ""
    assert client.post(path + "/translation-jobs", json=body).status_code == 503
    assert store.jobs() == []


def test_translation_job_guards_paused_and_conflicting_options(workspace):
    client, store, _, paper, _ = workspace
    path = f"/api/papers/{paper}/translation-jobs"
    body = {"target_language": "en", "start_page": 2, "end_page": 3}
    response = client.post(path, json=body)
    assert response.status_code == 200, response.text
    job = response.json()["job"]
    assert job["options"] == body
    assert client.post(path, json=body).json()["job_id"] == job["id"]
    assert client.post(path, json={**body, "target_language": "zh"}).status_code == 409
    assert client.post(path, json={**body, "start_page": 1}).status_code == 409
    assert client.post(f"/api/jobs/{job['id']}/pause").json()["status"] == "paused"
    assert client.post(path, json=body).json()["job"]["status"] == "paused"
    assert len(store.jobs()) == 1
    assert client.post(f"/api/jobs/{job['id']}/resume").json()["options"] == body
    assert client.post(f"/api/jobs/{job['id']}/cancel").json()["status"] == "cancelled"
    assert client.post(f"/api/jobs/{job['id']}/resume").status_code == 409
    newer = client.post(path, json=body).json()["job"]
    assert newer["id"] != job["id"]
    assert client.get(f"/api/jobs/{newer['id']}").json()["status"] == "queued"


def test_unparsed_or_failed_ocr_page_not_silently_translated(workspace):
    client, store, _, paper, _ = workspace
    failed = sample_page(2, "")
    failed["warnings"] = ["OCR unavailable"]
    store.add_page(paper, failed)
    assert client.post(f"/api/papers/{paper}/translation-estimate", json={}).status_code == 400
    assert client.post(f"/api/papers/{paper}/translation-jobs", json={}).status_code == 400
    assert store.jobs() == []
    # A truly blank page without parser warnings remains valid.
    store.add_page(paper, sample_page(2, ""))
    estimate = client.post(f"/api/papers/{paper}/translation-estimate", json={}).json()
    assert estimate["blank_pages"] == 1


def test_persistence_migrates_old_jobs_without_losing_language(tmp_path):
    import sqlite3
    directory = tmp_path / "legacy"
    directory.mkdir()
    with sqlite3.connect(directory / "research.sqlite3") as db:
        db.execute("CREATE TABLE jobs(id TEXT PRIMARY KEY,kind TEXT,paper_id TEXT,status TEXT,progress REAL,message TEXT,created_at TEXT,updated_at TEXT,result TEXT)")
        db.execute("INSERT INTO jobs VALUES ('old','translate','paper','error',50,'failed','t','t',?)", ('{"target_language":"en","translated_pages":2}',))
    job = Store(directory).job("old")
    assert job["options"]["target_language"] == "en"
    assert job["result"]["translated_pages"] == 2 and job["last_page"] == 0
