#!/usr/bin/env python3
"""Sustained mixed TCP workload against a disposable app and local model stub."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from collections import Counter
import hashlib
import json
from pathlib import Path
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.runtime_harness import RuntimeServer, wait_for
from scripts.stress_import import make_pdf, percentile


def run(args):
    from backend.backup import create_backup, verify_backup
    report = {'kind': 'isolated_mixed_tcp_soak', 'started_at': datetime.now(timezone.utc).isoformat(),
              'configuration': vars(args) | {'output': str(args.output)}, 'checks': {}, 'errors': [],
              'limits': ['Synthetic native PDFs and local model protocol stub, no external model calls.',
                         'RSS is sampled from the server process; this bounded run does not prove absence of all memory leaks.',
                         'Disk usage grows with deliberately retained note history; no physical disk exhaustion.']}
    started = time.monotonic()
    latencies, samples, errors = {}, [], []
    lock, stop = threading.Lock(), threading.Event()
    expected = {}
    operation_count = 0
    backups = 0
    try:
        with tempfile.TemporaryDirectory(prefix='research-runtime-soak-') as temporary:
            workspace = Path(temporary)
            with RuntimeServer(workspace) as server:
                def request(method, path, metric, status=200, **kwargs):
                    nonlocal operation_count
                    tick = time.perf_counter()
                    response = server.client.request(method, path, **kwargs)
                    assert response.status_code == status, f'{metric}: HTTP {response.status_code}: {response.text[:200]}'
                    with lock:
                        operation_count += 1
                        latencies.setdefault(metric, []).append((time.perf_counter() - tick) * 1000)
                    return response

                def import_one(index):
                    payload = make_pdf(index, 4)
                    value = request('POST', '/api/papers/upload', 'upload', files={
                        'files': (f'soak-{index}.pdf', payload, 'application/pdf')}).json()
                    pid = value['papers'][0]['id']
                    expected[pid] = hashlib.sha256(payload).hexdigest()
                    return pid, payload

                papers = [import_one(index) for index in range(30)]
                def all_ready():
                    rows = request('GET', '/api/papers', 'list').json()['papers']
                    return len(rows) == len(expected) and all(p['status'] == 'ready' for p in rows)
                wait_for(all_ready, 60)
                server.configure_model()
                notes = [request('POST', '/api/notes', 'create_note', 201,
                                 json={'title': f'Soak worker {i}', 'content': 'Initial content'}).json()
                         for i in range(args.workers)]
                mixed_start = time.monotonic()
                deadline = mixed_start + args.seconds

                def work(index):
                    note = notes[index]
                    iteration = 0
                    try:
                        while not stop.is_set() and time.monotonic() < deadline:
                            pid, payload = papers[(iteration + index) % len(papers)]
                            operation = iteration % 7
                            if operation == 0:
                                data = request('GET', f'/api/papers/{pid}/pages/2', 'page').json()
                                assert data['blocks'] and data['page'] == 2
                            elif operation == 1:
                                answer = request('POST', '/api/chat', 'chat', json={
                                    'question': 'ExperimentCode GPUs training steps', 'paper_ids': [pid]}).json()
                                assert answer['sources'] and all(s['paper_id'] == pid for s in answer['sources'])
                                assert answer['mode'] == 'model' and answer['answer_audit']['status'] == 'checked'
                                assert 'ExperimentCode' in answer['answer']
                            elif operation == 2:
                                note = request('PUT', f"/api/notes/{note['id']}", 'save_note', json={
                                    'revision': note['revision'], 'title': f'Soak worker {index}',
                                    'content': f'Iteration {iteration}', 'sources': [], 'paper_ids': [pid]}).json()
                            elif operation == 3:
                                result = request('POST', '/api/papers/upload', 'duplicate', files={
                                    'files': ('duplicate.pdf', payload, 'application/pdf')}).json()
                                assert len(result['duplicates']) == 1 and not result['papers']
                            elif operation == 4:
                                translation = request('POST', '/api/translate', 'translation', json={
                                    'paper_id': pid, 'page': 1}).json()
                                assert translation['translation'].startswith('测试服务译文：')
                            elif operation == 5:
                                image = request('GET', f'/api/papers/{pid}/pages/1/image', 'render',
                                                params={'scale': 1}).content
                                assert image.startswith(b'\x89PNG')
                            else:
                                jobs = request('GET', '/api/jobs', 'jobs').json()
                                assert not any(j['status'] == 'error' for j in jobs['active_jobs'])
                            iteration += 1
                            stop.wait(.05)
                        notes[index] = note
                    except Exception as exc:
                        with lock:
                            errors.append(f'worker {index}: {type(exc).__name__}: {exc}')
                        stop.set()

                next_import, next_backup, next_progress = mixed_start, mixed_start, mixed_start
                with ThreadPoolExecutor(max_workers=args.workers) as pool:
                    futures = [pool.submit(work, i) for i in range(args.workers)]
                    while time.monotonic() < deadline and not stop.is_set():
                        now = time.monotonic()
                        rss = subprocess.run(['ps', '-o', 'rss=', '-p', str(server.process.pid)],
                                             capture_output=True, text=True, check=True)
                        samples.append({'seconds': round(now - mixed_start, 2),
                                        'rss_mib': round(int(rss.stdout.strip()) / 1024, 2),
                                        'operations': operation_count})
                        if now >= next_progress:
                            print(json.dumps({'phase': 'running', **samples[-1], 'verified_backups': backups}), flush=True)
                            next_progress = now + 60
                        if now >= next_import and len(expected) < 60:
                            import_one(len(expected))
                            next_import = now + max(1, args.seconds / 30)
                        if now >= next_backup:
                            archive = workspace / f'backup-{backups}.zip'
                            backup = create_backup(workspace / 'data', archive)
                            assert verify_backup(archive, backup['archive_sha256'])['ok']
                            archive.unlink()
                            backups += 1
                            next_backup = now + 60
                        stop.wait(min(5, max(0, deadline - time.monotonic())))
                    stop.set()
                    for future in futures:
                        future.result()
                report['mixed_seconds'] = round(time.monotonic() - mixed_start, 2)
                report['errors'].extend(errors)
                wait_for(all_ready, 30)
                for note in notes:
                    persisted = request('GET', f"/api/notes/{note['id']}", 'read_note').json()
                    assert persisted == note
                with sqlite3.connect(workspace / 'data/research.sqlite3') as db:
                    assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
                    assert not db.execute('PRAGMA foreign_key_check').fetchall()
                    assert db.execute('SELECT count(*) FROM pages').fetchone()[0] == len(expected) * 4
                    assert db.execute("SELECT count(*) FROM jobs WHERE status!='done'").fetchone()[0] == 0
                    for pid, path, digest in db.execute('SELECT id,path,sha256 FROM papers'):
                        assert digest == expected[pid] == hashlib.sha256(Path(path).read_bytes()).hexdigest()
                    report['final_rows'] = {table: db.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
                                            for table in ['papers', 'pages', 'jobs', 'research_notes', 'note_versions', 'translations']}
                assert len(list((workspace / 'data/files').iterdir())) == len(expected)
                model_calls = [json.loads(line) for line in (workspace / 'model-calls.jsonl').read_text().splitlines()]
                report['local_model_calls'] = dict(Counter(call['kind'] for call in model_calls))
                report['external_model_requests'] = 0
                report['checks'].update(sqlite_and_file_integrity=True, no_task_backlog=True,
                                        saved_notes_exact=True, no_orphan_files=True, concurrent_backups=backups > 0,
                                        requested_duration=report['mixed_seconds'] >= args.seconds,
                                        all_workloads_exercised=all(len(latencies.get(name, [])) >= args.workers
                                            for name in ('page', 'chat', 'save_note', 'duplicate', 'translation', 'render', 'jobs')),
                                        normal_model_answer_path=report['local_model_calls'].get('chat') == len(latencies.get('chat', [])))
                report['workspace_mib'] = round(sum(p.stat().st_size for p in workspace.rglob('*') if p.is_file()) / 2**20, 2)
            report['checks']['server_stopped'] = server.process.poll() is not None
        report['checks']['temporary_workspace_removed'] = not workspace.exists()
    except Exception as exc:
        report['errors'].append(f'{type(exc).__name__}: {exc}')
    report['operations'] = operation_count
    report['verified_live_backups'] = backups
    report['rss_samples'] = samples
    if samples:
        warmed = [s['rss_mib'] for s in samples if s['seconds'] >= min(60, args.seconds / 4)] or [s['rss_mib'] for s in samples]
        tail = [s['rss_mib'] for s in samples[-min(12, len(samples)):]]
        report['server_rss_mib'] = {'peak': max(s['rss_mib'] for s in samples),
                                    'warm_median': round(statistics.median(warmed[:12]), 2),
                                    'tail_median': round(statistics.median(tail), 2)}
        growth = report['server_rss_mib']['tail_median'] - report['server_rss_mib']['warm_median']
        report['server_rss_mib']['growth_after_warmup'] = round(growth, 2)
        report['checks']['rss_within_test_budget'] = growth < 128 and report['server_rss_mib']['peak'] < 1024
    report['latency_ms'] = {name: {'count': len(values), 'median': round(statistics.median(values), 2),
                                  'p95': percentile(values, .95), 'max': round(max(values), 2)}
                            for name, values in latencies.items()}
    report['elapsed_seconds'] = round(time.monotonic() - started, 2)
    report['passed'] = not report['errors'] and bool(report['checks']) and all(report['checks'].values())
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=int, default=600)
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--output', type=Path, default=ROOT / 'test-results/runtime-soak.json')
    args = parser.parse_args()
    if not 10 <= args.seconds <= 3600 or not 1 <= args.workers <= 8:
        parser.error('seconds must be 10..3600 and workers 1..8')
    report = run(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k: v for k, v in report.items() if k != 'rss_samples'}, ensure_ascii=False, indent=2))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
