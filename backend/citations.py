"""Conservative local citation matching shared by reference lists and graphs.

Only reference entries are considered. Separate blocks are never concatenated:
an incomplete reference can remain unmatched instead of inventing a citation.
Scores identify matching rules, not calibrated probabilities.
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter, defaultdict
from difflib import SequenceMatcher


_ID = r"(?:\d{4}\.\d{4,5}|[a-z][a-z.\-]+/\d{7})"
_ARXIV = re.compile(
    r"(?:arxiv\s*:\s*|(?<![\w./])(?:https?://)?(?:www\.|export\.)?arxiv\.org/(?:abs|pdf)/\s*)"
    r"(?P<id>" + _ID + r")(?P<version>v[1-9]\d*)?(?![\w/]|\.\d)", re.I)
_BARE_ID = re.compile(r"(?P<id>" + _ID + r")(?P<version>v[1-9]\d*)?", re.I)
_REFERENCE_ID = re.compile(r"(?<![\w./])(?P<id>" + _ID + r")(?P<version>v[1-9]\d*)?(?![\w/]|\.\d)", re.I)
_DOI = re.compile(r"(?<!\w)10\.\d{4,9}/[^\s<>\"]+", re.I)
_REFERENCE_SECTION = re.compile(r"(?:\d+[.\s]*)?(?:references|bibliography|参考文献)\s*[:：]?", re.I)
# OCR can join a bracketed label to the author's name. Accept that form only
# at a line boundary; inline citations and compact numbered prose stay intact.
_ENTRY_START = re.compile(
    r"(?m)(?:^|(?<=\n))[ \t]*(?:\[\d{1,4}\](?:[ \t]+|(?=[^\W\d_]))|\d{1,3}[.)][ \t]+)"
    r"|(?<=\s)\[\d{1,4}\][ \t]+")
_LABEL = re.compile(r"^\s*(?:\[(\d{1,4})\](?:\s+|(?=[^\W\d_]))|(\d{1,3})[.)]\s+)")
_YEAR = re.compile(r"\b((?:19|20)\d{2})[a-z]?\b", re.I)


def _clean(value):
    value = unicodedata.normalize("NFKC", str(value or ""))
    value = re.sub(r"(?<=\w)[\-‐\u00ad][ \t]*\n\s*(?=\w)", "", value)
    return value.replace("\u00ad", "")


def normalize_title(value):
    """Normalize punctuation and PDF line-wrap hyphens, keeping whole words."""
    return " ".join(re.findall(r"[^\W_]+", _clean(value).casefold()))


def parse_arxiv(value):
    """Return (base_id, version) for a metadata ID, arXiv label or arXiv URL."""
    text = _clean(value).strip()
    match = _BARE_ID.fullmatch(text) or _ARXIV.search(text)
    if not match:
        return None
    identifier = match["id"].casefold()
    date = identifier.split("/")[-1][:4]
    if not 1 <= int(date[2:4]) <= 12:
        return None
    version = int(match["version"][1:]) if match["version"] else None
    return identifier, version


def _dois(value):
    result = []
    for match in _DOI.finditer(_clean(value)):
        doi = match[0].rstrip(".,;:]}>").casefold()
        while doi.endswith(")") and doi.count(")") > doi.count("("):
            doi = doi[:-1]
        if doi not in result:
            result.append(doi)
    return result


def _paper_doi(paper):
    # Never infer target DOI from an abstract, references, title or filename.
    values = _dois(paper.get("doi", ""))
    return values[0] if len(values) == 1 else None


def _version(paper):
    arxiv = parse_arxiv(paper.get("arxiv_id"))
    if arxiv and arxiv[1] is not None:
        return arxiv[1]
    value = paper.get("version")
    return int(value) if str(value or "").isdigit() and int(value) > 0 else None


def _family(paper):
    arxiv = parse_arxiv(paper.get("arxiv_id"))
    return str(paper.get("family_id") or ("arxiv:" + arxiv[0] if arxiv else paper["id"]))


def _latest(papers):
    return max(papers, key=lambda p: (_version(p) or 0, str(p.get("created_at") or ""), str(p["id"])))


def _paper_year(paper):
    year = paper.get("year")
    if str(year or "").isdigit() and 1900 <= int(year) <= 2099:
        return int(year)
    arxiv = parse_arxiv(paper.get("arxiv_id"))
    if arxiv:
        yy = int(arxiv[0].split("/")[-1][:2])
        return (1900 if "/" in arxiv[0] and yy >= 90 else 2000) + yy
    return None


def reference_entries(blocks):
    """Extract separate entries with original block coordinates and offsets.

    Recognizes reference blocks and explicitly named reference sections. A bare
    body mention cannot create an edge. Numbered entries and blank paragraphs
    are separated inside a block; wrapped lines within one entry stay together.
    """
    references = []
    for block in blocks:
        if block.get("kind") == "heading":
            continue
        if block.get("kind") != "reference" and not _REFERENCE_SECTION.fullmatch(str(block.get("section") or "").strip()):
            continue
        raw = str(block.get("text") or "")
        if _REFERENCE_SECTION.fullmatch(raw.strip()):
            continue
        starts = {0, len(raw)}
        starts.update(m.start() for m in _ENTRY_START.finditer(raw))
        starts.update(m.end() for m in re.finditer(r"\n[ \t]*\n", raw))
        boundaries = sorted(starts)
        for start, end in zip(boundaries, boundaries[1:]):
            piece = raw[start:end]
            left = len(piece) - len(piece.lstrip())
            text = piece.strip()
            if len(text) < 12 or _REFERENCE_SECTION.fullmatch(text):
                continue
            label = _LABEL.match(text)
            id_matches = list(_ARXIV.finditer(_clean(text))) + list(_REFERENCE_ID.finditer(_clean(text)))
            identifiers = list(dict.fromkeys((m["id"].casefold(), int(m["version"][1:]) if m["version"] else None) for m in id_matches))
            invalid_identifier = any(parse_arxiv(m[0]) is None for m in id_matches)
            invalid_identifier |= not id_matches and bool(re.search(r"arxiv\s*:|arxiv\.org/(?:abs|pdf)/", text, re.I))
            bases = {i[0] for i in identifiers}
            versions = {i[1] for i in identifiers if i[1] is not None}
            conflict = len(bases) > 1 or len(versions) > 1
            doi = _dois(text)
            year = _YEAR.search(text)
            references.append({"text": text, "label": str(int(label[1] or label[2])) if label else None,
                               "year": int(year[1]) if year else None,
                               "arxiv_id": next(iter(bases)) if len(bases) == 1 else None,
                               "version": next(iter(versions)) if len(versions) == 1 else None,
                               "doi": doi[0] if len(doi) == 1 else None,
                               "invalid_identifier": invalid_identifier,
                               "identifier_conflict": conflict or len(doi) > 1,
                               "page": block.get("page"), "block_id": block.get("id"),
                               "paper_id": block.get("paper_id"), "bbox": list(block.get("bbox") or []),
                               "block_text": raw, "start": start + left, "end": start + left + len(text)})
    return references


def _corroboration(reference, paper, author_prefix=None):
    text = " " + normalize_title(reference["text"] if author_prefix is None else author_prefix) + " "
    result = []
    for author in paper.get("authors") or []:
        name = author.get("name", "") if isinstance(author, dict) else str(author)
        surname = name.split(",", 1)[0] if "," in name else name.split()[-1] if name.split() else ""
        surname = normalize_title(surname)
        if len(surname) >= 3 and " " + surname + " " in text:
            result.append({"kind": "author", "value": surname})
    year = _paper_year(paper)
    if year and str(year) in _YEAR.findall(reference["text"]):
        result.append({"kind": "year", "value": year})
    return result


def _bounded_title(text, tokens, start, end):
    """Avoid matching a title embedded inside a different, longer title."""
    prefix = text[tokens[start - 1].end() if start else 0:tokens[start].start()]
    suffix = text[tokens[end - 1].end():tokens[end].start() if end < len(tokens) else len(text)]
    leading = text[:tokens[start].start()]
    label_only = not _LABEL.sub("", leading).strip()
    left = start == 0 or label_only or bool(re.search(r"[.?!;\"“”‘’)]", prefix))
    right = end == len(tokens) or bool(re.search(r"[.?!;\"“”‘’]", suffix))
    # Some formats separate venue/year/identifier with a comma instead of a dot.
    if end < len(tokens) and "," in suffix:
        right |= tokens[end][0].casefold() in {"in", "arxiv", "doi", "http", "https"} or bool(_YEAR.fullmatch(tokens[end][0]))
    return left and right


def _title_score(reference, paper, approximate=False, prepared=None, evidence=None):
    title = normalize_title(paper.get("title"))
    words = title.split()
    if len(title) < 12 or len(words) < 3:
        return 0.0
    text, tokens, terms = prepared or _prepare_text(reference["text"])
    if not approximate:
        for start in range(len(tokens) - len(words) + 1):
            end = start + len(words)
            if terms[start:end] == words and _bounded_title(text, tokens, start, end):
                # Short generic titles need additional bibliographic evidence.
                support = _corroboration(reference, paper, text[:tokens[start].start()])
                if len(title) >= 20 and len(words) >= 4 or support:
                    if evidence is not None:
                        evidence[paper["id"]] = support
                    return 0.9
        return 0.0
    if len(words) < 7 or len(title) < 45 or len(words) > 80:
        return 0.0
    corroboration = _corroboration(reference, paper)
    if not any(e["kind"] == "author" for e in corroboration):
        return 0.0
    if _paper_year(paper) and not any(e["kind"] == "year" for e in corroboration):
        return 0.0
    # An explicit limit prevents pathological reference blocks from quadratic work.
    if len(tokens) > 500:
        return 0.0
    for width in range(max(1, len(words) - 1), len(words) + 2):
        for start in range(len(tokens) - width + 1):
            end = start + width
            if not _bounded_title(text, tokens, start, end):
                continue
            candidate = " ".join(terms[start:end])
            if SequenceMatcher(None, title, candidate, autojunk=False).ratio() >= 0.94:
                support = _corroboration(reference, paper, text[:tokens[start].start()])
                if any(e["kind"] == "author" for e in support):
                    if evidence is not None:
                        evidence[paper["id"]] = support
                    return 0.8
    return 0.0


def _prepare_text(value):
    text = _clean(value)
    tokens = list(re.finditer(r"[^\W_]+", text))
    return text, tokens, [t[0].casefold() for t in tokens]


def _unmatched(rule="no_local_match", note="未匹配到本地文献。"):
    return {"match_rule": rule, "match_note": note, "confidence": 0.0, "cross_evidence": []}


def _author_keys(paper):
    result = set()
    for author in paper.get("authors") or []:
        name = author.get("name", "") if isinstance(author, dict) else str(author)
        surname = name.split(",", 1)[0] if "," in name else name.split()[-1] if name.split() else ""
        if value := normalize_title(surname):
            result.add(value)
    return result


def _conflicting_unidentified_authors(papers):
    unidentified = [p for p in papers if not parse_arxiv(p.get("arxiv_id")) and not _paper_doi(p)]
    names = [_author_keys(p) for p in unidentified]
    return any(left and right and left.isdisjoint(right)
               for index, left in enumerate(names) for right in names[index + 1:])


def _choose(reference, candidates, rule, confidence, note, evidence=None):
    evidence = evidence or {}
    families = defaultdict(list)
    for candidate in candidates:
        families[_family(candidate)].append(candidate)
    if len(families) > 1:
        # Full-title collisions can be resolved only by unique author evidence.
        supported = {family: values for family, values in families.items()
                     if any(any(e["kind"] == "author" for e in evidence.get(p["id"], []))
                            and (not _paper_year(p) or any(e["kind"] == "year" for e in evidence.get(p["id"], [])))
                            for p in values)}
        if len(supported) != 1 or rule.startswith(("arxiv", "doi")):
            return _unmatched("ambiguous_local_match", "多个本地文献符合此引用，交叉证据不足，不自动连接。")
        families = supported
    values = next(iter(families.values()))
    # Storage can group unknown-ID papers by title alone. That grouping is not
    # bibliographic proof when names identify different works with the same title.
    if rule.startswith("title") and _conflicting_unidentified_authors(values):
        supported = [p for p in values
                     if any(e["kind"] == "author" for e in evidence.get(p["id"], []))
                     and (not _paper_year(p) or any(e["kind"] == "year" for e in evidence.get(p["id"], [])))]
        if not supported or _conflicting_unidentified_authors(supported):
            return _unmatched("ambiguous_local_match", "同标题族内存在作者冲突且缺少稳定标识符，证据不足，不自动连接。")
        values = supported
    selected = _latest(values)
    return {"linked_paper_id": selected["id"], "linked_version": _version(selected),
            "linked_family_id": _family(selected), "match_rule": rule, "match_note": note,
            "confidence": confidence, "cross_evidence": evidence.get(selected["id"], [])}


def match_reference(reference, papers):
    """Resolve one parsed entry; explicit arXiv versions never fall back."""
    papers = [p for p in papers if p.get("id")]
    if reference.get("identifier_conflict"):
        return _unmatched("conflicting_identifiers", "一个引用条目含多个不一致的标识符，需核对原文。")
    if reference.get("invalid_identifier"):
        return _unmatched("invalid_arxiv_identifier", "原文含无法确认的 arXiv 标识符，不回退为标题连接。")
    arxiv = reference.get("arxiv_id")
    if arxiv:
        candidates = [p for p in papers if (parse_arxiv(p.get("arxiv_id")) or (None,))[0] == arxiv]
        if reference.get("doi") and candidates:
            consistent = [p for p in candidates if _paper_doi(p) in (None, reference["doi"])]
            if not consistent:
                return _unmatched("conflicting_identifiers", "原文 arXiv ID 与 DOI 对应的本地元数据不一致，需核对。")
            candidates = consistent
        version = reference.get("version")
        if version is not None:
            candidates = [p for p in candidates if _version(p) == version
                          and (not p.get("version") or int(p["version"]) == version)]
            if not candidates:
                return _unmatched("specified_version_not_found", f"原文指定的 arXiv v{version} 未入库，不使用其他版本替代。")
            return _choose(reference, candidates, "arxiv_id_and_version_exact", 1.0, f"按原文 arXiv ID 与 v{version} 精确匹配。")
        if not candidates:
            return _unmatched("arxiv_id_not_found", "该 arXiv ID 尚未入库。")
        return _choose(reference, candidates, "arxiv_id_latest_local_version", 1.0,
                       "原文未指定版本，按 arXiv ID 选择已入库的最新版本；可能不同于作者当时引用的版本。")
    doi = reference.get("doi")
    if doi:
        candidates = [p for p in papers if _paper_doi(p) == doi]
        if candidates:
            return _choose(reference, candidates, "doi_exact", 1.0, "按原文 DOI 与本地真实 DOI 元数据精确匹配。")
        # Absent DOI metadata does not prove a mismatch, but conflicting DOI does.
        papers = [p for p in papers if _paper_doi(p) in (None, doi)]
    prepared = _prepare_text(reference["text"])
    evidence = {}
    exact = [p for p in papers if _title_score(reference, p, prepared=prepared, evidence=evidence)]
    if exact:
        return _choose(reference, exact, "title_latest_local_version", 0.9,
                       "按完整标题匹配本地文献；原文未指定版本，选择本地最新版本，需核对。", evidence)
    approximate = [p for p in papers if _title_score(reference, p, approximate=True, prepared=prepared, evidence=evidence)]
    if approximate:
        return _choose(reference, approximate, "title_approximate_with_bibliographic_evidence", 0.8,
                       "长标题近似匹配，并核对作者及已知年份；属于推断连接，请核对原引用。", evidence)
    return _unmatched()


def link_references(blocks, papers):
    """Return the reference-list API's entries using the graph's same rules."""
    return [{**ref, **match_reference(ref, papers)} for ref in reference_entries(blocks)]


def citation_graph(papers, blocks):
    """Build a family-deduplicated graph with evidence on every accepted edge."""
    papers = [p for p in papers if p.get("id")]
    by_id = {p["id"]: p for p in papers}
    stored_families = defaultdict(list)
    for paper in papers:
        stored_families[_family(paper)].append(paper)
    family_keys = {p["id"]: _family(p) for p in papers}
    for family, values in stored_families.items():
        if _conflicting_unidentified_authors(values):
            for paper in values:
                if not parse_arxiv(paper.get("arxiv_id")) and not _paper_doi(paper):
                    # In a conflicted title-only family, merge only identical
                    # known author sets. Unknown authors keep an individual node.
                    authors = tuple(sorted(_author_keys(paper)))
                    family_keys[paper["id"]] = (family, "authors", authors) if authors else (family, "paper", paper["id"])
    families = defaultdict(list)
    for paper in papers:
        families[family_keys[paper["id"]]].append(paper)
    representatives = {family: _latest(values) for family, values in families.items()}
    edges = {}
    for reference in link_references(blocks, papers):
        source = by_id.get(reference.get("paper_id"))
        target = by_id.get(reference.get("linked_paper_id"))
        if not source or not target or family_keys[source["id"]] == family_keys[target["id"]]:
            continue
        source_id = representatives[family_keys[source["id"]]]["id"]
        target_id = representatives[family_keys[target["id"]]]["id"]
        edge = edges.setdefault((source_id, target_id), {"source": source_id, "target": target_id, "confidence": 0.0, "evidence": []})
        edge["confidence"] = max(edge["confidence"], reference["confidence"])
        evidence = {**reference, "source_paper_id": source["id"], "source_version": _version(source)}
        if evidence not in edge["evidence"]:
            edge["evidence"].append(evidence)
    indegrees = Counter(edge["target"] for edge in edges.values())
    nodes = []
    for paper in representatives.values():
        node = {"id": paper["id"], "title": paper.get("title") or "未命名文献", "citations": indegrees[paper["id"]]}
        if year := _paper_year(paper):
            node["year"] = year
        nodes.append(node)
    return {"nodes": nodes, "edges": sorted(edges.values(), key=lambda e: (e["source"], e["target"])),
            "scope": "仅当前入库文献的局部引用图；同族版本合并显示，缺少稳定ID且作者冲突的标题族保守拆分，原引用版本保留在每条边的 evidence 中。仅参考文献条目参与匹配，arXiv ID/版本或真实 DOI 精确匹配为 1.0、完整标题为 0.9、长标题加作者/年份的近似匹配为 0.8；评分不是统计概率。同名歧义、缺失指定版本不自动连接。citations 为局部入度，缺边不代表不存在引用。"}
