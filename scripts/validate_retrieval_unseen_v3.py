#!/usr/bin/env python3
"""只读核对 v3 原文、表格行序和冻结摘要；不导入或运行检索实现。"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path
import re
import sys
import unicodedata

import pymupdf

ROOT = Path(__file__).resolve().parents[1]


def normalized(value):
    value = unicodedata.normalize("NFKC", value).lower()
    value = re.sub(r"(?<=[a-z])-\s*\n\s*(?=[a-z])", "", value)
    return re.sub(r"\s+", " ", value).strip()


def leaves(target):
    if "all_of" in target or "alternatives" in target:
        for child in target.get("all_of", target.get("alternatives", [])):
            yield from leaves(child)
    else:
        yield target


def validate(path, freeze_path=None, preview_path=None):
    fixture = json.loads(path.read_text())
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if fixture.get("match_protocol") != "table_cells_v2":
        raise ValueError("v3 必须显式使用 table_cells_v2 协议")
    if freeze_path:
        freeze = json.loads(freeze_path.read_text())
        if freeze["fixture_sha256"] != digest or freeze["match_protocol"] != "table_cells_v2":
            raise ValueError("题集或协议不符合冻结记录")
        for key, source in (("parser_sha256", ROOT / "backend/documents.py"),
                            ("evaluator_sha256", ROOT / "scripts/evaluate_retrieval.py")):
            if hashlib.sha256(source.read_bytes()).hexdigest() != freeze[key]:
                raise ValueError(f"{key} 已改变；不能声称是同一受控实验")
    used = set()
    old_ids = set()
    for name in ("retrieval_cases.json", "retrieval_holdout.json", "retrieval_unseen_v2.json"):
        old = json.loads((ROOT / "tests/fixtures" / name).read_text())
        used.update(d["filename"] for d in old["documents"])
        old_ids.update(c["id"] for c in old["cases"])
    names = {d["filename"] for d in fixture["documents"]}
    if names & used or len(names) != len(fixture["documents"]):
        raise ValueError("文献重复或与此前三组题集重叠")
    native = {}
    total_pages = 0
    for document in fixture["documents"]:
        pdf_path = ROOT / "pdf" / document["filename"]
        if hashlib.sha256(pdf_path.read_bytes()).hexdigest() != document["sha256"]:
            raise ValueError(f"原 PDF 改变：{document['filename']}")
        with pymupdf.open(pdf_path) as pdf:
            native[document["filename"]] = [normalized(page.get_text()) for page in pdf]
            total_pages += len(pdf)
    ids, count, table_count = set(), 0, 0
    for case in fixture["cases"]:
        if case["id"] in ids | old_ids:
            raise ValueError(f"题目 ID 重复：{case['id']}")
        ids.add(case["id"])
        if not set(case["filenames"]).issubset(names):
            raise ValueError("文献范围超出冻结 PDF 列表")
        if not case["expected"] and not case.get("expect_no_sources"):
            raise ValueError("空来源题缺少拒答声明")
        for target in case["expected"]:
            for leaf in leaves(target):
                filename, page = leaf["filename"], leaf["page"]
                if filename not in case["filenames"] or not 1 <= page <= len(native[filename]):
                    raise ValueError(f"非法原文位置：{case['id']}")
                source = native[filename][page - 1]
                terms = list(leaf.get("terms", []))
                if "table_row" in leaf:
                    row = leaf["table_row"]
                    if not row["cells"] or not isinstance(row["start_column"], int) or row["start_column"] < 0:
                        raise ValueError("表格行必须含单元格和非负起列")
                    # A contiguous native-text row check supplements the recorded
                    # visual inspection; it does not infer table semantics.
                    terms.append(" ".join(row["cells"]))
                    table_count += 1
                absent = [term for term in terms if normalized(term) not in source]
                if absent:
                    raise ValueError(f"原 PDF 缺少摘录：{case['id']} p{page}: {absent}")
                count += 1
    report = {"ok": True, "fixture_sha256": digest, "documents": len(names), "pages": total_pages,
              "cases": len(ids), "gold_targets": sum(len(c["expected"]) for c in fixture["cases"]),
              "source_leaves_verified": count, "table_rows_verified": table_count,
              "previous_fixture_document_overlap": [], "match_protocol": "table_cells_v2",
              "review_kind": "AI-assisted original-PDF verification, not human blind testing"}
    if preview_path:
        # Compile only the evaluator's pure matching functions. In particular,
        # never import its intelligence module or run a retrieval query here.
        tree = ast.parse((ROOT / "scripts/evaluate_retrieval.py").read_text())
        selected = [n for n in tree.body if isinstance(n, ast.FunctionDef)
                    and n.name in {"normalized", "table_rows", "matches", "target_matches"}]
        scope = {"re": re, "unicodedata": unicodedata}
        exec(compile(ast.Module(body=selected, type_ignores=[]), "<evaluation matching only>", "exec"), scope)
        preview = json.loads(preview_path.read_text())
        blocks = [{**block, "filename": filename, "page": int(page)}
                  for key, value in preview.items() for filename, page in [key.rsplit(":", 1)]
                  for block in value["blocks"]]
        availability = [scope["target_matches"](blocks, target, "table_cells_v2")
                        for case in fixture["cases"] for target in case["expected"]]
        report["parsed_targets_available_without_retrieval"] = sum(availability)
        report["parsed_target_count"] = len(availability)
        if not all(availability):
            raise ValueError("部分金标在当前解析预览中不可用；需记录解析限制，不得根据检索结果修改金标")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=ROOT / "tests/fixtures/retrieval_unseen_v3.json")
    parser.add_argument("--freeze-file", type=Path)
    parser.add_argument("--parsed-preview", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        result = validate(args.fixture, args.freeze_file, args.parsed_preview)
    except (ValueError, OSError, KeyError) as error:
        print(str(error), file=sys.stderr)
        return 1
    content = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        if args.output.resolve() in {p.resolve() for p in (args.fixture, args.freeze_file, args.parsed_preview) if p}:
            parser.error("校验报告不能覆盖输入")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x") as stream:
            stream.write(content)
    print(content, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
