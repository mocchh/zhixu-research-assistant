"""Single-call question answering with locally rendered, auditable citations.

The contract guarantees structure and exposes failed checks. It does not prove
that a claim follows from a source, even when every local check passes.
"""
from __future__ import annotations

import json
import re

from . import answer_validation


QUESTION_OUTPUT_RULES = """本次是直接问答，仅输出一个 JSON 对象，不输出 Markdown 围栏或其他文字：
{"claims":[{"text":"直接回答所问的一条事实","source_indices":[1]}],"insufficient_evidence":false}
claims 最多 8 条，每条 text 最多 600 字，source_indices 为 1 至 3 个当前来源编号。
每条只表达一个有来源支持的事实或紧密关联的数值对。text 不填写来源编号，由应用追加；不要写长段、多句或跨实验复合陈述。
仅列回答当前问题所必需的事实。同一事实选择最直接的来源一次即可，不逐一重复摘要、引言和结论；省略题目未要求的消融实验、其他模型规模及补充背景。
找不到证据的子问不要猜测，设置 insufficient_evidence 为 true；完全无证据则 claims 为空。
JSON 只允许上述字段；不足说明由应用展示，不另写无来源的事实。"""


class AnswerFormatError(ValueError):
    pass


_LINE_BREAK = re.compile(r"[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]")
_TILDE_FENCE = re.compile(r"^\s*(?:(?:>\s*)|(?:(?:[-+*]|\d+[.)])\s+))*~{3,}")


def _render_claims(raw: str, source_count: int) -> tuple[list[str], bool]:
    """Keep each structured fact separate from Markdown document parsing."""
    if len(raw) > 20000:
        raise AnswerFormatError("模型草稿过长，未作为答案展示。")
    content = raw.strip()
    fence = re.fullmatch(r"```(?:json)?\s*\n(.*?)\n```", content, re.S | re.I)
    if fence:
        content = fence[1]

    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        data = json.loads(content, object_pairs_hook=unique_keys)
    except (ValueError, RecursionError):
        raise AnswerFormatError("模型未按逐条引用格式返回，当前展示原文摘录。") from None
    if (not isinstance(data, dict) or set(data) != {"claims", "insufficient_evidence"}
            or type(data["insufficient_evidence"]) is not bool
            or not isinstance(data["claims"], list) or len(data["claims"]) > 8):
        raise AnswerFormatError("模型草稿的字段或条目数量不符合要求，当前展示原文摘录。")
    if not data["claims"] and not data["insufficient_evidence"]:
        raise AnswerFormatError("模型未给出可核对的回答条目，当前展示原文摘录。")
    paragraphs = []
    for claim in data["claims"]:
        if not isinstance(claim, dict) or set(claim) != {"text", "source_indices"}:
            raise AnswerFormatError("模型条目缺少独立的事实与来源，当前展示原文摘录。")
        text, refs = claim["text"], claim["source_indices"]
        if (not isinstance(text, str) or not text.strip() or len(text) > 600
                or _LINE_BREAK.search(text)
                or not isinstance(refs, list) or not 1 <= len(refs) <= 3
                or any(type(n) is not int or not 1 <= n <= source_count for n in refs)
                or len(set(refs)) != len(refs)):
            raise AnswerFormatError("模型条目的文字或来源编号不符合要求，当前展示原文摘录。")
        if list(answer_validation.citation_markers(text)):
            raise AnswerFormatError("模型条目在正文中混入来源编号，当前展示原文摘录。")
        # Code spans deliberately bypass the general research citation checker.
        # A structured factual answer must not use them to hide unchecked data.
        if "`" in text or _TILDE_FENCE.match(text):
            raise AnswerFormatError("模型条目将事实放入代码标记，无法逐项核对，当前展示原文摘录。")
        paragraphs.append(text.strip() + " " + "".join(f"[{n}]" for n in refs))
    return paragraphs, data["insufficient_evidence"]


def render_draft(raw: str, source_count: int) -> tuple[str, bool]:
    """Validate a bounded contract and append the supplied citations locally."""
    paragraphs, insufficient = _render_claims(raw, source_count)
    return "\n\n".join(paragraphs), insufficient


def inspect_draft(raw: str, sources: list[dict]) -> dict:
    """Keep the full draft, while declining to promote failed checks to answers."""
    audit = {"status": "fallback", "raw_response": raw, "draft": raw,
             "semantic_entailment_verified": False, "issues": [], "citation_checks": {}}
    try:
        paragraphs, insufficient = _render_claims(raw, len(sources))
    except AnswerFormatError as error:
        audit["issues"] = [str(error)]
        return audit
    audit["draft"] = "\n\n".join(paragraphs)
    # The general checker ignores code and non-factual Markdown. That behavior
    # must never make an unexamined structured item count as a checked answer,
    # or let formatting in one item suppress the next item's diagnostics.
    checks = answer_validation.check_citation_consistency("", sources)
    checks["claims"] = []
    unreviewed = []
    for index, paragraph in enumerate(paragraphs):
        local = answer_validation.check_citation_consistency(paragraph, sources)
        if not local["claims"]:
            unreviewed.append(index + 1)
        checks["claims"].extend({**claim, "structured_claim_index": index + 1}
                                for claim in local["claims"])
    audit["citation_checks"] = checks
    if unreviewed:
        checks["unreviewed_claim_indices"] = unreviewed
        audit["issues"] = ["模型第 " + "、".join(map(str, unreviewed)) +
                           " 条事实未产生可核对的诊断，当前展示原文摘录。"]
        return audit
    blocking = {"invalid_citation", "missing_citation", "literal_not_found", "split_source_support",
                "relation_unverified", "supplement_scope_review", "citation_binding_conflict"}
    if any(blocking.intersection(claim.get("risk_codes", [])) for claim in checks["claims"]):
        audit["issues"] = answer_validation.concise_review_warnings(checks)
        return audit
    audit["status"] = "insufficient" if insufficient else "checked"
    return audit
