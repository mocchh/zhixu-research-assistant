"""Task interruption tests use events and fake parsers/providers, never OCR or APIs."""
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from backend import documents, intelligence
from backend.storage import Store, TaskConflict
from backend.tasks import TaskRunner, page_text, translation_chunks


def page(number, text=None):
    return {"page": number, "width": 600, "height": 800, "blocks": [
        {"text": text or f"Source page {number}", "bbox": [0, 0, 100, 20], "section": "", "kind": "paragraph", "order_index": 0}
    ], "warnings": []}


@pytest.fixture
def task_workspace(tmp_path):
    store = Store(tmp_path / "data")
    paper = store.create_paper("sample.pdf", tmp_path / "sample.pdf", "one", "test", 3)
    config = SimpleNamespace(get=lambda: {"api_key": "fake-only"})
    runner = TaskRunner(store, config)
    yield store, runner, paper["id"]
    runner.close()


def start_thread(runner, job):
    thread = threading.Thread(target=runner._execute, args=(job["id"], job["kind"], job["paper_id"], job["options"]))
    thread.start()
    return thread


def finish_thread(thread, release):
    release.set()
    thread.join(5)
    assert not thread.is_alive(), "fake task did not reach a checkpoint"


@pytest.mark.parametrize("action", ["pause", "cancel", "delete"])
def test_parse_inflight_page_checkpoint_and_no_resurrection(task_workspace, monkeypatch, action):
    store, runner, paper = task_workspace
    entered, release = threading.Event(), threading.Event()
    starts, parsed = [], []
    monkeypatch.setattr(documents, "inspect_pdf", lambda *a: {"title": "Real title", "page_count": 3, "warnings": []})
    def pages(path, start_page=1):
        starts.append(start_page)
        for number in range(start_page, 4):
            parsed.append(number)
            if number == 1:
                entered.set()
                assert release.wait(5)
            yield page(number)
    monkeypatch.setattr(documents, "iter_pages", pages)
    job = store.ensure_job("parse", paper)
    thread = start_thread(runner, job)
    try:
        assert entered.wait(5)
        if action == "delete":
            store.delete_paper(paper)
        else:
            assert runner.control(job["id"], action, run=False)["status"] == action + "_requested"
    finally:
        finish_thread(thread, release)
    assert parsed == [1]
    saved = store.job(job["id"])
    if action == "delete":
        assert saved["status"] == "cancelled"
        assert store.paper(paper) is None and store.completed_pages(paper) == set()
        return
    assert store.completed_pages(paper) == {1}
    assert saved["last_page"] == 1
    assert saved["status"] == ("paused" if action == "pause" else "cancelled")
    original_ids = [b["id"] for b in store.blocks([paper], page=1)]
    if action == "pause":
        runner.control(job["id"], "resume", run=False)
        runner._execute(job["id"], "parse", paper, {})
        assert starts == [1, 2]
        assert parsed == [1, 2, 3]
        assert store.job(job["id"])["status"] == "done"
        assert store.paper(paper)["status"] == "ready"
        assert [b["id"] for b in store.blocks([paper], page=1)] == original_ids


@pytest.mark.parametrize("action", ["pause", "cancel"])
def test_translation_chunk_pause_cancel_and_restart_reuses_outputs(task_workspace, monkeypatch, action):
    store, runner, paper = task_workspace
    original = "a" * 6500 + "b" * 6500 + "c" * 1000
    for n in range(1, 4):
        store.add_page(paper, page(n, original if n == 2 else None))
    entered, release = threading.Event(), threading.Event()
    calls = []
    async def translate(text, language, settings):
        calls.append((text, language))
        if len(calls) == 1:
            entered.set()
            assert release.wait(5)
        return f"translated-{text[0]}-{language}"
    monkeypatch.setattr(intelligence, "translate_text", translate)
    job = runner.start_translation(paper, "en", 2, 2, run=False)
    thread = start_thread(runner, job)
    try:
        assert entered.wait(5)
        result = runner.control(job["id"], action, run=False)
        assert result["status"] == action + "_requested"
        assert "服务方" in result["message"]
    finally:
        finish_thread(thread, release)
    saved = store.job(job["id"])
    assert saved["status"] == ("paused" if action == "pause" else "cancelled")
    assert len(calls) == 1 and saved["last_page"] == 0
    assert saved["result"]["completed_chunks"] == 1
    assert store.translation_chunk(paper, 2, "en", original, 0) == "translated-a-en"
    assert store.translation(paper, 2, "en", original) is None
    assert runner.translation_estimate(paper, "en", 2, 2)["pending_characters"] == 7500
    restored = Store(store.directory)
    resumed = TaskRunner(restored, runner.config)
    try:
        if action == "pause":
            job = resumed.control(job["id"], "resume", run=False)
        else:
            job = resumed.start_translation(paper, "en", 2, 2, run=False)
        resumed._execute(job["id"], "translate", paper, {})
    finally:
        resumed.close()
    assert [text[0] for text, _ in calls] == ["a", "b", "c"]
    assert all(lang == "en" for _, lang in calls)
    saved = restored.job(job["id"])
    assert saved["status"] == "done" and saved["last_page"] == 2
    assert saved["options"] == {"target_language": "en", "start_page": 2, "end_page": 2}
    assert restored.translation(paper, 2, "en", original) == "translated-a-en\n\ntranslated-b-en\n\ntranslated-c-en"
    assert restored.translation(paper, 1, "en", page_text(store.page(paper, 1))) is None


def test_translation_failure_resumes_language_and_page_range(task_workspace, monkeypatch):
    store, runner, paper = task_workspace
    for n in range(1, 4):
        store.add_page(paper, page(n))
    calls = []
    async def translate(text, language, settings):
        calls.append((text, language))
        if len(calls) == 2:
            raise intelligence.ConfigError("Mock provider timeout")
        return "translated " + text
    monkeypatch.setattr(intelligence, "translate_text", translate)
    job = runner.start_translation(paper, "en", 2, 3, run=False)
    runner._execute(job["id"], "translate", paper, {})
    failed = store.job(job["id"])
    assert failed["status"] == "error" and failed["last_page"] == 2
    assert failed["options"]["end_page"] == 3
    runner.control(job["id"], "resume", run=False)
    runner._execute(job["id"], "translate", paper, {})
    assert calls == [("Source page 2", "en"), ("Source page 3", "en"), ("Source page 3", "en")]
    assert store.job(job["id"])["status"] == "done"
    assert store.job(job["id"])["result"]["translated_pages"] == 2


def test_deleting_paper_during_provider_call_does_not_restore_cache(task_workspace, monkeypatch):
    store, runner, paper = task_workspace
    store.add_page(paper, page(1))
    entered, release = threading.Event(), threading.Event()
    async def translate(*args):
        entered.set()
        assert release.wait(5)
        return "response after deletion"
    monkeypatch.setattr(intelligence, "translate_text", translate)
    job = runner.start_translation(paper, "zh", 1, 1, run=False)
    thread = start_thread(runner, job)
    try:
        assert entered.wait(5)
        store.delete_paper(paper)
    finally:
        finish_thread(thread, release)
    assert store.job(job["id"])["status"] == "cancelled"
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM translation_chunks").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM translations").fetchone()[0] == 0


def test_changed_source_during_translation_preserves_new_cache_and_finishes_job(task_workspace, monkeypatch):
    store, runner, paper = task_workspace
    store.add_page(paper, page(1, "Original source"))
    entered, release = threading.Event(), threading.Event()

    async def translate(*args):
        entered.set()
        assert release.wait(5)
        return "Late old translation"

    monkeypatch.setattr(intelligence, "translate_text", translate)
    job = runner.start_translation(paper, "zh", 1, 1, run=False)
    thread = start_thread(runner, job)
    try:
        assert entered.wait(5)
        store.add_page(paper, page(1, "Corrected source"))
        assert store.save_translation(paper, 1, "zh", "Corrected source", "Current translation")
    finally:
        finish_thread(thread, release)
    assert store.translation(paper, 1, "zh", "Corrected source") == "Current translation"
    assert store.translation(paper, 1, "zh", "Original source") is None
    saved = store.job(job["id"])
    assert saved["status"] == "error" and "原文已更新" in saved["message"]
    assert saved["last_page"] == 0


def test_recovery_preserves_requested_controls_and_options(tmp_path, monkeypatch):
    store = Store(tmp_path / "data")
    options = {"target_language": "en", "start_page": 2, "end_page": 4}
    jobs = {}
    for n, state in enumerate(["processing", "pause_requested", "cancel_requested", "paused"]):
        paper = store.create_paper(f"{n}.pdf", tmp_path / f"{n}.pdf", str(n), "test", 5)
        job = store.ensure_job("translate", paper["id"], options)
        store.update_job(job["id"], status=state)
        jobs[state] = job["id"]
    runner = TaskRunner(Store(store.directory), SimpleNamespace(get=lambda: {"api_key": "fake"}))
    submitted = []
    monkeypatch.setattr(runner, "submit", lambda *args: submitted.append(args))
    try:
        runner.recover()
        runner.recover()
        assert len(submitted) == 1 and submitted[0][0] == jobs["processing"]
        assert submitted[0][3] == options
        assert store.job(jobs["pause_requested"])["status"] == "paused"
        assert store.job(jobs["cancel_requested"])["status"] == "cancelled"
        assert store.job(jobs["paused"])["status"] == "paused"
    finally:
        runner.close()


def test_shutdown_saves_current_chunk_and_queues_resume(task_workspace, monkeypatch):
    store, runner, paper = task_workspace
    original = "x" * 14000
    store.add_page(paper, page(1, original))
    entered, release = threading.Event(), threading.Event()
    async def translate(*args):
        entered.set()
        assert release.wait(5)
        return "saved chunk"
    monkeypatch.setattr(intelligence, "translate_text", translate)
    job = runner.start_translation(paper, "zh", 1, 1, run=False)
    thread = start_thread(runner, job)
    try:
        assert entered.wait(5)
        runner.close()
    finally:
        finish_thread(thread, release)
    assert store.job(job["id"])["status"] == "queued"
    assert store.translation_chunk(paper, 1, "zh", original, 0) == "saved chunk"
    assert store.translation_chunk(paper, 1, "zh", original, 1) is None


def test_concurrent_ensure_and_resume_conflict(task_workspace):
    store, runner, paper = task_workspace
    old = store.ensure_job("parse", paper)
    store.update_job(old["id"], status="error")
    with ThreadPoolExecutor(max_workers=6) as pool:
        jobs = list(pool.map(lambda _: store.ensure_job("parse", paper), range(12)))
    assert len({job["id"] for job in jobs}) == 1
    with pytest.raises(TaskConflict):
        runner.control(old["id"], "resume", run=False)
    paused = runner.control(jobs[0]["id"], "pause", run=False)
    assert store.ensure_job("parse", paper)["id"] == paused["id"]
    assert paused["status"] == "paused"
    with pytest.raises(TaskConflict):
        runner.control(old["id"], "resume", run=False)


def test_concurrent_translation_languages_create_only_one_active_job(task_workspace):
    store, runner, paper = task_workspace
    store.add_page(paper, page(1))
    def start(language):
        try:
            return runner.start_translation(paper, language, 1, 1, run=False)
        except TaskConflict:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(start, ["zh", "en"]))
    assert sum(result is not None for result in outcomes) == 1
    assert len(store.jobs()) == 1
    active = next(result for result in outcomes if result is not None)
    assert store.job(active["id"])["options"] == active["options"]


def test_finish_cannot_overwrite_requested_cancellation(task_workspace):
    store, runner, paper = task_workspace
    job = store.ensure_job("parse", paper)
    assert store.start_job(job["id"])
    runner.control(job["id"], "cancel", run=False)
    assert not store.finish_job(job["id"], "done", {}, paper_status="ready")
    assert not store.update_job(job["id"], status="done")
    assert not store.fail_job(job["id"], "late error")
    assert store.settle_job(job["id"])
    assert store.job(job["id"])["status"] == "cancelled"
    assert store.paper(paper)["status"] == "cancelled"


def test_resume_before_previous_worker_finally_is_not_lost(task_workspace, monkeypatch):
    store, runner, paper = task_workspace
    checkpoint, release, completed = threading.Event(), threading.Event(), threading.Event()
    calls = []
    def parse(job_id, paper_id):
        calls.append(job_id)
        if len(calls) == 1:
            store.control_job(job_id, "pause")
            assert store.settle_job(job_id)
            checkpoint.set()
            assert release.wait(5)
        else:
            store.finish_job(job_id, "done", {}, paper_status="ready")
            completed.set()
    monkeypatch.setattr(runner, "_parse", parse)
    job = store.ensure_job("parse", paper)
    assert runner.submit(job["id"], "parse", paper)
    try:
        assert checkpoint.wait(5)
        resumed = runner.control(job["id"], "resume", run=True)
        assert resumed["status"] == "queued"
    finally:
        release.set()
    assert completed.wait(5)
    runner.pool.shutdown(wait=True)
    assert calls == [job["id"], job["id"]]
    assert store.job(job["id"])["status"] == "done"


def test_chunk_split_preserves_all_characters():
    original = "a" * 4000 + "\n" + "b" * 9000 + "\n" + "c" * 100
    chunks = translation_chunks(original)
    assert "".join(chunks) == original
    assert all(0 < len(chunk) <= 6500 for chunk in chunks)
