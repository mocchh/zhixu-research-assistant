"""Opt-in OCR regression cases using image-only, locally generated PDFs.

These exercise the installed OCR engine, not a stub or an external model.
RUN_OCR_TESTS=1 enables them alongside the complete scanned-paper fixture.
"""
from __future__ import annotations

import os

import pymupdf
import pytest

from backend import documents


pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_OCR_TESTS") != "1", reason="Opt-in local OCR integration"
)


@pytest.fixture(autouse=True)
def require_engine():
    if not documents.get_ocr_status()["available"]:
        pytest.skip("A local OCR engine is unavailable")


def scan_pdf(tmp_path, draw, *, scale=1.5, crop=None):
    """Rasterize known text so expected content and physical locations are fixed."""
    with pymupdf.open() as source:
        page = source.new_page(width=600, height=800)
        draw(page)
        pixels = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
    path = tmp_path / "scan.pdf"
    with pymupdf.open() as scanned:
        page = scanned.new_page(width=600, height=800)
        page.insert_image(page.rect, stream=pixels.tobytes("png"))
        if crop:
            page.set_cropbox(pymupdf.Rect(crop))
        assert page.get_text().strip() == ""
        scanned.save(path)
    return path


def check_geometry(parsed):
    assert parsed["ocr"] and parsed["blocks"] and not parsed["warnings"]
    for block in parsed["blocks"]:
        x0, y0, x1, y1 = block["bbox"]
        assert 0 <= x0 < x1 <= parsed["width"]
        assert 0 <= y0 < y1 <= parsed["height"]


def test_scan_two_columns_preserve_reading_order(tmp_path):
    def draw(page):
        page.insert_text((40, 60), "Evidence from two independent experimental columns", fontsize=17)
        for y, left, right in [
            (140, "Alpha first evidence", "Gamma first evidence"),
            (210, "Beta second evidence", "Delta second evidence"),
        ]:
            page.insert_text((40, y), left, fontsize=13)
            page.insert_text((340, y), right, fontsize=13)

    parsed = next(documents.iter_pages(scan_pdf(tmp_path, draw)))
    check_geometry(parsed)
    text = "\n".join(block["text"] for block in parsed["blocks"])
    positions = [text.index(word) for word in ("Alpha", "Beta", "Gamma", "Delta")]
    assert positions == sorted(positions)


@pytest.mark.parametrize("scale,crop", [(1.5, (20, 30, 580, 770)), (1.0, None)])
def test_scan_numeric_evidence_and_coordinates_survive_crop_or_low_resolution(tmp_path, scale, crop):
    expected = []

    def draw(page):
        for y, text in [(140, "Accuracy 98.2 percent"), (200, "Learning rate 0.001"),
                        (260, "Training uses 128 samples")]:
            page.insert_text((80, y), text, fontsize=14)
        expected.extend(page.get_text("blocks"))

    path = scan_pdf(tmp_path, draw, scale=scale, crop=crop)
    parsed = next(documents.iter_pages(path))
    check_geometry(parsed)
    offset_x, offset_y = crop[:2] if crop else (0, 0)
    for raw, token in zip(expected, ("98.2", "0.001", "128")):
        block = next(block for block in parsed["blocks"] if token in block["text"])
        box = pymupdf.Rect(block["bbox"])
        target = pymupdf.Rect(raw[:4]) + (-offset_x, -offset_y, -offset_x, -offset_y)
        overlap = (box & target).get_area() / (box | target).get_area()
        assert overlap > .5, (token, box, target)


def test_scan_narrow_table_keeps_adjacent_prose_separate(tmp_path):
    def draw(page):
        page.insert_text((330, 120), "Table 1: Measured scores.", fontsize=11)
        for index, text in enumerate([
            "Adjacent prose explains the experiment.",
            "Control samples remain independent.",
            "The comparison uses equal resources.",
            "These sentences belong to the left column.",
        ]):
            page.insert_text((40, 150 + index * 24), text, fontsize=11)
        rows = [("Model", "Score", "Time"), ("Alpha", "82.5", "12"),
                ("Beta", "90.0", "18"), ("Gamma", "88.7", "15")]
        for index, values in enumerate(rows):
            for x, value in zip((340, 430, 520), values):
                page.insert_text((x, 150 + index * 24), value, fontsize=11)

    parsed = next(documents.iter_pages(scan_pdf(tmp_path, draw)))
    check_geometry(parsed)
    table = next(block for block in parsed["blocks"] if block["kind"] == "table" and "82.5" in block["text"])
    assert "Adjacentprose" not in "".join(table["text"].split())
    assert "Controlsamples" not in "".join(table["text"].split())
    assert table["bbox"][0] > 320
    text = "".join("".join(block["text"].split()) for block in parsed["blocks"])
    for token in ("Adjacentprose", "Controlsamples", "82.5", "90.0", "88.7"):
        assert text.count(token) == 1
