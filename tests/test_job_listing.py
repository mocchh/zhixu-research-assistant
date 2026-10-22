"""Actionable jobs are never hidden by bounded terminal history."""
import pytest
from fastapi.testclient import TestClient

from backend.app import create_app


@pytest.fixture
def workspace(tmp_path):
    app = create_app(tmp_path / "data", run_jobs=False)
    with TestClient(app) as client:
        yield client, app.state.store


def paper(store, number):
    return store.create_paper(f"fixture-{number}.pdf", store.files / f"fixture-{number}.pdf", f"test-{number}", "upload", 1)


def test_over_100_history_jobs_cannot_hide_any_actionable_job_and_old_pause_resumes(workspace):
    client, store = workspace
    statuses = ["paused", "queued", "processing", "pause_requested", "cancel_requested", "error"]
    actionable = []
    for n in range(105):
        p = paper(store, n)
        jid = store.create_job("parse", p["id"])
        state = statuses[n % len(statuses)]
        store.update_job(jid, status=state)
        store.update_paper(p["id"], status="paused" if state == "paused" else "processing")
        actionable.append(jid)
    historical = []
    for n in range(125):
        jid = store.create_job("translate", p["id"])
        store.update_job(jid, status="done" if n % 2 else "cancelled")
        historical.append(jid)
    # Equal timestamps exercise the id tie-breaker used by cursor pagination.
    with store.connect() as db:
        db.execute("UPDATE jobs SET created_at='2026-01-01T00:00:00+00:00' WHERE status IN ('done','cancelled')")
    cursor, seen = None, []
    while True:
        response = client.get("/api/jobs", params={"limit": 17, **({"cursor": cursor} if cursor else {})})
        assert response.status_code == 200
        result = response.json()
        assert {j["id"] for j in result["active_jobs"]} == set(actionable)
        assert len(result["history_jobs"]) <= 17
        assert result["jobs"] == result["active_jobs"] + result["history_jobs"]
        assert result["history_total"] == 125
        seen.extend(j["id"] for j in result["history_jobs"])
        cursor = result["next_cursor"]
        if cursor is None:
            break
    assert len(seen) == len(set(seen)) == 125
    assert set(seen) == set(historical)
    oldest_pause = actionable[0]
    response = client.post(f"/api/jobs/{oldest_pause}/resume")
    assert response.status_code == 200 and response.json()["status"] == "queued"
    assert store.paper(response.json()["paper_id"])["status"] == "queued"


def test_new_history_does_not_shift_older_cursor_pages(workspace):
    client, store = workspace
    p = paper(store, 1)
    ids = []
    for _ in range(5):
        jid = store.create_job("parse", p["id"])
        store.update_job(jid, status="done")
        ids.append(jid)
    first = client.get("/api/jobs?limit=2").json()
    new = store.create_job("parse", p["id"])
    store.update_job(new, status="done")
    seen = [j["id"] for j in first["history_jobs"]]
    cursor = first["next_cursor"]
    while cursor:
        result = client.get("/api/jobs", params={"limit": 2, "cursor": cursor}).json()
        seen.extend(j["id"] for j in result["history_jobs"])
        cursor = result["next_cursor"]
    assert len(seen) == len(set(seen)) == len(ids)
    assert set(seen) == set(ids)
    assert new not in seen


@pytest.mark.parametrize("cursor", ["!invalid!", "e30=", "WzEsMl0=", "W10="])
def test_invalid_history_cursor_returns_actionable_error(workspace, cursor):
    client, _ = workspace
    response = client.get("/api/jobs", params={"cursor": cursor})
    assert response.status_code == 400
    assert "分页位置无效" in response.json()["detail"]


@pytest.mark.parametrize("limit", [0, -1, 101])
def test_history_limit_is_bounded(workspace, limit):
    client, _ = workspace
    assert client.get("/api/jobs", params={"limit": limit}).status_code == 422
