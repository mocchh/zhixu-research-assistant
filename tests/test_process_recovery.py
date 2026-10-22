"""Actual SIGKILL and TCP disconnect tests on disposable application processes."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3

import httpx
import pytest

from scripts.runtime_harness import RuntimeServer, wait_for
from scripts.stress_import import make_pdf


@pytest.fixture
def server(tmp_path):
    with RuntimeServer(tmp_path / 'runtime') as running:
        yield running


def checked(response, status=200):
    assert response.status_code == status, response.text
    return response.json()


def upload(server, pages=4):
    payload = make_pdf(1, pages)
    result = checked(server.client.post('/api/papers/upload', files={'files': ('runtime.pdf', payload, 'application/pdf')}))
    return result['papers'][0]['id'], payload


def ready(server, pid):
    return wait_for(lambda: next((p for p in checked(server.client.get('/api/papers'))['papers']
                                 if p['id'] == pid and p['status'] == 'ready'), None))


def integrity(server):
    with sqlite3.connect(server.workspace / 'data/research.sqlite3') as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []


def test_killed_uncommitted_import_cleans_staging_file_and_allows_retry(server):
    server.arm('commit')
    with ThreadPoolExecutor(max_workers=1) as pool:
        request = pool.submit(upload, server, 1)
        server.reached('commit')
        assert len(list((server.workspace / 'data/files').glob('*.pdf'))) == 1
        server.stop(kill=True)
        with pytest.raises(httpx.HTTPError):
            request.result()
    server.start()
    assert checked(server.client.get('/api/papers'))['papers'] == []
    assert list((server.workspace / 'data/files').glob('*.pdf')) == []
    pid, _ = upload(server, 1)
    ready(server, pid)
    integrity(server)


def test_killed_parser_restarts_after_last_committed_page_without_duplicates(server):
    server.arm('parse')
    pid, payload = upload(server)
    server.reached('parse')
    first = checked(server.client.get(f'/api/papers/{pid}/pages/1'))
    with sqlite3.connect(server.workspace / 'data/research.sqlite3') as db:
        assert db.execute('SELECT count(*) FROM pages').fetchone()[0] == 2
    server.stop(kill=True)
    assert server.process.returncode < 0
    server.start()
    paper = ready(server, pid)
    assert paper['processed_pages'] == 4
    assert checked(server.client.get(f'/api/papers/{pid}/pages/1')) == first
    assert len(checked(server.client.get('/api/jobs'))['jobs']) == 1
    assert server.client.get(f'/api/papers/{pid}/file').content == payload
    assert len(list((server.workspace / 'data/files').glob('*.pdf'))) == 1
    integrity(server)


def test_killed_translator_reuses_saved_pages_and_repeats_only_uncommitted_call(server):
    pid, _ = upload(server, 3)
    ready(server, pid)
    server.configure_model()
    # Save page 1 before starting a range whose second page is interrupted.
    first = checked(server.client.post('/api/translate', json={'paper_id': pid, 'page': 1}))
    server.arm('model')
    job = checked(server.client.post(f'/api/papers/{pid}/translate', json={}))
    server.reached('model')
    server.stop(kill=True)
    server.start()
    wait_for(lambda: any(j['id'] == job['job_id'] and j['status'] == 'done'
                         for j in checked(server.client.get('/api/jobs'))['jobs']))
    calls = [json.loads(line)['hash'] for line in (server.workspace / 'model-calls.jsonl').read_text().splitlines()]
    assert sorted(Counter(calls).values()) == [1, 1, 2]
    again = checked(server.client.post('/api/translate', json={'paper_id': pid, 'page': 1}))
    assert again['translation'] == first['translation'] and again['cached']
    assert len((server.workspace / 'model-calls.jsonl').read_text().splitlines()) == 4
    with sqlite3.connect(server.workspace / 'data/research.sqlite3') as db:
        assert db.execute('SELECT count(*) FROM translations').fetchone()[0] == 3
    integrity(server)


@pytest.mark.parametrize('kind', ['notes', 'sessions'])
def test_killed_uncommitted_save_preserves_last_acknowledged_revision(server, kind):
    body = {'title': 'Saved before crash', 'paper_ids': []}
    body.update({'content': 'Durable text', 'sources': []} if kind == 'notes' else {'messages': []})
    item = checked(server.client.post(f'/api/{kind}', json=body), 201)
    body.update(revision=1, title='Acknowledged revision')
    saved = checked(server.client.put(f"/api/{kind}/{item['id']}", json=body))
    server.arm('commit')
    body.update(revision=2, title='Uncommitted revision')
    with ThreadPoolExecutor(max_workers=1) as pool:
        request = pool.submit(server.client.put, f"/api/{kind}/{item['id']}", json=body)
        server.reached('commit')
        server.stop(kill=True)
        with pytest.raises(httpx.HTTPError):
            request.result()
    server.start()
    assert checked(server.client.get(f"/api/{kind}/{item['id']}")) == saved
    retried = checked(server.client.put(f"/api/{kind}/{item['id']}", json=body))
    assert retried['revision'] == 3
    if kind == 'notes':
        assert [v['revision'] for v in retried['versions']] == [3, 2, 1]
    integrity(server)


@pytest.mark.parametrize('kind', ['notes', 'sessions'])
def test_sqlite_commit_failure_rolls_back_and_retry_is_safe(server, kind):
    body = {'title': 'Durable', 'paper_ids': []}
    body.update({'content': 'Saved', 'sources': []} if kind == 'notes' else {'messages': []})
    item = checked(server.client.post(f'/api/{kind}', json=body), 201)
    body.update(revision=1, title='New text')
    server.arm('commit', {'failure': True})
    failed = server.client.put(f"/api/{kind}/{item['id']}", json=body)
    assert failed.status_code == 503 and 'detail' in failed.json()
    assert 'database or disk' not in failed.text
    assert checked(server.client.get(f"/api/{kind}/{item['id']}")) == item
    assert checked(server.client.put(f"/api/{kind}/{item['id']}", json=body))['revision'] == 2
    integrity(server)


def test_tcp_client_disconnect_cancels_local_upstream_and_does_not_cache(server):
    pid, _ = upload(server, 1)
    ready(server, pid)
    server.configure_model()
    server.arm('model')
    with pytest.raises(httpx.ReadTimeout):
        server.client.post('/api/translate', json={'paper_id': pid, 'page': 1}, timeout=.6)
    server.reached('model')
    wait_for(lambda: (server.workspace / 'model.disconnected').exists(), 5)
    with sqlite3.connect(server.workspace / 'data/research.sqlite3') as db:
        assert db.execute('SELECT count(*) FROM translations').fetchone()[0] == 0
    result = checked(server.client.post('/api/translate', json={'paper_id': pid, 'page': 1}))
    assert result['translation'] and not result['cached']
    integrity(server)


@pytest.mark.parametrize('status', [429, 503])
def test_upstream_http_failure_is_retryable_without_saving_error_text(server, status):
    pid, _ = upload(server, 1)
    ready(server, pid)
    server.configure_model()
    server.arm('model', {'status': status})
    response = server.client.post('/api/translate', json={'paper_id': pid, 'page': 1})
    assert response.status_code == 502
    assert str(status) in response.json()['detail']
    assert 'local simulated failure' not in response.text
    with sqlite3.connect(server.workspace / 'data/research.sqlite3') as db:
        assert db.execute('SELECT count(*) FROM translations').fetchone()[0] == 0
    result = checked(server.client.post('/api/translate', json={'paper_id': pid, 'page': 1}))
    assert result['translation'] and not result['cached']
    integrity(server)


@pytest.mark.parametrize('malformed,mode,audit_status', [(False, 'model', 'checked'), (True, 'extractive', 'fallback')])
def test_tcp_qa_exercises_normal_answer_and_malformed_fallback(server, malformed, mode, audit_status):
    pid, _ = upload(server, 1)
    ready(server, pid)
    server.configure_model()
    if malformed:
        server.arm('model', {'content': 'Unstructured fixture answer'})
    answer = checked(server.client.post('/api/chat', json={
        'question': 'ExperimentCode GPUs training steps', 'paper_ids': [pid]}))
    assert answer['mode'] == mode
    assert answer['answer_audit']['status'] == audit_status
    assert answer['sources'] and all(source['paper_id'] == pid for source in answer['sources'])
    if malformed:
        assert answer['answer_audit']['raw_response'] == 'Unstructured fixture answer'
    else:
        assert 'ExperimentCode QX0001 uses 2 GPUs and 101 training steps.' in answer['answer']
    calls = [json.loads(line) for line in (server.workspace / 'model-calls.jsonl').read_text().splitlines()]
    assert len(calls) == 1 and calls[0]['kind'] == 'chat'
