"""Real SQLite interleavings between extracted metadata and user/task changes."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import pytest

from backend import documents
from backend.storage import Store
from backend.tasks import TaskRunner


@pytest.fixture
def parsing_workspace(tmp_path, monkeypatch):
    store = Store(tmp_path / "workspace")
    path = store.files / "source.pdf"
    path.write_bytes(b"original PDF bytes retained")
    paper = store.create_paper(path.name, path, "metadata-test", "test", 1)
    store.update_paper(paper["id"], title="Before extraction", tags=["before"], status="ready")
    metadata = {"title": "Extracted title", "authors": ["Extracted Author"], "abstract": "Extracted summary",
                "arxiv_id": None, "version": None, "page_count": 1, "tags": ["extracted"], "warnings": []}
    page = {"page": 1, "width": 100, "height": 100, "blocks": [{"text": "Parsed body", "bbox": [1, 2, 80, 20]}]}
    monkeypatch.setattr(documents, "inspect_pdf", lambda *args: dict(metadata))
    monkeypatch.setattr(documents, "iter_pages", lambda *args, **kwargs: iter([page] if kwargs.get("start_page", 1) <= 1 else []))
    runner = TaskRunner(store, SimpleNamespace(get=lambda: {"api_key": ""}))
    yield store, runner, paper["id"], path, metadata
    runner.close()


def blocked_metadata_write(monkeypatch, store):
    reached, release = Event(), Event()
    original = store.apply_extracted_metadata

    def delayed(*args):
        reached.set()
        assert release.wait(5), "test did not release the parser"
        return original(*args)

    monkeypatch.setattr(store, "apply_extracted_metadata", delayed)
    return reached, release


def test_user_edits_after_inspection_survive_parser_metadata_commit(parsing_workspace, monkeypatch):
    store, runner, pid, path, _ = parsing_workspace
    job = store.create_reparse(pid)
    reached, release = blocked_metadata_write(monkeypatch, store)
    with ThreadPoolExecutor(max_workers=1) as worker:
        parsing = worker.submit(runner._execute, job["id"], "parse", pid, {})
        try:
            assert reached.wait(5)
            store.update_paper(pid, title="User title while parser runs", title_custom=True,
                               tags=["user-tag"], tags_custom=True)
        finally:
            release.set()
        parsing.result(timeout=5)
    saved = store.paper(pid, private=True)
    assert saved["title"] == "User title while parser runs"
    assert saved["tags"] == ["user-tag"]
    assert saved["title_custom"] and saved["tags_custom"]
    assert saved["authors"] == ["Extracted Author"] and saved["abstract"] == "Extracted summary"
    assert saved["status"] == "ready" and store.job(job["id"])["status"] == "done"
    assert path.read_bytes() == b"original PDF bytes retained"


@pytest.mark.parametrize("action", ["pause", "cancel", "delete"])
def test_controls_before_metadata_commit_prevent_old_task_write(parsing_workspace, monkeypatch, action):
    store, runner, pid, path, _ = parsing_workspace
    job = store.create_reparse(pid)
    reached, release = blocked_metadata_write(monkeypatch, store)
    with ThreadPoolExecutor(max_workers=1) as worker:
        parsing = worker.submit(runner._execute, job["id"], "parse", pid, {})
        try:
            assert reached.wait(5)
            if action == "delete":
                store.delete_paper(pid)
            else:
                store.control_job(job["id"], action)
        finally:
            release.set()
        parsing.result(timeout=5)
    if action == "delete":
        assert store.paper(pid) is None
    else:
        saved = store.paper(pid)
        assert saved["title"] == "Before extraction" and saved["tags"] == ["before"]
        assert saved["authors"] == [] and saved["processed_pages"] == 0
    assert store.job(job["id"])["status"] == ("paused" if action == "pause" else "cancelled")
    assert store.blocks([pid]) == []
    # Store deletion does not remove bytes; the HTTP delete handler owns that.
    assert path.read_bytes() == b"original PDF bytes retained"


def test_paused_metadata_stage_survives_restart_then_preserves_user_edits(parsing_workspace, monkeypatch):
    store, runner, pid, _, _ = parsing_workspace
    job = store.create_reparse(pid)
    reached, release = blocked_metadata_write(monkeypatch, store)
    with ThreadPoolExecutor(max_workers=1) as worker:
        parsing = worker.submit(runner._execute, job["id"], "parse", pid, {})
        try:
            assert reached.wait(5)
            store.control_job(job["id"], "pause")
        finally:
            release.set()
        parsing.result(timeout=5)
    runner.close()
    reopened = Store(store.directory)
    restarted = TaskRunner(reopened, SimpleNamespace(get=lambda: {"api_key": ""}))
    try:
        restarted.recover()
        assert reopened.job(job["id"])["status"] == "paused"
        reopened.update_paper(pid, title="Edited while paused", title_custom=True,
                              tags=["resume-tag"], tags_custom=True)
        restarted.control(job["id"], "resume", run=False)
        restarted._execute(job["id"], "parse", pid, {})
        saved = reopened.paper(pid)
        assert saved["title"] == "Edited while paused" and saved["tags"] == ["resume-tag"]
        assert saved["processed_pages"] == 1 and saved["status"] == "ready"
        assert reopened.job(job["id"])["status"] == "done"
    finally:
        restarted.close()


def test_metadata_write_rejects_wrong_job_kind_or_document(parsing_workspace):
    store, _, pid, path, metadata = parsing_workspace
    translation = store.create_job("translate", pid)
    assert store.start_job(translation)
    assert not store.apply_extracted_metadata(pid, translation, metadata)
    other = store.create_paper("other.pdf", path, "other-hash", "test", 1)
    parse = store.create_job("parse", other["id"])
    assert store.start_job(parse)
    assert not store.apply_extracted_metadata(pid, parse, metadata)
    assert store.paper(pid)["title"] == "Before extraction"
