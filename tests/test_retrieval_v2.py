"""Retrieval behaviours on synthetic documents unrelated to acceptance gold."""
from copy import deepcopy

import pytest

from backend.intelligence import retrieve


def paper(identifier="p", title="Synthetic methods report"):
    return {"id": identifier, "title": title, "filename": identifier + ".pdf", "version": 1, "status": "ready"}


def block(identifier, text, *, paper_id="p", page=3, order=0, box=None, section="Methods"):
    return {"id": identifier, "paper_id": paper_id, "text": text, "page": page,
            "order_index": order, "bbox": box or [20, 30 + order * 50, 280, 70 + order * 50],
            "section": section, "kind": "paragraph"}


def test_matrix_products_retrieve_original_formula_with_separate_geometry():
    context = block("context", "Our adaptive transformation has a forward propagation rule. Its low-rank update is computed from the two matrices U and V and applied to the input z:")
    formula = block("formula", "h = W0z + UVz", order=1)
    sources = retrieve("前向传播公式如何组合 W0、U、V 和 z？", [paper()], [context, formula], limit=2)
    actual = next(source for source in sources if source["id"] == "formula")
    assert actual["text"] == formula["text"]
    assert actual["bbox"] == formula["bbox"] != context["bbox"]
    assert len(sources) <= 2


@pytest.mark.parametrize("location,expected", [
    ({}, True),
    ({"page": 4}, False),
    ({"bbox": [330, 80, 580, 120]}, False),
    ({"bbox": [20, 300, 280, 340]}, False),
    ({"paper_id": "other"}, False),
    ({"bbox": [1, 2]}, False),
])
def test_formula_context_never_crosses_page_column_scope_or_invalid_geometry(location, expected):
    context = block("context", "The lattice factorization formula is derived by using a stable algebraic decomposition of the learned transformation. We write its forward operation below:")
    formula = block("formula", "z = UVx", order=1)
    formula.update(location)
    sources = retrieve("lattice factorization formula", [paper()], [context, formula], limit=2)
    assert ("formula" in {source["id"] for source in sources}) is expected


def test_short_numeric_row_is_not_suppressed_as_a_chart_legend():
    row = block("row", "Mars Benchmark\n87 / 100\n93rd percentile")
    captions = [block(str(i), "The Mars Benchmark was administered by the research team and is used to evaluate the model's broad performance.", order=i + 1) for i in range(6)]
    result = retrieve("Mars Benchmark 的得分和百分位是多少？", [paper()], [*captions, row], limit=1)
    assert result[0]["id"] == "row"


def test_chinese_initialization_question_matches_new_symbols_and_method_name():
    target = block("initial", "For the Spectral Adapter, matrix U uses orthogonal initialization, while V is initialized to zero. This makes the initial update vanish.")
    distractor = block("uses", "The Spectral Adapter applies matrices U and V at inference time and compares several downstream performance results.", order=1)
    assert retrieve("Spectral Adapter 如何初始化矩阵 U 和 V，初始增量为什么为零？", [paper()], [distractor, target], limit=1)[0]["id"] == "initial"


def test_multi_paper_token_counts_prioritize_data_over_context_windows():
    papers = [paper("a", "Orion 8: Language systems"), paper("b", "Polaris 6: Language systems")]
    blocks = []
    for identifier, name, amount in [("a", "Orion 8", 9), ("b", "Polaris 6", 14)]:
        blocks.append(block(identifier + "-data", f"Our training corpus contains {amount} billion tokens collected from public documents.", paper_id=identifier, section="Pretraining Data"))
        for index in range(8):
            blocks.append(block(f"{identifier}-window-{index}", f"{name} pretraining expands the context length to {4096 + index} tokens and optimizes sequence length for faster decoding.", paper_id=identifier, order=index + 1, section="Context Extension"))
    sources = retrieve("比较 Orion 8 与 Polaris 6 各自预训练 token 数量。", papers, blocks, limit=4)
    assert {"a-data", "b-data"} <= {source["id"] for source in sources}
    assert len(sources) <= 4


def test_near_duplicate_removal_keeps_changed_numbers_and_negation():
    text = "The retrieval model evaluates benchmark accuracy on a held out dataset after training the encoder with a stable optimizer for 12 hours and using fixed random seeds."
    original = block("original", text)
    almost_copy = block("copy", text.replace("a stable optimizer", "the stable optimizer"), order=1)
    different_number = block("number", text.replace("12 hours", "24 hours"), order=2)
    negative = block("negation", text.replace("using fixed", "not using fixed"), order=3)
    sources = retrieve("retrieval model benchmark training hours", [paper()], [original, almost_copy, different_number, negative], limit=8)
    ids = {source["id"] for source in sources}
    assert len(ids & {"original", "copy"}) == 1
    assert {"number", "negation"} <= ids


def test_live_section_metadata_updates_ranking_without_changing_source_text():
    blocks = [block("one", "The corpus contains 7 billion tokens.", section="Evaluation"), block("two", "The corpus contains 8 billion tokens.", order=1, section="Pretraining")]
    assert retrieve("pretraining token count", [paper()], blocks, limit=1)[0]["id"] == "two"
    changed = deepcopy(blocks)
    changed[0]["section"], changed[1]["section"] = "Pretraining", "Evaluation"
    assert retrieve("pretraining token count", [paper()], changed, limit=1)[0]["id"] == "one"
