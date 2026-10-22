"""Citation rule regressions, with no model/network calls or answer rewriting."""
import hashlib
import json
from pathlib import Path

import pytest

from backend.answer_validation import (
    EVIDENCE_RULES_ADDITION,
    citation_markers,
    check_citation_consistency,
    concise_review_warnings,
)


def source(text, page=1, paper_id="p"):
    return {"id": f"{paper_id}-{page}", "paper_id": paper_id, "page": page, "text": text}


def risks(result):
    return {risk for claim in result["claims"] for risk in claim["risk_codes"]}


@pytest.mark.parametrize("unit", ["B", "M", "T", "K"])
def test_attached_parameter_scales_are_checked_as_numbers_and_units(unit):
    result = check_citation_consistency(f"总量为671{unit}，激活量为37{unit} [1]。", [source(f"671{unit} total and 37{unit} active parameters.")])
    assert result["method"] == "lexical_rules"
    assert result["semantic_entailment_verified"] is False
    claim = result["claims"][0]
    assert {item["literal"] for item in claim["numbers"]} == {"671", "37"}
    assert {item["literal"] for item in claim["quantities"]} == {f"671{unit}", f"37{unit}"}
    assert claim["status"] == "matched_literals"
    assert all(item["matched_source_indices"] == [1] for item in claim["quantities"])


def test_scale_mismatch_is_not_hidden_by_matching_bare_number():
    result = check_citation_consistency("总参数为37B [1]。", [source("37M parameters.")])
    claim = result["claims"][0]
    assert claim["numbers"][0]["status"] == "matched_literal"
    assert claim["quantities"][0]["status"] == "not_found_in_cited_sources"
    assert "literal_not_found" in risks(result)
    warning = concise_review_warnings(result)[0]
    assert "37B" in warning and "[1]" in warning


def test_word_scale_alias_and_scientific_notation_keep_legacy_matching():
    result = check_citation_consistency("共12小时，容差1e-9，beta为0.9，参数671B [1]。", [source("12 hours; tolerance 10−9; beta 0.90; 671 billion parameters.")])
    assert all(c["status"] == "matched_literals" for c in result["claims"])


def test_identifiers_bibliography_and_list_numbers_are_not_fact_values():
    result = check_citation_consistency("1. 模型参数为99B [1]。", [source("V99, P100, version v1 and references [99], [1, 2].")])
    claim = result["claims"][0]
    assert [n["literal"] for n in claim["numbers"]] == ["99"]
    assert claim["numbers"][0]["status"] == "not_found_in_cited_sources"


def test_bullets_are_not_supported_by_a_later_paragraph_citation():
    result = check_citation_consistency("## 参数摘要\n\n- **总参数量**：671B\n- **每个 token 激活的参数量**：37B\n- 使用稀疏专家\n\n原文为“671B total, 37B active” [1]。", [source("671B total, 37B active")])
    assert len(result["claims"]) == 4
    assert all("missing_citation" in c["risk_codes"] for c in result["claims"][:3])
    assert result["claims"][-1]["quotes"][0]["matched_source_indices"] == [1]


def test_nonfactual_headings_and_polite_or_insufficient_evidence_notes_are_not_red_flags():
    answer = "# 训练设置\n结论：\n- 参数设置\n谢谢你的提问。\n现有证据不足，无法确定。\n\n使用稀疏专家 [1]。"
    result = check_citation_consistency(answer, [source("We use sparse experts.")])
    assert len(result["claims"]) == 1
    assert result["claims"][0]["status"] == "no_checkable_literals"
    assert concise_review_warnings(result) == []


def test_numeric_fact_heading_is_checked_but_plain_section_heading_is_not():
    result = check_citation_consistency("## 训练设置\n## 使用8块GPU\n## 训练12小时 [1]", [source("12 hours with 4 GPUs.")])
    assert len(result["claims"]) == 2
    assert result["claims"][0]["numbers"][0]["literal"] == "8"
    assert "missing_citation" in result["claims"][0]["risk_codes"]
    assert result["claims"][1]["status"] == "matched_literals"


def test_marker_offsets_handle_combined_citations_and_skip_quotes_and_code():
    answer = '事实 [1, 2]。原文“原书目 [99]” [3]。\n```text\ncode [77]\n```\n"quoted [88]" [4][5]'
    markers = list(citation_markers(answer))
    assert [indices for _, _, indices in markers] == [[1, 2], [3], [4], [5]]
    assert [answer[start:end] for start, end, _ in markers] == ["[1, 2]", "[3]", "[4]", "[5]"]


def test_marker_offsets_preserve_multiline_and_escaped_quotations():
    answer = '“原文跨行\n[99]继续” [1]。"quoted \\"[88]\\" text" [2]'
    assert [indices for _, _, indices in citation_markers(answer)] == [[1], [2]]


@pytest.mark.parametrize("code", ["`lookup[99] = 671B`", "``lookup`[99] = 671B``", "```lookup``[99] = 671B```", "``lookup```[99] = 671B``"])
def test_inline_code_numbers_and_markers_are_ignored_but_external_sources_remain(code):
    answer = f"示例：{code}\n使用8 GPUs [1]，训练12小时 [2]。"
    markers = list(citation_markers(answer))
    assert [answer[start:end] for start, end, _ in markers] == ["[1]", "[2]"]
    result = check_citation_consistency(answer, [source("8 GPUs"), source("12 hours")])
    assert len(result["claims"]) == 2
    assert risks(result) == set()
    assert {item["literal"] for claim in result["claims"] for item in claim["numbers"]} == {"8", "12"}


def test_inline_code_cannot_split_or_lend_a_citation_to_surrounding_prose():
    result = check_citation_consistency("总量37B，函数为`lookup[99]; value = 671B` [1]。", [source("37B total")])
    assert len(result["claims"]) == 1
    assert result["claims"][0]["source_indices"] == [1]
    assert [item["literal"] for item in result["claims"][0]["numbers"]] == ["37"]
    assert risks(result) == set()


@pytest.mark.parametrize("text", ["`lookup[99]，训练12小时 [1]。", "``lookup[99]`，训练12小时 [1]。"])
def test_unmatched_inline_code_does_not_hide_later_claims_or_citations(text):
    assert [indices for _, _, indices in citation_markers(text)] == [[99], [1]]
    result = check_citation_consistency(text, [source("12 hours")])
    assert any(c["source_indices"] == [1] and any(n["literal"] == "12" for n in c["numbers"]) for c in result["claims"])


def test_inline_code_matching_does_not_cross_fences_and_handles_same_line_triple_ticks():
    answer = "```lookup[99]``` [1]\n```python\n`value[88]\n```\n`value[77]` [2]"
    assert [indices for _, _, indices in citation_markers(answer)] == [[1], [2]]


def test_paired_multiline_code_is_not_an_uncited_numeric_claim():
    answer = "示例：`lookup[88] = 228B\n[99] = 671B`\n总量37B [1]。"
    assert [indices for _, _, indices in citation_markers(answer)] == [[1]]
    result = check_citation_consistency(answer, [source("37B total")])
    assert len(result["claims"]) == 1
    assert risks(result) == set()


@pytest.mark.parametrize("join", ["，", "；", "; ", ". "])
def test_parallel_claims_cannot_borrow_the_other_clauses_citation(join):
    result = check_citation_consistency(f"总量为671B [1]{join}激活量为37B [2]。", [source("37B active."), source("671B total.")])
    assert len(result["claims"]) == 2
    assert [c["source_indices"] for c in result["claims"]] == [[1], [2]]
    assert all(c["status"] == "needs_review" for c in result["claims"])
    assert all(not c["quantities"][0]["matched_source_indices"] for c in result["claims"])


def test_quotation_matches_its_own_citation_not_any_other_quote_on_the_line():
    answer = '原文说“alpha evidence is stable” [1]；另处说“beta result is robust” [2]。'
    result = check_citation_consistency(answer, [source("beta result is robust"), source("alpha evidence is stable")])
    assert len(result["claims"]) == 2
    assert all(c["quotes"][0]["status"] == "not_found_in_cited_sources" for c in result["claims"])


def test_quote_cannot_be_assembled_across_sources_and_embedded_citations_are_preserved():
    split = check_citation_consistency('原文为“alpha evidence is stable” [1][2]。', [source("alpha evidence"), source("is stable")])
    assert split["claims"][0]["quotes"][0]["matched_source_indices"] == []
    exact = check_citation_consistency('原文为“alpha [99] is stable” [1]。', [source("alpha [99] is stable")])
    assert exact["claims"][0]["source_indices"] == [1]
    assert exact["claims"][0]["invalid_source_indices"] == []
    assert exact["claims"][0]["quotes"][0]["matched_source_indices"] == [1]


def test_shared_citation_group_keeps_per_source_matches_and_warns_about_combination():
    result = check_citation_consistency("模型总量为671B且激活37B [1, 2]。", [source("671B total.", 4), source("37B active.", 47)])
    claim = result["claims"][0]
    assert claim["quantities"][0]["matched_source_indices"] == [1]
    assert claim["quantities"][1]["matched_source_indices"] == [2]
    assert claim["joint_literal_source_indices"] == []
    assert {"split_source_support", "multiple_source_contexts"} <= risks(result)
    warning = concise_review_warnings(result)[0]
    assert "671B" in warning and "37B" in warning and "[1]" in warning and "[2]" in warning


def test_explicit_comparison_with_individual_citations_is_not_treated_as_one_experiment():
    result = check_citation_consistency("实验A总量671B [1]，实验B总量37B [2]。", [source("671B total", 4), source("37B total", 47)])
    assert risks(result) == set()
    assert result["semantic_entailment_verified"] is False


def test_anaphoric_attribution_remains_a_risk_without_any_numbers():
    result = check_citation_consistency("这些是消融实验中的基线模型，并非主模型 [1]。", [source("We compare FP8 and BF16 training.", 47)])
    assert "context_attribution_unverified" in risks(result)
    assert "同一表格、实验和配置" in concise_review_warnings(result)[0]


def test_unnecessary_supplement_is_a_scope_warning_not_a_falsehood_verdict():
    result = check_citation_consistency("补充说明：大模型训练3.5天 [1]。", [source("Big models train 3.5 days.")])
    assert result["claims"][0]["quantities"][0]["status"] == "matched_literal"
    assert risks(result) == {"supplement_scope_review"}
    assert "所必需" in concise_review_warnings(result)[0]
    assert "语义" in result["limitations"]


def test_invalid_source_and_unquoted_unreferenced_fact_remain_actionable():
    result = check_citation_consistency("模型采用稀疏路由。训练3.5天 [99]。", [source("3.5 days")])
    assert {"missing_citation", "invalid_citation", "literal_not_found"} <= risks(result)
    assert any("[99]" in warning for warning in concise_review_warnings(result))


def test_warnings_are_bounded_prioritized_and_count_remaining_risks():
    answer = "\n".join(f"- 参数量为{i}B [{i}]" for i in range(1, 7))
    result = check_citation_consistency(answer, [source("1M parameters")])
    warnings = concise_review_warnings(result)
    assert len(warnings) == 3
    assert "超出" in warnings[0]
    assert "另有" in warnings[-1]
    assert concise_review_warnings(result, limit=0) == []


def test_original_record_flags_known_risks_without_modifying_saved_answers():
    path = Path(__file__).parents[1] / "test-results/model-integration.json"
    if not path.exists():
        pytest.skip("Local recorded model response is not part of the repository fixture")
    original = path.read_bytes()
    report = json.loads(original)
    record = next(item for item in report["checks"] if item.get("kind") == "answer" and item["filename"].startswith("2412"))
    result = check_citation_consistency(record["result"]["answer"], record["result"]["sources"])
    assert {"missing_citation", "context_attribution_unverified", "supplement_scope_review"} <= risks(result)
    assert any(c["source_indices"] == [7] and "context_attribution_unverified" in c["risk_codes"] for c in result["claims"])
    assert result["semantic_entailment_verified"] is False
    assert hashlib.sha256(path.read_bytes()).digest() == hashlib.sha256(original).digest()


def test_prompt_requires_local_binding_and_matching_experiment_context():
    assert "Markdown列表项" in EVIDENCE_RULES_ADDITION
    assert "表头、表注" in EVIDENCE_RULES_ADDITION
    assert "不同页面、表格、消融实验" in EVIDENCE_RULES_ADDITION
    assert "不代表事实" in EVIDENCE_RULES_ADDITION


@pytest.mark.parametrize("markers", ["[1][2]", "[1]、[2]", "[1], [2]", "[1] and [2]", "[1, 2]"])
def test_prefix_source_enumerations_bind_one_following_claim(markers):
    result = check_citation_consistency(f"SOURCE {markers} 报告总量为16B。", [source("16B total"), source("16B total")])
    assert len(result["claims"]) == 1
    claim = result["claims"][0]
    assert claim["source_indices"] == [1, 2]
    assert claim["citation_binding"]["placement"] == "prefix"
    assert risks(result) == set()


def test_prefix_and_matching_suffix_remain_one_claim_including_supplement_label():
    result = check_citation_consistency("补充说明：SOURCE [1]、[2] 报告16B模型 [1][2]。", [source("16B model"), source("16B model")])
    assert len(result["claims"]) == 1
    assert result["claims"][0]["citation_binding"]["placement"] == "mixed"
    assert risks(result) == {"supplement_scope_review"}


def test_conflicting_prefix_cannot_lend_its_number_to_the_closer_suffix():
    result = check_citation_consistency("根据 [1]，总量为16B [2]。", [source("16B total"), source("32B total")])
    claim = result["claims"][0]
    assert claim["source_indices"] == [2]
    assert claim["citation_binding"]["prefix_source_indices"] == [1]
    assert claim["quantities"][0]["matched_source_indices"] == []
    assert {"citation_binding_conflict", "literal_not_found"} <= risks(result)
    assert "不能拼接" in concise_review_warnings(result)[0]


def test_prefix_does_not_support_the_next_sentence_or_other_parallel_claim():
    result = check_citation_consistency("[1] 总量为16B。激活量为2B。根据 [2]，总量为32B。", [source("16B total; 2B active"), source("32B total")])
    assert [claim["source_indices"] for claim in result["claims"]] == [[1], [], [2]]
    assert "missing_citation" in result["claims"][1]["risk_codes"]


@pytest.mark.parametrize(("answer", "text", "relation"), [
    ("基线模型为24B [1]。", "A baseline model comprises 24B parameters.", "baseline"),
    ("The control model has 24B parameters [1].", "对照模型包含24B参数。", "control"),
    ("消融实验采用24B模型 [1]。", "The ablation study uses a 24B model.", "ablation"),
    ("The experimental group uses 24B parameters [1].", "实验组使用24B参数。", "experiment"),
])
def test_relation_literal_matches_across_languages_but_does_not_claim_entailment(answer, text, relation):
    result = check_citation_consistency(answer, [source(text)])
    checks = result["claims"][0]["relation_checks"]
    assert checks == [{"relation": relation, "quantity": "24B", "source_index": 1,
                       "page": 1, "claim_polarity": "positive", "status": "local_relation_literal",
                       "reason": "relation_word_near_quantity", "context_excerpt": checks[0]["context_excerpt"]}]
    assert risks(result) == set()
    assert result["semantic_entailment_verified"] is False


def test_relation_from_another_source_cannot_support_a_matching_number():
    result = check_citation_consistency("24B是基线模型 [1][2]。", [source("Divergence was observed in a 24B model.", 9), source("We trained a baseline model with 24B parameters.", 7)])
    claim = result["claims"][0]
    assert claim["quantities"][0]["matched_source_indices"] == [1, 2]
    assert [(c["source_index"], c["status"]) for c in claim["relation_checks"]] == [(1, "relation_unverified"), (2, "local_relation_literal")]
    assert claim["relation_checks"][0]["reason"] == "relation_term_absent"
    warning = concise_review_warnings(result)[0]
    assert all(value in warning for value in ["24B", "基线", "[1]", "第 9 页", "未核验"])
    assert "错误" not in warning


@pytest.mark.parametrize("text", [
    "The baseline model has 48B parameters. A separate model has 24B parameters.",
    "The baseline model has 48B parameters, whereas a separate model has 24B parameters.",
    "基线模型为48B，但另一模型为24B。",
])
def test_other_sentence_or_contrast_clause_cannot_lend_a_relation(text):
    result = check_citation_consistency("24B是基线模型 [1]。", [source(text)])
    check = result["claims"][0]["relation_checks"][0]
    assert check["status"] == "relation_unverified"
    assert check["reason"] == "relation_term_in_other_context"
    assert "48b" not in check["context_excerpt"]


def test_claim_contrast_clauses_do_not_apply_baseline_to_the_other_model():
    result = check_citation_consistency("基线模型24B，而新模型48B [1]。", [source("The baseline model has 24B parameters, whereas the new model has 48B parameters.")])
    assert [c["quantity"] for c in result["claims"][0]["relation_checks"]] == ["24B"]
    assert risks(result) == set()


@pytest.mark.parametrize("text", ["The 24B model is not a baseline.", "24B模型不是基线。"])
def test_explicit_negated_role_is_not_a_positive_relation_match(text):
    result = check_citation_consistency("24B是基线模型 [1]。", [source(text)])
    check = result["claims"][0]["relation_checks"][0]
    assert check["status"] == "relation_unverified"
    assert check["reason"] == "relation_polarity_unverified"


def test_matching_negative_relation_is_still_only_a_literal_match():
    result = check_citation_consistency("24B不是基线模型 [1]。", [source("The 24B model is not a baseline.")])
    assert result["claims"][0]["relation_checks"][0]["claim_polarity"] == "negative"
    assert risks(result) == set()
    assert result["semantic_entailment_verified"] is False


def test_source_without_the_claimed_quantity_does_not_get_a_fabricated_relation_target():
    result = check_citation_consistency("24B是基线模型 [1][2]。", [source("A baseline has 24B parameters."), source("A method uses 48B parameters.")])
    assert [c["source_index"] for c in result["claims"][0]["relation_checks"]] == [1]
    assert "relation_unverified" not in risks(result)


def test_relation_without_an_explicit_referent_remains_unverified():
    result = check_citation_consistency("这些属于基线模型 [1]。", [source("The baseline model has 24B parameters.")])
    check = result["claims"][0]["relation_checks"][0]
    assert check["quantity"] is None
    assert check["reason"] == "referent_not_explicit"
    assert {"relation_unverified", "context_attribution_unverified"} <= risks(result)


def test_roles_and_numbers_inside_inline_code_do_not_trigger_relation_checks():
    result = check_citation_consistency("参数为24B，示例代码 `baseline = 48B` [1]。", [source("24B parameters")])
    assert result["claims"][0]["relation_checks"] == []
    assert risks(result) == set()


def test_v3_record_identifies_baseline_gap_for_the_specific_source_without_new_calls():
    path = Path(__file__).parents[1] / "test-results/model-v3-integration.json"
    if not path.exists():
        pytest.skip("Local recorded response is not a repository fixture")
    original = path.read_bytes()
    record = next(item for item in json.loads(original)["checks"] if item.get("kind") == "answer" and item["filename"].startswith("2412"))
    result = check_citation_consistency(record["result"]["answer"], record["result"]["sources"])
    supplement = next(c for c in result["claims"] if "supplement_scope_review" in c["risk_codes"])
    assert supplement["source_indices"] == [2, 6, 7]
    assert supplement["citation_binding"]["conflict"] is False
    baseline = [c for c in supplement["relation_checks"] if c["relation"] == "baseline" and c["quantity"] == "16B"]
    assert [(c["source_index"], c["page"], c["status"]) for c in baseline] == [(2, 48, "relation_unverified"), (6, 47, "local_relation_literal")]
    assert "missing_citation" in result["claims"][0]["risk_codes"]
    assert path.read_bytes() == original


@pytest.mark.parametrize("answer,text,relation,polarity", [
    ("24B模型保持稳定 [1]。", "The 24B model remains stable.", "stability", "positive"),
    ("24B模型保持稳定 [1]。", "We observe stability in the 24B model.", "stability", "positive"),
    ("The 24B model is unstable [1].", "24B模型不稳定。", "stability", "negative"),
    ("24B模型不稳定 [1]。", "We observe instability in the 24B model.", "stability", "negative"),
    ("24B模型发生发散 [1]。", "The 24B model diverged.", "divergence", "positive"),
    ("The 24B model diverges [1].", "24B模型出现发散。", "divergence", "positive"),
    ("24B模型没有发散 [1]。", "The 24B model shows no divergence.", "divergence", "negative"),
    ("The 24B model did not diverge [1].", "24B模型未出现发散。", "divergence", "negative"),
    ("24B模型没有发散 [1]。", "For the 24B model, divergence was not observed.", "divergence", "negative"),
    ("24B模型没有发散 [1]。", "The 24B model did not show divergence.", "divergence", "negative"),
])
def test_explicit_outcome_polarity_matches_locally_without_semantic_verdict(answer, text, relation, polarity):
    result = check_citation_consistency(answer, [source(text)])
    check = result["claims"][0]["relation_checks"][0]
    assert (check["relation"], check["claim_polarity"], check["status"]) == (relation, polarity, "local_relation_literal")
    assert risks(result) == set()
    assert result["semantic_entailment_verified"] is False


@pytest.mark.parametrize("answer,text", [
    ("24B模型保持稳定 [1]。", "The 24B model is unstable."),
    ("24B模型不稳定 [1]。", "The 24B model remains stable."),
    ("24B模型没有发散 [1]。", "The 24B model diverged."),
    ("24B模型出现发散 [1]。", "The 24B model shows no divergence."),
])
def test_opposite_outcome_polarity_is_unverified_even_when_numbers_match(answer, text):
    result = check_citation_consistency(answer, [source(text)])
    check = result["claims"][0]["relation_checks"][0]
    assert check["status"] == "relation_unverified"
    assert check["reason"] == "relation_polarity_unverified"
    assert "literal_not_found" not in risks(result)
    assert "relation_unverified" in risks(result)


def test_same_numbers_do_not_let_stability_borrow_from_divergent_experiment():
    sources = [source("The 24B model stays stable after 360B tokens.", 3),
               source("With 24B parameters and 360B tokens, the intervention causes divergence.", 9)]
    result = check_citation_consistency("24B模型训练360B tokens后保持稳定，没有发散 [2]。", sources)
    checks = result["claims"][0]["relation_checks"]
    assert all(c["source_index"] == 2 and c["status"] == "relation_unverified" for c in checks)
    assert {c["relation"] for c in checks} == {"stability", "divergence"}
    assert {c["reason"] for c in checks} == {"relation_term_absent", "relation_polarity_unverified"}
    assert all(f["matched_source_indices"] == [2] for f in result["claims"][0]["quantities"])
    assert any("[2]" in warning and "未核验" in warning for warning in concise_review_warnings(result))


def test_no_divergence_does_not_automatically_prove_stability():
    result = check_citation_consistency("24B模型保持稳定 [1]。", [source("The 24B model shows no divergence.")])
    check = result["claims"][0]["relation_checks"][0]
    assert check["relation"] == "stability"
    assert check["reason"] == "relation_term_absent"
    assert check["status"] == "relation_unverified"


def test_verbatim_outcome_quote_uses_full_quote_check_not_unanchored_relation_guess():
    matched = check_citation_consistency('原文为“The result is stable” [1]。', [source("The result is stable.")])
    mismatch = check_citation_consistency('原文为“The result is stable” [1]。', [source("The result is unstable.")])
    assert risks(matched) == set()
    assert "literal_not_found" in risks(mismatch)
    # Quoting just the role word cannot bypass the outcome check.
    role = check_citation_consistency('24B模型保持“stable” [1]。', [source("24B is unstable. A different model is stable.")])
    assert "relation_unverified" in risks(role)
