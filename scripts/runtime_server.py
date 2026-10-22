"""Test-only failure gates around the real app, SQLite and local HTTP model.

All gates live in a marked temporary directory. Production has no test routes.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def fixture_completion(messages):
    """Return the real answer contract for QA and recognizable text for translation."""
    source = messages[-1]['content']
    if any(m['role'] == 'system' and '"claims"' in m['content'] for m in messages):
        for index, text in re.findall(r'SOURCE \[(\d+)\][^\n]*\n(.*?)\nEND SOURCE', source, re.S):
            claim = re.search(r'ExperimentCode QX\d+ uses \d+ GPUs and \d+ training\s+steps\.', text)
            if claim:
                return 'chat', json.dumps({'claims': [{'text': ' '.join(claim[0].split()),
                                                       'source_indices': [int(index)]}],
                                           'insufficient_evidence': False})
        raise ValueError('Runtime QA fixture must contain synthetic experiment evidence')
    return 'translation', '测试服务译文：' + source


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path, required=True)
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    if not (workspace / '.runtime-isolated').is_file() or workspace == ROOT or ROOT in workspace.parents:
        parser.error('Use a marked temporary workspace outside the project')
    os.environ.update(RESEARCH_DATA_DIR=str(workspace / 'data'), PYTHON_DOTENV_DISABLED='1',
                      LLM_API_KEY='', LLM_BASE_URL='http://127.0.0.1:1/v1', LLM_MODEL='runtime-stub')
    sys.path.insert(0, str(ROOT))
    import uvicorn
    from fastapi import Request
    from fastapi.responses import JSONResponse
    from backend import documents
    from backend.app import create_app

    def take_gate(name):
        arm = workspace / f'{name}.arm'
        try:
            value = json.loads(arm.read_text())
            arm.unlink()
            (workspace / f'{name}.reached').touch()
            return value
        except FileNotFoundError:
            return None

    def gate_wait(name):
        deadline = time.monotonic() + 30
        while not (workspace / f'{name}.release').exists():
            if time.monotonic() > deadline:
                raise TimeoutError('Unreleased runtime fixture gate')
            time.sleep(.02)

    original_pages = documents.iter_pages
    def pages(*args, **kwargs):
        for page in original_pages(*args, **kwargs):
            if page['page'] == 3 and take_gate('parse') is not None:
                gate_wait('parse')
            yield page
    documents.iter_pages = pages
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    url = f'http://127.0.0.1:{sock.getsockname()[1]}'
    samples = workspace / 'samples'
    samples.mkdir(exist_ok=True)
    app = create_app(workspace / 'data', samples, extra_local_origins=[url])
    production_route_count = len(app.router.routes)
    connect = app.state.store.connect

    @contextmanager
    def controlled_connect():
        with connect() as db:
            yield db
            if db.total_changes:
                control = take_gate('commit')
                if control is not None:
                    if control.get('failure'):
                        raise sqlite3.OperationalError('database or disk is full')
                    gate_wait('commit')
    app.state.store.connect = controlled_connect
    # Recovered jobs can use the same private local stub after a port change.
    if app.state.config.public()['has_api_key']:
        app.state.config.save('runtime-local-stub', url + '/__runtime__/model', 'runtime-fixture-only')

    # Expose the Request annotation globally because postponed annotations are
    # resolved by FastAPI outside this function's local scope.
    globals()['Request'] = Request
    @app.post('/__runtime__/model/chat/completions')
    async def local_stub(request: Request):
        if request.headers.get('authorization') != 'Bearer runtime-fixture-only':
            return JSONResponse({'error': 'fixture credential required'}, status_code=403)
        body = await request.json()
        source = body['messages'][-1]['content']
        kind, completion = fixture_completion(body['messages'])
        digest = hashlib.sha256(source.encode()).hexdigest()
        with (workspace / 'model-calls.jsonl').open('a') as stream:
            stream.write(json.dumps({'hash': digest, 'kind': kind}) + '\n')
        control = take_gate('model')
        if control is not None:
            if control.get('status'):
                return JSONResponse({'error': 'local simulated failure'}, status_code=control['status'])
            if 'content' in control:
                completion = control['content']
            deadline = time.monotonic() + 30
            while 'content' not in control and not (workspace / 'model.release').exists():
                if await request.is_disconnected():
                    (workspace / 'model.disconnected').touch()
                    return JSONResponse({'error': 'fixture disconnected'}, status_code=499)
                if time.monotonic() > deadline:
                    return JSONResponse({'error': 'fixture timeout'}, status_code=504)
                await asyncio.sleep(.03)
        return JSONResponse({'choices': [{'message': {'content': completion}, 'finish_reason': 'stop'}]})

    # The production UI's '/' static mount must remain behind test routes.
    added = app.router.routes[production_route_count:]
    app.router.routes[:] = added + app.router.routes[:production_route_count]
    (workspace / 'server.json').write_text(json.dumps({'url': url}))
    try:
        uvicorn.Server(uvicorn.Config(app, log_level='warning', access_log=False)).run(sockets=[sock])
    finally:
        sock.close()


if __name__ == '__main__':
    main()
