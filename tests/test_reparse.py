import hashlib
from concurrent.futures import ThreadPoolExecutor

import pymupdf
import pytest
from fastapi.testclient import TestClient

from backend import documents
from backend.app import create_app
from backend.storage import TaskConflict


@pytest.fixture
def workspace(tmp_path):
    app = create_app(tmp_path / 'data', run_jobs=False)
    store = app.state.store
    path = store.files / 'original.pdf'
    with pymupdf.open() as pdf:
        for i in (1, 2):
            page = pdf.new_page()
            page.insert_text((72, 72), f'Real page {i} with preserved source text.')
        pdf.save(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    paper = store.create_paper(path.name, path, digest, 'upload', 2)
    store.update_paper(paper['id'], status='ready', title='Custom title', title_custom=True, tags=['kept'], tags_custom=True)
    store.add_page(paper['id'], {'page':1,'width':595,'height':842,'blocks':[{'text':'Outdated parsed block','bbox':[1,2,30,40]}]})
    block = store.blocks([paper['id']])[0]
    note = store.create_note('Saved note', 'Analysis', [paper['id']], [block], 'note')
    with TestClient(app) as client:
        yield client, app, paper['id'], path, digest, note


def test_full_reparse_updates_content_without_replacing_identity_or_research(workspace):
    client, app, pid, path, digest, note = workspace
    response = client.post(f'/api/papers/{pid}/reparse')
    assert response.status_code == 200
    assert app.state.store.blocks([pid]) == []
    assert app.state.store.paper(pid)['status'] == 'queued'
    app.state.runner._execute(response.json()['job_id'], 'parse', pid, {})
    paper = app.state.store.paper(pid, private=True)
    assert paper['status'] == 'ready' and paper['processed_pages'] == 2
    assert paper['id'] == pid and paper['title'] == 'Custom title' and paper['tags'] == ['kept']
    assert paper['path'] == str(path) and hashlib.sha256(path.read_bytes()).hexdigest() == digest
    assert paper['parser_version'] == getattr(documents, 'PARSER_VERSION', 'legacy')
    assert app.state.store.note(note['id'])['sources'] == note['sources']
    assert all('Outdated' not in b['text'] for b in app.state.store.blocks([pid]))
    assert any('Real page 2' in b['text'] for b in app.state.store.blocks([pid]))


@pytest.mark.parametrize('kind,state', [('parse','queued'),('parse','processing'),('translate','paused'),('translate','cancel_requested')])
def test_conflicting_jobs_leave_source_intact(workspace, kind, state):
    client, app, pid, _, _, _ = workspace
    job = app.state.store.create_job(kind, pid)
    app.state.store.update_job(job, status=state)
    before = app.state.store.blocks([pid])
    assert client.post(f'/api/papers/{pid}/reparse').status_code == 409
    assert app.state.store.blocks([pid]) == before


def test_concurrent_rebuild_requests_create_only_one_job(workspace):
    _, app, pid, _, _, _ = workspace
    def start():
        try: return app.state.store.create_reparse(pid)['id']
        except TaskConflict: return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: start(), range(2)))
    assert sum(result is not None for result in results) == 1
    assert len(app.state.store.jobs()) == 1


def test_parser_upgrade_never_mixes_old_checkpoints(workspace):
    _, app, pid, _, _, _ = workspace
    job = app.state.store.ensure_job('parse', pid)
    app.state.store.start_job(job['id'])
    assert app.state.store.prepare_parser(pid, job['id'], 'new-layout')
    assert app.state.store.completed_pages(pid) == set()
    app.state.store.add_page(pid, {'page':1,'width':100,'height':100,'blocks':[{'text':'new checkpoint','bbox':[1,2,30,40]}]}, job_id=job['id'])
    assert app.state.store.prepare_parser(pid, job['id'], 'new-layout')
    assert app.state.store.completed_pages(pid) == {1}
    assert app.state.store.prepare_parser(pid, job['id'], 'newer-layout')
    assert app.state.store.completed_pages(pid) == set()
