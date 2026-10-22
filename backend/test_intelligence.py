"""Behaviour tests for retrieval, evidence handling and bounded remote IO."""

import asyncio
import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from backend import intelligence as subject


def paper(paper_id="p1", **changes):
    return {"id": paper_id, "title": "A study of contextual language systems", "family_id": paper_id, "version": 1, **changes}


def block(block_id, text, paper_id="p1", **changes):
    return {"id": block_id, "paper_id": paper_id, "text": text, "page": 2, "bbox": [10, 20, 200, 90], "kind": "paragraph", "section": "Methods", "order_index": 0, **changes}


def run(coroutine):
    return asyncio.run(coroutine)


@pytest.fixture
def settings():
    return {"api_key": "test-private-key", "base_url": "https://model.example/v1", "model": "fixture-model", "timeout_seconds": 5}


def mock_client(monkeypatch, handler):
    original = httpx.AsyncClient
    transport = httpx.MockTransport(handler)

    def client(**kwargs):
        return original(transport=transport, **kwargs)

    monkeypatch.setattr(subject.httpx, "AsyncClient", client)
    monkeypatch.setattr(subject, "_arxiv_throttle", AsyncMock())


def test_bm25_ranks_relevant_text_preserving_source_geometry():
    source = block("optimizer", "We use Adam with beta parameters and an adaptive learning rate.")
    other = block("data", "The corpus contains parallel sentences for translation.")
    result = subject.retrieve("Adam 超参数如何设置？", [paper()], [other, source])
    assert result[0]["id"] == "optimizer"
    assert result[0]["bbox"] == source["bbox"]
    assert result[0]["text"] == source["text"]
    assert result[0]["score"] > 0


def test_chinese_query_can_retrieve_english_context():
    result = subject.retrieve("最大上下文长度是多少？", [paper()], [block("a", "The context window has a fixed sequence length."), block("b", "This work discusses optimization.")])
    assert result[0]["id"] == "a"


def test_chinese_training_query_handles_gpu_plural_and_verb_forms():
    result = subject.retrieve("训练使用多少GPU?", [paper()], [block("a", "We train the model with 8 NVIDIA P100 GPUs.")])
    assert result[0]["id"] == "a"


def test_coverage_does_not_starve_second_paper():
    blocks = [block(f"a{i}", "Architecture encoder attention model " * 3) for i in range(4)]
    blocks.append(block("b", "This architecture uses an encoder.", paper_id="p2"))
    sources = subject.retrieve("architecture encoder", [paper(), paper("p2")], blocks, limit=2)
    assert {source["paper_id"] for source in sources} == {"p1", "p2"}


def test_scope_excludes_unselected_paper_and_does_not_fabricate_matches():
    sources = subject.retrieve("zygomorphic", [paper()], [block("other", "zygomorphic", paper_id="p2"), block("one", "The model trains on text.")])
    assert sources == []


def test_index_cache_reuses_tokens_and_invalidates_edited_text(monkeypatch):
    subject._retrieval_index.cache_clear()
    original = subject._tokens
    documents_tokenized = []

    def tracked(text):
        if text.startswith("Document:"):
            documents_tokenized.append(text)
        return original(text)

    monkeypatch.setattr(subject, "_tokens", tracked)
    blocks = [block("a", "Document: adam optimizer")]
    subject.retrieve("adam", [paper()], blocks)
    blocks[0]["bbox"] = [20, 30, 210, 100]
    second = subject.retrieve("adam", [paper()], [dict(blocks[0])])
    assert len(documents_tokenized) == 1
    assert second[0]["bbox"] == [20, 30, 210, 100]
    blocks[0]["text"] = "Document: momentum instead"
    assert subject.retrieve("adam", [paper()], blocks) == []
    assert len(documents_tokenized) == 2


def test_version_filter_accepts_base_arxiv_id_plus_separate_version():
    papers = [paper("old", arxiv_id="1706.03762", version=1), paper("new", arxiv_id="1706.03762", version=7)]
    blocks = [block("a", "Adam beta 0.9", "old"), block("b", "Adam beta 0.98", "new")]
    assert {s["paper_id"] for s in subject.retrieve("1706.03762v7 Adam", papers, blocks)} == {"new"}
    assert {s["paper_id"] for s in subject.retrieve("v1 的 Adam", papers, blocks)} == {"old"}
    assert subject.retrieve("v9 Adam", papers, blocks) == []
    assert {s["paper_id"] for s in subject.retrieve("v1 与 v7 Adam 比较", papers, blocks)} == {"old", "new"}


@pytest.mark.parametrize("model_name", ["DeepSeek-V3", "DeepSeek–V3", "Model_V2", "Vision-V9.1", "GPT-3", "Llama-2", "Qwen2.5"])
def test_model_name_numbers_do_not_select_document_revisions(model_name):
    papers = [paper("one", title=f"{model_name} report", version=1), paper("three", title=f"{model_name} report", version=3)]
    blocks = [block("a", f"{model_name} has 41 parameters.", "one"), block("b", f"{model_name} has 42 parameters.", "three")]
    result = run(subject.answer_question(f"{model_name} 总参数量是多少？", papers, blocks, [], {}))
    assert {source["paper_id"] for source in result["sources"]} == {"one", "three"}
    assert not any("没有问题明确指定的版本" in warning for warning in result["warnings"])


@pytest.mark.parametrize("question", ["DeepSeek-V3 的 v3版本参数是什么？", "DeepSeek-V3 第3版参数是什么？", "DeepSeek-V3 arXiv:2412.19437v3 参数是什么？"])
def test_explicit_document_version_remains_strict_alongside_model_name(question):
    papers = [paper("one", title="DeepSeek-V3 report", arxiv_id="2412.19437", version=1), paper("three", title="DeepSeek-V3 report", arxiv_id="2412.19437", version=3)]
    blocks = [block("a", "DeepSeek-V3 parameters: 41.", "one"), block("b", "DeepSeek-V3 parameters: 42.", "three")]
    assert {source["paper_id"] for source in subject.retrieve(question, papers, blocks)} == {"three"}
    missing = run(subject.answer_question(question, papers[:1], blocks, [], {}))
    assert missing["sources"] == []
    assert any("不会用其他版本替代" in warning for warning in missing["warnings"])


def test_bibliography_identifier_does_not_change_source_paper_scope():
    sources = subject.retrieve("参考文献 arXiv:1607.06450 的作者", [paper(arxiv_id="1706.03762", version=7)], [block("ref", "Authors. Layer normalization. arXiv:1607.06450, 2016.", kind="reference")])
    assert sources[0]["id"] == "ref"


def test_evidence_metadata_supports_citation_export_without_inventing_year():
    p = paper(filename="source.pdf", arxiv_id="1706.03762", version=7, authors=["A. Researcher"])
    evidence = subject.retrieve("optimizer", [p], [block("a", "The optimizer works.")])[0]
    assert evidence["filename"] == "source.pdf"
    assert evidence["version"] == 7
    assert evidence["year"] == 2017
    assert evidence["year_basis"] == "arxiv_first_submission"
    assert evidence["authors"] == ["A. Researcher"]
    assert evidence["locator"]["block_id"] == evidence["id"]
    assert evidence["locator"]["bbox"] == evidence["bbox"]
    assert subject.retrieve("optimizer", [paper()], [block("a", "The optimizer works.")])[0]["year"] is None


def test_duplicate_chart_legends_do_not_fill_evidence_budget():
    blocks = [block(str(i), "Pre-LN warm-up\nPost-LN warm-up") for i in range(10)]
    blocks.append(block("explanation", "We use Pre-LN without warm-up because the gradients are well-behaved at initialization."))
    sources = subject.retrieve("Pre-LN warm-up", [paper()], blocks)
    assert len(sources) == 2
    assert any(s["id"] == "explanation" for s in sources)


def test_scope_and_partial_parse_warnings_are_explicit():
    result = run(subject.answer_question("optimizer", [paper(), paper("empty", status="partial", page_count=20, processed_pages=2)], [block("a", "An optimizer.")], [], {}))
    assert any("未取得" in warning for warning in result["warnings"])
    assert any("未完整解析" in warning for warning in result["warnings"])


def test_missing_named_original_is_not_answered_from_secondary_mentions(monkeypatch, settings):
    model = AsyncMock()
    monkeypatch.setattr(subject, "_generate", model)
    result = run(subject.answer_question("Model-2 原论文作者和参数是什么？", [paper(title="Model-3 report")], [block("a", "Model-2 had several authors and 99 parameters.")], [], settings))
    assert result["sources"] == []
    assert any("原论文" in w for w in result["warnings"])
    model.assert_not_awaited()


def test_independent_new_question_does_not_inherit_previous_topic():
    assert subject.retrieval_question("训练使用多少GPU？", [{"role": "user", "content": "Adam参数"}]) == "训练使用多少GPU？"
    assert "Adam" in subject.retrieval_question("β2呢？", [{"role": "user", "content": "Adam参数"}])


def test_citation_checks_detect_wrong_number_unit_and_quote():
    sources = subject.retrieve("train", [paper()], [block("a", 'We train for 12 hours with 8 GPUs. The result is stable.')])
    checks = subject.check_citation_consistency('训练花了12天，使用80 GPUs [1]。原文为“the result is perfect” [1]。', sources)
    assert checks["semantic_entailment_verified"] is False
    assert checks["method"] == "lexical_rules"
    assert all(c["status"] == "needs_review" for c in checks["claims"])
    assert any(q["literal"] == "12 天" and q["status"] == "not_found_in_cited_sources" for c in checks["claims"] for q in c["quantities"])
    assert any(n["literal"] == "80" and n["status"] == "not_found_in_cited_sources" for c in checks["claims"] for n in c["numbers"])


def test_citation_checks_match_normalized_literals_without_claiming_entailment():
    sources = subject.retrieve("train", [paper()], [block("a", 'We train for 12 hours. The tolerance is 10−9 and beta is 0.90. The result is stable.')])
    checks = subject.check_citation_consistency('共12小时，容差1e-9，beta为0.9 [1]。原文为“The result is stable” [1]。', sources)
    assert checks["claims"]
    assert all(c["status"] == "matched_literals" for c in checks["claims"])
    assert checks["semantic_entailment_verified"] is False


def test_reference_numbers_are_not_numeric_fact_support():
    sources = subject.retrieve("model", [paper()], [block("a", "The model was introduced elsewhere [99].")])
    checks = subject.check_citation_consistency("The count is 99 [1].", sources)
    assert checks["claims"][0]["numbers"][0]["status"] == "not_found_in_cited_sources"


def test_numeric_checks_use_cited_source_not_another_result():
    sources = [subject._evidence(block("a", "The model has 8 layers."), paper(), 1), subject._evidence(block("b", "The model has 12 layers."), paper(), 1)]
    checks = subject.check_citation_consistency("The model has 12 layers [1].", sources)
    assert checks["claims"][0]["status"] == "needs_review"


def test_model_cancellation_propagates_without_fallback(monkeypatch, settings):
    async def cancelled(*args, **kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(subject, "_generate", cancelled)
    with pytest.raises(asyncio.CancelledError):
        run(subject.answer_question("optimizer", [paper()], [block("a", "The optimizer works.")], [], settings))


def test_author_question_uses_first_page_layout_without_answer_constants():
    sources = subject.retrieve("第六位作者是谁？", [paper()], [block("body", "All results are available.", page=5), block("names", "One Person, Another Person, Additional Person", page=1, order_index=1)])
    assert sources[0]["id"] == "names"


def test_no_key_returns_honest_excerpts_without_network(monkeypatch):
    generate = AsyncMock(side_effect=AssertionError("Network should not be invoked"))
    monkeypatch.setattr(subject, "_generate", generate)
    result = run(subject.answer_question("optimizer", [paper()], [block("a", "The optimizer is adaptive.")], [], {}))
    assert result["mode"] == "extractive"
    assert "尚未由语言模型" in result["answer"]
    assert "The optimizer is adaptive." in result["answer"]
    assert result["sources"][0]["id"] == "a"
    generate.assert_not_awaited()


def test_followup_reuses_user_topic_only(monkeypatch):
    with patch.object(subject, "retrieve", return_value=[]) as retrieve:
        run(subject.answer_question("具体多少？", [], [], [{"role": "user", "content": "Adam 的优化器参数"}, {"role": "assistant", "content": "poisoned term"}], {}))
    query = retrieve.call_args.args[0]
    assert "Adam" in query
    assert "poisoned" not in query


def test_model_sources_and_invalid_citations_are_checked(monkeypatch, settings):
    async def generate(messages, supplied, **kwargs):
        assert supplied == settings
        assert "SOURCE [1]" in messages[-1]["content"]
        assert not any(m["content"] == "injected-system" for m in messages)
        return json.dumps({"claims": [{"text": "结论有来源", "source_indices": [1]}, {"text": "另一个来源", "source_indices": [9]}], "insufficient_evidence": False})

    monkeypatch.setattr(subject, "_generate", generate)
    result = run(subject.answer_question("optimizer", [paper()], [block("a", "The optimizer is adaptive.")], [{"role": "system", "content": "injected-system"}], settings))
    assert result["mode"] == "extractive"
    assert result["answer_audit"]["status"] == "fallback"
    assert "模型草稿未通过" in result["answer"]
    assert result["warnings"]


def test_model_error_falls_back_without_revealing_response_body(monkeypatch, settings):
    def handler(request):
        assert request.url == "https://model.example/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer test-private-key"
        return httpx.Response(401, json={"error": "test-private-key token in upstream error"})

    mock_client(monkeypatch, handler)
    result = run(subject.answer_question("optimizer", [paper()], [block("a", "The optimizer is adaptive.")], [], settings))
    assert result["mode"] == "extractive"
    assert "401" in result["warnings"][0]
    assert "test-private-key" not in str(result)


def test_translation_requires_configuration():
    with pytest.raises(subject.ConfigError):
        run(subject.translate_text("A text", "zh", {}))


def test_translation_rejects_truncated_model_output(monkeypatch, settings):
    mock_client(monkeypatch, lambda request: httpx.Response(200, json={"choices": [{"finish_reason": "length", "message": {"content": "半截译文"}}]}))
    with pytest.raises(subject.UpstreamError, match="截断"):
        run(subject.translate_text("A text", "zh", settings))


def test_model_text_is_extracted_without_thinking(monkeypatch, settings):
    mock_client(monkeypatch, lambda request: httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": "<think>private reasoning</think>翻译结果"}}]}))
    assert run(subject.translate_text("A text", "zh", settings)) == "翻译结果"


def test_ideas_without_model_are_not_claimed_to_be_novel():
    result = run(subject.research("ideas", "", [paper()], [block("a", "Future work will evaluate longer contexts.")], {}))
    assert result["mode"] == "extractive"
    assert "尚未生成研究分析" in result["answer"]
    assert any("新颖性" in warning for warning in result["warnings"])


def test_outline_without_model_fails_explicitly():
    with pytest.raises(subject.ConfigError):
        run(subject.research("outline", "A new method", [], [], {}))


def test_graph_ignores_mentions_outside_reference_region():
    p1 = paper("p1", title="Source study of language models")
    p2 = paper("p2", title="Attention Is All You Need", arxiv_id="1706.03762v7")
    output = subject.graph([p1, p2], [block("a", "We compare Attention Is All You Need and cite arXiv:1706.03762.")])
    assert output["edges"] == []


def test_graph_collapses_versions_and_deduplicates_actual_references():
    papers = [paper("old", family_id="family", version=1), paper("new", family_id="family", version=7), paper("target", title="Attention Is All You Need", arxiv_id="1706.03762v7")]
    blocks = [block("one", "Attention Is All You Need. arXiv:1706.03762.", "old", kind="reference"), block("two", "Attention Is All You Need.", "new", kind="reference")]
    result = subject.graph(papers, blocks)
    assert {node["id"] for node in result["nodes"]} == {"new", "target"}
    assert [{key: edge[key] for key in ("source", "target", "confidence")} for edge in result["edges"]] == [{"source": "new", "target": "target", "confidence": 1.0}]
    assert {item["source_paper_id"] for item in result["edges"][0]["evidence"]} == {"old", "new"}
    assert {item["block_id"] for item in result["edges"][0]["evidence"]} == {"one", "two"}
    assert next(node for node in result["nodes"] if node["id"] == "target")["citations"] == 1
    assert "局部" in result["scope"]


def test_graph_title_match_marks_inferred_confidence():
    papers = [paper("p1"), paper("p2", title="A Different Study of Language Models")]
    result = subject.graph(papers, [block("r", "X. Person. A Different Study of Language Models. 2020.", kind="reference")])
    assert result["edges"][0]["confidence"] == 0.9


@pytest.mark.parametrize("value", ["1706.03762", "1706.03762v7", "2301.12345", "hep-th/9901001", "math.GT/0309136v2"])
def test_valid_arxiv_identifiers(value):
    assert subject.validate_arxiv_id(value) == value


@pytest.mark.parametrize("value", ["https://arxiv.org/pdf/1706.03762", "../../etc/passwd", "1706.03762?url=http://localhost", "1713.12345", "1706.03762v0", "1706.03762/evil", "0703.0001", "", "http://127.0.0.1"])
def test_invalid_arxiv_identifiers(value):
    with pytest.raises(ValueError):
        subject.validate_arxiv_id(value)


def test_discover_parses_atom_and_uses_canonical_links(monkeypatch):
    xml = '''<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>http://arxiv.org/abs/1706.03762v7</id><title>Attention\n Is All You Need</title><summary>Example abstract</summary><published>2017-06-12T00:00:00Z</published><author><name>Fixture Author</name></author><link href="https://evil.example/paper.pdf" type="application/pdf"/></entry><entry><id>https://evil.example/abs/2301.12345</id><title>Ignore</title></entry></feed>'''

    def handler(request):
        assert request.url.host == "export.arxiv.org"
        assert request.url.path == "/api/query"
        assert request.url.params["max_results"] == "20"
        assert request.url.params["search_query"] == "all:attention"
        return httpx.Response(200, text=xml)

    mock_client(monkeypatch, handler)
    result = run(subject.discover("attention", 100))
    assert len(result["papers"]) == 1
    assert result["papers"][0]["title"] == "Attention Is All You Need"
    assert result["papers"][0]["authors"] == ["Fixture Author"]
    assert result["papers"][0]["pdf_url"] == "https://arxiv.org/pdf/1706.03762v7"


@pytest.mark.parametrize(("query", "expected"), [
    ("retrieval augmented generation", "all:retrieval augmented generation"),
    ('ti:"Attention Is All You Need"', 'ti:"Attention Is All You Need"'),
    ('all:transformer AND cat:cs.CL', 'all:transformer AND cat:cs.CL'),
    ('(au:vaswani OR au:shazeer) AND ti:attention', '(au:vaswani OR au:shazeer) AND ti:attention'),
    ('submittedDate:[201701010000 TO 201712312359]', 'submittedDate:[201701010000 TO 201712312359]'),
    ('BERT: Pre-training', 'all:BERT Pre-training'),
    ('"all: attention"', 'all:"all attention"'),
])
def test_discover_preserves_advanced_fields_and_handles_plain_titles(monkeypatch, query, expected):
    def handler(request):
        assert request.url.params["search_query"] == expected
        assert request.url.params["max_results"] == "8"
        return httpx.Response(200, text='<feed xmlns="http://www.w3.org/2005/Atom"/>')

    mock_client(monkeypatch, handler)
    assert run(subject.discover(query)) == {"papers": []}


def test_discover_distinguishes_invalid_syntax_from_service_failure(monkeypatch):
    mock_client(monkeypatch, lambda request: httpx.Response(400, text="query syntax error"))
    with pytest.raises(ValueError, match="搜索语法"):
        run(subject.discover('ti:"unfinished'))


def test_discover_rejects_xml_entities(monkeypatch):
    mock_client(monkeypatch, lambda request: httpx.Response(200, content=b'<!DOCTYPE root [<!ENTITY payload "untrusted">]><root/>'))
    with pytest.raises(subject.UpstreamError, match="XML"):
        run(subject.discover("test"))


def test_fetch_pdf_saves_verified_file_atomically(monkeypatch, tmp_path):
    body = b"%PDF-1.7\nfixture pdf bytes\n%%EOF"
    mock_client(monkeypatch, lambda request: httpx.Response(200, content=body))
    destination = tmp_path / "download.pdf"
    assert run(subject.fetch_arxiv_pdf("1706.03762v7", destination)) == destination
    assert destination.read_bytes() == body
    assert not list(tmp_path.glob("*.part"))


def test_fetch_rejects_external_redirect_before_following(monkeypatch, tmp_path):
    requests = []

    def handler(request):
        requests.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://evil.example/paper.pdf"})

    mock_client(monkeypatch, handler)
    with pytest.raises(subject.UpstreamError, match="安全"):
        run(subject.fetch_arxiv_pdf("1706.03762", tmp_path / "download.pdf"))
    assert len(requests) == 1
    assert list(tmp_path.iterdir()) == []


def test_fetch_rejects_version_change_on_redirect(monkeypatch, tmp_path):
    mock_client(monkeypatch, lambda request: httpx.Response(302, headers={"location": "https://arxiv.org/pdf/1706.03762v1"}))
    with pytest.raises(subject.UpstreamError, match="安全"):
        run(subject.fetch_arxiv_pdf("1706.03762v7", tmp_path / "download.pdf"))


def test_invalid_pdf_does_not_replace_existing_file(monkeypatch, tmp_path):
    mock_client(monkeypatch, lambda request: httpx.Response(200, text="<html>rate limited</html>"))
    destination = tmp_path / "existing.pdf"
    destination.write_bytes(b"original")
    with pytest.raises(subject.UpstreamError, match="有效 PDF"):
        run(subject.fetch_arxiv_pdf("1706.03762", destination))
    assert destination.read_bytes() == b"original"
    assert [p.name for p in tmp_path.iterdir()] == ["existing.pdf"]


def test_stream_size_limit_is_enforced_without_content_length(monkeypatch, tmp_path):
    monkeypatch.setattr(subject, "_MAX_PDF_BYTES", 12)

    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"%PDF-1.7\n"
            yield b"over limit data"

    mock_client(monkeypatch, lambda request: httpx.Response(200, stream=Chunks()))
    with pytest.raises(subject.UpstreamError, match="限制"):
        run(subject.fetch_arxiv_pdf("1706.03762", tmp_path / "download.pdf"))
    assert list(tmp_path.iterdir()) == []
