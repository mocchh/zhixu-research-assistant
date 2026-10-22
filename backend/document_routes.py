"""Document navigation and deterministic exports backed by stored evidence."""
from __future__ import annotations

import difflib
import html
import json
import re
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Literal

import pymupdf
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from . import citations as citation_matching
from . import documents, intelligence
from .tasks import page_text


def citation_metadata(paper):
    arxiv = paper.get("arxiv_id")
    version = paper.get("version")
    year = None
    if arxiv:
        match = re.match(r"(?:[a-z.-]+/)?(\d{2})\d{2}(?:\.|\d)", arxiv, re.I)
        if match:
            yy = int(match[1])
            year = (1900 if yy >= 90 else 2000) + yy
    identifier = (arxiv + (f"v{version}" if version else "")) if arxiv else None
    return {"paper_id": paper["id"], "title": paper["title"], "authors": paper.get("authors", []),
            "year": year, "arxiv_id": arxiv, "version": version, "filename": paper["filename"],
            "url": f"https://arxiv.org/abs/{identifier}" if identifier else None,
            "local_url": f"/api/papers/{paper['id']}/file"}


def _bib_escape(value):
    replacements = {"\\": r"\textbackslash{}", "{": r"\{", "}": r"\}", "&": r"\&",
                    "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}"}
    return "".join(replacements.get(c, c) for c in " ".join(str(value).split()))


def bibtex(paper):
    meta = citation_metadata(paper)
    fields = {"title": meta["title"]}
    if meta["authors"]:
        fields["author"] = " and ".join(meta["authors"])
    if meta["year"]:
        fields["year"] = meta["year"]
    if meta["arxiv_id"]:
        fields.update(archivePrefix="arXiv", eprint=meta["arxiv_id"], url=meta["url"])
    if meta["version"]:
        fields["note"] = f"Version {meta['version']}"
    key = "paper" + re.sub(r"[^A-Za-z0-9]", "", paper["id"])
    return "@misc{" + key + ",\n" + ",\n".join(f"  {k} = {{{_bib_escape(v)}}}" for k, v in fields.items()) + "\n}\n"


def _evidence(paper, block):
    return {"id": block["id"], "paper_id": paper["id"], "paper_title": paper["title"],
            "page": block["page"], "bbox": block["bbox"], "text": block["text"], "score": 1,
            "filename": paper["filename"], "arxiv_id": paper.get("arxiv_id"), "version": paper.get("version"),
            "authors": paper.get("authors", [])}


def _normal(value):
    return " ".join(unicodedata.normalize("NFKC", value).casefold().replace("-\n", "").replace("-", " ").split())


def _labels(text):
    labels = []
    for group in re.findall(r"\[([\d,;\s\-–]+)\]", text):
        for item in re.split(r"[,;]", group):
            item = item.strip()
            span = re.fullmatch(r"(\d{1,4})\s*[-–]\s*(\d{1,4})", item)
            if span:
                start, end = map(int, span.groups())
                if 0 < end - start <= 30:
                    labels.extend(str(i) for i in range(start, end + 1))
            elif re.fullmatch(r"\d{1,4}", item):
                labels.append(str(int(item)))
    return list(dict.fromkeys(labels))


def compare_documents(left, right, left_blocks, right_blocks):
    # Compare complete normalized blocks; every displayed change retains its PDF page.
    a = [" ".join(b["text"].split()) for b in left_blocks]
    b = [" ".join(b["text"].split()) for b in right_blocks]
    matcher = difflib.SequenceMatcher(None, a, b, autojunk=True)
    changes = []
    truncated = False
    for kind, i, j, k, l in matcher.get_opcodes():
        if kind == "equal":
            continue
        # A whole replaced region can be enormous; split it into reviewable blocks.
        count = max(j - i, l - k)
        for offset in range(count):
            lb = left_blocks[i + offset] if i + offset < j else None
            rb = right_blocks[k + offset] if k + offset < l else None
            before, after = (lb["text"] if lb else ""), (rb["text"] if rb else "")
            truncated |= len(before) > 8000 or len(after) > 8000
            changes.append({"kind": "replace" if lb and rb else "delete" if lb else "insert",
                            "left_page": lb["page"] if lb else None, "right_page": rb["page"] if rb else None,
                            "before": before[:8000], "after": after[:8000]})
            if len(changes) >= 500:
                truncated = True
                break
        if len(changes) >= 500:
            break
    return {"left": left, "right": right, "similarity": round(matcher.ratio(), 4), "changes": changes,
            "truncated": truncated,
            "notes": ["按解析文本块比较；相似度表示完全匹配文本块的比例。排版变化、OCR误差也会产生差异，图像或公式视觉差异需查看原页。"]}


def create_document_router(store):
    router = APIRouter(prefix="/api")

    def get_paper(paper_id, private=False):
        p = store.paper(paper_id, private=private)
        if not p:
            raise HTTPException(404, "未找到该文献")
        return p

    def refs(paper_id):
        blocks = store.blocks([paper_id])
        by_id = {b["id"]: b for b in blocks}
        return citation_matching.link_references(blocks, store.papers()), by_id

    @router.get("/papers/{paper_id}/search")
    def search(paper_id: str, q: str = Query(min_length=1, max_length=500), limit: int = Query(default=40, ge=1, le=100)):
        paper = get_paper(paper_id)
        query = _normal(q)
        if not query:
            raise HTTPException(400, "请输入要查找的原文")
        words = query.split()
        matches = []
        for block in store.blocks([paper_id]):
            normalized = _normal(block["text"])
            if query in normalized or (len(words) > 1 and all(w in normalized for w in words)):
                matches.append(_evidence(paper, block))
        return {"matches": matches[:limit], "total": len(matches), "query": q}

    @router.get("/papers/{paper_id}/references")
    def references(paper_id: str):
        get_paper(paper_id)
        return {"references": refs(paper_id)[0]}

    @router.get("/papers/{paper_id}/pages/{page}/reference-links")
    def reference_links(paper_id: str, page: int):
        paper = get_paper(paper_id)
        if not 1 <= page <= paper["page_count"]:
            raise HTTPException(404, "页码超出范围")
        references, blocks = refs(paper_id)
        by_label = {r["label"]: r for r in references if r.get("label")}
        links = []
        for block in blocks.values():
            if block["page"] != page or block["kind"] == "reference":
                continue
            for label in _labels(block["text"]):
                ref = by_label.get(label)
                if ref:
                    target = _evidence(paper, blocks[ref["block_id"]])
                    target["text"] = ref["text"]
                    links.append({"label": label, "source": _evidence(paper, block), "target": target})
        return {"links": links}

    @router.get("/papers/{paper_id}/pages/{page}/text-layer")
    def text_layer(paper_id: str, page: int):
        paper = get_paper(paper_id, private=True)
        if not 1 <= page <= paper["page_count"]:
            raise HTTPException(404, "页码超出范围")
        parsed = store.page(paper_id, page)
        with documents.PYMUPDF_LOCK, pymupdf.open(paper["path"]) as pdf:
            selected = pdf[page - 1]
            spans = []
            # A scanned body may coexist with a native footer or watermark.
            # Once OCR has parsed this page, use that complete source instead.
            if not parsed or not parsed["ocr"]:
                for word in selected.get_text("words", sort=True):
                    box = pymupdf.Rect(word[:4]) * selected.rotation_matrix
                    box &= selected.rect
                    if box.is_empty or not word[4].strip():
                        continue
                    spans.append({"text": word[4], "bbox": list(box), "font_size": round(box.height * .78, 2)})
            mode = "native"
            if not spans:
                mode = "ocr"
                for block in parsed["blocks"] if parsed else []:
                    lines = [line for line in block["text"].splitlines() if line.strip()]
                    x0, y0, x1, y1 = block["bbox"]
                    line_height = (y1 - y0) / max(1, len(lines))
                    for i, line in enumerate(lines):
                        spans.append({"text": line, "bbox": [x0, y0 + i * line_height, x1, y0 + (i + 1) * line_height], "font_size": max(1, line_height * .8)})
            return {"width": selected.rect.width, "height": selected.rect.height, "mode": mode, "spans": spans}

    @router.get("/papers/{paper_id}/compare/{other_id}")
    def compare(paper_id: str, other_id: str):
        left, right = get_paper(paper_id), get_paper(other_id)
        if left["family_id"] != right["family_id"]:
            raise HTTPException(400, "请选择同一论文的两个版本进行比较")
        if any(p["status"] != "ready" for p in (left, right)):
            raise HTTPException(409, "两个版本都完整解析后才能比较；未解析页面不能视为版本增删。")
        return compare_documents(left, right, store.blocks([paper_id]), store.blocks([other_id]))

    @router.get("/papers/{paper_id}/citation")
    def citation(paper_id: str, format: Literal["bibtex", "json"] = "bibtex"):
        paper = get_paper(paper_id)
        if format == "json":
            return citation_metadata(paper)
        return Response(bibtex(paper), media_type="application/x-bibtex", headers={"Content-Disposition": f'attachment; filename="{paper_id}.bib"'})

    @router.get("/citations/export")
    def citations(paper_ids: str = Query(min_length=1, max_length=5000)):
        ids = list(dict.fromkeys(paper_ids.split(",")))
        if len(ids) > 100:
            raise HTTPException(400, "一次最多导出100篇文献")
        return Response("\n".join(bibtex(get_paper(i)) for i in ids), media_type="application/x-bibtex", headers={"Content-Disposition": 'attachment; filename="references.bib"'})

    @router.get("/papers/{paper_id}/translations/export")
    def translations_export(paper_id: str, target_language: Literal["zh", "en"] = "zh", format: Literal["md", "html"] = "md"):
        paper = get_paper(paper_id)
        grouped = defaultdict(list)
        for block in store.blocks([paper_id]):
            grouped[block["page"]].append(block)
        exported, translated = [], 0
        for number in range(1, paper["page_count"] + 1):
            original = page_text({"blocks": grouped[number]})
            text = store.translation(paper_id, number, target_language, original) if original else None
            translated += bool(text)
            exported.append((number, text or "[本页尚无有效缓存译文]"))
        if not translated:
            raise HTTPException(409, "尚无可导出的译文，请先翻译至少一页")
        meta = citation_metadata(paper)
        title = f"{paper['title']} — {target_language}"
        provenance = f"来源：{meta['url'] or paper['filename']}；应用文献ID：{paper_id}；已译 {translated}/{paper['page_count']} 页。"
        if format == "html":
            content = '<!doctype html><html lang="' + target_language + '"><meta charset="utf-8"><title>' + html.escape(title) + '</title><style>body{max-width:900px;margin:3rem auto;padding:1rem;font:16px/1.8 system-ui}pre{white-space:pre-wrap;font:inherit}section{break-before:page}</style><h1>' + html.escape(title) + '</h1><p>' + html.escape(provenance) + '</p>' + "".join(f"<section><h2>PDF 第 {n} 页</h2><pre>{html.escape(t)}</pre></section>" for n, t in exported) + "</html>"
            media = "text/html; charset=utf-8"
        else:
            content = f"# {title}\n\n{provenance}\n\n" + "\n\n".join(f"## PDF 第 {n} 页\n\n{t}" for n, t in exported)
            media = "text/markdown; charset=utf-8"
        return Response(content, media_type=media, headers={"Content-Disposition": f'attachment; filename="{paper_id}-{target_language}.{format}"'})

    @router.get("/graph/export")
    def graph_export(format: Literal["mermaid", "dot"] = "mermaid"):
        papers = [p for p in store.papers() if p["status"] in {"ready", "partial"}]
        graph = intelligence.graph(papers, store.blocks([p["id"] for p in papers]))
        ids = {n["id"]: f"p{i}" for i, n in enumerate(graph["nodes"])}
        if format == "mermaid":
            labels = [f'  {ids[n["id"]]}["{html.escape(n["title"], quote=True).replace(chr(10), " ").replace("[", "&#91;").replace("]", "&#93;")}"]' for n in graph["nodes"]]
            edges = [f'  {ids[e["source"]]} --> {ids[e["target"]]}' for e in graph["edges"]]
            content = "flowchart LR\n" + "\n".join(labels + edges)
        else:
            labels = [f'  {ids[n["id"]]} [label={json.dumps(n["title"], ensure_ascii=False)}];' for n in graph["nodes"]]
            edges = [f'  {ids[e["source"]]} -> {ids[e["target"]]};' for e in graph["edges"]]
            content = "digraph references {\n" + "\n".join(labels + edges) + "\n}"
        return Response(content, media_type="text/plain; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="references.{"mmd" if format == "mermaid" else "dot"}"'})

    return router
