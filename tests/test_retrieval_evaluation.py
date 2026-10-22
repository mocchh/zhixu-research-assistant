"""The evaluation must require complete evidence and fail on missing inputs."""
from copy import deepcopy
import hashlib
import json

import pytest

from scripts import evaluate_retrieval as evaluation


def test_equivalent_evidence_requires_every_part_of_a_composite_target():
    sources = [{"filename": "a.pdf", "page": 2, "text": "The normalization is inside the residual block."}]
    location = {"filename": "a.pdf", "page": 2, "terms": ["inside the residual block"]}
    training = {"filename": "a.pdf", "page": 3, "terms": ["without warm-up"]}
    target = {"alternatives": [{"filename": "a.pdf", "page": 1, "terms": ["complete summary"]}, {"all_of": [location, training]}]}
    assert not evaluation.target_matches(sources, target)
    sources.append({"filename": "a.pdf", "page": 3, "text": "Training works without warm-up."})
    assert evaluation.target_matches(sources, target)


def test_missing_pdf_is_not_reported_as_a_success(tmp_path):
    fixture = {"documents": [{"filename": "does-not-exist-for-retrieval-test.pdf", "sha256": "missing"}], "cases": []}
    with pytest.raises(FileNotFoundError):
        evaluation.load_documents(fixture, None, tmp_path)


def test_acceptance_fixture_has_real_hashes_and_multiple_document_categories():
    fixture = json.loads((evaluation.ROOT / "tests/fixtures/retrieval_cases.json").read_text())
    assert len(fixture["cases"]) >= 20
    assert {"scan", "long_book", "version", "missing", "multi_paper", "reference", "formula"} <= {case["category"] for case in fixture["cases"]}
    assert all(len(doc["sha256"]) == 64 for doc in fixture["documents"])


def test_holdout_is_frozen_and_uses_documents_outside_development_fixture():
    directory = evaluation.ROOT / "tests/fixtures"
    original = json.loads((directory / "retrieval_cases.json").read_text())
    holdout_path = directory / "retrieval_holdout.json"
    holdout = json.loads(holdout_path.read_text())
    freeze = json.loads((directory / "retrieval_holdout.freeze.json").read_text())
    original_names = {doc["filename"] for doc in original["documents"]}
    holdout_names = {doc["filename"] for doc in holdout["documents"]}
    assert 6 <= len(holdout_names) <= 10
    assert not (original_names & holdout_names)
    assert len(holdout["cases"]) >= 20
    assert {"numeric", "formula", "multi_paper", "version_scope", "missing_version", "missing_paper"} <= {case["category"] for case in holdout["cases"]}
    assert len({case["id"] for case in holdout["cases"]}) == len(holdout["cases"])
    assert all(set(case["filenames"]) <= holdout_names for case in holdout["cases"])
    assert hashlib.sha256(holdout_path.read_bytes()).hexdigest() == freeze["fixture_sha256"]
    assert hashlib.sha256((directory / "retrieval_cases.json").read_bytes()).hexdigest() == freeze["original_fixture_sha256"]


def test_frozen_evaluation_rejects_changed_gold_and_retriever(tmp_path, monkeypatch):
    fixture = tmp_path / "fixture.json"
    fixture.write_text('{"cases": []}')
    retriever = tmp_path / "retriever.py"
    retriever.write_text("# original implementation")
    monkeypatch.setattr(evaluation.intelligence, "__file__", str(retriever))
    freeze = tmp_path / "freeze.json"
    freeze.write_text(json.dumps({
        "fixture_sha256": hashlib.sha256(fixture.read_bytes()).hexdigest(),
        "retriever_sha256": hashlib.sha256(retriever.read_bytes()).hexdigest(),
    }))
    assert evaluation.verify_freeze(fixture, freeze)["verified"]
    original_gold = fixture.read_bytes()
    fixture.write_text('{"cases": ["changed"]}')
    with pytest.raises(ValueError, match="fixture_sha256 mismatch"):
        evaluation.verify_freeze(fixture, freeze)
    fixture.write_bytes(original_gold)
    retriever.write_text("# changed implementation")
    with pytest.raises(ValueError, match="retriever_sha256 mismatch"):
        evaluation.verify_freeze(fixture, freeze)


def frozen_holdout_report():
    return json.loads((evaluation.ROOT / "tests/fixtures/retrieval_holdout.baseline.json").read_text())


def test_baseline_allows_known_misses_without_claiming_full_pass():
    baseline = frozen_holdout_report()
    result = evaluation.compare_baseline(deepcopy(baseline), baseline)
    assert result["regression_check"] == "passed"
    assert result["known_failed_cases"] == 10
    assert baseline["passed_cases"] == 12
    assert baseline["case_count"] == 22


def test_baseline_rejects_loss_of_one_target_from_already_failing_case():
    baseline = frozen_holdout_report()
    result = deepcopy(baseline)
    case = next(case for case in result["cases"] if case["id"] == "cross_memory_methods")
    assert case["gold_retrieved"] == [True, False] and not case["passed"]
    case["gold_retrieved"] = [False, False]
    checked = evaluation.compare_baseline(result, baseline)
    assert checked["regression_check"] == "failed"
    assert any("previously retrieved gold target lost" in error for error in checked["regressions"])


@pytest.mark.parametrize("field,value,error", [
    ("fixture_sha256", "different-fixture", "fixture_sha256"),
    ("locator_integrity", 0.99, "locator integrity"),
    ("version_or_scope_leaks", 1, "scope leakage"),
    ("gold_available", 21, "gold targets are unavailable"),
])
def test_baseline_never_waives_changed_inputs_or_integrity_failures(field, value, error):
    baseline = frozen_holdout_report()
    result = deepcopy(baseline)
    result[field] = value
    checked = evaluation.compare_baseline(result, baseline)
    assert checked["regression_check"] == "failed"
    assert any(error in item for item in checked["regressions"])


def test_baseline_rejects_missing_document_refusal_regression():
    baseline = frozen_holdout_report()
    result = deepcopy(baseline)
    case = next(case for case in result["cases"] if case["id"] == "absent_original")
    case["missing_source_behavior_passed"] = False
    case["passed"] = False
    checked = evaluation.compare_baseline(result, baseline)
    assert checked["regression_check"] == "failed"
    assert any("missing-source refusal regressed" in item for item in checked["regressions"])
