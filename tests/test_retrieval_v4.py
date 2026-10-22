"""Synthetic document-version and compound-question regressions; no LLM calls."""
import pytest

from backend import intelligence


def paper(pid="p", version=1, **extra):
    return {"id": pid, "title": "Synthetic methods report", "filename": pid + ".pdf",
            "version": version, "arxiv_id": "2501.12345", "status": "ready", **extra}


def block(identifier, text, pid="p", **extra):
    return {"id": identifier, "text": text, "paper_id": pid, "page": 2,
            "kind": "paragraph", "section": "Methods", "order_index": 0,
            "bbox": [30, 40, 260, 80], **extra}


@pytest.mark.parametrize("question", [
    "Corpus v2 的实验准确率是多少？", "数据集 v2 的实验准确率是多少？",
    "软件 v2 的实验准确率是多少？", "模型 v2 的实验准确率是多少？",
    "the v2 dataset accuracy", "the v2 software accuracy", "the v2 model accuracy",
    "Dataset Corpus v2 accuracy", "Toolset v2 accuracy", "Model-X v2 accuracy",
    "Corpus v2 paper accuracy", "模型 v2 论文的实验准确率是多少？",
])
def test_entity_versions_do_not_filter_document_revision_and_remain_search_terms(question):
    p = paper(version=7)
    sources = [block("old", "Corpus v1 software model experiment accuracy 71."),
               block("requested", "Corpus v2 software model experiment accuracy 83.")]
    selected, warnings = intelligence._scope_papers(question, [p])
    assert selected == [p] and warnings == []
    assert "v2" in intelligence._query_weights(intelligence._content_question(question))
    assert intelligence.retrieve(question, [p], sources, limit=1)[0]["id"] == "requested"


@pytest.mark.parametrize("question", [
    "论文 v2 的实验准确率是多少？", "文献第2版的实验准确率是多少？",
    "v2版本的实验准确率是多少？", "第2版的实验准确率是多少？",
    "Model-X 第2版的实验准确率是多少？", "the paper version v2 accuracy",
    "v2 paper accuracy", "arXiv:2501.12345v2 的实验准确率是多少？",
])
def test_explicit_paper_revisions_remain_strict(question):
    papers = [paper("old", version=1), paper("wanted", version=2)]
    sources = [block("a", "The experiment accuracy is 72.", "old"), block("b", "The experiment accuracy is 85.", "wanted")]
    result = intelligence.retrieve(question, papers, sources)
    assert result and {s["paper_id"] for s in result} == {"wanted"}
    assert intelligence.retrieve(question, papers[:1], sources) == []


def test_paper_revision_and_dataset_revision_in_same_question_have_separate_roles():
    question = "论文 v3 在 Corpus v2 数据集上的准确率？"
    papers = [paper("old", version=2), paper("wanted", version=3)]
    sources = [block("old-paper", "Corpus v2 accuracy 90.", "old"),
               block("wrong-data", "Corpus v1 accuracy 72.", "wanted"),
               block("right-data", "Corpus v2 accuracy 85.", "wanted")]
    content = intelligence._content_question(question)
    assert "v2" in content and "v3" not in content
    result = intelligence.retrieve(question, papers, sources, limit=1)
    assert result[0]["id"] == "right-data"


@pytest.mark.parametrize("question", ["模型第2版的优化器", "数据集第2版的处理方式", "软件第2版的算法"])
def test_ordinal_entity_versions_are_content_when_entity_is_explicit(question):
    p = paper(version=9)
    assert intelligence._scope_papers(question, [p])[0] == [p]
    assert "第2版" in intelligence._content_question(question)


def test_compound_topics_retrieve_minority_clause_without_increasing_budget():
    dominant = [block(f"compression-{i}", f"Our compression algorithm reduces storage through sparse encoding. The compression ratio is {i+2}, and our sparse encoding method preserves input accuracy.", order_index=i)
                for i in range(14)]
    minority = block("latency", "Decoding latency is bounded by a queue with asynchronous dispatch.", page=9, bbox=[40, 100, 260, 145])
    query = "How does compression ratio depend on sparse encoding storage? What bounds decoding latency?"
    sources = intelligence.retrieve(query, [paper()], [*dominant, minority], limit=8)
    assert len(sources) <= 8
    assert "latency" in {s["id"] for s in sources}
    assert any(s["id"].startswith("compression") for s in sources)
    found = next(s for s in sources if s["id"] == "latency")
    assert found["text"] == minority["text"] and found["bbox"] == minority["bbox"]


def test_four_explicit_topics_each_have_evidence_within_eight_sources():
    topics = [("compression", "Compression ratio depends on sparse encoding storage."),
              ("latency", "Decoding latency is bounded by the dispatch queue."),
              ("checksum", "Packet checksum verification uses parity bits."),
              ("recovery", "Failure recovery restores a durable checkpoint.")]
    sources = [block(name, text, page=i+2) for i, (name, text) in enumerate(topics)]
    sources += [block(f"repeat-{i}", f"Compression ratio depends on sparse encoding storage with compression factor {i+3}.") for i in range(12)]
    query = "How does compression ratio depend on sparse encoding storage? What bounds decoding latency? How does packet checksum verification work? How does failure recovery operate?"
    result = intelligence.retrieve(query, [paper()], sources, limit=8)
    assert {"latency", "checksum", "recovery"} <= {s["id"] for s in result}
    assert any("Compression" in s["text"] for s in result)
    assert len(result) <= 8


def test_compound_coverage_respects_each_selected_paper_and_version():
    papers = [paper("a", version=1), paper("b", version=2)]
    sources = []
    for pid in ("a", "b"):
        sources.extend(block(f"{pid}-compression-{i}", f"Compression ratio depends on sparse encoding storage with factor {i+4}.", pid) for i in range(10))
        sources.append(block(pid + "-latency", "Decoding latency is bounded by asynchronous dispatch.", pid))
    sources.append(block("unselected", "Decoding latency decoding latency.", "outside"))
    query = "How does compression ratio depend on sparse encoding storage? What bounds decoding latency?"
    result = intelligence.retrieve(query, papers, sources, limit=8)
    assert {"a-latency", "b-latency"} <= {s["id"] for s in result}
    assert {s["paper_id"] for s in result} == {"a", "b"}
    assert {s["version"] for s in result} == {1, 2} and len(result) <= 8
