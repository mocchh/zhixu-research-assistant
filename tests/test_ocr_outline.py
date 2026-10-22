"""Structure regressions from real scanned pages, without rerunning an OCR model.

The fixture preserves the OCR engine's original text and line boxes.  Replaying
it through the normal PDF parser isolates structure inference from recognition
variability; the opt-in OCR suite separately exercises the installed engine.
"""
from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

import pymupdf
import pytest

from backend import documents


ROOT = Path(__file__).resolve().parents[1]
SCAN = ROOT / "pdf/1706.03762v1_img.pdf"
FIXTURE = ROOT / "tests/fixtures/ocr_transformer_lines.json"


@pytest.fixture(scope="module")
def recognized():
    value = json.loads(FIXTURE.read_text())
    assert value["sha256"] == hashlib.sha256(SCAN.read_bytes()).hexdigest()
    assert [page["page"] for page in value["pages"]] == list(range(1, 16))
    assert value["engine"] == "rapidocr-onnxruntime"
    return value


@pytest.fixture
def replay(monkeypatch, recognized):
    def ocr_page(page, _path):
        stored = recognized["pages"][page.number]
        assert (page.rect.width, page.rect.height) == (stored["width"], stored["height"])
        return deepcopy(stored["lines"]), list(stored["warnings"])

    monkeypatch.setattr(documents, "_ocr_page", ocr_page)
    return lambda start_page=1: list(documents.iter_pages(SCAN, start_page=start_page))


def all_blocks(pages):
    return [{**block, "page": page["page"], "id": f"p{page['page']}-b{i}"}
            for page in pages for i, block in enumerate(page["blocks"])]


def outline(pages):
    return documents.sections_from_blocks(all_blocks(pages))


def contains_box(outer, inner):
    # _lines_block rounds coordinates to three decimal places.
    return (outer[0] <= inner[0] + .001 and outer[1] <= inner[1] + .001
            and outer[2] >= inner[2] - .001 and outer[3] >= inner[3] - .001)


@pytest.mark.parametrize("page_number,text", [
    (5, "bottoms of the encoder and decoder stacks. The positional encodings have the same dimension dmodel"),
    (9, "for both WSJ only and the semi-supervised setting."),
    (14, ".="),
])
def test_tall_ocr_body_and_symbol_boxes_are_not_headings(replay, recognized, page_number, text):
    pages = replay()
    page = pages[page_number - 1]
    originals = [line for line in recognized["pages"][page_number - 1]["lines"] if line["text"] == text]
    assert originals, "The regression must exercise original OCR text and geometry."
    assert text not in {section["title"] for section in outline(pages)}
    for original in originals:
        containing = [block for block in page["blocks"]
                      if text in block["text"] and contains_box(block["bbox"], original["bbox"])]
        assert containing, "Removing a false heading must retain its text and original location."
        assert all(block["kind"] != "heading" for block in containing)


def test_scan_formulas_and_figure_captions_stay_out_of_outline(replay, recognized):
    pages = replay()
    titles = [section["title"] for section in outline(pages)]
    assert not any(re.match(r"(?:Figure|Fig\.|Table)\s*\d", title, re.I) for title in titles)
    assert not any(title.startswith(("PE(", "PEpos.")) for title in titles)
    formula_lines = [line for line in recognized["pages"][4]["lines"] if line["text"].startswith("PE(")]
    assert len(formula_lines) == 2
    for line in formula_lines:
        assert any(line["text"] in block["text"] and contains_box(block["bbox"], line["bbox"])
                   for block in pages[4]["blocks"])


def test_real_numbered_and_unnumbered_scan_headings_survive(replay):
    sections = outline(replay())
    titles = {section["title"]: section for section in sections}
    for title, page in [("Attention Is All You Need", 1), ("Abstract", 1),
                        ("1Introduction", 1), ("3.5Positional Encoding", 5),
                        ("AttentionVisualizations", 13)]:
        assert title in titles
        assert titles[title]["page"] == page
    assert titles["3.5Positional Encoding"]["level"] == 2


def test_reference_page_continues_until_the_next_real_section(replay):
    pages = replay()
    for reference_page in pages[10:12]:
        assert reference_page["blocks"]
        assert all(block["kind"] == "reference" and block["section"].casefold() == "references"
                   for block in reference_page["blocks"])
    visualization_page = pages[12]
    section = next(block for block in visualization_page["blocks"] if block["text"] == "AttentionVisualizations")
    assert section["kind"] == "heading"
    assert all(block["kind"] != "reference" for block in visualization_page["blocks"])


def test_all_visible_scan_reference_labels_have_separate_source_anchors(replay, recognized):
    # The OCR text contains every bracketed label, but often omits the following
    # space.  Splitting only on "] " used to merge neighbouring references.
    raw_labels = [match.group(1) for page in recognized["pages"][9:12]
                  for line in page["lines"]
                  if (match := re.match(r"^\[(\d+)\]", line["text"]))]
    assert raw_labels == [str(number) for number in range(1, 37)]
    references = documents.extract_references(all_blocks(replay()))
    assert [ref["label"] for ref in references] == raw_labels
    for ref in references:
        assert ref["page"] == (10 if int(ref["label"]) <= 14 else 11 if int(ref["label"]) <= 33 else 12)
        assert ref["block_id"]
        assert ref["text"].startswith(f"[{ref['label']}]")
        assert len(re.findall(r"(?m)^\[\d+\]", ref["text"])) == 1
    assert "Rethinking the inception architecture" in references[31]["text"]
    assert "1: Long Papers),pages 434-443.ACL,August 2013." in references[35]["text"]
    assert "Generating sequences with recurrent neural networks" in references[9]["text"]
    assert "Generating sequences" not in references[8]["text"]


@pytest.mark.parametrize("start_page", [10, 11, 12, 13, 14])
def test_resumed_scan_parse_matches_full_parse_reference_context(replay, start_page):
    full = replay()
    assert replay(start_page) == full[start_page - 1:]


def test_ocr_classification_is_repeatable_and_keeps_all_source_characters(recognized):
    for page in recognized["pages"]:
        lines = deepcopy(page["lines"])
        original = deepcopy(lines)
        first = documents._ocr_blocks(lines, page["width"])
        second = documents._ocr_blocks(lines, page["width"])
        assert first == second
        assert lines == original, "Cached raw recognition must not be mutated by classification."
        source = Counter(character for line in lines for character in line["text"] if not character.isspace())
        parsed = Counter(character for block in first for character in block["text"] if not character.isspace())
        assert parsed == source, page["page"]


def line(text, x, y, width=400, height=10):
    return {"text": text, "bbox": [x, y, x + width, y + height], "size": height, "bold": False}


@pytest.mark.parametrize("height", [12, 14, 18])
def test_capitalized_prose_in_a_continuous_paragraph_is_not_a_heading(height):
    lines = [line("The training samples are selected before optimization", 80, 80),
             line("We trained the network on eight GPUs", 80, 90, height=height),
             line("and evaluated the result on the held out sample.", 80, 90 + height),
             line("The reported score uses the same evaluation protocol.", 80, 100 + height)]
    blocks = documents._ocr_blocks(lines, 600)
    assert not any(block["kind"] == "heading" for block in blocks)
    assert "We trained the network on eight GPUs" in "\n".join(block["text"] for block in blocks)


def test_isolated_unnumbered_sentence_case_heading_remains_a_heading():
    lines = [line("The previous paragraph ends with its supporting evidence.", 80, 50),
             line("Experimental setup", 80, 85, width=150, height=14),
             line("The model is trained with the following fixed parameters.", 80, 120),
             line("Each comparison uses an independent evaluation sample.", 80, 132)]
    blocks = documents._ocr_blocks(lines, 600)
    heading = next(block for block in blocks if block["text"] == "Experimental setup")
    assert heading["kind"] == "heading"


@pytest.mark.parametrize("title", ["2 Models: architecture and training", "2 Results on GPT-3.5"])
def test_numbered_headings_allow_descriptive_colons_and_model_versions(title):
    lines = [line("The preceding paragraph ends here.", 80, 40),
             line(title, 80, 75, width=260, height=12),
             line("The following paragraph explains the experimental configuration.", 80, 110),
             line("Each experiment reports the same fixed evaluation metrics.", 80, 122)]
    heading = next(block for block in documents._ocr_blocks(lines, 600) if block["text"] == title)
    assert heading["kind"] == "heading"


@pytest.mark.parametrize("title", ["Introduction", "Methods"])
def test_isolated_single_word_sections_do_not_require_an_excessive_box_height(title):
    lines = [line("The previous section finishes before this large blank space.", 80, 40),
             line(title, 80, 100, width=100, height=12),
             line("This section describes the experiment and its evaluation protocol.", 80, 140),
             line("The remaining text gives the necessary reproducibility details.", 80, 152)]
    heading = next(block for block in documents._ocr_blocks(lines, 600) if block["text"] == title)
    assert heading["kind"] == "heading"


def test_simultaneous_unnumbered_column_headings_keep_their_own_body_text():
    lines = [line("The first column ends its preceding paragraph.", 40, 40, width=230),
             line("The second column ends its preceding paragraph.", 330, 40, width=230),
             line("Experimental Methods", 40, 80, width=190, height=14),
             line("Additional Results", 330, 80, width=180, height=14),
             line("The method uses a fixed training protocol.", 40, 110, width=230),
             line("and retains the same evaluation sample.", 40, 122, width=230),
             line("The results report an independent comparison.", 330, 110, width=230),
             line("and include the observed uncertainty.", 330, 122, width=230)]
    blocks = documents._ocr_blocks(lines, 600)
    assert [block["text"] for block in blocks if block["kind"] == "heading"] == [
        "Experimental Methods", "Additional Results"]
    for text, left in [("The method uses", 40), ("The results report", 330)]:
        body = next(block for block in blocks if text in block["text"])
        assert body["kind"] == "paragraph"
        assert body["bbox"][0] == left
        assert body["bbox"][2] <= left + 230


def test_split_reference_row_ignores_baseline_jitter_without_crossing_columns():
    lines = [line("[9]First Author. Previous reference.", 40, 80, width=230),
             line("[10]Second Author.", 40, 110, width=90),
             line("A title belonging to the second author.", 136, 109.6, width=150),
             line("[11]Third Author. Other column reference.", 340, 109.5, width=230)]
    blocks = documents._ocr_blocks(lines, 600)
    reference = next(block for block in blocks if block["text"].startswith("[10]"))
    assert reference["text"] == "[10]Second Author. A title belonging to the second author."
    assert reference["bbox"] == [40, 109.6, 286, 120]
    assert any(block["text"].startswith("[11]Third Author.") and block["bbox"][0] == 340
               for block in blocks)


@pytest.mark.parametrize("reference_heading", ["References:", "Bibliography："])
@pytest.mark.parametrize("next_heading", ["Appendix A", "AttentionVisualizations"])
def test_punctuated_reference_heading_continues_across_pages_and_stops_at_a_section(
        tmp_path, monkeypatch, reference_heading, next_heading):
    path = tmp_path / "reference-boundary.pdf"
    with pymupdf.open() as doc:
        doc.new_page(width=600, height=800)
        doc.new_page(width=600, height=800)
        doc.save(path)
    recognition = [
        [line(reference_heading, 80, 50, width=120, height=12),
         line("[1]First Author. A study with a reference spanning two pages.", 80, 90)],
        [line("The continuation supplies the venue, pages 45-51, 2020.", 80, 40),
         line("[2]Second Author. Another reproducible experiment.", 80, 75),
         line("Journal of Experimental Methods, 2021.", 80, 87),
         line(next_heading, 80, 145, width=190, height=12),
         line("The appendix describes additional measurements.", 80, 185),
         line("These measurements belong to the new section.", 80, 197)],
    ]
    monkeypatch.setattr(documents, "_requires_ocr", lambda *_: True)
    monkeypatch.setattr(documents, "_ocr_page", lambda page, _path: (deepcopy(recognition[page.number]), []))
    pages = list(documents.iter_pages(path))
    assert list(documents.iter_pages(path, start_page=2)) == pages[1:]
    assert pages[0]["blocks"][0]["text"] == reference_heading
    second_page = pages[1]["blocks"]
    next_section_index = next(i for i, block in enumerate(second_page) if block["text"] == next_heading)
    assert second_page[next_section_index]["kind"] == "heading"
    assert all(block["kind"] == "reference" and block["section"] == reference_heading
               for block in second_page[:next_section_index])
    assert all(block["kind"] != "reference" and block["section"] == next_heading
               for block in second_page[next_section_index:])
    for page, original in zip(pages, recognition):
        for source in original:
            assert any(source["text"] in block["text"] and contains_box(block["bbox"], source["bbox"])
                       for block in page["blocks"])
