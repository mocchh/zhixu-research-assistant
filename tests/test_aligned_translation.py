"""Verify pairing contracts and persistence, not the quality of an LLM translation."""
import hashlib
import json

import pymupdf
import pytest
from fastapi.testclient import TestClient

from backend import intelligence
from backend.app import create_app
from backend.backup import create_backup, restore_backup
from backend.storage import Store
from backend.translation_routes import parse_pairs, sentence_spans, source_segments, translate_segments


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "")
    app = create_app(tmp_path / "data", run_jobs=False)
    store = app.state.store
    path = store.files / "paper.pdf"
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((72, 72), "First result. Second result.")
        pdf.save(path)
    paper = store.create_paper(path.name, path, hashlib.sha256(path.read_bytes()).hexdigest(), "upload", 1)
    store.add_page(paper["id"], {"page": 1, "width": 595, "height": 842, "blocks": [
        {"text": "First result is 0.25. Second result follows Fig. 2.", "bbox": [72, 60, 450, 100], "kind": "paragraph"},
        {"text": "E = mc²\n(1)", "bbox": [72, 110, 250, 130], "kind": "formula"}]})
    store.update_paper(paper["id"], status="ready")
    with TestClient(app) as client:
        yield client, app, paper["id"]


def endpoint(pid):
    return f"/api/papers/{pid}/pages/1/aligned-translation"


def configure(app):
    app.state.config.save("test", "http://127.0.0.1:1/v1", "local-test")


def test_offsets_preserve_original_and_abbreviations():
    text = "Dr. A. Smith reports 0.25. We use e.g. two layers. 你好。下一句！"
    parts = [text[a:b] for a, b in sentence_spans(text)]
    assert parts == ["Dr. A. Smith reports 0.25.", "We use e.g. two layers.", "你好。", "下一句！"]
    long = "first " * 1000
    assert "".join(long[a:b].replace(" ", "") for a, b in sentence_spans(long)) == long.replace(" ", "")
    assert all(b-a <= 1800 for a,b in sentence_spans(long))


def test_no_key_preview_has_real_source_offsets_and_block_coordinates(workspace):
    client, app, pid = workspace
    result = client.get(endpoint(pid)).json()
    blocks = {b["id"]: b for b in app.state.store.blocks([pid])}
    assert not result["cached"] and len(result["segments"]) == 3
    for segment in result["segments"]:
        block = blocks[segment["block_id"]]
        assert segment["source"] == block["text"][segment["start"]:segment["end"]]
        assert segment["bbox"] == block["bbox"] and segment["translation"] is None
    assert client.post(endpoint(pid), json={}).status_code == 503
    assert client.get(endpoint(pid)+'/export').status_code == 409
    assert client.get(endpoint("missing")).status_code == 404
    assert client.get(endpoint(pid).replace('/1/', '/0/')).status_code == 404
    assert client.post(endpoint(pid), json={"target_language":"fr"}).status_code == 422


@pytest.mark.parametrize("payload", [
    {"translations": [{"id": "a", "text": "译文"}]},
    {"translations": [{"id": "a", "text": "译文"}, {"id": "a", "text": "重复"}]},
    {"translations": [{"id": "a", "text": "译文"}, {"id": "bad", "text": "新增"}]},
    {"translations": [{"id": "a", "text": "译文"}, {"id": "b", "text": " "}]},
    {"translations": [{"id": "a", "text": "译文"}, {"id": "b", "text": 7}]},
    {"translations": [{"id": "a", "text": "译文", "source":"伪造"}, {"id": "b", "text": "内容"}]},
    {"translations": "bad"}, [], None,
])
def test_incomplete_duplicate_or_fabricated_ids_are_rejected(payload):
    with pytest.raises(intelligence.UpstreamError):
        parse_pairs(json.dumps(payload), {"a", "b"})


def test_model_order_does_not_control_source_pairing_and_cache_survives_restore(workspace, monkeypatch, tmp_path):
    client, app, pid = workspace
    configure(app)
    calls = []
    async def generate(messages, settings, max_tokens):
        items = json.loads(messages[-1]["content"]); calls.append(items)
        return json.dumps({"translations": [{"id": item["id"], "text": "译文:" + item["source"]} for item in reversed(items)]})
    monkeypatch.setattr(intelligence, "_generate", generate)
    first = client.post(endpoint(pid), json={})
    assert first.status_code == 200, first.text
    result = first.json()
    assert all(s["translation"] == "译文:" + s["source"] for s in result["segments"])
    assert not result["cached"] and len(calls) == 1
    assert client.post(endpoint(pid), json={}).json()["cached"] and len(calls) == 1
    assert client.get(endpoint(pid)+'?target_language=en').json()["cached"] is False
    exported = client.get(endpoint(pid)+'/export')
    assert exported.status_code == 200 and result['segments'][0]['block_id'] in exported.text
    html_export = client.get(endpoint(pid)+'/export?format=html')
    assert 'href="#target-1"' in html_export.text and 'href="#source-1"' in html_export.text
    archive = tmp_path / 'backup.zip'
    create_backup(app.state.store.directory, archive)
    target = tmp_path / 'restored'
    restore_backup(archive, target)
    with TestClient(create_app(target, run_jobs=False)) as restored:
        saved = restored.get(endpoint(pid)).json()
        assert saved['cached'] and saved['segments'] == result['segments']


def test_bad_later_batch_never_saves_partial_result(workspace, monkeypatch):
    client, app, pid = workspace
    configure(app)
    page = app.state.store.page(pid, 1)
    page['blocks'][0]['text'] = 'A sentence. ' * 16
    app.state.store.add_page(pid, page)
    count = 0
    async def generate(messages, settings, max_tokens):
        nonlocal count
        count += 1
        if count == 2: return '{"translations":[]}'
        return json.dumps({'translations':[{'id':s['id'],'text':'测试译文'} for s in json.loads(messages[-1]['content'])]})
    monkeypatch.setattr(intelligence, '_generate', generate)
    assert client.post(endpoint(pid), json={}).status_code == 502
    assert count == 2 and not client.get(endpoint(pid)).json()['cached']


def test_reparse_during_generation_rejects_stale_cache_and_preserves_new_source(workspace, monkeypatch):
    client, app, pid = workspace
    configure(app)
    async def generate(messages, settings, max_tokens):
        items = json.loads(messages[-1]['content'])
        page = app.state.store.page(pid, 1)
        page['blocks'][0]['text'] = 'Changed source.'
        app.state.store.add_page(pid, page)
        return json.dumps({'translations':[{'id':s['id'],'text':'Old result'} for s in items]})
    monkeypatch.setattr(intelligence, '_generate', generate)
    assert client.post(endpoint(pid), json={}).status_code == 409
    current = client.get(endpoint(pid)).json()
    assert not current['cached'] and current['segments'][0]['source'] == 'Changed source.'


def test_reparse_invalidates_old_cache_and_delete_cascades(workspace, monkeypatch):
    client, app, pid = workspace
    configure(app)
    async def generate(messages, settings, max_tokens):
        return json.dumps({'translations':[{'id':s['id'],'text':'测试译文'} for s in json.loads(messages[-1]['content'])]})
    monkeypatch.setattr(intelligence, '_generate', generate)
    assert client.post(endpoint(pid), json={}).status_code == 200
    page = app.state.store.page(pid, 1)
    page['blocks'][0]['bbox'] = [72, 60, 460, 100]
    app.state.store.add_page(pid, page)
    assert not client.get(endpoint(pid)).json()['cached']
    assert client.get(endpoint(pid)+'/export').status_code == 409
    assert client.delete(f'/api/papers/{pid}').status_code == 200
    with app.state.store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM aligned_translations').fetchone()[0] == 0


@pytest.mark.parametrize("kind", ["table", "formula"])
@pytest.mark.parametrize("length", [1801, 4500, 4501])
def test_table_formula_stay_whole_and_oversized_generation_is_rejected(workspace, monkeypatch, kind, length):
    client, app, pid = workspace
    configure(app)
    source = "α" * length
    page = app.state.store.page(pid, 1)
    page["blocks"] = [{"text": " \n" + source + "\n ", "kind": kind, "bbox": [1, 2, 30, 40]}]
    app.state.store.add_page(pid, page)
    calls = []

    async def generate(messages, settings, max_tokens):
        items = json.loads(messages[-1]["content"])
        calls.append(items)
        return json.dumps({"translations": [{"id": s["id"], "text": "译文"} for s in items]})

    monkeypatch.setattr(intelligence, "_generate", generate)
    preview = client.get(endpoint(pid)).json()
    assert len(preview["segments"]) == 1
    segment = preview["segments"][0]
    assert segment["source"] == source and segment["start"] == 2 and segment["end"] == length + 2
    result = client.post(endpoint(pid), json={})
    if length > 4500:
        assert result.status_code == 400 and "整页翻译" in result.json()["detail"]
        assert calls == [] and not client.get(endpoint(pid)).json()["cached"]
    else:
        assert result.status_code == 200, result.text
        assert len(calls) == 1 and calls[0][0]["source"] == source


def test_batch_source_characters_never_exceed_limit(workspace, monkeypatch):
    client, app, pid = workspace
    configure(app)
    lengths = [4500, 2300, 2200, 1801, 1801]
    page = app.state.store.page(pid, 1)
    page["blocks"] = [{"text": str(i) * length, "kind": "table", "bbox": [1, i * 30, 30, i * 30 + 20]}
                      for i, length in enumerate(lengths)]
    app.state.store.add_page(pid, page)
    batches = []

    async def generate(messages, settings, max_tokens):
        items = json.loads(messages[-1]["content"])
        batches.append(items)
        return json.dumps({"translations": [{"id": s["id"], "text": "译文"} for s in items]})

    monkeypatch.setattr(intelligence, "_generate", generate)
    result = client.post(endpoint(pid), json={})
    assert result.status_code == 200, result.text
    assert [sum(len(s["source"]) for s in batch) for batch in batches] == [4500, 4500, 3602]
    assert [len(s["source"]) for batch in batches for s in batch] == lengths


def test_service_rejects_oversized_segment_before_any_provider_call(monkeypatch):
    import asyncio

    async def unexpected_call(*args, **kwargs):
        pytest.fail("An oversized source must not reach the provider")

    monkeypatch.setattr(intelligence, "_generate", unexpected_call)
    with pytest.raises(ValueError, match="整页翻译"):
        asyncio.run(translate_segments([{"id": "formula", "source": "x" * 4501}], "zh", {}))
