import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from backend import answer_contract, intelligence


def draft(text="The training run used 6 GPUs", refs=None, insufficient=False):
    return json.dumps({"claims": [{"text": text, "source_indices": refs or [1]}],
                       "insufficient_evidence": insufficient})


SOURCE = {"text": "The training run used 6 GPUs for 10 hours.", "paper_id": "p", "page": 2}


def test_citations_are_rendered_next_to_each_claim_and_do_not_verify_semantics():
    audit = answer_contract.inspect_draft(draft(), [SOURCE])
    assert audit["status"] == "checked"
    assert audit["draft"] == "The training run used 6 GPUs [1]"
    assert audit["semantic_entailment_verified"] is False
    assert audit["raw_response"] == draft()


@pytest.mark.parametrize("raw", [
    'A free-form uncited answer.',
    '{"claims":[],"insufficient_evidence":false}',
    '{"claims":[],"insufficient_evidence":true,"supplement":"invented fact"}',
    '{"claims":[],"claims":[],"insufficient_evidence":true}',
    '{"claims":[],"insufficient_evidence":"false"}',
    draft(refs=[99]), draft(refs=[True]), draft(refs=[1, 1]),
    draft(text="The training run used 6 GPUs [1]"),
    draft(text="First fact\nAnother uncited fact"),
])
def test_malformed_or_unbound_drafts_fall_back_without_losing_raw_response(raw):
    audit = answer_contract.inspect_draft(raw, [SOURCE])
    assert audit["status"] == "fallback"
    assert audit["raw_response"] == raw
    assert audit["issues"]


def test_missing_quantity_and_hidden_uncited_sentence_are_not_promoted():
    for text in ["The training run used 600 GPUs", "The training run used 6 GPUs. The run lasted 10 hours"]:
        audit = answer_contract.inspect_draft(draft(text), [SOURCE])
        assert audit["status"] == "fallback"
        assert audit["citation_checks"]["claims"]


def test_valid_json_fence_and_explicit_insufficient_evidence():
    raw = '```json\n{"claims":[],"insufficient_evidence":true}\n```'
    assert answer_contract.inspect_draft(raw, [SOURCE])["status"] == "insufficient"


@pytest.mark.parametrize("raw,mode", [(draft(), "model"), (draft("The run used 600 GPUs"), "extractive"), ("Unstructured response", "extractive")])
def test_single_provider_call_preserves_audit_in_success_and_fallback(monkeypatch, raw, mode):
    model = AsyncMock(return_value=raw)
    monkeypatch.setattr(intelligence, "_generate", model)
    paper = {"id": "p", "title": "Training system", "status": "ready"}
    block = {**SOURCE, "id": "b", "bbox": [1, 2, 30, 40]}
    result = asyncio.run(intelligence.answer_question("training GPU", [paper], [block], [], {"api_key": "test-only"}))
    assert result["mode"] == mode
    assert result["answer_audit"]["raw_response"] == raw
    assert result["sources"][0]["text"] == SOURCE["text"]
    assert model.await_count == 1
    if mode == "extractive":
        assert "模型草稿未通过" in result["answer"]
        assert "600 GPUs" not in result["answer"]


def test_empty_supported_answer_displays_evidence_gap(monkeypatch):
    model = AsyncMock(return_value='{"claims":[],"insufficient_evidence":true}')
    monkeypatch.setattr(intelligence, "_generate", model)
    result = asyncio.run(intelligence.answer_question("training GPU", [{"id": "p", "title": "Training system"}],
                    [{**SOURCE, "id": "b", "bbox": [1, 2, 30, 40]}], [], {"api_key": "test-only"}))
    assert "现有证据不足" in result["answer"]
    assert result["answer_audit"]["status"] == "insufficient"
    assert model.await_count == 1


@pytest.mark.parametrize("text", [
    "The model used `999 GPUs`", "`The model used 999 GPUs`", "The model used ``999 GPUs``",
])
def test_code_format_cannot_hide_an_unsupported_quantity(text):
    raw = draft(text)
    result = answer_contract.inspect_draft(raw, [SOURCE])
    assert result['status'] == 'fallback'
    assert result['raw_response'] == raw
    assert '代码标记' in result['issues'][0]


@pytest.mark.parametrize("text,expected", [
    ("24B模型训练360B tokens后保持稳定，没有发散", "fallback"),
    ("The 24B model trained on 360B tokens exhibits model divergence", "checked"),
])
def test_outcome_relation_gate_uses_current_claim_source_and_preserves_draft(text, expected):
    sources = [{"text": "The 24B model is stable after 360B tokens.", "page": 3},
               {"text": "With 24B parameters and 360B tokens, the intervention causes model divergence.", "page": 9}]
    raw = draft(text, refs=[2])
    audit = answer_contract.inspect_draft(raw, sources)
    assert audit["status"] == expected
    assert audit["raw_response"] == raw
    assert audit["draft"] == text + " [2]"
    assert audit["semantic_entailment_verified"] is False
    if expected == "fallback":
        assert any("relation_unverified" in c["risk_codes"] for c in audit["citation_checks"]["claims"])
        assert any("未核验" in issue and "[2]" in issue for issue in audit["issues"])


@pytest.mark.parametrize("prefix", ["~~~", "~~~~python ", "   ~~~", "> ~~~", "- ~~~", "1. ~~~", "> - ~~~"])
def test_tilde_fences_cannot_mask_fact_items_or_later_items(prefix):
    raw = json.dumps({"claims": [
        {"text": "The training run used 6 GPUs", "source_indices": [1]},
        {"text": prefix + "The training run used 999 GPUs", "source_indices": [1]},
    ], "insufficient_evidence": False})
    audit = answer_contract.inspect_draft(raw, [SOURCE])
    assert audit["status"] == "fallback"
    assert audit["raw_response"] == raw
    assert "代码标记" in audit["issues"][0]


@pytest.mark.parametrize("separator", ["\n", "\r", "\r\n", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"])
def test_all_line_separators_are_rejected_in_factual_items(separator):
    raw = draft("The training run used 6 GPUs" + separator + "~~~999 GPUs")
    audit = answer_contract.inspect_draft(raw, [SOURCE])
    assert audit["status"] == "fallback"
    assert audit["raw_response"] == raw


def test_one_unreviewed_item_cannot_hide_behind_a_valid_item(monkeypatch):
    checker = answer_contract.answer_validation.check_citation_consistency
    def skip_one(text, sources):
        result = checker(text, sources)
        if "unreviewed" in text:
            result["claims"] = []
        return result
    monkeypatch.setattr(answer_contract.answer_validation, "check_citation_consistency", skip_one)
    raw = json.dumps({"claims": [
        {"text": "The training run used 6 GPUs", "source_indices": [1]},
        {"text": "The unreviewed run used 999 GPUs", "source_indices": [1]},
    ], "insufficient_evidence": False})
    audit = answer_contract.inspect_draft(raw, [SOURCE])
    assert audit["status"] == "fallback"
    assert audit["citation_checks"]["unreviewed_claim_indices"] == [2]
    assert audit["citation_checks"]["claims"][0]["structured_claim_index"] == 1


def test_nonempty_fact_cannot_be_promoted_when_checker_returns_no_claims(monkeypatch):
    monkeypatch.setattr(answer_contract.answer_validation, "check_citation_consistency",
                        lambda *_: {"claims": [], "semantic_entailment_verified": False})
    audit = answer_contract.inspect_draft(draft(), [SOURCE])
    assert audit["status"] == "fallback"
    assert audit["citation_checks"]["unreviewed_claim_indices"] == [1]
    assert audit["semantic_entailment_verified"] is False


@pytest.mark.parametrize("text", [
    "The mathematical relation is x ~ y with 6 samples",
    "The learning rate is 10^-3",
    "The count is approximately ~~6~~ 6 samples",
])
def test_non_fence_math_notation_remains_available(text):
    audit = answer_contract.inspect_draft(draft(text), [{"text": text, "page": 2}])
    assert audit["status"] == "checked"
    assert audit["citation_checks"]["claims"]
    assert audit["semantic_entailment_verified"] is False
