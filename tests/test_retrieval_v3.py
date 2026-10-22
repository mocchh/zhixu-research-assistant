"""Known regression behaviours, using synthetic sources and no model service."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from backend import intelligence


def paper(identifier="p", title="Synthetic systems report"):
    return {"id": identifier, "title": title, "filename": identifier + ".pdf", "version": 1, "status": "ready"}


def block(identifier, text, paper_id="p", order=0, **changes):
    return {"id": identifier, "text": text, "paper_id": paper_id, "page": 3,
            "kind": "paragraph", "section": "Methods", "order_index": order,
            "bbox": [20, 30 + 35 * order, 280, 60 + 35 * order], **changes}


def test_numbered_memory_stages_are_evidence_not_short_plot_legends():
    intro = block("intro", "The optimizer, gradient and parameter partitioning stages save memory at different communication costs. When enabled cumulatively:")
    stages = [
        block("stage-a", "1) Optimizer partitioning: 3x memory reduction, unchanged communication volume;", order=1),
        block("stage-b", "2) Gradient partitioning: 6x memory reduction, unchanged communication volume;", order=2),
        block("stage-c", "3) Parameter partitioning: linear memory savings with degree, increased communication volume.", order=3),
    ]
    distractors = [block(f"discussion-{i}", f"We evaluate optimizer, gradient and parameter partitioning across {i+10} training settings. These stages help distributed models and improve the experimental results.", order=i+8)
                   for i in range(12)]
    sources = intelligence.retrieve("逐步启用 optimizer、gradient 和 parameter partitioning，阶段分别节省多少内存，通信代价是什么？",
                                    [paper()], [intro, *stages, *distractors], limit=8)
    assert {b["id"] for b in stages} <= {s["id"] for s in sources}
    originals = {b["id"]: b for b in [intro, *stages, *distractors]}
    assert len(sources) <= 8
    assert all(s["text"] == originals[s["id"]]["text"] and s["bbox"] == originals[s["id"]]["bbox"] for s in sources)


def test_cross_paper_mask_fraction_is_not_confused_with_total_training_tokens():
    papers = [paper("a", "Orion: A text model"), paper("b", "Vela: A text model")]
    blocks = []
    for pid, name, fraction in [("a", "Orion", 20), ("b", "Vela", 30)]:
        blocks.append(block(pid + "-objective", f"We drop out {fraction}% of tokens. The masked spans are replaced by a sentinel token during pre-training.", pid, section="Pretraining objective"))
        for index in range(10):
            blocks.append(block(f"{pid}-{index}", f"{name} pretraining uses {index+1} trillion tokens in the training corpus, with a token batch size of 2048.", pid, index+5, section="Training data"))
    sources = intelligence.retrieve("比较 Orion 与 Vela 各自预训练删除多少比例的 token，用什么标记替换遮住的片段？", papers, blocks, limit=8)
    assert {"a-objective", "b-objective"} <= {s["id"] for s in sources}


def test_cross_paper_memory_analysis_and_default_optimizer_configuration():
    papers = [paper("a", "Orion: Memory efficient models"), paper("b", "Preference Learning: A training method")]
    targets = [block("memory", "Mixed precision AdaGrad stores optimizer states and parameters in memory. We count storage in bytes.", "a"),
               block("default", "Unless noted otherwise, we use the RMSprop optimizer by default, with a learning rate of 0.002.", "b")]
    distractors = [block(f"{pid}-{n}", f"We study training model performance under {n+1} settings. Orion and PL training methods are evaluated on the benchmark.", pid, n+3)
                   for pid in ("a", "b") for n in range(10)]
    sources = intelligence.retrieve("分别根据 Orion 的混合精度状态内存分析和 PL 的默认训练设置，讨论或采用什么优化器？", papers, [*targets, *distractors], limit=8)
    assert {"memory", "default"} <= {s["id"] for s in sources}


@pytest.mark.parametrize("phrase", ["Helios 原论文", "Helios 的原始论文", "Helios original paper"])
def test_alphabetic_original_name_missing_cannot_use_secondary_body_mentions(monkeypatch, phrase):
    model = AsyncMock()
    monkeypatch.setattr(intelligence, "_generate", model)
    result = asyncio.run(intelligence.answer_question(phrase + " 的公式是什么？", [paper(title="Vela technical report")],
                                                     [block("secondary", "Helios uses the equation h = Wx, according to earlier work.")], [], {"api_key": "fake-only"}))
    assert result["sources"] == []
    assert any("Helios" in w and "原论文" in w for w in result["warnings"])
    model.assert_not_awaited()


def test_named_original_present_narrows_scope_without_excluding_itself():
    papers = [paper("a", "Helios: State space models"), paper("b", "Vela technical report")]
    blocks = [block("original", "Our state space formula is h = Wx.", "a"),
              block("secondary", "Helios formula formula formula h = Wy.", "b")]
    sources = intelligence.retrieve("Helios 原论文的状态空间公式是什么？", papers, blocks)
    assert sources and {s["paper_id"] for s in sources} == {"a"}


def test_two_explicit_originals_form_a_union_and_missing_one_refuses_complete_comparison():
    papers = [paper("a", "Helios: Training models"), paper("b", "Vela: Training models")]
    blocks = [block("a-source", "We train for 8 hours.", "a"), block("b-source", "We train for 16 hours.", "b")]
    question = "Helios 原论文与 Vela 原论文的训练耗时各是多少？"
    sources = intelligence.retrieve(question, papers, blocks)
    assert {s["paper_id"] for s in sources} == {"a", "b"}
    assert intelligence.retrieve(question, papers[:1], blocks) == []


def test_table_condition_and_units_keep_original_rows_available():
    table = block("measurements", "Table 8: Capacity and measured throughput\nModel | Capacity factor | Examples/sec | Hours\nCedar | 1.0 | 780 | 45.2\nElm | 1.0 | 910 | 36.1", kind="table")
    other = block("other", "Table 9: Classification accuracy\nModel | Capacity factor | Accuracy\nCedar | 2.0 | 87.2\nElm | 2.0 | 89.3", order=1, kind="table")
    sources = intelligence.retrieve("Table 8 中 capacity factor 为1.0时 Cedar 与 Elm 的速度 examples/sec 和 hours 是多少？", [paper()], [other, table], limit=1)
    assert sources[0]["id"] == "measurements"
    assert sources[0]["text"] == table["text"] and sources[0]["bbox"] == table["bbox"]


@pytest.mark.parametrize("abstract", [
    "Our method improves on Helios on three benchmarks.",
    "We compare our method with Helios and train the model on a new dataset.",
    "We introduce a new model based on Helios.",
    "We introduce an evaluation of Helios.",
    "We train Helios on our dataset.",
    "Earlier work by Smith introduced Helios.",
])
def test_secondary_abstract_mentions_do_not_identify_original_source(abstract):
    candidate = {**paper(title="Vela: Better state space models"), "abstract": abstract}
    sources = intelligence.retrieve("Helios 原论文的公式是什么？", [candidate], [block("secondary", "The Helios formula is h = Wx.")])
    assert sources == []


@pytest.mark.parametrize("metadata", [
    {"title": "Helios2: Fast state space models"},
    {"title": "Helios-2: Fast state space models"},
    {"title": "Helios 2: Fast state space models"},
    {"filename": "Helios2.pdf"},
    {"abstract": "We introduce Helios2, a state space model."},
    {"abstract": "We propose a new architecture, the Helios2, for sequence modeling."},
])
def test_original_identity_does_not_accept_name_substrings_or_other_generations(metadata):
    candidate = {**paper(), **metadata}
    assert intelligence.retrieve("Helios 原论文的公式是什么？", [candidate], [block("wrong", "Our formula is h = Wx.")]) == []


@pytest.mark.parametrize("name,abstract", [
    ("Helios", "We introduce Helios, a state space model."),
    ("Helios", "We propose a new architecture called Helios for sequences."),
    ("Helios", "We call our method Helios."),
    ("Transformer", "We propose a new simple network architecture, the Transformer, based solely on attention mechanisms."),
    ("GPT-3", "Specifically, we train GPT-3, an autoregressive language model with 175 billion parameters."),
    ("Janus-4", "We report the development of Janus-4, a multimodal model."),
])
def test_explicit_abstract_self_introduction_identifies_named_original(name, abstract):
    candidate = {**paper(), "abstract": abstract}
    sources = intelligence.retrieve(name + " 原论文的公式是什么？", [candidate], [block("original", "Our formula is h = Wx.")])
    assert sources and sources[0]["id"] == "original"


def test_exact_filename_identity_accepts_separator_variants_and_not_larger_generation():
    candidate = {**paper(), "filename": "janus_4.pdf"}
    source = block("original", "Our formula is h = Wx.")
    assert intelligence.retrieve("Janus-4 原论文的公式是什么？", [candidate], [source])
    candidate["filename"] = "janus_40.pdf"
    assert intelligence.retrieve("Janus-4 原论文的公式是什么？", [candidate], [source]) == []
