"""Worker bookkeeping must survive storage errors, not just provider errors."""
import sqlite3
from types import SimpleNamespace
from uuid import uuid4

import pytest

from backend.storage import Store
from backend.tasks import TaskRunner
from backend.workspace_routes import create_workspace_router
from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_startup_cleanup_keeps_committed_pdfs_other_names_and_symlinks(tmp_path):
    store = Store(tmp_path / 'data')
    orphan = store.files / f'{uuid4().hex}.pdf'
    orphan.write_bytes(b'incomplete staging file')
    saved = store.files / f'{uuid4().hex}.pdf'
    saved.write_bytes(b'committed PDF')
    store.create_paper('saved.pdf', saved, 'saved', 'test', 1)
    unknown = store.files / 'manually-retained.pdf'
    unknown.write_bytes(b'keep other names')
    target = tmp_path / 'outside.pdf'
    target.write_bytes(b'keep symlink target')
    link = store.files / f'{uuid4().hex}.pdf'
    link.symlink_to(target)
    assert store.cleanup_interrupted_imports() == 1
    assert not orphan.exists()
    assert saved.read_bytes() == b'committed PDF'
    assert unknown.is_file() and link.is_symlink() and target.is_file()
    assert store.cleanup_interrupted_imports() == 0


def test_storage_outage_releases_worker_slot_so_recovered_job_can_resume(tmp_path, monkeypatch):
    store = Store(tmp_path / 'data')
    paper = store.create_paper('fixture.pdf', tmp_path / 'fixture.pdf', 'sample', 'test', 1)
    runner = TaskRunner(store, SimpleNamespace(get=lambda: {'api_key': ''}))
    job = store.ensure_job('parse', paper['id'])
    key = ('parse', paper['id'])
    runner.active[key] = job['id']
    runner.pending[key] = (job['id'], 'parse', paper['id'], {})
    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError('database is locked')
    with monkeypatch.context() as patch:
        patch.setattr(store, 'start_job', unavailable)
        patch.setattr(store, 'settle_job', unavailable)
        patch.setattr(store, 'fail_job', unavailable)
        patch.setattr(store, 'job', unavailable)
        try:
            runner._execute(job['id'], 'parse', paper['id'], {})
        except sqlite3.OperationalError:
            pass
    try:
        assert runner.active == {} and runner.pending == {}
        # Once the storage is available, an explicit retry can acquire a slot.
        submitted = []
        monkeypatch.setattr(runner.pool, 'submit', lambda *args: submitted.append(args))
        assert runner.submit(job['id'], 'parse', paper['id'], {})
        assert len(submitted) == 1
    finally:
        runner.close()


@pytest.mark.parametrize('action,expected', [('pause', 'paused'), ('cancel', 'cancelled'), ('resume', 'queued')])
def test_control_recovers_unowned_processing_job_after_storage_outage(tmp_path, monkeypatch, action, expected):
    store = Store(tmp_path / 'data')
    paper = store.create_paper('fixture.pdf', tmp_path / 'fixture.pdf', 'sample', 'test', 1)
    config = SimpleNamespace(get=lambda: {'api_key': ''})
    runner = TaskRunner(store, config)
    job = store.ensure_job('parse', paper['id'])
    runner.active[('parse', paper['id'])] = job['id']
    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError('database is locked')
    with monkeypatch.context() as patch:
        patch.setattr(runner, '_parse', unavailable)
        patch.setattr(store, 'settle_job', unavailable)
        patch.setattr(store, 'fail_job', unavailable)
        with pytest.raises(sqlite3.OperationalError):
            runner._execute(job['id'], 'parse', paper['id'], {})
    assert store.job(job['id'])['status'] == 'processing' and not runner.active
    app = FastAPI()
    app.include_router(create_workspace_router(store, runner, config, run_jobs=False))
    try:
        with TestClient(app) as client:
            result = client.post(f"/api/jobs/{job['id']}/{action}")
            assert result.status_code == 200
            assert result.json()['status'] == expected
        assert store.paper(paper['id'])['status'] == expected
    finally:
        runner.close()
