"""Backups use disposable workspaces, real SQLite transactions and local PDFs."""
import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import sys
import threading
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pymupdf
import pytest

from backend import backup
from backend.backup import BackupError, create_backup, restore_backup, verify_backup
from backend.storage import Store
from backend.tasks import TaskRunner


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sql_dump(store):
    with store.connect() as db:
        return "\n".join(db.iterdump())


@pytest.fixture
def workspace(tmp_path):
    store = Store(tmp_path / "data")
    pdf = store.files / "original.pdf"
    with pymupdf.open() as doc:
        for number in (1, 2):
            page = doc.new_page()
            page.insert_text((72, 72), f"Snapshot paper page {number}")
        doc.save(pdf)
    paper = store.create_paper("original.pdf", pdf, digest(pdf), "test", 2)
    for number in (1, 2):
        store.add_page(paper["id"], {"page": number, "width": 595, "height": 842, "page_label": str(number),
            "warnings": [], "blocks": [{"text": f"Snapshot paper page {number}", "bbox": [72, 60, 300, 75],
            "kind": "paragraph", "section": "Introduction", "order_index": 0}]})
    store.update_paper(paper["id"], status="ready", tags=["备份测试"], title="Snapshot title", title_custom=True)
    evidence = store.blocks([paper["id"]])[0]
    session = store.create_session("保留来源", [paper["id"]], [
        {"id": "u1", "role": "user", "content": "Question", "paper_ids": [paper["id"]], "sources": [], "status": "completed"},
        {"id": "a1", "role": "assistant", "content": "Answer", "paper_ids": [paper["id"]], "sources": [evidence], "status": "completed", "request_id": "req1"}])
    note = store.create_note("第一版", "Version one", [paper["id"]], [evidence], "review")
    note = store.save_note(note["id"], 1, "第二版", "Version two", [paper["id"]], [evidence], "review")
    store.save_reading_position(paper["id"], 0, 2, 0.6)
    store.save_translation(paper["id"], 1, "zh", "Snapshot paper page 1", "已完成译文")
    jobs = {}
    for state in ("queued", "processing", "pause_requested", "paused", "cancel_requested", "done", "error", "cancelled"):
        job_id = store.create_job("translate", paper["id"], {"target_language": "en", "start_page": 2, "end_page": 2})
        store.update_job(job_id, status=state)
        jobs[state] = job_id
    store.save_translation_chunk(paper["id"], 2, "en", "Snapshot paper page 2", 0, "Saved chunk", jobs["processing"])
    store.checkpoint_job(jobs["processing"], 0, 50, "saved", current_page=2, completed_chunks=1)
    secret = "backup-must-never-copy-this-api-key"
    (store.directory / "settings.json").write_text(json.dumps({"api_key": secret}))
    (store.directory / ".env").write_text("LLM_API_KEY=" + secret)
    (store.directory / "unreferenced.txt").write_text("not workspace content")
    return {"store": store, "paper": paper["id"], "pdf": pdf, "session": session["id"], "note": note["id"], "jobs": jobs, "secret": secret}


def rewrite_archive(source, destination, mutate):
    with zipfile.ZipFile(source) as archive:
        contents = {name: archive.read(name) for name in archive.namelist()}
    mutate(contents)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in contents.items():
            archive.writestr(name, content)


def test_complete_round_trip_preserves_workspace_and_pauses_jobs(workspace, tmp_path, monkeypatch):
    store, paper = workspace["store"], workspace["paper"]
    before_sql, before_pdf = sql_dump(store), workspace["pdf"].read_bytes()
    main_hash = digest(store.path)
    archive = tmp_path / "workspace.zip"
    report = create_backup(store.directory, archive)
    assert report["paper_count"] == 1 and report["file_count"] == 3
    assert not report["settings_included"]
    assert sql_dump(store) == before_sql and workspace["pdf"].read_bytes() == before_pdf
    assert digest(store.path) == main_hash
    assert verify_backup(archive, report["archive_sha256"])["ok"]
    with zipfile.ZipFile(archive) as content:
        assert not any("settings" in name or ".env" in name for name in content.namelist())
        assert all(workspace["secret"].encode() not in content.read(name) for name in content.namelist())
        assert "unreferenced.txt" not in content.namelist()
    target = tmp_path / "restored"
    restored_report = restore_backup(archive, target, report["archive_sha256"])
    assert restored_report["active_tasks_paused"]
    restored = Store(target)
    assert restored.session(workspace["session"]) == store.session(workspace["session"])
    assert restored.note(workspace["note"]) == store.note(workspace["note"])
    assert restored.note_version(workspace["note"], 1) == store.note_version(workspace["note"], 1)
    assert restored.reading_position(paper) == store.reading_position(paper)
    assert restored.blocks([paper]) == store.blocks([paper])
    assert restored.translation(paper, 1, "zh", "Snapshot paper page 1") == "已完成译文"
    assert restored.translation_chunk(paper, 2, "en", "Snapshot paper page 2", 0) == "Saved chunk"
    for state, job_id in workspace["jobs"].items():
        job = restored.job(job_id)
        expected = "paused" if state in {"queued", "processing", "pause_requested", "paused"} else "cancelled" if state == "cancel_requested" else state
        assert job["status"] == expected
        assert job["options"] == store.job(job_id)["options"]
        assert job["result"] == store.job(job_id)["result"]
    saved = restored.paper(paper, private=True)
    expected_pdf = target / "files" / f"{saved['sha256']}.pdf"
    assert saved["path"] == str(expected_pdf.resolve()) and expected_pdf.read_bytes() == before_pdf
    assert saved["status"] == "ready" and saved["title_custom"]
    with pymupdf.open(saved["path"]) as pdf:
        assert len(pdf) == 2 and "Snapshot paper" in pdf[0].get_text()
    assert not (target / "settings.json").exists() and not (target / ".env").exists()
    runner = TaskRunner(restored, SimpleNamespace(get=lambda: {"api_key": ""}))
    submitted = []
    monkeypatch.setattr(runner, "submit", lambda *args: submitted.append(args))
    try:
        runner.recover()
        assert submitted == []
    finally:
        runner.close()


def test_restore_pauses_parsing_and_preserves_checkpoint(workspace, tmp_path):
    store, paper = workspace["store"], workspace["paper"]
    job = store.ensure_job("parse", paper)
    store.start_job(job["id"])
    store.checkpoint_job(job["id"], 1, 50, "Page one")
    archive = tmp_path / "parse.zip"
    create_backup(store.directory, archive)
    target = tmp_path / "empty"
    target.mkdir()
    restore_backup(archive, target)
    restored = Store(target)
    assert restored.paper(paper)["status"] == "paused"
    assert restored.job(job["id"])["status"] == "paused"
    assert restored.job(job["id"])["last_page"] == 1
    assert restored.completed_pages(paper) == {1, 2}


def test_restore_preserves_requested_parse_cancellation(workspace, tmp_path):
    store, paper = workspace["store"], workspace["paper"]
    job = store.ensure_job("parse", paper)
    store.start_job(job["id"])
    store.control_job(job["id"], "cancel")
    assert store.paper(paper)["status"] == "processing"
    archive = tmp_path / "cancel.zip"
    create_backup(store.directory, archive)
    restore_backup(archive, tmp_path / "restored")
    restored = Store(tmp_path / "restored")
    assert restored.paper(paper)["status"] == "cancelled"
    assert restored.job(job["id"])["status"] == "cancelled"


def test_restore_preserves_destination_populated_during_verification(workspace, tmp_path, monkeypatch):
    from contextlib import contextmanager
    archive = tmp_path / "original.zip"
    create_backup(workspace["store"].directory, archive)
    target = tmp_path / "target"
    target.mkdir()
    original_verified = backup._verified
    @contextmanager
    def verified(*args, **kwargs):
        with original_verified(*args, **kwargs) as contents:
            (target / "concurrent-file.txt").write_text("keep this file")
            yield contents
    monkeypatch.setattr(backup, "_verified", verified)
    with pytest.raises(BackupError):
        restore_backup(archive, target)
    assert (target / "concurrent-file.txt").read_text() == "keep this file"
    assert not (target / backup.DATABASE).exists()
    assert not list(tmp_path.glob(".research-restore-*"))


def test_live_wal_backup_contains_committed_transactions(workspace, tmp_path):
    store = workspace["store"]
    with store.connect() as live:
        live.execute("PRAGMA wal_autocheckpoint=0")
        live.execute("UPDATE research_notes SET content='Committed in WAL' WHERE id=?", (workspace["note"],))
        live.commit()
        assert Path(str(store.path) + "-wal").stat().st_size > 0
        archive = tmp_path / "wal.zip"
        create_backup(store.directory, archive)
        # Opening a backup must never commit another connection's transaction.
        live.execute("UPDATE research_notes SET content='Uncommitted' WHERE id=?", (workspace["note"],))
        create_backup(store.directory, tmp_path / "uncommitted.zip")
        live.rollback()
    restore_backup(archive, tmp_path / "restored-wal")
    restore_backup(tmp_path / "uncommitted.zip", tmp_path / "restored-uncommitted")
    assert Store(tmp_path / "restored-wal").note(workspace["note"])["content"] == "Committed in WAL"
    assert Store(tmp_path / "restored-uncommitted").note(workspace["note"])["content"] == "Committed in WAL"
    assert store.note(workspace["note"])["content"] == "Committed in WAL"


def test_concurrent_real_sqlite_writer_keeps_note_and_versions_consistent(workspace, tmp_path):
    store, note_id = workspace["store"], workspace["note"]
    started, stop = threading.Event(), threading.Event()
    failures, revisions = [], []
    def writer():
        try:
            while not stop.is_set():
                note = store.note(note_id)
                next_revision = note["revision"] + 1
                store.save_note(note_id, note["revision"], f"Revision {next_revision}", f"content-{next_revision}", [], [], "note")
                revisions.append(next_revision)
                started.set()
        except BaseException as exc:
            failures.append(exc)
    thread = threading.Thread(target=writer)
    thread.start()
    try:
        assert started.wait(5)
        archive = tmp_path / "concurrent.zip"
        create_backup(store.directory, archive)
    finally:
        stop.set()
        thread.join(5)
    assert not thread.is_alive() and not failures and revisions
    restore_backup(archive, tmp_path / "restored")
    restored = Store(tmp_path / "restored")
    note = restored.note(note_id)
    assert note["content"] == f"content-{note['revision']}"
    assert len(note["versions"]) == note["revision"]
    latest = restored.note_version(note_id, note["revision"])
    assert latest["content"] == note["content"] and latest["title"] == note["title"]


@pytest.mark.parametrize("moment", ["copy", "final_check"])
def test_concurrent_real_pdf_deletion_fails_without_publishing(workspace, tmp_path, monkeypatch, moment):
    store, pdf = workspace["store"], workspace["pdf"]
    requested, removed = threading.Event(), threading.Event()
    failures = []
    original_copy, original_recheck = backup._copy_and_hash, backup._recheck_sources
    pdf_identity = pdf.stat().st_ino
    def delete():
        try:
            assert requested.wait(5)
            store.delete_paper(workspace["paper"])
            pdf.unlink()
        except BaseException as exc:
            failures.append(exc)
        finally:
            removed.set()
    def copy(source, *args, **kwargs):
        result = original_copy(source, *args, **kwargs)
        if moment == "copy" and hasattr(source, "fileno") and os.fstat(source.fileno()).st_ino == pdf_identity:
            requested.set()
            assert removed.wait(5)
        return result
    def recheck(*args):
        if moment == "final_check":
            requested.set()
            assert removed.wait(5)
        return original_recheck(*args)
    monkeypatch.setattr(backup, "_copy_and_hash", copy)
    monkeypatch.setattr(backup, "_recheck_sources", recheck)
    thread = threading.Thread(target=delete)
    thread.start()
    output = tmp_path / "deleted.zip"
    try:
        with pytest.raises(BackupError):
            create_backup(store.directory, output)
    finally:
        requested.set()
        thread.join(5)
    assert not failures and not thread.is_alive()
    assert not output.exists() and not list(tmp_path.glob(".research-backup-*"))


@pytest.mark.parametrize("kind", ["external", "symlink", "hardlink", "missing", "changed"])
def test_source_file_boundary_and_hash_failures(workspace, tmp_path, kind):
    store, pdf = workspace["store"], workspace["pdf"]
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(pdf.read_bytes())
    if kind == "external":
        with store.connect() as db:
            db.execute("UPDATE papers SET path=?", (str(outside),))
    elif kind == "symlink":
        pdf.unlink()
        pdf.symlink_to(outside)
    elif kind == "hardlink":
        pdf.unlink()
        os.link(outside, pdf)
    elif kind == "missing":
        pdf.unlink()
    else:
        pdf.write_bytes(pdf.read_bytes() + b"changed")
    with pytest.raises(BackupError):
        create_backup(store.directory, tmp_path / "bad.zip")
    assert not (tmp_path / "bad.zip").exists()


@pytest.mark.parametrize("kind", ["pdf", "database", "traversal", "extra_secret", "manifest_path", "duplicate", "symlink"])
def test_archive_tampering_rejected_before_restore(workspace, tmp_path, kind):
    original, changed = tmp_path / "original.zip", tmp_path / "changed.zip"
    create_backup(workspace["store"].directory, original)
    def alter(contents):
        pdf = next(name for name in contents if name.endswith(".pdf"))
        if kind in {"pdf", "database"}:
            key = pdf if kind == "pdf" else backup.DATABASE
            contents[key] = contents[key][:-1] + bytes([contents[key][-1] ^ 1])
        elif kind == "traversal":
            contents["../escape.txt"] = b"escape"
        elif kind == "extra_secret":
            contents["settings.json"] = b'{"api_key":"secret"}'
        elif kind == "manifest_path":
            manifest = json.loads(contents[backup.MANIFEST])
            manifest["papers"][0]["path"] = "/outside.pdf"
            contents[backup.MANIFEST] = json.dumps(manifest).encode()
    rewrite_archive(original, changed, alter)
    if kind == "duplicate":
        with pytest.warns(UserWarning), zipfile.ZipFile(changed, "a") as archive:
            archive.writestr(backup.MANIFEST, b"{}")
    elif kind == "symlink":
        with zipfile.ZipFile(changed) as archive:
            contents = {name: archive.read(name) for name in archive.namelist()}
        with zipfile.ZipFile(changed, "w") as archive:
            for name, content in contents.items():
                info = zipfile.ZipInfo(name)
                info.create_system = 3
                info.external_attr = (stat.S_IFLNK | 0o777) << 16 if name.endswith(".pdf") else (stat.S_IFREG | 0o600) << 16
                archive.writestr(info, content)
    with pytest.raises(BackupError):
        verify_backup(changed)
    with pytest.raises(BackupError):
        restore_backup(changed, tmp_path / "restored")
    assert not (tmp_path / "restored").exists() and not (tmp_path / "escape.txt").exists()


def test_database_external_path_rejected_even_with_updated_manifest_hash(workspace, tmp_path):
    original, changed = tmp_path / "original.zip", tmp_path / "changed.zip"
    create_backup(workspace["store"].directory, original)
    def alter(contents):
        db_path = tmp_path / "malicious.sqlite3"
        db_path.write_bytes(contents[backup.DATABASE])
        with sqlite3.connect(db_path) as db:
            db.execute("UPDATE papers SET path='/outside.pdf'")
        contents[backup.DATABASE] = db_path.read_bytes()
        manifest = json.loads(contents[backup.MANIFEST])
        manifest["database"]["sha256"] = digest(db_path)
        manifest["database"]["size"] = db_path.stat().st_size
        contents[backup.MANIFEST] = json.dumps(manifest).encode()
    rewrite_archive(original, changed, alter)
    with pytest.raises(BackupError, match="引用与清单不一致"):
        verify_backup(changed)


def test_pinned_archive_hash_detects_rewritten_manifest(workspace, tmp_path):
    original, changed = tmp_path / "original.zip", tmp_path / "changed.zip"
    report = create_backup(workspace["store"].directory, original)
    def alter(contents):
        manifest = json.loads(contents[backup.MANIFEST])
        manifest["created_at"] = "Changed but internally consistent"
        contents[backup.MANIFEST] = json.dumps(manifest).encode()
    rewrite_archive(original, changed, alter)
    assert verify_backup(changed)["ok"]
    with pytest.raises(BackupError, match="指定摘要不一致"):
        verify_backup(changed, report["archive_sha256"])
    with pytest.raises(BackupError):
        restore_backup(changed, tmp_path / "restored", report["archive_sha256"])


def test_database_trigger_is_rejected_before_restore_updates(workspace, tmp_path):
    original, changed = tmp_path / "original.zip", tmp_path / "trigger.zip"
    create_backup(workspace["store"].directory, original)
    def alter(contents):
        path = tmp_path / "trigger.sqlite3"
        path.write_bytes(contents[backup.DATABASE])
        with sqlite3.connect(path) as db:
            db.execute("CREATE TRIGGER unwanted AFTER UPDATE ON papers BEGIN DELETE FROM chat_sessions; END")
        contents[backup.DATABASE] = path.read_bytes()
        manifest = json.loads(contents[backup.MANIFEST])
        manifest["database"].update(sha256=digest(path), size=path.stat().st_size)
        contents[backup.MANIFEST] = json.dumps(manifest).encode()
    rewrite_archive(original, changed, alter)
    with pytest.raises(BackupError, match="触发器"):
        restore_backup(changed, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


def test_existing_targets_and_source_nested_output_are_rejected(workspace, tmp_path):
    archive = tmp_path / "original.zip"
    create_backup(workspace["store"].directory, archive)
    original_hash = digest(archive)
    with pytest.raises(BackupError):
        create_backup(workspace["store"].directory, archive)
    assert digest(archive) == original_hash
    with pytest.raises(BackupError):
        create_backup(workspace["store"].directory, workspace["store"].directory / "nested.zip")
    for target in (workspace["store"].directory, archive):
        with pytest.raises(BackupError):
            restore_backup(archive, target)
    link = tmp_path / "linked"
    link.symlink_to(workspace["store"].directory, target_is_directory=True)
    with pytest.raises(BackupError):
        restore_backup(archive, link)


def test_cli_create_verify_restore_and_chinese_errors(workspace, tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts" / "backup.py"
    def run(*args):
        return subprocess.run([sys.executable, str(script), *map(str, args)], capture_output=True, text=True, timeout=20)
    assert "恢复" in run("--help").stdout
    archive = tmp_path / "cli.zip"
    created = run("create", "--data", workspace["store"].directory, "--output", archive)
    assert created.returncode == 0, created.stderr
    sha = json.loads(created.stdout)["archive_sha256"]
    assert run("verify", archive, "--sha256", sha).returncode == 0
    assert run("restore", archive, "--target", tmp_path / "restored").returncode == 0
    failed = run("restore", archive, "--target", workspace["store"].directory)
    assert failed.returncode == 1 and "拒绝覆盖" in failed.stderr and "Traceback" not in failed.stderr
