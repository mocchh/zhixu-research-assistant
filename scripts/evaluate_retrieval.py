#!/usr/bin/env python3
"""Offline PDF retrieval acceptance suite; never writes the application database.

Run: .venv/bin/python scripts/evaluate_retrieval.py --output /tmp/retrieval-report.json
Parsing cache lives in the OS temporary directory and is invalidated by PDF and
parser hashes. --database optionally reuses a read-only parsed database snapshot.
The default fixture is a development acceptance set. --fixture selects another
source-target set; --freeze-file verifies its pre-run fixture and retriever hashes.
Neither score measures model answer quality or semantic entailment.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import statistics
import sys
import tempfile
import time
import unicodedata

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend import documents, intelligence  # noqa: E402


def normalized(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).lower()
    value = re.sub(r"(?<=[a-z])-\s*\n\s*(?=[a-z])", "", value)
    return re.sub(r"\s+", " ", value).strip()


MATCH_PROTOCOLS = ("strict_v1", "table_cells_v2")


def table_rows(text: str) -> list[list[str]]:
    """Only explicit parser cell separators, never guessed column positions."""
    return [[normalized(cell) for cell in re.split(r"\s+\|\s+", line)]
            for line in text.splitlines() if re.search(r"\s+\|\s+", line)]


def matches(source: dict, gold: dict, protocol: str = "strict_v1") -> bool:
    if "alternatives" in gold:
        return any(matches(source, candidate, protocol) for candidate in gold["alternatives"])
    if source.get("filename") != gold["filename"] or source["page"] != gold["page"]:
        return False
    table = protocol == "table_cells_v2" and source.get("kind") == "table"
    rows = table_rows(source["text"]) if table else []
    if "table_row" in gold:
        expected = gold["table_row"]
        cells = [normalized(cell) for cell in expected["cells"]]
        column = expected["start_column"]
        # Preserve row membership, cell boundaries, order and column offset.
        if not rows or not cells or not isinstance(column, int) or column < 0:
            return False
        if not any(row[column:column + len(cells)] == cells for row in rows):
            return False
    terms = gold.get("terms", [])
    if not terms and "table_row" not in gold:
        return False
    for term in terms:
        needle = normalized(term)
        if table and len(re.findall(r"(?<![\w.])\d+(?:\.\d+)?", needle)) >= 2:
            # A numeric sequence cannot be assembled across different rows.
            if not any(re.search(r"(?<![\w.])" + re.escape(needle) + r"(?![\w.])", normalized(re.sub(r"\s+\|\s+", " ", line)))
                       for line in source["text"].splitlines()):
                return False
        elif needle not in normalized(source["text"]):
            return False
    return True


def target_matches(sources: list[dict], target: dict, protocol: str = "strict_v1") -> bool:
    if "alternatives" in target:
        return any(target_matches(sources, candidate, protocol) for candidate in target["alternatives"])
    if "all_of" in target:
        return all(target_matches(sources, part, protocol) for part in target["all_of"])
    return any(matches(source, target, protocol) for source in sources)


def verify_freeze(fixture_path: Path, freeze_path: Path) -> dict:
    """Reject a changed fixture or retriever before evaluating a frozen round."""
    frozen = json.loads(freeze_path.read_text())
    actual = {
        "fixture_sha256": hashlib.sha256(fixture_path.read_bytes()).hexdigest(),
        "retriever_sha256": hashlib.sha256(Path(intelligence.__file__).read_bytes()).hexdigest(),
    }
    for name, path in (("evaluator_sha256", Path(__file__)), ("parser_sha256", Path(documents.__file__))):
        if name in frozen:
            actual[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    for key, value in actual.items():
        if frozen.get(key) != value:
            raise ValueError(f"Frozen {key} mismatch; this is not the frozen evaluation round")
    return {**actual, "frozen_at_utc": frozen.get("frozen_at_utc"), "verified": True}


def compare_baseline(result: dict, baseline: dict) -> dict:
    """Permit known misses, but never turn a new correctness regression green.

    The baseline pins the source targets, not the retriever implementation. A
    future implementation may improve retrieval without rewriting first-run data.
    """
    regressions = []
    digest = result.get("fixture_sha256")
    if not digest or digest != baseline.get("fixture_sha256"):
        regressions.append("fixture_sha256 differs from the baseline")
    if result.get("k") != baseline.get("k"):
        regressions.append("retrieval k differs from the baseline")
    if result.get("match_protocol", "strict_v1") != baseline.get("match_protocol", "strict_v1"):
        regressions.append("match_protocol differs from the baseline; compare the same scoring protocol")
    current = {case["id"]: case for case in result["cases"]}
    previous = {case["id"]: case for case in baseline["cases"]}
    if (set(current) != set(previous) or len(current) != len(result["cases"])
            or len(previous) != len(baseline["cases"])):
        regressions.append("case IDs differ or are duplicated")
    if result.get("gold_available") != result.get("gold_targets"):
        regressions.append("some gold targets are unavailable in the parsed corpus")
    if result.get("locator_integrity") != 1.0:
        regressions.append("locator integrity must be 100%")
    if result.get("version_or_scope_leaks") != 0:
        regressions.append("version or scope leakage detected")
    for identifier, case in current.items():
        if not all(case["gold_available"]):
            regressions.append(f"{identifier}: gold target unavailable")
        if case["version_or_scope_leaks"]:
            regressions.append(f"{identifier}: version or scope leakage")
        old = previous.get(identifier)
        if old is None:
            continue
        before, after = old["gold_retrieved"], case["gold_retrieved"]
        if len(before) != len(after):
            regressions.append(f"{identifier}: gold target count changed")
        elif any(was_found and not is_found for was_found, is_found in zip(before, after)):
            regressions.append(f"{identifier}: previously retrieved gold target lost")
        if old["passed"] and not case["passed"]:
            regressions.append(f"{identifier}: previously passing case failed")
        if old["missing_source_behavior_passed"] is True and case["missing_source_behavior_passed"] is not True:
            regressions.append(f"{identifier}: missing-source refusal regressed")
    return {
        "regression_check": "failed" if regressions else "passed",
        "regressions": regressions,
        "known_failed_cases": sum(not case["passed"] and identifier in previous and not previous[identifier]["passed"] for identifier, case in current.items()),
        "baseline_failed_cases": [identifier for identifier, case in previous.items() if not case["passed"]],
        "retriever_changed_since_baseline": result.get("retriever_sha256") != baseline.get("retriever_sha256"),
        "regression_check_meaning": "Passed means no regression from the frozen first run; it does not mean every retrieval case passed.",
    }


def load_documents(fixture: dict, database: Path | None, cache: Path) -> tuple[list, list]:
    papers, blocks = [], []
    parser_sha = hashlib.sha256(Path(documents.__file__).read_bytes()).hexdigest()
    db = sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True) if database else None
    if db:
        db.row_factory = sqlite3.Row
    try:
        for doc in fixture["documents"]:
            path = ROOT / "pdf" / doc["filename"]
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != doc["sha256"]:
                raise ValueError(f"PDF changed: {doc['filename']}; review gold before updating its hash")
            if db:
                row = db.execute("SELECT * FROM papers WHERE filename=? AND sha256=? ORDER BY created_at DESC LIMIT 1", (doc["filename"], digest)).fetchone()
                if not row:
                    raise ValueError(f"No matching parsed PDF in database: {doc['filename']}")
                paper = dict(row)
                paper["authors"] = json.loads(paper.get("authors") or "[]")
                rows = [dict(row) for row in db.execute("SELECT * FROM blocks WHERE paper_id=? ORDER BY page,order_index", (paper["id"],))]
                for block in rows:
                    block["bbox"] = json.loads(block["bbox"])
            else:
                cache.mkdir(parents=True, exist_ok=True)
                identity = hashlib.sha256((digest + parser_sha + json.dumps(documents.get_ocr_status(), sort_keys=True)).encode()).hexdigest()
                entry = cache / f"{identity}.json"
                if entry.is_file():
                    data = json.loads(entry.read_text())
                    paper, rows = data["paper"], data["blocks"]
                else:
                    print(f"Parsing {doc['filename']}…", file=sys.stderr, flush=True)
                    meta = documents.inspect_pdf(path, doc["filename"])
                    paper = {**meta, "id": doc["filename"], "filename": doc["filename"], "status": "ready", "ocr_pages": 0}
                    paper["family_id"] = intelligence._arxiv_family(meta.get("arxiv_id")) or doc["filename"]
                    rows = []
                    for page in documents.iter_pages(path):
                        paper["ocr_pages"] += int(page.get("ocr", False))
                        for index, block in enumerate(page["blocks"]):
                            rows.append({**block, "id": f"{doc['filename']}:{page['page']}:{index}", "paper_id": paper["id"], "page": page["page"]})
                    paper["processed_pages"] = meta["page_count"]
                    entry.write_text(json.dumps({"paper": paper, "blocks": rows}, ensure_ascii=False))
            papers.append(paper)
            blocks.extend(rows)
    finally:
        if db:
            db.close()
    return papers, blocks


async def evaluate(fixture: dict, papers: list[dict], blocks: list[dict], protocol: str = "strict_v1") -> dict:
    if protocol not in MATCH_PROTOCOLS:
        raise ValueError("Unknown matching protocol")
    cases, total_gold, hits, located, returned, leaks = [], 0, 0, 0, 0, 0
    lookup = {block["id"]: block for block in blocks}
    metadata = {paper["id"]: paper for paper in papers}
    full_sources = [{**block, "filename": metadata[block["paper_id"]]["filename"]} for block in blocks]
    durations = []
    for case in fixture["cases"]:
        scoped = [p for p in papers if p["filename"] in case["filenames"]]
        started = time.perf_counter()
        response = await intelligence.answer_question(case["question"], scoped, blocks, case.get("history", []), {})
        durations.append((time.perf_counter() - started) * 1000)
        sources = [{**s, "kind": lookup.get(s["id"], {}).get("kind")} for s in response["sources"][:fixture["k"]]]
        # Gold availability is reported separately from retrieval failure. A PDF
        # parser/OCR change cannot silently turn a missing gold into a passing case.
        available = [target_matches(full_sources, gold, protocol) for gold in case["expected"]]
        found = [target_matches(sources, gold, protocol) for gold in case["expected"]]
        total_gold += len(found)
        hits += sum(found)
        allowed = case.get("allowed_filenames", case["filenames"])
        escaped = [s["filename"] for s in sources if s["filename"] not in allowed]
        leaks += len(escaped)
        for source in sources:
            original = lookup.get(source["id"], {})
            box = source.get("bbox")
            located += int(source["paper_id"] == original.get("paper_id") and source["page"] == original.get("page") and source["text"] == original.get("text") and box == original.get("bbox") and isinstance(box, list) and len(box) == 4 and box[2] > box[0] and box[3] > box[1])
        returned += len(sources)
        absent_ok = not sources and bool(response["warnings"]) if case.get("expect_no_sources") else None
        passed = bool(absent_ok) if absent_ok is not None else bool(found) and all(found) and all(available) and not escaped
        cases.append({"id": case["id"], "category": case["category"], "passed": passed, "gold_available": available, "gold_retrieved": found, "missing_source_behavior_passed": absent_ok, "version_or_scope_leaks": escaped,
                      "returned": [{"filename": s["filename"], "page": s["page"], "id": s["id"], "score": s["score"]} for s in sources], "warnings": response["warnings"]})
    largest = max(papers, key=lambda p: sum(b["paper_id"] == p["id"] for b in blocks))
    intelligence._retrieval_index.cache_clear()
    started = time.perf_counter()
    intelligence.retrieve("Adam momentum gradient RMSProp", [largest], blocks)
    cold = (time.perf_counter() - started) * 1000
    warm = []
    for _ in range(5):
        started = time.perf_counter()
        intelligence.retrieve("Adam momentum gradient RMSProp", [largest], blocks)
        warm.append((time.perf_counter() - started) * 1000)
    missing = [c for c in cases if c["missing_source_behavior_passed"] is not None]
    categories = {}
    for category in sorted({c["category"] for c in cases}):
        group = [c for c in cases if c["category"] == category]
        categories[category] = {"cases": len(group), "passed": sum(c["passed"] for c in group)}
    return {"method": "BM25 with deterministic lexical/layout reranking; no embedding or model calls", "match_protocol": protocol, "fixture_kind": fixture.get("fixture_kind", "development_acceptance_not_held_out"), "categories": categories,
            "k": fixture["k"], "case_count": len(cases), "passed_cases": sum(c["passed"] for c in cases), "recall_at_k": hits / total_gold if total_gold else 0,
            "gold_targets": total_gold, "gold_hits": hits, "gold_available": sum(sum(c["gold_available"]) for c in cases), "locator_integrity": located / returned if returned else 0,
            "returned_sources": returned, "version_or_scope_leaks": leaks, "missing_source_passes": sum(c["missing_source_behavior_passed"] for c in missing), "missing_source_cases": len(missing),
            "median_query_ms_including_index_builds": round(statistics.median(durations), 2), "cache_measurement": {"document": largest["filename"], "blocks": sum(b["paper_id"] == largest["id"] for b in blocks), "cold_ms": round(cold, 2), "warm_median_ms": round(statistics.median(warm), 2), "cache_info": intelligence.retrieval_cache_info()},
            "limits": ["Gold targets are specific page-and-snippet matches; correct unlisted passages may still be misses.", "Locator integrity means original block geometry was retained, not that OCR or formula recognition is correct.", "No model answer correctness or semantic entailment is measured."], "cases": cases}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=ROOT / "tests/fixtures/retrieval_cases.json")
    parser.add_argument("--database", type=Path, help="Optional read-only reuse of already parsed matching PDFs")
    parser.add_argument("--cache-dir", type=Path, default=Path(tempfile.gettempdir()) / "research-retrieval-eval")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--freeze-file", type=Path, help="Verify the fixture and retriever hashes recorded before the first run")
    parser.add_argument("--baseline", type=Path, help="Fail only on regressions from a same-fixture first-run report; known misses remain in the report")
    parser.add_argument("--match-protocol", choices=MATCH_PROTOCOLS, default="strict_v1", help="Explicit scoring version; v2 handles table cell separators while preserving rows and columns")
    parser.add_argument("--run-label", help="Informational label recorded in the result, e.g. after_version_name_fix")
    args = parser.parse_args()
    try:
        protected_inputs = [args.fixture, args.freeze_file, args.baseline]
        if args.output and any(path and args.output.resolve() == path.resolve() for path in protected_inputs):
            raise ValueError("Output must not overwrite a fixture, freeze manifest, or baseline report")
        fixture = json.loads(args.fixture.read_text())
        freeze = verify_freeze(args.fixture, args.freeze_file) if args.freeze_file else None
        if args.freeze_file:
            frozen_protocol = json.loads(args.freeze_file.read_text()).get("match_protocol", "strict_v1")
            if frozen_protocol != args.match_protocol:
                raise ValueError("Frozen match_protocol mismatch; refusing to evaluate")
        baseline = json.loads(args.baseline.read_text()) if args.baseline else None
        papers, blocks = load_documents(fixture, args.database, args.cache_dir)
    except (OSError, ValueError, sqlite3.Error) as error:
        print(json.dumps({"status": "not_evaluated", "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2
    result = asyncio.run(evaluate(fixture, papers, blocks, args.match_protocol))
    result["parse_source"] = "read_only_database" if args.database else "pdf_parser_with_hash_cache"
    result["fixture_sha256"] = hashlib.sha256(args.fixture.read_bytes()).hexdigest()
    result["retriever_sha256"] = hashlib.sha256(Path(intelligence.__file__).read_bytes()).hexdigest()
    result["parser_sha256"] = hashlib.sha256(Path(documents.__file__).read_bytes()).hexdigest()
    result["evaluator_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    result["evaluated_at_utc"] = datetime.now(timezone.utc).isoformat()
    result["freeze_verification"] = freeze
    if args.run_label:
        result["run_label"] = args.run_label
    if baseline is not None:
        result.update(compare_baseline(result, baseline))
        result["limits"].append("This run reuses known baseline cases as a regression check; it is not a fresh holdout evaluation.")
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({key: value for key, value in result.items() if key != "cases"}, ensure_ascii=False, indent=2))
    for case in result["cases"]:
        if not case["passed"]:
            known = baseline is not None and case["id"] in result["baseline_failed_cases"]
            label = "KNOWN MISS" if known else "FAIL"
            print(f"{label} {case['id']}: available={case['gold_available']} found={case['gold_retrieved']}", file=sys.stderr)
    if baseline is not None:
        return 0 if result["regression_check"] == "passed" else 1
    return 0 if result["passed_cases"] == result["case_count"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
