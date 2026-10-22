"""Verification modes cannot silently expand the approved provider requests."""
import asyncio
import json
from unittest.mock import AsyncMock

from scripts import verify_model as subject


def test_answers_only_never_calls_translation_paths(monkeypatch, tmp_path):
    papers = [{"id": str(index), "filename": name, "status": "ready"} for index, name in enumerate(
        ("1706.03762v7.pdf", "2412.19437v1.pdf"))]
    block = {"id": "block", "text": "Original evidence", "bbox": [1, 2, 3, 4]}
    class Config:
        def __init__(self, path): pass
        def get(self): return {"base_url": "http://model.invalid/v1", "model": "test", "api_key": "unused"}
        def public(self): return self.get()
    class Store:
        def __init__(self, path): pass
        def papers(self): return papers
        def blocks(self, ids): return [block]
    answer = AsyncMock(return_value={"answer": "Evidence [1].", "mode": "model", "sources": [block]})
    forbidden = AsyncMock(side_effect=AssertionError("Translation is outside the answers-only scope"))
    monkeypatch.setattr(subject, "ModelConfig", Config)
    monkeypatch.setattr(subject, "Store", Store)
    monkeypatch.setattr(subject.intelligence, "answer_question", answer)
    monkeypatch.setattr(subject.intelligence, "translate_text", forbidden)
    monkeypatch.setattr(subject, "translate_segments", forbidden)
    report = asyncio.run(subject.verify(tmp_path, answers_only=True))
    assert report["integration_passed"] and report["requested_checks"] == 2
    assert answer.await_count == 2
    forbidden.assert_not_awaited()


def test_changed_approved_payload_stops_before_network(monkeypatch, tmp_path):
    approved = tmp_path / "preview.json"
    approved.write_text(json.dumps({"destination": "https://example.invalid", "proposed_requests": [
        {"model": "test", "messages": [{"role": "user", "content": "approved source"}], "max_tokens": 2200}
    ]}))
    original = AsyncMock(side_effect=AssertionError("Provider must not be called"))
    monkeypatch.setattr(subject.intelligence, "_generate", original)
    async def changed(data, answers_only=False):
        return await subject.intelligence._generate(
            [{"role": "user", "content": "changed source"}],
            {"base_url": "https://example.invalid", "model": "test"})
    monkeypatch.setattr(subject, "verify", changed)
    output = tmp_path / "result.json"
    monkeypatch.setattr(subject.sys, "argv", ["verify_model.py", "--answers-only", "--approved-preview", str(approved), "--output", str(output)])
    assert subject.main() == 1
    assert not json.loads(output.read_text())["integration_passed"]
    original.assert_not_awaited()


def test_integrated_citation_marking_keeps_quotes_and_normalizes_grouped_sources():
    answer = 'A result [1,2]. Original wording: "see [99]" [1]. Invalid claim [88].'
    result, warnings = subject.intelligence._citation_check(answer, [{}, {}])
    assert 'result [1][2]' in result
    assert '"see [99]" [1]' in result
    assert 'Invalid claim [引用无效]' in result
    assert warnings
