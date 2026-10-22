"""Real SQLite/PDF import failure recovery, without provider or network calls."""
import hashlib
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pymupdf
import pytest
from fastapi.testclient import TestClient

from backend import app as app_module, documents
from backend.app import create_app


def pdf_bytes(text="Import recovery fixture"):
    with documents.PYMUPDF_LOCK, pymupdf.open() as pdf:
        pdf.new_page().insert_text((72, 72), text)
        return pdf.tobytes()


@pytest.fixture
def workspace(tmp_path):
    samples = tmp_path / "samples"
    samples.mkdir()
    app = create_app(tmp_path / "data", samples, run_jobs=False)
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, app, samples


def upload(client, content, name="fixture.pdf"):
    return client.post("/api/papers/upload", files={"files": (name, content, "application/pdf")})


def inject_job_failure(store):
    with store.connect() as db:
        db.execute("""CREATE TRIGGER fail_import_job BEFORE INSERT ON jobs
            BEGIN SELECT RAISE(ABORT, 'private simulated database failure'); END""")


def test_job_insert_failure_rolls_back_paper_and_allows_same_pdf_retry(workspace):
    client, app, _ = workspace
    store, content = app.state.store, pdf_bytes()
    inject_job_failure(store)
    failed = upload(client, content)
    assert failed.status_code == 500
    assert failed.json()["status"] == "failed"
    assert failed.json()["errors"][0]["filename"] == "fixture.pdf"
    assert "private simulated" not in failed.text
    assert store.papers() == [] and store.jobs() == []
    assert list(store.files.iterdir()) == []
    with store.connect() as db:
        db.execute("DROP TRIGGER fail_import_job")
    accepted = upload(client, content)
    assert accepted.status_code == 200 and len(accepted.json()["papers"]) == 1
    assert accepted.json()["duplicates"] == []
    job = store.jobs()[0]
    assert job["paper_id"] == accepted.json()["papers"][0]["id"]
    app.state.runner._execute(job["id"], "parse", job["paper_id"], {})
    assert store.paper(job["paper_id"])["status"] == "ready"
    assert len(list(store.files.glob("*.pdf"))) == 1


def test_statistics_count_parsed_pages_instead_of_announced_pdf_pages(workspace):
    client, app, _ = workspace
    response = upload(client, pdf_bytes())
    assert response.status_code == 200
    assert client.get('/api/stats').json()['pages'] == 0
    job = app.state.store.jobs()[0]
    app.state.runner._execute(job['id'], 'parse', job['paper_id'], {})
    assert client.get('/api/stats').json()['pages'] == 1


@pytest.mark.parametrize("raises", [True, False])
def test_post_commit_dispatch_failure_keeps_pdf_and_retryable_job(tmp_path, monkeypatch, raises):
    app = create_app(tmp_path / "data", run_jobs=True)
    def unavailable(*args):
        if raises:
            raise RuntimeError("synthetic executor unavailable")
        return False
    monkeypatch.setattr(app.state.runner, "submit", unavailable)
    with TestClient(app) as client:
        content = pdf_bytes()
        response = upload(client, content)
        assert response.status_code == 200
        paper = response.json()["papers"][0]
        assert paper["status"] == "error"
        stored = app.state.store.paper(paper["id"], private=True)
        assert hashlib.sha256(Path(stored["path"]).read_bytes()).hexdigest() == hashlib.sha256(content).hexdigest()
        assert app.state.store.jobs()[0]["status"] == "error"
        monkeypatch.setattr(app.state.runner, "submit", lambda *args: True)
        retry = client.post(f"/api/papers/{paper['id']}/retry")
        assert retry.status_code == 200
        app.state.runner._execute(retry.json()["job_id"], "parse", paper["id"], {})
        assert app.state.store.paper(paper["id"])["status"] == "ready"
        assert len(app.state.store.papers()) == 1


def test_queued_import_recovers_on_restart_when_dispatch_and_error_marking_fail(tmp_path, monkeypatch):
    data = tmp_path / "data"
    app = create_app(data, run_jobs=True)
    monkeypatch.setattr(app.state.runner, "submit", lambda *args: False)
    def cannot_mark(*args):
        raise RuntimeError("temporary state write failure")
    monkeypatch.setattr(app.state.store, "fail_queued_import", cannot_mark)
    with TestClient(app) as client:
        response = upload(client, pdf_bytes())
        assert response.status_code == 200
        pid = response.json()["papers"][0]["id"]
        assert app.state.store.paper(pid)["status"] == "queued"
        assert app.state.store.jobs()[0]["status"] == "queued"
        assert len(list(app.state.store.files.glob("*.pdf"))) == 1
    restarted = create_app(data, run_jobs=True)
    with TestClient(restarted) as client:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if restarted.state.store.paper(pid)["status"] == "ready":
                break
            time.sleep(.02)
        assert restarted.state.store.paper(pid)["status"] == "ready"
        assert len(restarted.state.store.papers()) == 1
        assert client.get(f"/api/papers/{pid}/file").status_code == 200


def test_concurrent_deduplication_keeps_one_pdf_and_one_parse_job(workspace):
    client, app, _ = workspace
    content = pdf_bytes()
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: upload(client, content), range(6)))
    assert all(response.status_code == 200 for response in results)
    assert sum(len(response.json()["papers"]) for response in results) == 1
    assert sum(len(response.json()["duplicates"]) for response in results) == 5
    assert len(app.state.store.papers()) == len(app.state.store.jobs()) == 1
    assert len(list(app.state.store.files.glob("*.pdf"))) == 1


def test_mixed_batch_reports_every_file_and_continues_after_failure(workspace):
    client, app, _ = workspace
    response = client.post("/api/papers/upload", files=[
        ("files", ("good.pdf", pdf_bytes("First actual PDF"), "application/pdf")),
        ("files", ("bad.pdf", b"invalid content", "application/pdf")),
        ("files", ("later.pdf", pdf_bytes("Later actual PDF"), "application/pdf")),
    ])
    assert response.status_code == 207
    result = response.json()
    assert result["status"] == "partial"
    assert [paper["filename"] for paper in result["papers"]] == ["good.pdf", "later.pdf"]
    assert result["errors"] == [{"filename": "bad.pdf", "status": 400, "detail": "bad.pdf 不是有效 PDF"}]
    assert len(app.state.store.papers()) == len(app.state.store.jobs()) == 2
    assert len(list(app.state.store.files.glob("*.pdf"))) == 2


def test_all_invalid_batch_is_non_success_with_per_file_errors(workspace):
    client, app, _ = workspace
    response = client.post("/api/papers/upload", files=[
        ("files", ("broken.pdf", b"bad", "application/pdf")),
        ("files", ("wrong.txt", b"text", "text/plain")),
    ])
    assert response.status_code == 400
    assert response.json()["status"] == "failed"
    assert {error["filename"] for error in response.json()["errors"]} == {"broken.pdf", "wrong.txt"}
    assert app.state.store.papers() == [] and list(app.state.store.files.iterdir()) == []


def test_oversized_files_have_413_result_and_no_staging_leak(workspace, monkeypatch):
    client, app, _ = workspace
    monkeypatch.setattr(app_module, "MAX_UPLOAD", 10)
    response = upload(client, pdf_bytes())
    assert response.status_code == 413
    assert response.json()["errors"][0]["status"] == 413
    assert list(app.state.store.files.iterdir()) == []


def test_duplicate_and_invalid_file_is_partial_without_second_job(workspace):
    client, app, _ = workspace
    content = pdf_bytes()
    assert upload(client, content).status_code == 200
    result = client.post("/api/papers/upload", files=[
        ("files", ("copy.pdf", content, "application/pdf")),
        ("files", ("broken.pdf", b"bad", "application/pdf")),
    ])
    assert result.status_code == 207
    assert len(result.json()["duplicates"]) == len(result.json()["errors"]) == 1
    assert result.json()["papers"] == []
    assert len(app.state.store.jobs()) == 1


def test_sample_batch_keeps_originals_and_reports_invalid_paths_separately(workspace):
    client, app, samples = workspace
    original = pdf_bytes()
    (samples / "good.pdf").write_bytes(original)
    (samples / "bad.pdf").write_bytes(b"invalid")
    response = client.post("/api/samples/import", json={"filenames": ["../outside.pdf", "good.pdf", "bad.pdf"]})
    assert response.status_code == 207
    assert [p["filename"] for p in response.json()["papers"]] == ["good.pdf"]
    assert [e["filename"] for e in response.json()["errors"]] == ["../outside.pdf", "bad.pdf"]
    assert (samples / "good.pdf").read_bytes() == original
    assert (samples / "bad.pdf").read_bytes() == b"invalid"
    assert len(list(app.state.store.files.iterdir())) == 1
