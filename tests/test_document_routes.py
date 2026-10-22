import hashlib

import pymupdf
import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.documents import extract_references
from backend.document_routes import bibtex, citation_metadata, compare_documents
from backend.tasks import page_text


@pytest.fixture
def documents_client(tmp_path):
    app = create_app(tmp_path / "data", tmp_path / "samples", run_jobs=False)
    store = app.state.store
    papers = []
    for version, bleu in ((1, "41.0"), (7, "41.8")):
        path = store.files / f"version{version}.pdf"
        pdf = pymupdf.open()
        for index in range(2):
            page = pdf.new_page(width=612, height=792)
            page.insert_text((60, 80), f"Multi-head Attention version {version}")
        pdf.save(path)
        pdf.close()
        paper = store.create_paper(path.name, path, hashlib.sha256(path.read_bytes()).hexdigest(), "upload", 2)
        paper = store.update_paper(paper["id"], title="Evidence & Attention {Study}", authors=["Alice Example", "Bob Researcher"], arxiv_id="1706.03762", version=version, family_id="arxiv:1706.03762", status="ready")
        blocks = [
            {"text": f"Multi-head attention achieves BLEU {bleu}. See [1, 2–3].", "kind": "paragraph", "bbox": [60, 70, 550, 90]},
            {"text": "An unrelated sentence.", "kind": "paragraph", "bbox": [60, 100, 500, 120]},
        ]
        store.add_page(paper["id"], {"page": 1, "width": 612, "height": 792, "blocks": blocks})
        refs = [{"text": f"[{i}] Alice Example. Referenced paper number {i}. 2016. arXiv:1607.06450.", "kind": "reference", "bbox": [60, 60+i*30, 550, 80+i*30]} for i in (1, 2, 3)]
        store.add_page(paper["id"], {"page": 2, "width": 612, "height": 792, "blocks": refs})
        papers.append(paper)
    with TestClient(app) as client:
        yield client, store, papers


def test_search_returns_correct_version_and_source_coordinates(documents_client):
    client, store, (v1, v7) = documents_client
    result = client.get(f"/api/papers/{v1['id']}/search", params={"q": "multi head"}).json()
    assert result["total"] == 1
    hit = result["matches"][0]
    assert "41.0" in hit["text"] and "41.8" not in hit["text"]
    assert hit["paper_id"] == v1["id"] and hit["version"] == 1
    assert hit["bbox"] == store.blocks([v1["id"]], page=1)[0]["bbox"]
    assert client.get(f"/api/papers/{v7['id']}/search", params={"q": "no such text"}).json()["matches"] == []
    assert client.get(f"/api/papers/{v1['id']}/search", params={"q": " "}).status_code == 400


def test_reference_links_preserve_return_location(documents_client):
    client, store, (v1, _) = documents_client
    refs = client.get(f"/api/papers/{v1['id']}/references").json()["references"]
    assert len(refs) == 3 and refs[0]["arxiv_id"] == "1607.06450"
    links = client.get(f"/api/papers/{v1['id']}/pages/1/reference-links").json()["links"]
    assert [l["label"] for l in links] == ["1", "2", "3"]
    for link in links:
        assert link["source"]["page"] == 1 and link["target"]["page"] == 2
        assert link["target"]["text"].startswith(f"[{link['label']}]")
        assert link["source"]["paper_id"] == link["target"]["paper_id"] == v1["id"]
    assert client.get(f"/api/papers/{v1['id']}/pages/2/reference-links").json()["links"] == []


def test_references_preserve_explicit_modern_and_legacy_versions():
    refs = extract_references([{"id": "reference", "page": 4, "kind": "reference", "text": "[1] A. Author. A scientific paper. arXiv:1706.03762v1.\n[2] B. Author. An older paper. arXiv:hep-th/9901001v2."}])
    assert [(r["arxiv_id"], r["version"]) for r in refs] == [("1706.03762", 1), ("hep-th/9901001", 2)]


def test_explicit_reference_version_never_links_another_local_version(documents_client):
    client, store, (v1, v7) = documents_client
    texts = [
        "[1] A. Author. Evidence & Attention {Study}. arXiv:1706.03762v1.",
        "[2] A. Author. Evidence & Attention {Study}. arXiv:1706.03762v9.",
        "[3] A. Author. Evidence & Attention {Study}. arXiv:1706.03762.",
        "[4] A. Author. Evidence & Attention {Study}. arXiv:9999.99999v1.",
    ]
    store.add_page(v7["id"], {"page": 2, "width": 612, "height": 792, "blocks": [{"text": text, "kind": "reference", "bbox": [60, 60 + i * 30, 550, 80 + i * 30]} for i, text in enumerate(texts)]})
    refs = client.get(f"/api/papers/{v7['id']}/references").json()["references"]
    assert refs[0]["version"] == 1
    assert refs[0]["linked_paper_id"] == v1["id"]
    assert refs[0]["linked_version"] == 1
    assert refs[0]["match_rule"] == "arxiv_id_and_version_exact"
    assert refs[1]["version"] == 9 and "linked_paper_id" not in refs[1]
    assert refs[1]["match_rule"] == "specified_version_not_found"
    assert refs[2]["version"] is None
    assert refs[2]["linked_paper_id"] == v7["id"]
    assert refs[2]["match_rule"] == "arxiv_id_latest_local_version"
    assert "未指定版本" in refs[2]["match_note"]
    # An explicit unmatched identifier must not fall back to a same-title paper.
    assert "linked_paper_id" not in refs[3]


def test_text_layer_uses_original_pdf_words(documents_client):
    client, _, (v1, _) = documents_client
    layer = client.get(f"/api/papers/{v1['id']}/pages/1/text-layer").json()
    assert layer["mode"] == "native"
    assert "Multi-head" in [s["text"] for s in layer["spans"]]
    for span in layer["spans"]:
        x0, y0, x1, y1 = span["bbox"]
        assert 0 <= x0 < x1 <= layer["width"]
        assert 0 <= y0 < y1 <= layer["height"]
    assert client.get(f"/api/papers/{v1['id']}/pages/999/text-layer").status_code == 404


def test_text_layer_prefers_parsed_ocr_over_partial_native_text(documents_client):
    client, store, (paper, _) = documents_client
    # The PDF still contains native words, as a scan with a native footer would.
    # Its completed OCR source includes the body that those words cannot supply.
    store.add_page(paper["id"], {"page": 1, "width": 612, "height": 792, "ocr": True,
        "blocks": [{"text": "Scanned research body.\nSecond OCR line.", "kind": "paragraph", "bbox": [60, 100, 500, 140]}]})
    layer = client.get(f"/api/papers/{paper['id']}/pages/1/text-layer").json()
    assert layer["mode"] == "ocr"
    assert [span["text"] for span in layer["spans"]] == ["Scanned research body.", "Second OCR line."]
    assert [span["bbox"] for span in layer["spans"]] == [[60, 100, 500, 120], [60, 120, 500, 140]]


def test_text_layer_keeps_native_text_available_during_reparse(documents_client):
    client, store, (paper, _) = documents_client
    store.create_reparse(paper["id"])
    assert store.page(paper["id"], 1) is None
    layer = client.get(f"/api/papers/{paper['id']}/pages/1/text-layer").json()
    assert layer["mode"] == "native"
    assert "Multi-head" in [span["text"] for span in layer["spans"]]


def test_compare_exposes_changed_number_and_pages(documents_client):
    client, store, (v1, v7) = documents_client
    result = client.get(f"/api/papers/{v1['id']}/compare/{v7['id']}").json()
    assert 0 < result["similarity"] < 1
    assert len(result["changes"]) == 1
    change = result["changes"][0]
    assert "41.0" in change["before"] and "41.8" in change["after"]
    assert change["left_page"] == change["right_page"] == 1
    same = client.get(f"/api/papers/{v1['id']}/compare/{v1['id']}").json()
    assert same["changes"] == [] and same["similarity"] == 1
    store.update_paper(v7["id"], family_id="unrelated")
    assert client.get(f"/api/papers/{v1['id']}/compare/{v7['id']}").status_code == 400


@pytest.mark.parametrize("side", [0, 1])
def test_compare_rejects_partially_parsed_version_as_missing_not_deleted(documents_client, side):
    client, store, versions = documents_client
    incomplete = versions[side]
    store.update_paper(incomplete["id"], status="partial")
    response = client.get(f"/api/papers/{versions[0]['id']}/compare/{versions[1]['id']}")
    assert response.status_code == 409
    assert "完整解析" in response.json()["detail"]
    assert "changes" not in response.json()


def test_bibtex_escapes_content_and_does_not_invent_year(documents_client):
    client, _, (v1, v7) = documents_client
    result = client.get(f"/api/papers/{v7['id']}/citation")
    assert result.status_code == 200 and "attachment" in result.headers["content-disposition"]
    assert r"Evidence \& Attention \{Study\}" in result.text
    assert "1706.03762v7" in result.text and "2017" in result.text
    assert "Alice Example and Bob Researcher" in result.text
    unknown = {**v1, "arxiv_id": None, "version": None}
    assert citation_metadata(unknown)["year"] is None
    assert "year =" not in bibtex(unknown)
    bulk = client.get("/api/citations/export", params={"paper_ids": f"{v1['id']},{v7['id']},{v1['id']}"})
    assert bulk.text.count("@misc{") == 2


def test_translation_export_marks_missing_and_ignores_stale_cache(documents_client):
    client, store, (v1, _) = documents_client
    endpoint = f"/api/papers/{v1['id']}/translations/export"
    assert client.get(endpoint).status_code == 409
    source = page_text(store.page(v1["id"], 1))
    store.save_translation(v1["id"], 1, "zh", source, "译文 <script>alert(1)</script>")
    store.save_translation(v1["id"], 2, "zh", "stale source", "过期译文")
    exported = client.get(endpoint)
    assert "已译 1/2 页" in exported.text and "本页尚无有效缓存译文" in exported.text
    assert "过期译文" not in exported.text
    html_result = client.get(endpoint, params={"format": "html"})
    assert "<script>" not in html_result.text and "&lt;script&gt;" in html_result.text
    assert client.get(endpoint, params={"target_language": "en"}).status_code == 409


def test_graph_exports_real_nodes_with_safe_labels(documents_client):
    client, _, _ = documents_client
    mermaid = client.get("/api/graph/export", params={"format": "mermaid"})
    dot = client.get("/api/graph/export", params={"format": "dot"})
    assert mermaid.text.startswith("flowchart LR") and "Evidence &amp; Attention" in mermaid.text
    assert dot.text.startswith("digraph references") and "label=" in dot.text


def test_large_version_change_is_bounded_and_explicit():
    old = [{"text": f"old paragraph {i}", "page": i+1} for i in range(700)]
    new = [{"text": f"new paragraph {i}", "page": i+1} for i in range(700)]
    result = compare_documents({"id": "a"}, {"id": "b"}, old, new)
    assert result["truncated"] and len(result["changes"]) == 500
