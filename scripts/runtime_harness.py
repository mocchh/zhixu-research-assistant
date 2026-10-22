"""Disposable TCP server for runtime checks; never opens the user's data."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time

import httpx

ROOT = Path(__file__).resolve().parents[1]


def wait_for(check, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(.05)
    raise AssertionError(f"Runtime check timed out after {timeout} seconds")


class RuntimeServer:
    def __init__(self, workspace, python=sys.executable):
        self.workspace = Path(workspace).resolve()
        if self.workspace == ROOT or ROOT in self.workspace.parents:
            raise ValueError("Runtime workspace must be outside the project")
        self.workspace.mkdir(parents=True, exist_ok=True)
        (self.workspace / '.runtime-isolated').touch()
        self.python = str(python)
        self.process = self.client = self.log = None

    def start(self):
        metadata = self.workspace / 'server.json'
        metadata.unlink(missing_ok=True)
        env = {**os.environ, 'PYTHON_DOTENV_DISABLED': '1', 'LLM_API_KEY': '',
               'LLM_BASE_URL': 'http://127.0.0.1:1/v1',
               'NO_PROXY': '127.0.0.1,localhost',
               'RESEARCH_DATA_DIR': str(self.workspace / 'data')}
        self.log = (self.workspace / 'server.log').open('ab')
        self.process = subprocess.Popen([self.python, str(ROOT / 'scripts/runtime_server.py'),
                                        '--workspace', str(self.workspace)], cwd=ROOT,
                                       env=env, stdout=self.log, stderr=self.log)
        def ready():
            if self.process.poll() is not None:
                raise AssertionError((self.workspace / 'server.log').read_text()[-4000:])
            try:
                self.url = json.loads(metadata.read_text())['url']
                return httpx.get(self.url + '/api/health', timeout=.5, trust_env=False).status_code == 200
            except (OSError, ValueError, httpx.HTTPError):
                return False
        try:
            wait_for(ready, 30)
            self.client = httpx.Client(base_url=self.url, timeout=15, trust_env=False)
            return self
        except BaseException:
            self.stop()
            raise

    def stop(self, kill=False):
        if self.process and self.process.poll() is None:
            self.process.kill() if kill else self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if self.client:
            self.client.close()
        if self.log:
            self.log.close()

    def arm(self, name, value=None):
        (self.workspace / f'{name}.reached').unlink(missing_ok=True)
        (self.workspace / f'{name}.release').unlink(missing_ok=True)
        (self.workspace / f'{name}.arm').write_text(json.dumps(value or {}))

    def reached(self, name):
        return wait_for(lambda: (self.workspace / f'{name}.reached').exists())

    def configure_model(self):
        response = self.client.put('/api/settings', json={
            'model': 'runtime-local-stub', 'base_url': self.url + '/__runtime__/model',
            'api_key': 'runtime-fixture-only'})
        response.raise_for_status()

    def __enter__(self):
        return self.start()

    def __exit__(self, *args):
        self.stop()
