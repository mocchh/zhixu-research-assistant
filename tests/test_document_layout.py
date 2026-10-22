"""Geometric parser regression cases; no model calls or corpus writes."""
from pathlib import Path
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pymupdf
import pytest

from backend import documents
ROOT = Path(__file__).resolve().parents[1]


def save(doc, tmp_path, name="layout.pdf"):
    path = tmp_path / name
    doc.save(path)
    doc.close()
    return path


def paragraphs(page):
    page.insert_text((40, 50), "Ordinary body text remains searchable and correctly located.", fontsize=10)


def grid(page, x=50, y=150, caption=False):
    values = [["Model", "Score", "Seconds"], ["Alpha", "82.5", "12"], ["Beta", "90.0", "18"], ["Gamma", "", "15"]]
    for col in range(4):
        page.draw_line((x+col*100, y), (x+col*100, y+140))
    for row in range(5):
        page.draw_line((x, y+row*35), (x+300, y+row*35))
    for row, cells in enumerate(values):
        for col, value in enumerate(cells):
            page.insert_text((x+10+col*100, y+22+row*35), value, fontsize=10)
    if caption:
        page.insert_text((x, y+158), "Table 1: Measured performance.", fontsize=10)
    return values


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
@pytest.mark.parametrize("crop", [False, True])
def test_native_bbox_matches_visible_ink_after_rotation_and_crop(tmp_path, rotation, crop):
    doc = pymupdf.open()
    page = doc.new_page(width=600, height=800)
    page.insert_text((70, 140), "Coordinate evidence survives the page transformation.", fontsize=12, color=(1, 0, 0))
    if crop:
        page.set_cropbox(pymupdf.Rect(20, 30, 570, 750))
    page.set_rotation(rotation)
    pixels = page.get_pixmap(alpha=False)
    array = np.frombuffer(pixels.samples, dtype=np.uint8).reshape(pixels.height, pixels.width, 3)
    ys, xs = np.where((array[:, :, 0] > 180) & (array[:, :, 1] < 80) & (array[:, :, 2] < 80))
    path = save(doc, tmp_path)
    parsed = next(documents.iter_pages(path))
    block = next(block for block in parsed["blocks"] if "Coordinate evidence" in block["text"])
    x0, y0, x1, y1 = block["bbox"]
    assert len(xs) > 100
    assert x0 <= xs.min()+1 <= xs.max()+1 <= x1+1
    assert y0 <= ys.min()+1 <= ys.max()+1 <= y1+1
    assert (parsed["width"], parsed["height"]) == (pixels.width, pixels.height)


def test_ruled_grid_without_caption_keeps_cells_and_empty_column(tmp_path):
    doc = pymupdf.open(); page = doc.new_page(); paragraphs(page)
    expected = grid(page)
    blocks = next(documents.iter_pages(save(doc, tmp_path)))["blocks"]
    table = next(block for block in blocks if block["kind"] == "table")
    assert table["text"].splitlines() == [" | ".join(row) for row in expected]
    text = "\n".join(block["text"] for block in blocks)
    assert text.count("Alpha") == 1 and "Ordinary body text" in text


@pytest.mark.parametrize("rotation", [0, 90])
def test_vertical_grid_without_data_row_rules_preserves_numeric_rows(tmp_path, rotation):
    doc = pymupdf.open(); page = doc.new_page(); paragraphs(page)
    for x in (50, 150, 250, 350):
        page.draw_line((x, 100), (x, 220))
    for y in (100, 130, 220):
        page.draw_line((50, y), (350, y))
    values = [["Count", "Score", "Seconds"], ["2", "93.1", "19"], ["8", "88.7", "7.5"], ["32", "85.6", "2.3"]]
    for i, row in enumerate(values):
        for j, value in enumerate(row):
            page.insert_text((60 + j*100, 118 if i == 0 else 130 + i*25), value, fontsize=10)
    page.set_rotation(rotation)
    blocks = next(documents.iter_pages(save(doc, tmp_path)))["blocks"]
    table = next(block for block in blocks if block["kind"] == "table")
    assert table["text"].splitlines() == [" | ".join(row) for row in values]
    assert "Ordinary body text" in "\n".join(block["text"] for block in blocks)


@pytest.mark.parametrize("content,offset", [(["wrapped", "description"], 0), (["12", "24"], 9)])
def test_multiline_cells_are_not_split_without_aligned_numeric_evidence(tmp_path, content, offset):
    doc = pymupdf.open(); page = doc.new_page()
    for x in (50, 200, 350):
        page.draw_line((x, 100), (x, 220))
    for y in (100, 130, 220):
        page.draw_line((50, y), (350, y))
    for x, title in ((60, "Column A"), (210, "Column B")):
        page.insert_text((x, 118), title, fontsize=10)
    for i in range(2):
        page.insert_text((60, 155+i*25), str(i+1), fontsize=10)
        page.insert_text((210, 155+i*25+offset), content[i], fontsize=10)
    table = next(block for block in next(documents.iter_pages(save(doc, tmp_path)))["blocks"] if block["kind"] == "table")
    assert table["text"].splitlines() == ["Column A | Column B", "1 2 | " + " ".join(content)]


@pytest.mark.parametrize("rotation", [90, 180, 270])
def test_rotated_table_uses_rendered_coordinates(tmp_path, rotation):
    doc = pymupdf.open(); page = doc.new_page(); grid(page)
    page.set_rotation(rotation)
    expected = list(pymupdf.Rect(50, 150, 350, 290) * page.rotation_matrix)
    blocks = next(documents.iter_pages(save(doc, tmp_path)))["blocks"]
    table = next(block for block in blocks if block["kind"] == "table")
    assert table["bbox"] == pytest.approx(expected, abs=.01)
    assert "Alpha | 82.5 | 12" in table["text"]


def test_partial_table_overlap_retains_prose_in_the_same_native_block():
    lines = [{"text": text, "bbox": box, "size": 10} for text, box in [
        ("cell alpha", [50, 100, 120, 115]),
        ("cell beta", [50, 120, 120, 135]),
        ("Following paragraph survives", [50, 140, 270, 155]),
    ]]
    block = documents._lines_block(lines, "paragraph")
    with documents.PYMUPDF_LOCK:
        result = documents._replace_table_region([block], lines, pymupdf.Rect(40, 90, 150, 138), "cell alpha | cell beta")
    assert next(item for item in result if item["kind"] == "paragraph")["text"] == "Following paragraph survives"
    assert sum(item["text"].count("cell alpha") for item in result) == 1


@pytest.mark.parametrize("caption_below", [False, True])
def test_narrow_three_line_table_does_not_capture_adjacent_prose(tmp_path, caption_below):
    doc = pymupdf.open(); page = doc.new_page(width=600, height=800)
    page.insert_textbox(pymupdf.Rect(40, 170, 320, 290), "Adjacent prose belongs to the left column. " * 4, fontsize=11)
    for y in (160, 185, 255):
        page.draw_line((400, y), (515, y))
    data = [("Parameter", "Value"), ("n_layers", "32"), ("head_dim", "128"), ("vocab_size", "32000")]
    for i, (key, value) in enumerate(data):
        page.insert_text((405, 178+i*23), key, fontsize=9)
        page.insert_text((485, 178+i*23), value, fontsize=9)
    page.insert_text((390, 273 if caption_below else 150), "Table 1: Configuration.", fontsize=9)
    blocks = next(documents.iter_pages(save(doc, tmp_path)))["blocks"]
    table = next(block for block in blocks if block["kind"] == "table")
    assert "n_layers | 32" in table["text"]
    assert "vocab_size | 32000" in table["text"]
    assert "Adjacent prose" not in table["text"]
    assert sum(" ".join(block["text"].split()).count("Adjacent prose") for block in blocks) == 4
    assert table["bbox"][0] >= 389


def test_two_tables_on_same_baseline_are_not_merged(tmp_path):
    doc = pymupdf.open(); page = doc.new_page(width=800, height=700)
    grid(page, x=40)
    grid(page, x=430)
    blocks = next(documents.iter_pages(save(doc, tmp_path)))["blocks"]
    tables = [block for block in blocks if block["kind"] == "table"]
    assert len(tables) == 2
    assert all(block["bbox"][2]-block["bbox"][0] == 300 for block in tables)
    assert sum(block["text"].count("Alpha") for block in blocks) == 2


def test_failed_table_detection_preserves_source_words(tmp_path, monkeypatch):
    doc = pymupdf.open(); page = doc.new_page(); grid(page)
    path = save(doc, tmp_path)
    def fail(*args, **kwargs):
        raise RuntimeError("malformed drawing")
    monkeypatch.setattr(pymupdf.Page, "find_tables", fail)
    text = "\n".join(block["text"] for block in next(documents.iter_pages(path))["blocks"])
    for value in ("Alpha", "Beta", "Gamma", "82.5", "90.0"):
        assert value in text


def test_equation_and_number_stay_together_without_becoming_a_heading(tmp_path):
    doc = pymupdf.open(); page = doc.new_page(); paragraphs(page)
    page.insert_text((180, 160), "E = m c", fontsize=14, fontname="tiit")
    page.insert_text((224, 153), "2", fontsize=9)
    page.insert_text((510, 160), "(1)", fontsize=12)
    page.insert_text((40, 220), "We set x = 3 in this experiment and compare the results.", fontsize=10)
    page.insert_text((40, 260), "2 Experimental Results", fontsize=14, fontname="hebo")
    blocks = next(documents.iter_pages(save(doc, tmp_path)))["blocks"]
    equation = next(block for block in blocks if block["kind"] == "formula")
    assert equation["text"] == "E = m c2 (1)"
    assert equation["bbox"][0] == 180 and equation["bbox"][2] > 524
    assert next(block for block in blocks if "We set x" in block["text"])["kind"] == "paragraph"
    assert next(block for block in blocks if "2 Experimental Results" in block["text"])["kind"] == "heading"
    assert not any(block["text"] == "(1)" for block in blocks)


def test_asymmetric_columns_keep_column_order_and_spanning_heading(tmp_path):
    doc = pymupdf.open(); page = doc.new_page(width=600, height=800)
    page.insert_text((40, 55), "A wide heading spanning both columns of this document", fontsize=17)
    for y, left, right in [(120, "Left first evidence", "Right first evidence"), (190, "Left second evidence", "Right second evidence")]:
        page.insert_text((40, y), left, fontsize=10)
        page.insert_text((260, y), right, fontsize=10)
    page.insert_text((500, 770), "1", fontsize=10)
    blocks = next(documents.iter_pages(save(doc, tmp_path)))["blocks"]
    body = [block["text"] for block in blocks if "evidence" in block["text"]]
    assert body == ["Left first evidence", "Left second evidence", "Right first evidence", "Right second evidence"]
    assert blocks[0]["text"].startswith("A wide heading")


def test_prose_and_chart_axes_are_not_tables(tmp_path):
    doc = pymupdf.open(); page = doc.new_page(); paragraphs(page)
    page.insert_textbox(pymupdf.Rect(40, 90, 480, 200), "An ordinary paragraph contains multiple words aligned to a margin. " * 5, fontsize=10)
    page.draw_line((50, 230), (50, 350)); page.draw_line((50, 350), (300, 350))
    for x in range(80, 290, 40):
        page.draw_line((x, 347), (x, 353))
    blocks = next(documents.iter_pages(save(doc, tmp_path)))["blocks"]
    assert not any(block["kind"] == "table" for block in blocks)


def test_three_or_more_table_row_fragments_keep_their_shared_evidence(tmp_path):
    doc = pymupdf.open(); page = doc.new_page(); paragraphs(page)
    for x, value in [(50, "Exam"), (200, "298 / 400"), (320, "90th"), (440, "10th")]:
        page.insert_text((x, 160), value, fontsize=10)
    blocks = next(documents.iter_pages(save(doc, tmp_path)))["blocks"]
    assert any(all(term in block["text"] for term in ["Exam", "298 / 400", "90th", "10th"]) for block in blocks)


def test_ocr_bbox_is_not_rotated_twice(tmp_path, monkeypatch):
    source = pymupdf.open(); page = source.new_page(width=300, height=400); paragraphs(page)
    image = page.get_pixmap().tobytes("png"); source.close()
    doc = pymupdf.open(); page = doc.new_page(width=300, height=400)
    page.insert_image(page.rect, stream=image); page.set_rotation(90)
    path = save(doc, tmp_path)
    monkeypatch.setattr(documents, "_recognize_image", lambda *_: [{"text": "OCR text already uses rendered page coordinates", "bbox": [210, 50, 230, 190], "size": 12, "bold": False}])
    result = next(documents.iter_pages(path))
    assert result["ocr"] and result["blocks"][0]["bbox"] == [210, 50, 230, 190]


@pytest.mark.parametrize("mirror", [False, True])
def test_ocr_table_does_not_capture_a_neighboring_prose_column(mirror):
    def line(text, box):
        if mirror:
            box = [600-box[2], box[1], 600-box[0], box[3]]
        return {"text": text, "bbox": box, "size": 11, "bold": False}

    lines = [line("Table 1: Measured scores.", [330, 110, 460, 123])]
    for row, values in enumerate([("Model", "Score", "Time"), ("Alpha", "82.5", "12"),
                                  ("Beta", "90.0", "18"), ("Gamma", "88.7", "15")]):
        y = 140 + row*24
        lines.append(line(f"Adjacent sentence {row} belongs to a separate column.", [40, y, 270, y+12]))
        lines.extend(line(text, [x, y, x+40, y+12]) for x, text in zip((340, 430, 520), values))
    with pymupdf.open() as doc:
        page = doc.new_page(width=600, height=800)
        blocks = documents._legacy_table_blocks(page, documents._ocr_blocks(lines, 600), lines, True)
    table = next(block for block in blocks if block["kind"] == "table")
    assert "Adjacent" not in table["text"]
    assert (table["bbox"][2] < 280) if mirror else (table["bbox"][0] > 320)
    text = "\n".join(block["text"] for block in blocks)
    for token in ("Adjacent sentence 0", "Adjacent sentence 3", "82.5", "88.7"):
        assert text.count(token) == 1


@pytest.mark.parametrize("prose_y", [None, 450, 240])
def test_ocr_short_caption_keeps_all_columns_of_a_wide_table(prose_y):
    lines = [{"text": "Table 1: Results.", "bbox": [40, 110, 130, 123], "size": 11, "bold": False}]
    for row, values in enumerate([("Model", "Score", "Time"), ("Alpha", "82.5", "12"),
                                  ("Beta", "90.0", "18"), ("Gamma", "88.7", "15")]):
        y = 140 + row*24
        for x, text in zip((40, 300, 520), values):
            lines.append({"text": text, "bbox": [x, y, x+40, y+12], "size": 11, "bold": False})
    if prose_y:
        for y in (prose_y, prose_y+24):
            lines.append({"text": "Distant prose belongs to a later section.", "bbox": [350, y, 580, y+12],
                          "size": 11, "bold": False})
    with pymupdf.open() as doc:
        page = doc.new_page(width=600, height=800)
        blocks = documents._legacy_table_blocks(page, documents._ocr_blocks(lines, 600), lines, True)
    table = next(block for block in blocks if block["kind"] == "table")
    assert "Alpha | 82.5 | 12" in table["text"]
    assert "Gamma | 88.7 | 15" in table["text"]
    assert table["bbox"][2] == 560
    if prose_y:
        assert "Distant prose" not in table["text"]
        assert sum(block["text"].count("Distant prose") for block in blocks) == 2


def test_generator_releases_shared_lock_between_yields(tmp_path):
    doc = pymupdf.open(); paragraphs(doc.new_page()); paragraphs(doc.new_page())
    iterator = documents.iter_pages(save(doc, tmp_path))
    next(iterator)
    def acquire():
        obtained = documents.PYMUPDF_LOCK.acquire(timeout=.3)
        if obtained:
            documents.PYMUPDF_LOCK.release()
        return obtained
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(acquire).result(timeout=1)
    iterator.close()


def test_concurrent_parsing_serializes_pymupdf_access(tmp_path, monkeypatch):
    paths = []
    for i in range(2):
        doc = pymupdf.open(); paragraphs(doc.new_page()); paths.append(save(doc, tmp_path, f"thread-{i}.pdf"))
    original = documents._page_blocks
    active = 0
    peak = 0
    counter = threading.Lock()
    def observe(*args):
        nonlocal active, peak
        with counter:
            active += 1; peak = max(peak, active)
        time.sleep(.03)
        try:
            return original(*args)
        finally:
            with counter:
                active -= 1
    monkeypatch.setattr(documents, "_page_blocks", observe)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda path: list(documents.iter_pages(path)), paths))
    assert len(results) == 2 and peak == 1


def test_real_mistral_narrow_bottom_caption_preserves_parameters():
    path = ROOT / "pdf/2310.06825v1.pdf"
    if not path.exists():
        pytest.skip("Real Mistral PDF unavailable")
    iterator = documents.iter_pages(path, start_page=2)
    page = next(iterator); iterator.close()
    table = next(block for block in page["blocks"] if block["kind"] == "table" and "Table 1:" in block["text"])
    assert "n_layers | 32" in table["text"]
    assert "context_len | 8192" in table["text"]
    assert "vocab_size | 32000" in table["text"]
    assert "Sliding Window Attention" not in table["text"]


def test_real_transformer_table_and_references_remain_intact():
    path = ROOT / "pdf/1706.03762v1.pdf"
    if not path.exists():
        pytest.skip("Real Transformer PDF unavailable")
    pages = list(documents.iter_pages(path))
    table = next(block for block in pages[7]["blocks"] if block["kind"] == "table")
    assert "Transformer (big) | 28.4 | 41.0" in table["text"]
    assert "ConvS2S Ensemble [9] | 26.36 | 41.29" in table["text"]
    blocks = [{**block, "page": page["page"]} for page in pages for block in page["blocks"]]
    assert len(documents.extract_references(blocks)) == 36


def test_real_exam_table_keeps_existing_row_evidence_together():
    path = ROOT / "pdf/2303.08774v6.pdf"
    if not path.exists():
        pytest.skip("Real exam table PDF unavailable")
    iterator = documents.iter_pages(path, start_page=5)
    page = next(iterator); iterator.close()
    assert any(all(term in block["text"] for term in ["Uniform Bar Exam", "298 / 400", "90th"])
               for block in page["blocks"])
