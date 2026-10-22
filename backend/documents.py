"""PDF parsing with page coordinates and a real, optional OCR fallback.

All public page numbers are one-based. Original PDFs are never modified.
The parser deliberately preserves the source text, including contradictory
numbers in different versions; it does not use a language model to fill gaps.
"""
from __future__ import annotations

import csv
import io
import importlib.util
import json
import math
import os
import re
import shutil
import statistics
import struct
import subprocess
import sys
import tempfile
import threading
import unicodedata
from collections import OrderedDict
from functools import wraps
from pathlib import Path
from typing import Iterator

import pymupdf as fitz

MAX_PDF_BYTES = 250 * 1024 * 1024
PARSER_VERSION = "layout-2026-10-22.2"
MAX_PAGES = 2000
MAX_RENDER_PIXELS = 20_000_000
_OCR_LOCK = threading.Lock()
_VISION_BINARY: Path | None = None
_VISION_ERROR: str | None = None
_OCR_CACHE: OrderedDict[tuple, tuple[list[dict], list[str]]] = OrderedDict()
_CACHE_LOCK = threading.Lock()
_RAPID_LOCK = threading.Lock()
_RAPID_ENGINE = None
_ARXIV = re.compile(r"(?<!\d)(\d{4}\.\d{4,5})(?:v(\d+))?", re.I)
_SECTION = re.compile(r"^(\d{1,2}(?:\.\d{1,2}){0,3})\.?\s*([A-Z][^\n]{1,130})$")
_TABLE_CAPTION = re.compile(r"^Table\s*\d+[:.]", re.I)
_REFERENCE_START = r"\[\d{1,4}\](?=\s|[^\W\d_])"
_SPECIAL_HEADINGS = {
    "abstract", "references", "bibliography", "acknowledgments",
    "acknowledgements", "appendix", "appendices", "conclusion", "conclusions",
}
_OCR_SPECIAL_HEADINGS = _SPECIAL_HEADINGS | {
    "introduction", "background", "methods", "methodology", "results",
    "discussion", "limitations", "related work", "参考文献",
}


def _reference_section(text: str) -> bool:
    return text.strip().rstrip(":：").casefold() in {"references", "bibliography", "参考文献"}

# Every caller sharing this module (including HTTP document routes) must use
# this lock around PyMuPDF access. Never hold it across a generator yield.
PYMUPDF_LOCK = threading.RLock()


def _serialized(function):
    @wraps(function)
    def call(*args, **kwargs):
        with PYMUPDF_LOCK:
            return function(*args, **kwargs)
    return call


@_serialized
def _native_rect(page):
    return page.rect * page.derotation_matrix


@_serialized
def _open_pdf(path: Path) -> fitz.Document:
    path = Path(path)
    if not path.is_file():
        raise ValueError("PDF 文件不存在。")
    if path.stat().st_size > MAX_PDF_BYTES:
        raise ValueError("PDF 超过 250 MB 的单文件限制。")
    with path.open("rb") as stream:
        if b"%PDF-" not in stream.read(1024):
            raise ValueError("文件内容不是有效的 PDF。")
    try:
        doc = fitz.open(path)
    except Exception as exc:
        raise ValueError("无法打开 PDF，文件可能已损坏。") from exc
    if doc.needs_pass:
        doc.close()
        raise ValueError("暂不支持需要密码的 PDF，请上传解密后的文件。")
    if not doc.is_pdf or not 0 < doc.page_count <= MAX_PAGES:
        doc.close()
        raise ValueError(f"PDF 页数必须介于 1 和 {MAX_PAGES} 之间。")
    return doc


@_serialized
def _check_page(page: fitz.Page) -> None:
    width, height = page.rect.width, page.rect.height
    if not all(math.isfinite(x) and 1 <= x <= 14400 for x in (width, height)):
        raise ValueError("PDF 页面尺寸异常。")


def _clean(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).replace("\x00", "")
    return re.sub(r"[ \t]+", " ", text).strip()


def _union(boxes: list[list[float]]) -> list[float]:
    return [min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes)]


def _clip_bbox(box: list[float], width: float, height: float) -> list[float] | None:
    """Normalize and intersect a rectangle with the rendered page bounds."""
    try:
        x0, y0, x1, y1 = (float(value) for value in box)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(value) for value in (x0, y0, x1, y1)):
        return None
    x0, x1 = sorted((max(0.0, min(width, x0)), max(0.0, min(width, x1))))
    y0, y1 = sorted((max(0.0, min(height, y0)), max(0.0, min(height, y1))))
    if x0 >= x1 or y0 >= y1:
        return None
    return [x0, y0, x1, y1]


def _heading_level(text: str) -> int:
    match = _SECTION.match(text.strip())
    if match:
        return match.group(1).count(".") + 1
    return 1


def _is_heading(text: str, size: float, body_size: float, bold: bool = False) -> bool:
    text = text.strip()
    if not text or len(text) > 150 or "\n" in text:
        return False
    if re.fullmatch(r"[\d.]+", text):
        return False
    if text.lower() in _SPECIAL_HEADINGS:
        return True
    if _SECTION.match(text) and not re.search(r"[=<>]|\.{3}", text):
        return size >= body_size * .93 or bold
    if re.match(r"^(?:Appendix\s+)?[A-Z](?:\.\d+)*\.?\s+[A-Z]", text):
        return bold or size > body_size * 1.04
    return (size >= body_size * 1.16 or (bold and size > body_size * 1.03)) and len(text) < 110


def _reading_order(items: list[dict], width: float) -> list[dict]:
    """Handle the common single/two-column layouts, retaining wide headings."""
    if not items:
        return []
    midpoint = width / 2
    # Look for a real gutter instead of assuming equally wide columns. Wide
    # headings/tables may cross it; isolated page numbers cannot establish it.
    edges = sorted({round(x["bbox"][i], 2) for x in items for i in (0, 2)})
    candidates = []
    for a, b in zip(edges, edges[1:]):
        cut = (a + b) / 2
        if b - a < 8 or not width * .2 < cut < width * .8:
            continue
        left = [x for x in items if x["bbox"][2] <= a + .1 and len(x["text"]) > 2]
        right = [x for x in items if x["bbox"][0] >= b - .1 and len(x["text"]) > 2]
        if min(len(left), len(right)) < 2:
            continue
        overlap = min(max(x["bbox"][3] for x in left), max(x["bbox"][3] for x in right)) - max(min(x["bbox"][1] for x in left), min(x["bbox"][1] for x in right))
        if overlap < 20:
            continue
        cross = [x for x in items if x["bbox"][0] < cut < x["bbox"][2]
                 and x["bbox"][2] - x["bbox"][0] < width * .65]
        if cross:
            continue
        candidates.append((min(len(left), len(right)), min(b-a, 60), -abs(cut-width/2), cut))
    if candidates:
        midpoint = max(candidates)[-1]
    wide = sorted([x for x in items if x["bbox"][0] < midpoint - width * .1
                   and x["bbox"][2] > midpoint + width * .1],
                  key=lambda x: (x["bbox"][1], x["bbox"][0]))
    narrow = [x for x in items if x not in wide]
    result: list[dict] = []
    for anchor in wide + [None]:
        limit = anchor["bbox"][1] if anchor else float("inf")
        band = [x for x in narrow if x["bbox"][1] < limit]
        narrow = [x for x in narrow if x["bbox"][1] >= limit]
        left = [x for x in band if x["bbox"][0] < midpoint]
        right = [x for x in band if x["bbox"][0] >= midpoint]
        # Require actual columns, not just a right-aligned page number.
        if len(left) >= 2 and len(right) >= 2:
            result.extend(sorted(left, key=lambda x: (x["bbox"][1], x["bbox"][0])))
            result.extend(sorted(right, key=lambda x: (x["bbox"][1], x["bbox"][0])))
        else:
            result.extend(sorted(band, key=lambda x: (x["bbox"][1], x["bbox"][0])))
        if anchor:
            result.append(anchor)
    return result


def _formula_line(line: dict) -> bool:
    """Conservative display-math label, never a conversion to LaTeX."""
    text = line["text"]
    if not 3 <= len(text) <= 180 or re.search(r"[.!?。]$", text):
        return False
    operators = sum(c in "=+−×÷∑∏∫√≈≠≤≥∞∂∇⊗∈" for c in text)
    if not operators:
        return False
    words = re.findall(r"[A-Za-z]{3,}", text)
    math_words = {"sin", "cos", "tan", "log", "exp", "max", "min", "arg", "softmax", "attention", "relu", "norm", "sqrt", "sum", "lim"}
    prose = [word for word in words if word.lower() not in math_words]
    return not prose and ("=" in text or operators >= 2 or line.get("math_font", False))


def _join_formula_numbers(blocks: list[dict], width: float) -> list[dict]:
    consumed = set()
    for formula in blocks:
        if formula["kind"] != "formula":
            continue
        box = formula["bbox"]
        candidates = []
        for i, block in enumerate(blocks):
            other = block["bbox"]
            if i in consumed or not re.fullmatch(r"\(\d{1,3}[a-z]?\)", block["text"]):
                continue
            gap = other[0] - box[2]
            overlap = min(box[3], other[3]) - max(box[1], other[1])
            if 0 <= gap < width * .6 and overlap > min(box[3]-box[1], other[3]-other[1]) * .4:
                candidates.append((gap, i, block))
        if candidates:
            _, i, number = min(candidates, key=lambda x: x[0])
            formula["text"] += " " + number["text"]
            formula["bbox"] = _union([box, number["bbox"]])
            consumed.add(i)
    return [block for i, block in enumerate(blocks) if i not in consumed]


@_serialized
def _native_blocks(page: fitz.Page) -> tuple[list[dict], list[dict]]:
    groups: list[list[dict]] = []
    all_lines: list[dict] = []
    sizes: list[float] = []
    for raw in page.get_text("dict", flags=fitz.TEXTFLAGS_DICT & ~fitz.TEXT_PRESERVE_IMAGES)["blocks"]:
        if raw.get("type") != 0:
            continue
        lines = []
        for line in raw.get("lines", []):
            spans = line.get("spans", [])
            text = _clean("".join(span.get("text", "") for span in spans))
            if not text:
                continue
            size = max((span.get("size", 10) for span in spans), default=10)
            item = {"text": text, "bbox": list(line["bbox"]), "size": size,
                    "bold": any(span.get("flags", 0) & 16 for span in spans),
                    "math_font": any(re.search(r"CM(?:MI|SY|EX)|Math|Symbol", span.get("font", ""), re.I) for span in spans)}
            # Ignore vertical arXiv stamps in the prose and metadata heuristics.
            direction = line.get("dir", (1, 0))
            if abs(direction[1]) > .3:
                continue
            lines.append(item)
            all_lines.append(item)
            if len(text) > 30:
                sizes.append(size)
        # TeX often places the section number and name in separate PDF lines
        # on the exact same baseline. Join these before classifying headings.
        merged_lines = []
        for line in lines:
            if merged_lines and re.fullmatch(r"\d{1,2}(?:\.\d{1,2}){0,3}\.?", merged_lines[-1]["text"]):
                previous = merged_lines[-1]
                if abs(line["bbox"][1] - previous["bbox"][1]) < 3:
                    previous["text"] += " " + line["text"]
                    previous["bbox"] = _union([previous["bbox"], line["bbox"]])
                    previous["bold"] = previous["bold"] or line["bold"]
                    previous["size"] = max(previous["size"], line["size"])
                    continue
            merged_lines.append(dict(line))
        if merged_lines:
            groups.append(merged_lines)
    body_size = statistics.median(sizes) if sizes else 10
    blocks = []
    for lines in groups:
        pending = []
        # Three or more aligned fragments commonly form a table row. Keep its
        # evidence together even when border/caption detection is inconclusive.
        centers = [(line["bbox"][1] + line["bbox"][3]) / 2 for line in lines]
        row_like = len(lines) >= 3 and max(centers)-min(centers) < body_size * .7
        for line in lines:
            formula = _formula_line(line)
            if formula or _is_heading(line["text"], line["size"], body_size, line["bold"]):
                if pending:
                    blocks.append(_lines_block(pending, "paragraph"))
                    pending = []
                blocks.append(_lines_block([line], "formula" if formula else "heading"))
            else:
                if pending:
                    previous = pending[-1]
                    same_row = abs(line["bbox"][1]-previous["bbox"][1]) < body_size * .4
                    separated = line["bbox"][0]-previous["bbox"][2] > body_size * 2
                    if same_row and separated and not row_like:
                        blocks.append(_lines_block(pending, "paragraph"))
                        pending = []
                pending.append(line)
        if pending:
            blocks.append(_lines_block(pending, "paragraph"))
    width = _native_rect(page).width
    return _reading_order(_join_formula_numbers(blocks, width), width), all_lines


def _lines_block(lines: list[dict], kind: str) -> dict:
    return {"text": "\n".join(x["text"] for x in lines), "kind": kind,
            "bbox": [round(x, 3) for x in _union([x["bbox"] for x in lines])],
            "section": "", "order_index": 0}


def _vision_executable() -> Path:
    global _VISION_BINARY, _VISION_ERROR
    with _OCR_LOCK:
        if _VISION_BINARY and _VISION_BINARY.exists():
            return _VISION_BINARY
        if _VISION_ERROR:
            raise RuntimeError(_VISION_ERROR)
        compiler = shutil.which("swiftc")
        source = Path(__file__).resolve().parent.parent / "scripts" / "ocr_macos.swift"
        if sys.platform != "darwin" or not compiler or not source.exists():
            raise RuntimeError("Apple Vision OCR 不可用。")
        cache = Path(tempfile.mkdtemp(prefix="research-agent-vision-"))
        binary = cache / "ocr_macos"
        module_cache = cache / "modules"
        module_cache.mkdir()
        env = {**os.environ, "SWIFT_MODULECACHE_PATH": str(module_cache),
               "CLANG_MODULE_CACHE_PATH": str(module_cache)}
        try:
            compiled = subprocess.run(
                [compiler, "-O", "-module-cache-path", str(module_cache),
                 str(source), "-o", str(binary)],
                capture_output=True, text=True, timeout=180, env=env,
            )
            if compiled.returncode:
                raise RuntimeError("Apple Vision OCR 编译失败：" + compiled.stderr[-600:])
            _VISION_BINARY = binary
            return binary
        except (OSError, subprocess.TimeoutExpired, RuntimeError) as exc:
            _VISION_ERROR = str(exc)
            raise RuntimeError(_VISION_ERROR) from exc


def get_ocr_status() -> dict:
    if shutil.which("tesseract"):
        return {"available": True, "engine": "tesseract"}
    if importlib.util.find_spec("rapidocr_onnxruntime"):
        return {"available": True, "engine": "rapidocr-onnxruntime"}
    if sys.platform == "darwin" and shutil.which("swiftc") and not _VISION_ERROR:
        return {"available": True, "engine": "apple-vision"}
    return {"available": False, "engine": "none"}


def _recognize_image(png: bytes, width: float, height: float) -> list[dict]:
    global _RAPID_ENGINE
    status = get_ocr_status()
    if not status["available"]:
        raise RuntimeError("没有可用的 OCR 引擎；请安装 Tesseract，或在 macOS 安装命令行开发工具。")
    with tempfile.TemporaryDirectory(prefix="research-agent-page-") as directory:
        image_path = Path(directory) / "page.png"
        image_path.write_bytes(png)
        if status["engine"] == "rapidocr-onnxruntime":
            import onnxruntime
            onnxruntime.disable_telemetry_events()
            from rapidocr_onnxruntime import RapidOCR
            pixel_width, pixel_height = struct.unpack(">II", png[16:24])
            # ONNX sessions are shared; cap CPU threads and serialize inference.
            with _RAPID_LOCK:
                if _RAPID_ENGINE is None:
                    _RAPID_ENGINE = RapidOCR(intra_op_num_threads=1, inter_op_num_threads=1)
                rows, _ = _RAPID_ENGINE(str(image_path))
            lines = []
            for quadrilateral, text, confidence in rows or []:
                xs, ys = [x[0] for x in quadrilateral], [x[1] for x in quadrilateral]
                bbox = [min(xs)*width/pixel_width, min(ys)*height/pixel_height,
                        max(xs)*width/pixel_width, max(ys)*height/pixel_height]
                if text.strip() and bbox[3]-bbox[1] <= (bbox[2]-bbox[0])*1.2:
                    lines.append({"text": _clean(text), "bbox": bbox,
                                  "size": bbox[3]-bbox[1], "bold": False})
            return lines
        if status["engine"] == "apple-vision":
            result = subprocess.run([str(_vision_executable()), str(image_path)],
                                    capture_output=True, text=True, timeout=90)
            if result.returncode:
                raise RuntimeError("Apple Vision OCR 失败：" + result.stderr[-400:])
            rows = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
            return [{"text": _clean(x["text"]),
                     "bbox": [x["bbox"][0] * width, x["bbox"][1] * height,
                              x["bbox"][2] * width, x["bbox"][3] * height],
                     "size": (x["bbox"][3] - x["bbox"][1]) * height,
                     "bold": False}
                    for x in rows if x.get("text", "").strip()]
        executable = shutil.which("tesseract")
        result = subprocess.run([executable, str(image_path), "stdout", "-l", "eng", "tsv"],
                                capture_output=True, text=True, timeout=90)
        if result.returncode:
            raise RuntimeError("Tesseract OCR 失败，请检查 eng 语言包：" + result.stderr[-400:])
        data = list(csv.DictReader(io.StringIO(result.stdout), delimiter="\t"))
        page_rows = [x for x in data if x.get("level") == "1"]
        if not page_rows:
            return []
        sx = width / float(page_rows[0]["width"])
        sy = height / float(page_rows[0]["height"])
        lines: OrderedDict[tuple, list[dict]] = OrderedDict()
        for row in data:
            if row.get("level") != "5" or not row.get("text", "").strip():
                continue
            key = tuple(row[k] for k in ("block_num", "par_num", "line_num"))
            x, y, w, h = (float(row[k]) for k in ("left", "top", "width", "height"))
            lines.setdefault(key, []).append({"text": row["text"],
                                              "bbox": [x*sx, y*sy, (x+w)*sx, (y+h)*sy]})
        return [{"text": _clean(" ".join(x["text"] for x in words)),
                 "bbox": _union([x["bbox"] for x in words]),
                 "size": statistics.median(x["bbox"][3]-x["bbox"][1] for x in words),
                 "bold": False} for words in lines.values()]


@_serialized
def _ocr_page(page: fitz.Page, path: Path) -> tuple[list[dict], list[str]]:
    stat = path.stat()
    key = (str(path.resolve()), stat.st_size, stat.st_mtime_ns, page.number)
    with _CACHE_LOCK:
        cached = _OCR_CACHE.get(key)
        if cached:
            _OCR_CACHE.move_to_end(key)
            return cached
    warnings = []
    try:
        scale = min(2.5, math.sqrt(MAX_RENDER_PIXELS / (page.rect.width * page.rect.height)))
        png = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False).tobytes("png")
        lines = _recognize_image(png, page.rect.width, page.rect.height)
        if not lines:
            warnings.append(f"第 {page.number+1} 页 OCR 未识别到文字，请核对原页。")
    except Exception as exc:
        lines = []
        warnings.append(f"第 {page.number+1} 页需要 OCR，但未能完成：{exc}")
    if not warnings:
        with _CACHE_LOCK:
            _OCR_CACHE[key] = (lines, warnings)
            while len(_OCR_CACHE) > 128:
                _OCR_CACHE.popitem(last=False)
    return lines, warnings


def _ocr_heading(line: dict, lines: list[dict], body: float) -> bool:
    """OCR box height includes descenders/math and is not a font-size signal.

    Require title-like text and separation from surrounding text before using
    height as supporting evidence. Numbered sections can lose their spaces.
    This deliberately leaves uncertain text searchable as ordinary paragraphs.
    """
    text = line["text"].strip()
    if not text or len(text) > 110 or "\n" in text:
        return False
    if text.rstrip(":：").casefold() in _OCR_SPECIAL_HEADINGS:
        return True
    if (not any(c.isalpha() for c in text)
            or re.match(r"^(?:Figure|Fig\.?|Table)\s*\d", text, re.I)
            or re.search(r"[=<>∑∏∫√≈≠≤≥⊗]|[.!?。;,]$|@|https?://", text)):
        return False
    numbered = _SECTION.match(text)
    name = numbered.group(2) if numbered else text
    # A decimal or footnote followed by a sentence is not a section heading.
    if numbered and (int(numbered.group(1).split(".")[0]) == 0
                     or re.search(r"[!?;]", name)):
        return False
    if not name[0].isupper() and not re.search(r"[\u3400-\u9fff]", name):
        return False
    words = re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ]+", re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name))
    if len(words) > 12:
        return False
    box = line["bbox"]
    above = [other["bbox"][3] for other in lines if other is not line
             and other["bbox"][3] <= box[1] + .5
             and min(box[2], other["bbox"][2]) > max(box[0], other["bbox"][0])]
    gap = box[1] - max(above, default=0)
    if gap < body * .65:
        return False
    if numbered:
        return True
    if re.match(r"^(?:Appendix\s+)?[A-Z](?:\.\d+)*\.?\s+[A-Z]", text):
        return line["size"] >= body * 1.04
    if line["size"] < body * 1.16 or re.search(r"[.;,:!?]", name):
        return False
    # Diagram labels often have one large word and share a row with other
    # labels. Do not turn those into document sections.
    peers = any(other is not line and (other["bbox"][2] <= box[0] or other["bbox"][0] >= box[2])
                and min(box[3], other["bbox"][3]) - max(box[1], other["bbox"][1]) > body * .4
                for other in lines)
    if peers:
        following = [other for other in lines if 0 <= other["bbox"][1] - box[3] <= body * 5
                     and abs(other["bbox"][0] - box[0]) < 20 and len(other["text"]) > 30]
        if len(following) < 2:
            return False
    if len(words) == 1:
        return line["size"] >= body * 1.28 and len(name) >= 4
    content = [word for word in words if word.lower() not in {"a", "an", "the", "of", "and", "in", "on", "for", "to", "with"}]
    title_case = bool(content) and sum(word[0].isupper() for word in content) / len(content) >= .6
    return title_case or (len(words) <= 8 and gap >= body)


def _ocr_reference_rows(lines: list[dict], body: float) -> list[dict]:
    """Join a split reference's author and title before sorting its rows.

    Small OCR baseline jitter otherwise sorts the right-hand title before its
    bracketed label, attaching it to the previous reference. Only adjacent,
    vertically overlapping fragments qualify; separate columns stay separate.
    """
    consumed = set()
    joined = {}
    for i, line in enumerate(lines):
        if not re.match(r"^" + _REFERENCE_START, line["text"]):
            continue
        current = dict(line)
        for j, other in sorted(enumerate(lines), key=lambda item: item[1]["bbox"][0]):
            if j == i or j in consumed or re.match(r"^" + _REFERENCE_START, other["text"]):
                continue
            box, following = current["bbox"], other["bbox"]
            overlap = min(box[3], following[3]) - max(box[1], following[1])
            if (0 <= following[0] - box[2] <= body * 1.5
                    and overlap >= min(box[3] - box[1], following[3] - following[1]) * .6):
                current["text"] += " " + other["text"]
                current["bbox"] = _union([box, following])
                current["size"] = max(current["size"], other["size"])
                consumed.add(j)
        joined[i] = current
    return [joined.get(i, line) for i, line in enumerate(lines) if i not in consumed]


def _ocr_blocks(lines: list[dict], width: float) -> list[dict]:
    if not lines:
        return []
    sizes = [x["size"] for x in lines if x["size"] > 0]
    body = statistics.median(sizes) if sizes else 10
    lines = _ocr_reference_rows(lines, body)
    blocks = []
    pending: list[dict] = []
    for line in _reading_order(lines, width):
        formula = _formula_line(line)
        if formula or _ocr_heading(line, lines, body):
            if pending:
                blocks.append(_lines_block(pending, "paragraph"))
                pending = []
            blocks.append(_lines_block([line], "formula" if formula else "heading"))
            continue
        if pending:
            previous = pending[-1]
            gap = line["bbox"][1] - previous["bbox"][3]
            same_column = abs(line["bbox"][0] - previous["bbox"][0]) < 20
            if (re.match(r"^" + _REFERENCE_START, line["text"])
                    or not (same_column and -.5 <= gap < max(6, body*.9))):
                blocks.append(_lines_block(pending, "paragraph"))
                pending = []
        pending.append(line)
    if pending:
        blocks.append(_lines_block(pending, "paragraph"))
    return blocks


@_serialized
def _requires_ocr(page: fitz.Page, blocks: list[dict]) -> bool:
    if sum(len(x["text"]) for x in blocks) >= 40:
        return False
    extent = _native_rect(page)
    area = extent.get_area()
    for image in page.get_image_info():
        rect = fitz.Rect(image["bbox"]) & extent
        if rect.get_area() > area * .10:
            return True
    return False


@_serialized
def _legacy_table_blocks(page: fitz.Page, blocks: list[dict], lines: list[dict], ocr: bool) -> list[dict]:
    """Keep simple ruled tables together; preserve cell text without inventing it."""
    captions = [b for b in blocks if _TABLE_CAPTION.match(b["text"])]
    if not captions:
        return blocks
    rules = []
    if not ocr:
        rules = [drawing["rect"] for drawing in page.get_drawings()
                 if drawing["rect"].height < 2 and drawing["rect"].width > page.rect.width*.25]
    for caption in captions:
        cap_bottom = caption["bbox"][3]
        next_caption = min((b["bbox"][1] for b in captions if b["bbox"][1] > cap_bottom), default=page.rect.height)
        candidates = sorted([r for r in rules if cap_bottom <= r.y0 < next_caption], key=lambda r: r.y0)
        if candidates and candidates[0].y0 - cap_bottom < 50:
            contiguous = [candidates[0]]
            for rule in candidates[1:]:
                if rule.y0 - contiguous[-1].y0 > 110:
                    break
                contiguous.append(rule)
            if len(contiguous) < 2:
                continue
            rectangle = fitz.Rect(min(r.x0 for r in contiguous), contiguous[0].y0,
                                  max(r.x1 for r in contiguous), contiguous[-1].y0)
            selected = [x for x in lines if rectangle.contains(fitz.Point(
                        (x["bbox"][0]+x["bbox"][2])/2, (x["bbox"][1]+x["bbox"][3])/2))]
        elif ocr:
            available = sorted([x for x in lines if cap_bottom-.5 < x["bbox"][1] < next_caption],
                               key=lambda x: x["bbox"][1])
            nearby = []
            last_bottom = cap_bottom
            for line in available:
                if line["bbox"][1] - last_bottom > (30 if not nearby else 20):
                    break
                nearby.append(line)
                last_bottom = max(last_bottom, line["bbox"][3])
            # A scan has no vector rules to reveal the table's width. Require
            # repeated, separate prose before treating either side as another
            # column. The prose must share baselines with cell fragments, so
            # subsequent paragraphs cannot truncate a wide table even when
            # they start immediately below its final row.
            prose = []
            cell_row_lines = set()
            for row in _baseline_rows(nearby):
                if sum(len(line["text"]) < 25 for line in row) >= 2:
                    cell_row_lines.update(id(line) for line in row)
                    prose.extend(line for line in row
                                 if len(line["text"]) >= 25 and re.search(r"[.!?。]$", line["text"]))
            left = [line for line in prose if line["bbox"][2] < caption["bbox"][0] - 16]
            right = [line for line in prose if line["bbox"][0] > caption["bbox"][2] + 16]
            left_edge = max(line["bbox"][2] for line in left) if len(left) >= 2 else 0
            right_edge = min(line["bbox"][0] for line in right) if len(right) >= 2 else page.rect.width
            available = [line for line in nearby
                         if left_edge < (line["bbox"][0] + line["bbox"][2]) / 2 < right_edge]
            selected = []
            last_bottom = cap_bottom
            for line in available:
                gap = line["bbox"][1] - last_bottom
                if gap > (30 if not selected else 20):
                    break
                # Prose directly below a table is never treated as a cell.
                standalone_prose = (id(line) not in cell_row_lines and len(line["text"]) >= 25
                                    and re.search(r"[.!?。]$", line["text"]))
                if len(selected) > 4 and (len(line["text"]) > 100 or standalone_prose):
                    break
                selected.append(line)
                last_bottom = max(last_bottom, line["bbox"][3])
            if len(selected) < 8:
                continue
            rectangle = fitz.Rect(_union([x["bbox"] for x in selected]))
        else:
            continue
        if len(selected) < 5:
            continue
        rows: list[list[dict]] = []
        for line in sorted(selected, key=lambda x: ((x["bbox"][1]+x["bbox"][3])/2, x["bbox"][0])):
            center = (line["bbox"][1]+line["bbox"][3])/2
            if rows and abs(center - statistics.median((x["bbox"][1]+x["bbox"][3])/2 for x in rows[-1])) < 4:
                rows[-1].append(line)
            else:
                rows.append([line])
        table_text = caption["text"] + "\n" + "\n".join(
            " | ".join(x["text"] for x in sorted(row, key=lambda x: x["bbox"][0])) for row in rows)
        rectangle |= fitz.Rect(caption["bbox"])
        remaining = []
        for block in blocks:
            region = fitz.Rect(block["bbox"])
            if block is caption or ((region & rectangle).get_area() / max(1, region.get_area()) > .8):
                continue
            remaining.append(block)
        remaining.append({"text": table_text, "kind": "table", "bbox": list(rectangle),
                          "section": "", "order_index": 0})
        blocks = _reading_order(remaining, page.rect.width)
    return blocks


def _in_region(line, rectangle):
    box = line["bbox"]
    return rectangle.contains(fitz.Point((box[0] + box[2]) / 2, (box[1] + box[3]) / 2))


def _replace_table_region(blocks, lines, rectangle, text):
    """Replace covered lines, retaining prose sharing a MuPDF text block."""
    remaining = []
    for block in blocks:
        region = fitz.Rect(block["bbox"])
        if (region & rectangle).is_empty:
            remaining.append(block)
            continue
        belonging = [line for line in lines if _in_region(line, region)]
        covered = [line for line in belonging if _in_region(line, rectangle)]
        if not covered:
            remaining.append(block)
            continue
        outside = [line for line in belonging if not _in_region(line, rectangle)]
        if outside:
            remaining.append(_lines_block(outside, block["kind"]))
    remaining.append({"text": text, "kind": "table", "bbox": list(rectangle), "section": "", "order_index": 0})
    return remaining


def _baseline_rows(lines):
    rows = []
    for line in sorted(lines, key=lambda x: ((x["bbox"][1]+x["bbox"][3])/2, x["bbox"][0])):
        center = (line["bbox"][1]+line["bbox"][3])/2
        tolerance = max(3, min(5, line["size"] * .45))
        if rows and abs(center - statistics.median((x["bbox"][1]+x["bbox"][3])/2 for x in rows[-1])) < tolerance:
            rows[-1].append(line)
        else:
            rows.append([line])
    return [sorted(row, key=lambda line: line["bbox"][0]) for row in rows]


@_serialized
def _strict_tables(page, drawings):
    # find_tables normalizes rotated pages internally, but caller-supplied paths
    # are in native coordinates. Extract a copied result in native space, then
    # restore rotation before rendering or converting evidence coordinates.
    rotation = page.rotation
    try:
        if rotation:
            page.set_rotation(0)
        finder = page.find_tables(strategy="lines_strict", paths=drawings, use_layout=False)
        return [{"rows": table.row_count, "cols": table.col_count,
                 "bbox": list(table.bbox), "values": table.extract(),
                 "cells": [[list(cell) if cell else None for cell in row.cells] for row in table.rows]}
                for table in finder.tables]
    finally:
        if rotation:
            page.set_rotation(rotation)


def _table_rows(table, lines):
    """Recover aligned numeric rows inside cells without horizontal separators.

    A ruled detector can return one tall cell per column for several data rows.
    Split only when all cells contain matching numeric lines at matching heights;
    wrapped prose or uneven cell contents remain a single physical table row.
    """
    result = []
    number = re.compile(r"[+−-]?(?:\d+(?:[.,]\d+)*|\.\d+)(?:[eE][+−-]?\d+)?%?")
    compact = lambda text: re.sub(r"\s+", "", text)
    for values, cells in zip(table["values"], table["cells"]):
        output = [_clean(value or "").replace("\n", " ") for value in values]
        native_cells = []
        for i, (value, cell) in enumerate(zip(values, cells)):
            rows = _baseline_rows([line for line in lines if cell and _in_region(line, fitz.Rect(cell))])
            texts = [" ".join(line["text"] for line in row) for row in rows]
            if compact(" ".join(texts)) != compact(_clean(value or "")):
                native_cells.append([])
                continue
            # Prefer the original text's spacing, e.g. a subscript joined to P.
            output[i] = " ".join(texts)
            native_cells.append([(text, statistics.median((line["bbox"][1]+line["bbox"][3])/2 for line in row))
                                 for text, row in zip(texts, rows)])
        count = len(native_cells[0]) if native_cells else 0
        aligned_numbers = (len(native_cells) >= 2 and count >= 2
                           and all(len(cell) == count and all(number.fullmatch(text) for text, _ in cell)
                                   for cell in native_cells)
                           and all(max(cell[i][1] for cell in native_cells)-min(cell[i][1] for cell in native_cells) <= 3
                                   for i in range(count)))
        if aligned_numbers:
            result.extend([[cell[i][0] for cell in native_cells] for i in range(count)])
        else:
            result.append(output)
    return result


@_serialized
def _table_blocks(page: fitz.Page, blocks: list[dict], lines: list[dict], ocr: bool) -> list[dict]:
    if ocr:
        return _legacy_table_blocks(page, blocks, lines, ocr)
    extent = _native_rect(page)
    drawings = page.get_drawings()
    occupied = []
    # Line-only detection handles explicit cell borders. Do not use full-page
    # text detection: ordinary prose can otherwise become a giant false table.
    if drawings and len(drawings) <= 5000:
        try:
            tables = _strict_tables(page, drawings)
            for table in tables:
                rows = _table_rows(table, lines)
                filled = sum(bool(value) for row in rows for value in row)
                if table["rows"] < 2 or table["cols"] < 2 or filled < 4 or filled / (len(rows) * table["cols"]) < .45:
                    continue
                rectangle = fitz.Rect(table["bbox"])
                text = "\n".join(" | ".join(row) for row in rows)
                blocks = _replace_table_region(blocks, lines, rectangle, text)
                occupied.append(rectangle)
        except (ValueError, RuntimeError, TypeError, AttributeError):
            # A malformed drawing must not remove the existing text extraction.
            pass
    captions = [block for block in blocks if _TABLE_CAPTION.match(block["text"])]
    rules = [drawing["rect"] for drawing in drawings
             if drawing["rect"].height < 2 and drawing["rect"].width >= 40]
    groups = []
    for rule in sorted(rules, key=lambda value: (value.x0, value.x1, value.y0)):
        group = next((group for group in groups if abs(group[0].x0-rule.x0) <= 8 and abs(group[0].x1-rule.x1) <= 8), None)
        if group is None:
            groups.append([rule])
        else:
            group.append(rule)
    regions = []
    for group in groups:
        band = []
        for rule in sorted(group, key=lambda value: value.y0):
            if band and rule.y0-band[-1].y0 > 110:
                if len(band) >= 2:
                    regions.append(fitz.Rect(min(r.x0 for r in band), band[0].y0, max(r.x1 for r in band), band[-1].y0))
                band = []
            band.append(rule)
        if len(band) >= 2:
            regions.append(fitz.Rect(min(r.x0 for r in band), band[0].y0, max(r.x1 for r in band), band[-1].y0))
    for caption in captions:
        cap = fitz.Rect(caption["bbox"])
        candidates = []
        for rectangle in regions:
            overlap = max(0, min(cap.x1, rectangle.x1)-max(cap.x0, rectangle.x0))
            gap = min(abs(rectangle.y0-cap.y1), abs(cap.y0-rectangle.y1))
            if overlap < min(cap.width, rectangle.width)*.6 or gap > 50:
                continue
            if any((rectangle & previous).get_area() > rectangle.get_area()*.5 for previous in occupied):
                continue
            selected = [line for line in lines if _in_region(line, rectangle)]
            rows = _baseline_rows(selected)
            if len(rows) < 2 or sum(len(row) >= 2 for row in rows) < 2:
                continue
            # A caption near two body-text rules is insufficient table evidence.
            if sum(len(line["text"]) > 120 for line in selected) > len(selected)*.2:
                continue
            candidates.append((gap, rectangle, rows))
        if not candidates:
            continue
        _, rectangle, rows = min(candidates, key=lambda item: item[0])
        text = caption["text"] + "\n" + "\n".join(" | ".join(line["text"] for line in row) for row in rows)
        occupied.append(rectangle)
        region = rectangle | cap
        blocks = _replace_table_region(blocks, lines, region, text)
    return _reading_order(blocks, extent.width)


@_serialized
def _page_blocks(page: fitz.Page, path: Path) -> tuple[list[dict], list[dict], bool, list[str]]:
    _check_page(page)
    blocks, lines = _native_blocks(page)
    warnings: list[str] = []
    ocr = _requires_ocr(page, blocks)
    if ocr:
        lines, warnings = _ocr_page(page, path)
        blocks = _ocr_blocks(lines, page.rect.width)
    height = page.rect.height if ocr else _native_rect(page).height
    blocks = [b for b in blocks if not (b["bbox"][1] > height*.91
              and re.fullmatch(r"[\divxlcdm]+", b["text"], re.I))]
    blocks = _table_blocks(page, blocks, lines, ocr)
    for block in blocks:
        if re.match(r"^" + _REFERENCE_START, block["text"]):
            block["kind"] = "reference"
        elif _TABLE_CAPTION.match(block["text"]):
            block["kind"] = "table"
    return blocks, lines, ocr, warnings


def _metadata_title(lines: list[dict], metadata: dict, filename: str, height: float) -> str:
    embedded = _clean(metadata.get("title", "") or "")
    if len(embedded) >= 8 and embedded.lower() not in {"untitled", "microsoft word"}:
        return embedded
    candidates = [x for x in lines if 1 < len(x["text"]) < 220 and x["bbox"][1] < height*.5
                  and x["bbox"][2]-x["bbox"][0] > (x["bbox"][3]-x["bbox"][1])*2
                  and "@" not in x["text"] and not re.match(r"^(?:arxiv|https?://)", x["text"], re.I)]
    if candidates:
        largest = max(x["size"] for x in candidates)
        top = sorted([x for x in candidates if x["size"] >= largest*.90],
                     key=lambda x: (x["bbox"][1], x["bbox"][0]))
        # Authors in a separate large-font band should not enter a wrapped title.
        selected = [top[0]]
        for line in top[1:]:
            if line["bbox"][1] - selected[-1]["bbox"][3] > largest*1.6:
                break
            selected.append(line)
        return _clean(re.sub(r"(?<=\w)-\s+(?=\w)", "", " ".join(x["text"] for x in selected)))[:500]
    return Path(filename).stem


def _authors(lines: list[dict], title: str, metadata: dict, height: float) -> list[str]:
    title_words = set(re.findall(r"\w+", title.lower()))
    title_lines = [x for x in lines if len(x["text"]) >= 8 and len(set(re.findall(r"\w+", x["text"].lower())) & title_words)
                   / max(1, len(set(re.findall(r"\w+", x["text"].lower())))) > .72]
    title_size = max((x["size"] for x in title_lines if x["bbox"][1] < height*.45), default=10)
    title_end = max((x["bbox"][3] for x in title_lines if x["bbox"][1] < height*.45
                     and x["size"] >= title_size*.90), default=0)
    abstract_y = min((x["bbox"][1] for x in lines if x["text"].lower() == "abstract"), default=height*.62)
    found: list[str] = []
    corporate = []
    bad = re.compile(r"university|department|institute|laborator|school|college|research|brain|@|https?://|"
                     r"equal contribution|correspond|copyright|published|conference|journal|proceedings|"
                     r"technical report|major contributors|google|microsoft|\binc\b|\busa\b|\bchina\b", re.I)
    for line in sorted(lines, key=lambda x: (round(x["bbox"][1]/3), x["bbox"][0])):
        if not title_end <= line["bbox"][1] < abstract_y:
            continue
        text = re.sub(r"(?<=\w)[\d*†‡§¶⋄∗]+B?", ",", line["text"])
        text = re.sub(r"[\d*†‡§¶⋄∗↑↕▲◇]+", "", text).strip(" ,;()")
        if not 3 <= len(text) <= 240:
            continue
        # A wrapped, all-capitals title can be split inside a word by PDF spans
        # (for example "LAN" / "GUAGE MODELS"). Do not report its tail as an author.
        compact = re.sub(r"\W", "", text.casefold())
        if len(compact) >= 8 and text.isupper() and compact in re.sub(r"\W", "", title.casefold()):
            continue
        if text in {"OpenAI", "DeepSeek-AI", "Qwen Team", "BigScience Workshop"} or re.match(r"^Llama Team", text):
            corporate.append(text)
            continue
        if bad.search(text):
            continue
        for name in re.split(r"\s*(?:,|;|\band\b|\s{2,})\s*", text):
            name = name.strip(" ·")
            if " " not in name:
                name = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name)
            words = name.split()
            if 2 <= len(words) <= 5 and all(re.match(r"^[A-ZÀ-ÖØ-ÞŁ][\w.\-’']*$", word) for word in words):
                if name not in found:
                    found.append(name)
    if found:
        return found[:100]
    if corporate:
        return list(dict.fromkeys(corporate))
    author = _clean(metadata.get("author", "") or "")
    return [x.strip() for x in re.split(r";|\band\b", author) if x.strip()][:100]


@_serialized
def inspect_pdf(path: Path, filename: str) -> dict:
    path = Path(path)
    warnings: list[str] = []
    with _open_pdf(path) as doc:
        blocks, lines, ocr, page_warnings = _page_blocks(doc[0], path)
        warnings.extend(page_warnings)
        if ocr:
            warnings.append("封面无可用文字层，已尝试 OCR；作者、公式和数字请核对原文。")
        text = "\n".join(x["text"] for x in blocks)
        height = doc[0].rect.height if ocr else _native_rect(doc[0]).height
        title = _metadata_title(lines, doc.metadata, filename, height)
        authors = _authors(lines, title, doc.metadata, height)
        # Some reports put their abstract after a full page of contributors.
        if not re.search(r"\bAbstract\b", text, re.I) and not ocr:
            for number in range(1, min(3, doc.page_count)):
                extra, _ = _native_blocks(doc[number])
                text += "\n" + "\n".join(x["text"] for x in extra)
                if re.search(r"\bAbstract\b", text, re.I):
                    break
        abstract = ""
        match = re.search(r"(?:^|\n)Abstract\s*[:.—-]?\s*\n?(.+)", text, re.I | re.S)
        if match:
            abstract = re.split(r"\n(?:1\.?\s*Introduction|Introduction|Keywords|∗|\*|†)", match.group(1), maxsplit=1, flags=re.I)[0]
            abstract = _clean(re.sub(r"\s+", " ", abstract))[:5000]
        # Prefer the explicit cover stamp over the filename and PDF CreationDate.
        raw_cover = doc[0].get_text()
        arxiv_match = re.search(r"arXiv\s*:\s*(\d{4}\.\d{4,5})(?:v(\d+))?", raw_cover, re.I)
        if not arxiv_match:
            arxiv_match = _ARXIV.search(filename)
        if title == Path(filename).stem:
            warnings.append("未可靠识别标题，暂用文件名；可在文献详情中修改。")
        if not authors:
            warnings.append("未可靠识别作者列表，请核对封面。")
        if ocr and not blocks:
            warnings.append("扫描文件尚未完成 OCR，不能视为已解析。")
        tags = []
        if doc.page_count >= 200:
            tags.append("长文档")
        if ocr:
            tags.append("扫描件")
        if arxiv_match:
            tags.append("arXiv")
        return {"title": title, "authors": authors, "abstract": abstract,
                "arxiv_id": arxiv_match.group(1) if arxiv_match else None,
                "version": int(arxiv_match.group(2)) if arxiv_match and arxiv_match.group(2) else None,
                "page_count": doc.page_count, "tags": tags,
                "warnings": list(dict.fromkeys(warnings))}


@_serialized
def _read_page(doc, number, path, section):
    page = doc[number]
    blocks, _, ocr, warnings = _page_blocks(page, path)
    for order, block in enumerate(blocks):
        if block["kind"] == "heading":
            section = block["text"]
        elif _reference_section(section):
            block["kind"] = "reference"
        block["section"] = section
        block["order_index"] = order
    merged = []
    for block in blocks:
        if (merged and block["kind"] == "reference" and merged[-1]["kind"] == "reference"
                and re.match(r"^\[\d+\]", merged[-1]["text"])
                and not re.match(r"^\[\d+\]", block["text"])):
            merged[-1]["text"] += "\n" + block["text"]
            merged[-1]["bbox"] = _union([merged[-1]["bbox"], block["bbox"]])
        else:
            merged.append(block)
    blocks = merged
    invalid_location = False
    for order, block in enumerate(blocks):
        block["order_index"] = order
        box = block["bbox"]
        if not ocr and page.rotation and all(math.isfinite(float(value)) for value in box):
            box = list(fitz.Rect(box) * page.rotation_matrix)
        clipped = _clip_bbox(box, page.rect.width, page.rect.height)
        if clipped is None:
            # Retain searchable text even when its source geometry is
            # unusable; the warning explains the coarser page locator.
            clipped = [0.0, 0.0, page.rect.width, page.rect.height]
            invalid_location = True
        block["bbox"] = clipped
    if invalid_location:
        warnings = [*warnings, f"第 {number+1} 页有文本块坐标无效，已保留文字并使用整页定位。"]
    result = {"page": number + 1, "width": page.rect.width, "height": page.rect.height,
              "page_label": page.get_label() or str(number + 1),
              "blocks": blocks, "ocr": ocr, "warnings": warnings}
    return result, section


def iter_pages(path: Path, start_page: int = 1) -> Iterator[dict]:
    path = Path(path)
    with PYMUPDF_LOCK:
        doc = _open_pdf(path)
        count = doc.page_count
    try:
        if not isinstance(start_page, int) or not 1 <= start_page <= count + 1:
            raise ValueError("起始页码超出 PDF 范围。")
        if start_page == count + 1:
            return
        section = ""
        for number in range(start_page - 2, -1, -1):
            with PYMUPDF_LOCK:
                preceding, _, _, _ = _page_blocks(doc[number], path)
            headings = [block["text"] for block in preceding if block["kind"] == "heading"]
            if headings:
                section = headings[-1]
                break
        for number in range(start_page - 1, count):
            result, section = _read_page(doc, number, path, section)
            yield result  # Caller/SQLite/other documents may run between pages.
    finally:
        with PYMUPDF_LOCK:
            doc.close()


@_serialized
def render_page(path: Path, page: int, scale: float = 1.5) -> bytes:
    if not isinstance(page, int):
        raise ValueError("页码必须为整数。")
    if not math.isfinite(scale) or not .25 <= scale <= 4:
        raise ValueError("渲染比例必须介于 0.25 和 4 之间。")
    with _open_pdf(Path(path)) as doc:
        if not 1 <= page <= doc.page_count:
            raise ValueError("页码超出 PDF 范围。")
        selected = doc[page - 1]
        _check_page(selected)
        safe_scale = min(scale, math.sqrt(MAX_RENDER_PIXELS / (selected.rect.width * selected.rect.height)))
        return selected.get_pixmap(matrix=fitz.Matrix(safe_scale, safe_scale), alpha=False).tobytes("png")


def sections_from_blocks(blocks: list[dict]) -> list[dict]:
    sections = []
    continuing_notice = False
    frontmatter = True
    notice = re.compile(
        r"\b(?:provided proper attribution|hereby grants? permission|all rights reserved|"
        r"this work is subject to|copyright in this work|work performed while at|"
        r"equal contribution|listing order is random)\b|^copyright\s*(?:©|\(c\)|\d{4})|"
        r"^[f†‡*∗]?\s*work performed while at\b",
        re.I,
    )
    footnoted_name = re.compile(r"(?:[A-ZÀ-ÖØ-ÞŁ][\w.’'\-]*\s+){1,5}"
                                r"[A-ZÀ-ÖØ-ÞŁ][\w.’'\-]*\s*[*∗†‡↑§,]+$")
    for block in blocks:
        text = block.get("text", "").strip()
        numbered = bool(_SECTION.match(text))
        # Restrict author heuristics to the cover before the abstract/body.
        if block.get("page", 1) != 1 or text.casefold() == "abstract" or numbered:
            frontmatter = False
        if not numbered and notice.search(text):
            continuing_notice = not text.endswith((".", "!", "?"))
            continue
        if continuing_notice:
            # TeX can split a small-print legal sentence into several blocks.
            continuation = bool(text) and text[0].islower() and not numbered
            continuing_notice = continuation and not text.endswith((".", "!", "?"))
            if continuation:
                continue
        if (frontmatter and (footnoted_name.fullmatch(text)
                            or re.search(r"\b[^\s@]+@[^\s@]+\.[^\s@]+", text))):
            continue
        if block.get("kind") == "heading" and 0 < len(text) < 150:
            sections.append({"title": text, "page": block.get("page", 1),
                             "block_id": block.get("id"), "level": _heading_level(text)})
    return sections


def extract_references(blocks: list[dict]) -> list[dict]:
    references = []
    for block in blocks:
        if block.get("kind") != "reference":
            continue
        for text in re.split(r"\n(?=" + _REFERENCE_START + ")", block["text"]):
            text = _clean(text)
            if len(text) < 20:
                continue
            label = re.match(r"^\[(\d+)\]", text)
            year = re.search(r"\b((?:19|20)\d{2})[a-z]?\b", text)
            arxiv = re.search(r"arXiv\s*:\s*(\d{4}\.\d{4,5}|[a-z][a-z.\-]+/\d{7})(?:v(\d+))?", text, re.I)
            doi = re.search(r"\b10\.\d{4,9}/[^\s]+", text)
            references.append({"text": text, "label": label.group(1) if label else None,
                               "year": int(year.group(1)) if year else None,
                               "arxiv_id": arxiv.group(1) if arxiv else None,
                               "version": int(arxiv.group(2)) if arxiv and arxiv.group(2) else None,
                               "doi": doi.group(0).rstrip(".,;") if doi else None,
                               "page": block.get("page"), "block_id": block.get("id")})
    return references
