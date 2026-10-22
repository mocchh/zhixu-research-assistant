#!/usr/bin/env python3
"""核对新题的原 PDF 摘录与冻结摘要；此命令不运行检索。"""
from __future__ import annotations

import argparse
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


def validate(fixture_path, freeze_path=None):
    fixture = json.loads(fixture_path.read_text())
    sha = hashlib.sha256(fixture_path.read_bytes()).hexdigest()
    if freeze_path:
        freeze = json.loads(freeze_path.read_text())
        if freeze["fixture_sha256"] != sha:
            raise ValueError("新题集与冻结 SHA256 不一致")
    old_documents = set()
    for name in ("retrieval_cases.json", "retrieval_holdout.json"):
        old = json.loads((ROOT / "tests/fixtures" / name).read_text())
        old_documents.update(document["filename"] for document in old["documents"])
    names = {document["filename"] for document in fixture["documents"]}
    if names & old_documents:
        raise ValueError("新题文献与旧题集存在重叠")
    if len(names) != len(fixture["documents"]):
        raise ValueError("文献记录重复")
    native, page_counts = {}, {}
    for document in fixture["documents"]:
        path = ROOT / "pdf" / document["filename"]
        if hashlib.sha256(path.read_bytes()).hexdigest() != document["sha256"]:
            raise ValueError(f"原 PDF 摘要变化：{document['filename']}")
        with pymupdf.open(path) as pdf:
            native[document["filename"]] = [normalized(page.get_text()) for page in pdf]
            page_counts[document["filename"]] = len(pdf)
    identifiers, checked = set(), 0
    for case in fixture["cases"]:
        if case["id"] in identifiers:
            raise ValueError("题目 ID 重复")
        identifiers.add(case["id"])
        if not set(case["filenames"]).issubset(names):
            raise ValueError(f"范围包含未冻结文献：{case['id']}")
        if not case["expected"] and not case.get("expect_no_sources"):
            raise ValueError(f"没有来源目标且未声明缺资料：{case['id']}")
        for target in case["expected"]:
            for leaf in leaves(target):
                filename, page = leaf["filename"], leaf["page"]
                if filename not in case["filenames"] or not 1 <= page <= page_counts[filename]:
                    raise ValueError(f"原文页或范围无效：{case['id']}")
                text = native[filename][page-1]
                missing = [term for term in leaf["terms"] if normalized(term) not in text]
                if missing:
                    raise ValueError(f"原 PDF 未找到摘录：{case['id']} {filename} p{page}: {missing}")
                checked += 1
    return {"ok": True, "fixture_sha256": sha, "documents": len(names), "cases": len(identifiers),
            "gold_targets": sum(len(case["expected"]) for case in fixture["cases"]),
            "source_leaves_verified": checked, "old_fixture_document_overlap": [],
            "review_kind": "AI-assisted original-PDF text verification plus separately recorded visual checks; not human blind testing"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=ROOT / "tests/fixtures/retrieval_unseen_v2.json")
    parser.add_argument("--freeze-file", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        report = validate(args.fixture, args.freeze_file)
    except (ValueError, OSError, KeyError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    content = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        if args.output.resolve() in {args.fixture.resolve(), args.freeze_file.resolve() if args.freeze_file else None}:
            parser.error("校验输出不能覆盖题集或冻结清单")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(content)
    print(content, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
