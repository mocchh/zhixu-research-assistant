import hashlib
import json
from pathlib import Path

import pymupdf
import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.storage import Store


def make_pdf(path, stamp="arXiv:2401.12345v1", pages=2):
    doc = pymupdf.open()
    for index in range(pages):
        page = doc.new_page()
        page.insert_text((60, 60), "Evidence Based Research", fontsize=20)
        page.insert_text((60, 90), "Alice Example and Bob Researcher", fontsize=11)
        page.insert_text((60, 130), stamp, fontsize=10)
        page.insert_text((60, 180), "1 Introduction" if index == 0 else "2 Experiments", fontsize=15)
        page.insert_text((60, 220), "We train the model with 8 NVIDIA P100 GPUs.", fontsize=11)
        page.insert_text((60, 245), "The Adam optimizer uses beta1=0.9 and beta2=0.98.", fontsize=11)
    doc.save(path)
    doc.close()
    return path.read_bytes()


def test_isolated_server_origin_is_explicit_and_loopback_only(tmp_path):
    origin = "http://127.0.0.1:18080"
    default = create_app(tmp_path / "default", run_jobs=False)
    with TestClient(default) as client:
        assert client.post("/api/sessions", json={}, headers={"Origin": origin}).status_code == 403
    isolated = create_app(tmp_path / "isolated", run_jobs=False, extra_local_origins=[origin])
    with TestClient(isolated) as client:
        assert client.post("/api/sessions", json={}, headers={"Origin": origin}).status_code == 201
        assert client.post("/api/sessions", json={}, headers={"Origin": "https://foreign.example"}).status_code == 403
    for invalid in ("http://foreign.example:18080", "http://127.0.0.1", "http://127.0.0.1:18080/path", "http://user@localhost:18080"):
        with pytest.raises(ValueError, match="额外来源"):
            create_app(tmp_path / "invalid", run_jobs=False, extra_local_origins=[invalid])


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    samples = tmp_path / "samples"
    samples.mkdir()
    make_pdf(samples / "source.pdf")
    app = create_app(tmp_path / "data", samples, run_jobs=False)
    with TestClient(app) as client:
        yield client, app, samples


def import_and_parse(client, app, filename="source.pdf"):
    response = client.post("/api/samples/import", json={"filenames": [filename]})
    assert response.status_code == 200, response.text
    paper = response.json()["papers"][0]
    job = app.state.store.jobs()[0]
    app.state.runner._execute(job["id"], "parse", paper["id"], {})
    result = client.get("/api/papers/" + paper["id"]).json()["paper"]
    assert result["status"] == "ready", result
    return result


def test_upload_dedup_delete_preserves_original(workspace):
    client, app, samples = workspace
    paper = import_and_parse(client, app)
    response = client.post("/api/papers/upload", files={"files": ("renamed.pdf", (samples / "source.pdf").read_bytes(), "application/pdf")})
    assert response.status_code == 200
    assert response.json()["papers"] == []
    assert response.json()["duplicates"][0]["paper_id"] == paper["id"]
    assert client.get("/api/stats").json()["papers"] == 1
    assert client.delete("/api/papers/" + paper["id"]).status_code == 200
    assert client.get("/api/papers/" + paper["id"]).status_code == 404
    assert (samples / "source.pdf").is_file()
    assert app.state.store.blocks([paper["id"]]) == []
    assert not list(app.state.store.files.glob("*.pdf"))


def test_reading_and_evidence_are_actual_source(workspace):
    client, app, _ = workspace
    paper = import_and_parse(client, app)
    page = client.get(f"/api/papers/{paper['id']}/pages/2").json()
    assert page["width"] > 0 and page["height"] > 0
    assert any("NVIDIA" in b["text"] for b in page["blocks"])
    image = client.get(f"/api/papers/{paper['id']}/pages/2/image")
    assert image.status_code == 200
    assert image.content.startswith(b"\x89PNG")
    assert client.get(f"/api/papers/{paper['id']}/pages/99").status_code == 404
    result = client.post("/api/chat", json={"question": "训练使用多少GPU?", "paper_ids": [paper["id"]]}).json()
    assert result["mode"] == "extractive"
    assert result["sources"]
    block_map = {b["id"]: b for b in app.state.store.blocks([paper["id"]])}
    for source in result["sources"]:
        assert source["text"] == block_map[source["id"]]["text"]
        assert source["bbox"] == block_map[source["id"]]["bbox"]
    assert "NVIDIA" in result["answer"]


def test_failed_answer_draft_and_source_audit_survive_session_reload(workspace, monkeypatch):
    from backend import intelligence
    client, app, _ = workspace
    paper = import_and_parse(client, app)
    raw = json.dumps({"claims": [{"text": "训练用了999块GPU", "source_indices": [1]}], "insufficient_evidence": False})
    calls = []
    async def generate(*args, **kwargs):
        calls.append(1)
        return raw
    monkeypatch.setattr(intelligence, "_generate", generate)
    client.put('/api/settings', json={"model": "test", "base_url": "https://model.invalid/v1", "api_key": "test-only"})
    result = client.post('/api/chat', json={"question": "训练使用多少 GPU?", "paper_ids": [paper['id']]}).json()
    assert result['mode'] == 'extractive' and len(calls) == 1
    assert result['answer_audit']['raw_response'] == raw
    session = client.post('/api/sessions', json={"messages": [{"id": "answer", "role": "assistant",
        "content": result['answer'], "paper_ids": [paper['id']], "sources": result['sources'],
        "mode": result['mode'], "warnings": result['warnings'], "answer_audit": result['answer_audit']}]}).json()
    saved = client.get('/api/sessions/' + session['id']).json()['messages'][0]
    assert saved['answer_audit'] == result['answer_audit']
    assert saved['sources'] == result['sources']
    assert '999' not in saved['content'] and '999' in saved['answer_audit']['draft']


def test_version_relationship_and_explicit_scope(workspace):
    client, app, samples = workspace
    first = import_and_parse(client, app)
    make_pdf(samples / "new.pdf", stamp="arXiv:2401.12345v7")
    latest = import_and_parse(client, app, "new.pdf")
    assert latest["family_id"] == first["family_id"]
    assert latest["version"] == 7
    detail = client.get(f"/api/papers/{first['id']}").json()
    assert len(detail["versions"]) == 2
    answer = client.post("/api/chat", json={"question": "NVIDIA GPU", "paper_ids": [first["id"]]}).json()
    assert {x["paper_id"] for x in answer["sources"]} == {first["id"]}
    default_answer = client.post("/api/chat", json={"question": "NVIDIA GPU", "paper_ids": []}).json()
    assert {x["paper_id"] for x in default_answer["sources"]} == {latest["id"]}


def test_invalid_upload_and_sample_traversal(workspace):
    client, app, _ = workspace
    assert client.post("/api/samples/import", json={"filenames": ["../secret.pdf"]}).status_code == 400
    assert client.post("/api/papers/upload", files={"files": ("bad.pdf", b"not a pdf", "application/pdf")}).status_code == 400
    assert not list(app.state.store.files.glob("*.pdf"))
    assert client.get("/api/stats").json()["papers"] == 0
    assert client.post("/api/chat", json={"question": " ", "paper_ids": []}).status_code == 400


def test_settings_mask_key_and_reject_cross_origin(workspace):
    client, app, _ = workspace
    secret = "test-secret-not-a-real-api-key"
    response = client.put("/api/settings", json={"model": "test", "base_url": "https://example.org/v1", "api_key": secret})
    assert response.status_code == 200
    assert secret not in response.text
    assert secret not in client.get("/api/settings").text
    assert secret not in client.get("/api/health").text
    assert client.get("/api/settings").json()["has_api_key"]
    assert app.state.config.path.stat().st_mode & 0o777 == 0o600
    assert client.put("/api/settings", json={"model": "test", "base_url": "http://example.org/v1"}).status_code == 400
    assert client.post("/api/samples/import", headers={"Origin": "https://untrusted.example"}, json={"filenames": ["source.pdf"]}).status_code == 403


def test_translation_configuration_and_persistent_cache(workspace):
    client, app, _ = workspace
    paper = import_and_parse(client, app)
    assert client.post("/api/translate", json={"paper_id": paper["id"], "page": 1}).status_code == 503
    assert client.post(f"/api/papers/{paper['id']}/translate", json={}).status_code == 503
    store = app.state.store
    original = "\n\n".join(b["text"] for b in store.page(paper["id"], 1)["blocks"])
    assert store.save_translation(paper["id"], 1, "zh", original, "译文")
    reopened = Store(store.directory)
    assert reopened.translation(paper["id"], 1, "zh", original) == "译文"
    assert reopened.translation(paper["id"], 1, "en", original) is None
    assert reopened.translation(paper["id"], 1, "zh", "changed") is None


def test_parse_resume_reuses_completed_pages(workspace):
    client, app, _ = workspace
    response = client.post("/api/samples/import", json={"filenames": ["source.pdf"]})
    paper_id = response.json()["papers"][0]["id"]
    store = app.state.store
    from backend import documents
    paper = store.paper(paper_id, private=True)
    first_page = next(documents.iter_pages(Path(paper["path"])))
    store.add_page(paper_id, first_page)
    old_ids = [b["id"] for b in store.blocks([paper_id], page=1)]
    job = store.jobs()[0]
    app.state.runner._execute(job["id"], "parse", paper_id, {})
    assert store.paper(paper_id)["status"] == "ready"
    assert store.paper(paper_id)["processed_pages"] == 2
    assert [b["id"] for b in store.blocks([paper_id], page=1)] == old_ids
    assert store.jobs()[0]["progress"] == 100


def test_title_matching_bridges_missing_arxiv_identifier(workspace):
    client, app, samples = workspace
    first = import_and_parse(client, app)
    make_pdf(samples / "publisher.pdf", stamp="Publisher copy")
    second = import_and_parse(client, app, "publisher.pdf")
    assert second["arxiv_id"] is None
    assert second["family_id"] == first["family_id"]
    assert len(client.get(f"/api/papers/{first['id']}").json()["versions"]) == 2


def test_duplicate_translation_language_and_outline_errors(workspace):
    client, app, _ = workspace
    paper = import_and_parse(client, app)
    client.put("/api/settings", json={"model": "test", "base_url": "https://example.org/v1", "api_key": "fake-local-test-only"})
    zh = client.post(f"/api/papers/{paper['id']}/translate", json={"target_language": "zh"})
    assert zh.status_code == 200
    same = client.post(f"/api/papers/{paper['id']}/translate", json={"target_language": "zh"})
    assert same.json()["job_id"] == zh.json()["job_id"]
    other = client.post(f"/api/papers/{paper['id']}/translate", json={"target_language": "en"})
    assert other.status_code == 409
    bad_outline = client.post("/api/research", json={"paper_ids": [paper["id"]], "task": "outline", "idea": ""})
    assert bad_outline.status_code == 400


def test_chart_endpoint_outputs_image_and_errors(workspace):
    client, _, _ = workspace
    result = client.post("/api/charts", files={"file": ("results.csv", b"step,accuracy\n1,0.7\n2,0.8\n3,0.9\n", "text/csv")}, data={"kind": "line"})
    assert result.status_code == 200, result.text
    assert "<svg" in result.json()["svg"]
    assert result.json()["png_base64"].startswith("iVBOR")
    assert result.json()["rows"] == 3
    assert client.post("/api/charts", files={"file": ("bad.csv", b"x,y\n1,NaN\n", "text/csv")}, data={"kind": "line"}).status_code == 400


def test_retry_preserves_user_metadata_and_cancelled_jobs(workspace):
    client, app, _ = workspace
    paper = import_and_parse(client, app)
    client.patch(f"/api/papers/{paper['id']}", json={"title": "我的论文标题", "tags": ["毕业论文"]})
    response = client.post(f"/api/papers/{paper['id']}/retry")
    app.state.runner._execute(response.json()["job_id"], "parse", paper["id"], {})
    updated = app.state.store.paper(paper["id"])
    assert updated["title"] == "我的论文标题"
    assert updated["tags"] == ["毕业论文"]
    job = app.state.store.create_job("translate", paper["id"])
    app.state.store.delete_paper(paper["id"])
    app.state.store.update_job(job, status="done", message="should not overwrite cancellation")
    assert next(j for j in app.state.store.jobs() if j["id"] == job)["status"] == "cancelled"


def test_unknown_identifier_cannot_bridge_conflicting_works(workspace):
    client, app, samples = workspace
    one = import_and_parse(client, app)
    make_pdf(samples / "other.pdf", stamp="arXiv:2501.00002v1")
    two = import_and_parse(client, app, "other.pdf")
    make_pdf(samples / "unknown.pdf", stamp="Unidentified publisher")
    unknown = import_and_parse(client, app, "unknown.pdf")
    families = {app.state.store.paper(p["id"])["family_id"] for p in (one, two, unknown)}
    assert len(families) == 3
