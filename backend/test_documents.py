"""Regression checks against the actual supplied papers, plus unsafe inputs."""
from __future__ import annotations

import os
from pathlib import Path

import pymupdf
import pytest

from backend import documents

SAMPLES = Path(__file__).resolve().parent.parent / "pdf"


@pytest.fixture
def transformer():
    path = SAMPLES / "1706.03762v1.pdf"
    if not path.exists():
        pytest.skip("Project's sample PDFs are unavailable")
    return path


def test_metadata_uses_cover_content_and_author_order(transformer):
    result = documents.inspect_pdf(transformer, "renamed-document.pdf")
    assert result["title"] == "Attention Is All You Need"
    assert len(result["authors"]) == 8
    assert result["authors"][5] == "Aidan N. Gomez"
    assert result["arxiv_id"] == "1706.03762"
    assert result["version"] == 1
    assert result["page_count"] == 15
    assert "28.4" in result["abstract"]
    assert "Recurrent neural networks" not in result["abstract"]


def test_sections_tables_references_and_coordinates(transformer):
    pages = list(documents.iter_pages(transformer))
    blocks = [{**block, "page": page["page"], "id": f'{page["page"]}-{block["order_index"]}'}
              for page in pages for block in page["blocks"]]
    sections = documents.sections_from_blocks(blocks)
    multihead = next(x for x in sections if x["title"] == "3.2.2 Multi-Head Attention")
    assert (multihead["page"], multihead["level"]) == (4, 3)
    assert all(x["title"] != "3.5 days." for x in sections)
    table = next(x for x in pages[7]["blocks"] if x["kind"] == "table")
    assert "Transformer (big) | 28.4 | 41.0" in table["text"]
    assert "ConvS2S Ensemble [9] | 26.36 | 41.29" in table["text"]
    references = documents.extract_references(blocks)
    reference = next(x for x in references if x["label"] == "1")
    assert reference["arxiv_id"] == "1607.06450"
    assert reference["year"] == 2016
    assert reference["page"] == 10
    assert len(references) == 36
    for page in pages:
        assert not page["ocr"]
        for block in page["blocks"]:
            x0, y0, x1, y1 = block["bbox"]
            assert 0 <= x0 < x1 <= page["width"] + 1
            assert 0 <= y0 < y1 <= page["height"] + 1


def test_resumed_parsing_preserves_page_and_section(transformer):
    whole = list(documents.iter_pages(transformer))
    resumed = list(documents.iter_pages(transformer, start_page=8))
    assert resumed == whole[7:]
    assert list(documents.iter_pages(transformer, start_page=16)) == []


def test_v7_toc_filters_legal_notice_but_keeps_nested_sections():
    path = SAMPLES / "1706.03762v7.pdf"
    if not path.exists():
        pytest.skip("Transformer v7 fixture unavailable")
    blocks = [{**block, "page": page["page"]}
              for page in documents.iter_pages(path) for block in page["blocks"]]
    titles = [item["title"] for item in documents.sections_from_blocks(blocks)]
    assert "3.2.2 Multi-Head Attention" in titles
    assert "Attention Is All You Need" in titles
    assert "Abstract" in titles
    assert not any("proper attribution" in title or "reproduce the tables" in title
                   or title == "scholarly works." for title in titles)


def test_toc_author_filter_is_limited_to_frontmatter():
    titles = ["Attention Is All You Need", "Aidan N. Gomez*↑", "name@example.com", "Abstract",
              "fWork performed while at Google Brain.",
              "1 Copyright and Licensing", "Copyright and Licensing", "Research Methods"]
    blocks = [{"text": title, "kind": "heading", "page": 1, "id": str(i)}
              for i, title in enumerate(titles)]
    sections = documents.sections_from_blocks(blocks)
    assert [item["title"] for item in sections] == [titles[0], titles[3], *titles[5:]]
    assert blocks[1]["text"] == "Aidan N. Gomez*↑"  # Source evidence stays intact.


def test_t5_overflow_is_clipped_without_losing_text():
    path = SAMPLES / "1910.10683v4.pdf"
    if not path.exists():
        pytest.skip("T5 fixture unavailable")
    iterator = documents.iter_pages(path, start_page=50)
    page = next(iterator)
    iterator.close()
    block = next(b for b in page["blocks"] if "Joey loved to go outside" in b["text"])
    assert "his cousin Jimmy" in block["text"]
    assert block["bbox"][2] == page["width"] == 612
    for item in page["blocks"]:
        x0, y0, x1, y1 = item["bbox"]
        assert 0 <= x0 < x1 <= page["width"]
        assert 0 <= y0 < y1 <= page["height"]


def test_unusable_coordinates_keep_text_with_a_page_locator(tmp_path, monkeypatch):
    path = tmp_path / "bounds.pdf"
    with pymupdf.open() as doc:
        doc.new_page(width=100, height=100)
        doc.save(path)
    texts = ["Partly outside", "Entirely outside", "Non-finite geometry", "Inside"]
    boxes = [[-10, -5, 120, 80], [110, 20, 130, 40], [float("nan"), 0, 20, 30], [5, 10, 30, 40]]
    blocks = [{"text": text, "bbox": box, "kind": "paragraph", "section": "", "order_index": i}
              for i, (text, box) in enumerate(zip(texts, boxes))]
    monkeypatch.setattr(documents, "_page_blocks", lambda *_: (blocks, [], False, []))
    page = next(documents.iter_pages(path))
    assert [b["text"] for b in page["blocks"]] == texts
    assert page["blocks"][0]["bbox"] == [0, 0, 100, 80]
    assert page["blocks"][1]["bbox"] == page["blocks"][2]["bbox"] == [0, 0, 100, 100]
    assert page["blocks"][3]["bbox"] == boxes[3]
    assert len(page["warnings"]) == 1 and "整页定位" in page["warnings"][0]


def test_render_and_bad_page_arguments(transformer):
    assert documents.render_page(transformer, 8).startswith(b"\x89PNG\r\n\x1a\n")
    with pytest.raises(ValueError):
        documents.render_page(transformer, 0)
    with pytest.raises(ValueError):
        documents.render_page(transformer, 1, float("inf"))


def test_rejects_non_pdf_and_password(tmp_path):
    invalid = tmp_path / "invalid.pdf"
    invalid.write_text("This is not a PDF")
    with pytest.raises(ValueError, match="不是有效"):
        documents.inspect_pdf(invalid, invalid.name)
    encrypted = tmp_path / "encrypted.pdf"
    with pymupdf.open() as doc:
        doc.new_page().insert_text((30, 30), "Private document")
        doc.save(encrypted, encryption=pymupdf.PDF_ENCRYPT_AES_256,
                 owner_pw="owner", user_pw="reader")
    with pytest.raises(ValueError, match="密码"):
        documents.inspect_pdf(encrypted, encrypted.name)


def test_missing_ocr_is_reported_not_silently_successful(transformer, tmp_path, monkeypatch):
    scanned = tmp_path / "scan.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_image(page.rect, stream=documents.render_page(transformer, 1))
        doc.save(scanned)
    monkeypatch.setattr(documents, "get_ocr_status", lambda: {"available": False, "engine": "none"})
    page = next(documents.iter_pages(scanned))
    assert page["ocr"]
    assert page["blocks"] == []
    assert any("未能完成" in warning for warning in page["warnings"])


def test_long_book_has_physical_and_printed_page_labels():
    path = SAMPLES / "UnderstandingDeepLearning_02_09_26_C.pdf"
    if not path.exists():
        pytest.skip("Long-book fixture unavailable")
    metadata = documents.inspect_pdf(path, path.name)
    assert metadata["page_count"] == 541
    iterator = documents.iter_pages(path, start_page=15)
    page = next(iterator)
    iterator.close()
    assert (page["page"], page["page_label"]) == (15, "1")
    tail = list(documents.iter_pages(path, start_page=541))
    assert len(tail) == 1
    assert tail[0]["page_label"] == "527"
    assert tail[0]["blocks"]


@pytest.mark.skipif(os.environ.get("RUN_OCR_TESTS") != "1", reason="Opt-in real OCR integration test (~90 seconds)")
def test_real_scanned_paper_all_pages():
    path = SAMPLES / "1706.03762v1_img.pdf"
    if not path.exists() or not documents.get_ocr_status()["available"]:
        pytest.skip("Scan fixture or OCR engine unavailable")
    metadata = documents.inspect_pdf(path, path.name)
    assert metadata["title"] == "Attention Is All You Need"
    pages = list(documents.iter_pages(path))
    assert len(pages) == 15
    assert [page["page"] for page in pages] == list(range(1, 16))
    assert all(page["ocr"] and page["blocks"] and not page["warnings"] for page in pages)
    for page in pages:
        for block in page["blocks"]:
            x0, y0, x1, y1 = block["bbox"]
            assert 0 <= x0 < x1 <= page["width"]
            assert 0 <= y0 < y1 <= page["height"]
    training = "\n".join(b["text"] for b in pages[6]["blocks"])
    assert "0.98" in training and "Adam" in training
    results = "\n".join(b["text"] for b in pages[7]["blocks"])
    assert "28.4" in results and "26.36" in results
    blocks = [{**block, "page": page["page"], "id": f'{page["page"]}-{block["order_index"]}'}
              for page in pages for block in page["blocks"]]
    sections = documents.sections_from_blocks(blocks)
    assert len(sections) == 26
    titles = {section["title"] for section in sections}
    assert {"Abstract", "3.5Positional Encoding", "AttentionVisualizations"} <= titles
    assert not titles.intersection({
        "bottoms of the encoder and decoder stacks. The positional encodings have the same dimension dmodel",
        "for both WSJ only and the semi-supervised setting.", ".=",
    })
    assert [reference["label"] for reference in documents.extract_references(blocks)] == [str(i) for i in range(1, 37)]


def test_wrapped_title_fragment_is_not_an_author():
    def line(text, y, size=12):
        return {"text": text, "bbox": [60, y, 500, y+size], "size": size, "bold": False}
    lines = [line("LORA: LOW-RANK ADAPTATION OF LARGE LAN", 50, 18),
             line("GUAGE MODELS", 70, 18), line("Edward Hu", 100),
             line("Yelong Shen", 120), line("Abstract", 160)]
    authors = documents._authors(lines, "LORA: LOW-RANK ADAPTATION OF LARGE LANGUAGE MODELS", {}, 792)
    assert authors == ["Edward Hu", "Yelong Shen"]
