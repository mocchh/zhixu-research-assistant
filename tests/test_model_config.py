"""Credential boundaries, using disposable settings and fake keys only."""
import json

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.config import ModelConfig


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "environment-test-key")
    monkeypatch.setenv("LLM_BASE_URL", "https://provider-a.invalid/v1")
    return ModelConfig(tmp_path)


def test_same_service_preserves_key_when_only_model_changes(config):
    config.save("first", "https://provider-a.invalid/v1", "fake-a")
    public = config.save("second", "https://PROVIDER-A.invalid:443/v1/", "")
    assert public["has_api_key"] is True
    assert config.get()["api_key"] == "fake-a"
    assert "fake-a" not in json.dumps(public)


@pytest.mark.parametrize("destination", [
    "https://provider-b.invalid/v1", "https://provider-a.invalid:8443/v1",
    "https://provider-a.invalid/another-tenant", "http://127.0.0.1:9000/v1",
])
def test_changed_service_drops_old_key_even_after_reload(config, destination):
    config.save("first", "https://provider-a.invalid/v1", "fake-a")
    assert config.save("second", destination)["has_api_key"] is False
    assert ModelConfig(config.path.parent).get()["api_key"] == ""
    # Returning to a previous address must not resurrect its old credentials.
    assert config.save("first", "https://provider-a.invalid/v1")["has_api_key"] is False


def test_new_key_explicitly_connects_new_service(config):
    config.save("first", "https://provider-a.invalid/v1", "fake-a")
    assert config.save("second", "https://provider-b.invalid/v1", "fake-b")["has_api_key"]
    assert config.get()["api_key"] == "fake-b"
    assert config.path.stat().st_mode & 0o777 == 0o600


def test_clear_overrides_environment_credentials_persistently(config):
    assert config.get()["api_key"] == "environment-test-key"
    assert not config.save("first", "https://provider-a.invalid/v1", clear_api_key=True)["has_api_key"]
    assert not ModelConfig(config.path.parent).public()["has_api_key"]
    assert not config.save("another-model", "https://provider-a.invalid/v1")["has_api_key"]


@pytest.mark.parametrize("damaged", ['{', 'null', '[]', '{"api_key":42}', '{"model":"first"}', '\udcff'])
def test_damaged_or_incomplete_override_never_reactivates_environment_key(config, damaged):
    config.save("first", "https://provider-a.invalid/v1", clear_api_key=True)
    config.path.write_bytes(damaged.encode("utf-8", errors="surrogateescape"))
    assert config.get()["api_key"] == ""
    assert not ModelConfig(config.path.parent).public()["has_api_key"]


def test_unreadable_override_never_reactivates_environment_key(config, monkeypatch):
    config.save("first", "https://provider-a.invalid/v1", clear_api_key=True)
    def fail_read(*args, **kwargs):
        raise PermissionError("simulated permission failure")
    monkeypatch.setattr(type(config.path), "read_text", fail_read)
    assert not config.public()["has_api_key"]


def test_conflicting_clear_and_new_key_keeps_existing_configuration(config):
    config.save("first", "https://provider-a.invalid/v1", "fake-a")
    before = config.path.read_bytes()
    with pytest.raises(ValueError, match="不能同时"):
        config.save("second", "https://provider-b.invalid/v1", "fake-b", clear_api_key=True)
    assert config.path.read_bytes() == before


@pytest.mark.parametrize("address", ["https://provider-a.invalid:0/v1", "https://provider-a.invalid:99999/v1", "https://provider-a.invalid:bad/v1", "https://[invalid/v1"])
def test_invalid_address_does_not_overwrite_saved_key(config, address):
    config.save("first", "https://provider-a.invalid/v1", "fake-a")
    before = config.path.read_bytes()
    with pytest.raises(ValueError):
        config.save("second", address)
    assert config.path.read_bytes() == before


def test_failed_atomic_write_retains_previous_settings(config, monkeypatch):
    config.save("first", "https://provider-a.invalid/v1", "fake-a")
    before = config.path.read_bytes()
    def fail_replace(*args):
        raise OSError("simulated disk failure")
    monkeypatch.setattr("backend.config.os.replace", fail_replace)
    with pytest.raises(OSError):
        config.save("second", "https://provider-b.invalid/v1", "fake-b")
    assert config.path.read_bytes() == before
    assert list(config.path.parent.glob("settings-*.tmp")) == []


def test_settings_api_switch_and_disconnect_do_not_send_old_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "environment-test-key")
    app = create_app(tmp_path, run_jobs=False)
    def no_network(*args, **kwargs):
        raise AssertionError("No model request should be dispatched without credentials")
    monkeypatch.setattr("backend.app.httpx.AsyncClient", no_network)
    with TestClient(app) as client:
        body = {"model": "first", "base_url": "https://provider-a.invalid/v1", "api_key": "fake-a"}
        assert client.put("/api/settings", json=body).status_code == 200
        body = {"model": "second", "base_url": "https://provider-b.invalid/v1"}
        switched = client.put("/api/settings", json=body)
        assert switched.status_code == 200
        assert switched.json()["has_api_key"] is False
        assert client.post("/api/settings/test").status_code == 503
        assert client.get("/api/health").json()["model_configured"] is False
        assert client.put("/api/settings", json={**body, "api_key": "fake-b"}).json()["has_api_key"]
        disconnected = client.put("/api/settings", json={**body, "clear_api_key": True})
        assert disconnected.status_code == 200
        assert disconnected.json()["has_api_key"] is False
        assert client.post("/api/settings/test").status_code == 503
        assert "fake-b" not in client.get("/api/settings").text
    assert ModelConfig(tmp_path).get()["api_key"] == ""
