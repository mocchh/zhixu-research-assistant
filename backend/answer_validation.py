"""Bounded, deterministic citation diagnostics; never semantic verification.

Citation groups bind to their adjacent claim, without pooling prefix/suffix refs.
Literal matches are kept per source, never pooled into proof of an experiment,
causal explanation, attribution, arithmetic result, or translated quotation.
This module neither changes an answer nor calls a model.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import re
import unicodedata


EVIDENCE_RULES_ADDITION = """先直接回答用户所问，省略与问题无关的补充实验、背景数字和反复引文。
每条事实、Markdown列表项和并列分句都紧跟自己的来源编号；不要在长句末堆叠编号来共同支撑多个实验。
数量必须与其对象、总量/激活量、单位、实验设置及版本绑定。同一论文的不同页面、表格、消融实验不能互相代替证据。
表中数字只说明所在行列；归属某个实验需要该表的表头、表注或明确对应的说明。缺这些上下文时说证据不足，不从其他实验补全。
若回答必须比较实验，把各实验分开写，并分别引用其数值与身份说明。不要因为若干数字分别出现在多个SOURCE中就把它们拼成一个实验。
逐字引句只能引用实际包含该完整引句的SOURCE；释义或中文翻译不加表示逐字引用的引号。
已有词面核验只能提示风险，不代表事实、实验归属或翻译语义正确。"""

LIMITATIONS = "仅核对局部引用编号、逐来源的数字/相邻单位/逐字引句及明确关系词的局部共现；关系词未出现只表示未核验，出现也不证明语义、实验归属、否定范围、计算、单位换算或来源内部一致。"

_CITATION = re.compile(r"\[\s*(\d+(?:\s*[,，]\s*\d+)*)\s*\]")
_QUOTE = re.compile(r'“([^”\n]+)”|"([^"\n]+)"')
_CORE_NUMBER = r"[-+]?\d+(?:,\d{3})*(?:\.\d+)?(?:e[-+]?\d+)?"
_UNITS = r"(?:billion|million|trillion|thousand|milliseconds?|seconds?|minutes?|hours?|tokens?|parameters?|steps?|gpus?|cpus?|hrs?|secs?|mins?|days?|gb|mb|tb|bleu|ms|[kmbt]|%|小时|天|毫秒|秒|分钟|参数|步)"
_QUANTITY = re.compile(rf"(?<![a-z0-9_.βαεθλ-])({_CORE_NUMBER})\s*({_UNITS})(?![a-z])", re.I)
_NUMBER = re.compile(rf"(?<![a-z0-9_.βαεθλ-]){_CORE_NUMBER}(?![a-z0-9_]|\.\d)", re.I)
_LIST_PREFIX = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)(?:\[[ xX]\]\s*)?")
_ATTRIBUTION = re.compile(r"(?:这些|上述|这些数字|它们|前者|后者).{0,32}(?:是|来自|属于|对应)|(?:these|those)\b.{0,60}\b(?:are|come from|belong|correspond)", re.I)
_SUPPLEMENT = re.compile(r"^(?:补充(?:说明)?|另外|此外|顺带|additional(?:ly)?|in addition|as an aside)\s*[:：,，]?", re.I)
_CITATION_JOIN = re.compile(r"\s*(?:(?:[,，、]|\band\b|及|和)\s*)*", re.I)
_PREFIX_INTRO = re.compile(r"(?:(?:根据|据|见|参见|来自|来源|资料|source[s]?|according\s+to|see|in|from)\s*)*[:：]?", re.I)
_RELATIONS = {
    "baseline": re.compile(r"\bbaselines?\b|\breference\s+models?\b|基线|基准模型", re.I),
    "control": re.compile(r"\bcontrol\s+(?:groups?|models?|conditions?|experiments?)\b|对照(?:组|模型|条件|实验)", re.I),
    "ablation": re.compile(r"\bablation(?:s|\s+(?:stud(?:y|ies)|experiments?))?\b|消融(?:实验)?", re.I),
    # Bare labels such as "experiment A" describe organization, not a role.
    "experiment": re.compile(r"\bexperimental\s+(?:groups?|models?|conditions?)\b|实验(?:组|条件)", re.I),
    "stability": re.compile(r"\b(?:stable|stability|unstable|instability)\b|不稳定|稳定", re.I),
    "divergence": re.compile(r"\bdiverg(?:e|es|ed|ing|ence)\b|发散", re.I),
}
_RELATION_LABELS = {"baseline": "基线", "control": "对照", "ablation": "消融", "experiment": "实验组/条件",
                    "stability": "稳定结果", "divergence": "发散结果"}
_CONTEXT_BREAK = re.compile(r"[。！？!?；;]|\.(?=\s|$)|\b(?:whereas|while|but|however|in\s+contrast)\b|但是|但|而(?:非|不是)?|相反", re.I)


def _literal_text(text: str) -> str:
    text = re.sub(r"10\s*(?:\^\s*\{?\s*[-−]|[−⁻])\s*([\d⁰¹²³⁴⁵⁶⁷⁸⁹]+)\}?", lambda m: "1e-" + unicodedata.normalize("NFKC", m[1]), text)
    text = unicodedata.normalize("NFKC", text).replace("−", "-").lower()
    text = re.sub(r"(?<=[a-z])-\s*\n\s*(?=[a-z])", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _number_value(value: str) -> str:
    try:
        return str(Decimal(value.replace(",", "")).normalize())
    except InvalidOperation:
        return value


def _unit_value(unit: str) -> str:
    unit = unit.lower()
    aliases = {"b": "billion", "m": "million", "t": "trillion", "k": "thousand",
               "hrs": "hour", "hr": "hour", "小时": "hour", "天": "day",
               "secs": "second", "sec": "second", "秒": "second", "毫秒": "millisecond", "ms": "millisecond",
               "mins": "minute", "min": "minute", "分钟": "minute", "参数": "parameter", "步": "step"}
    return aliases.get(unit, unit[:-1] if unit.endswith("s") else unit)


def _without_quotes(text: str) -> str:
    return _QUOTE.sub(lambda m: " " * len(m[0]), text)


def _without_citations(text: str) -> str:
    return _CITATION.sub(" ", text)


def _mask_inline_code(text: str) -> str:
    """Mask paired backtick runs while retaining all offsets and newlines."""
    runs = list(re.finditer(r"`+", text))
    next_run, latest = {}, {}
    for i in range(len(runs) - 1, -1, -1):
        size = len(runs[i][0])
        next_run[i] = latest.get(size)
        latest[size] = i
    parts, start, i = [], 0, 0
    while i < len(runs):
        opening = runs[i]
        prefix = text[:opening.start()]
        escaped = (len(prefix) - len(prefix.rstrip("\\"))) % 2 == 1
        closing = next_run[i]
        if escaped or closing is None:
            # An unmatched run is ordinary text; it must not hide later claims.
            i += 1
            continue
        end = runs[closing].end()
        parts.extend([text[start:opening.start()], re.sub(r"[^\r\n]", " ", text[opening.start():end])])
        start, i = end, closing + 1
    return "".join(parts) + text[start:]


def _mask_code(text: str) -> str:
    """Keep inline matching within prose, never across a fenced code block."""
    parts, prose, fence = [], [], None
    for line in text.splitlines(keepends=True):
        marker = re.match(r"\s*(`{3,}|~{3,})([^\r\n]*)", line)
        if fence:
            parts.append(re.sub(r"[^\r\n]", " ", line))
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip():
                fence = None
        elif marker and (marker[1][0] != "`" or "`" not in marker[2]):
            parts.append(_mask_inline_code("".join(prose)))
            prose = []
            fence = marker[1]
            parts.append(re.sub(r"[^\r\n]", " ", line))
        else:
            prose.append(line)
    parts.append(_mask_inline_code("".join(prose)))
    return "".join(parts)


def citation_markers(answer: str):
    """Yield (start, end, indices) outside quotes, inline code and code blocks.

    Offsets index the unchanged answer, so callers can label invalid markers
    without modifying quotations or using a global replacement expression.
    """
    offset = 0
    closing_quote = None
    for line in _mask_code(answer).splitlines(keepends=True):
        i = 0
        while i < len(line):
            character = line[i]
            escaped = character in {'"', '“', '”'} and i > 0 and (len(line[:i]) - len(line[:i].rstrip("\\"))) % 2 == 1
            if closing_quote:
                if character == closing_quote and not escaped:
                    closing_quote = None
            elif character in {'"', '“'} and not escaped:
                closing_quote = '”' if character == '“' else '"'
            else:
                match = _CITATION.match(line, i)
                if match:
                    yield offset + i, offset + match.end(), [int(n) for n in re.findall(r"\d+", match[1])]
                    i = match.end()
                    continue
            i += 1
        offset += len(line)


def _numbers(text: str) -> set[str]:
    # Quantities have a special suffix boundary so 671B is included but V3,
    # P100 and v1 remain identifiers. Scales are checked separately below.
    return {_number_value(m[0]) for m in _NUMBER.finditer(text)} | {_number_value(m[1]) for m in _QUANTITY.finditer(text)}


def _citation_groups(text: str):
    """Keep enumerated markers together, without crossing factual text."""
    groups = []
    for start, end, indices in citation_markers(text):
        if groups and _CITATION_JOIN.fullmatch(text[groups[-1][1]:start]):
            previous = groups.pop()
            groups.append((previous[0], end, sorted(set(previous[2] + indices))))
        else:
            groups.append((start, end, indices))
    return groups


def _prefix_intro(text: str) -> bool:
    plain = re.sub(r"[*_`>|]", "", text).strip(" ,，:：")
    plain = _SUPPLEMENT.sub("", plain).strip()
    return bool(_PREFIX_INTRO.fullmatch(plain))


def _citation_binding(prose: str) -> dict:
    groups = _citation_groups(prose)
    prefix = groups[0][2] if groups and _prefix_intro(prose[:groups[0][0]]) and prose[groups[0][1]:].strip(" ,，:：。.!?！？") else []
    suffix = groups[-1][2] if groups and (not prefix or len(groups) > 1) else []
    # A conflicting leading group cannot lend literals to the closer suffix.
    refs = suffix or prefix
    return {"placement": "mixed" if prefix and suffix else "prefix" if prefix else "suffix" if suffix else "none",
            "prefix_source_indices": sorted(set(prefix)), "suffix_source_indices": sorted(set(suffix)),
            "source_indices": sorted(set(refs)),
            "conflict": bool(prefix and suffix and set(prefix) != set(suffix))}


def _fragments(answer: str):
    """Bind leading SOURCE groups or trailing groups, then stop at the clause."""
    for raw, code_masked in zip(answer.splitlines(), _mask_code(answer).splitlines()):
        if not code_masked.strip():
            continue
        heading = re.match(r"^\s*#{1,6}\s+", raw)
        if heading:
            raw, code_masked = raw[heading.end():], code_masked[heading.end():]
        listed = bool(_LIST_PREFIX.match(raw))
        prefix = _LIST_PREFIX.match(raw)
        if prefix:
            raw, code_masked = raw[prefix.end():], code_masked[prefix.end():]
        left, right = len(raw)-len(raw.lstrip()), len(raw.rstrip())
        line, code_masked = raw[left:right], code_masked[left:right]
        if re.fullmatch(r"[|\s:-]+", line):
            continue
        masked = _without_quotes(code_masked)
        groups = {start: end for start, end, _ in _citation_groups(code_masked)}
        start = i = 0
        while i < len(line):
            if i in groups:
                stop = groups[i]
                if _prefix_intro(code_masked[start:i]) and code_masked[stop:].strip(" ,，:：。.!?！？"):
                    i = stop
                    continue
                if re.search(r"[\w\u3400-\u9fff]", _without_citations(line[start:i])):
                    yield line[start:stop].strip(" ,，;；。.!?！？"), listed, code_masked[start:stop]
                    start = stop
                i = stop
                continue
            character = masked[i]
            terminal = character in "。！？!?；;" or (character == "." and (i + 1 == len(line) or masked[i + 1].isspace()))
            if terminal:
                # Also accept the common punctuation-before-citation form.
                after = i + 1
                while after < len(line) and line[after].isspace():
                    after += 1
                if not _CITATION.match(masked, after):
                    fragment = line[start:i + 1].strip(" ,，;；。.!?！？")
                    if fragment:
                        yield fragment, listed, code_masked[start:i + 1]
                    start = i + 1
            i += 1
        rest = line[start:].strip(" ,，;；。.!?！？")
        if rest:
            yield rest, listed, code_masked[start:]


def _is_fact_candidate(text: str, listed: bool, findings: list) -> bool:
    plain = re.sub(r"[*_`>|]", "", text).strip()
    if findings:
        return True
    if plain.endswith((":", "：")) or plain in {"结论", "总结", "说明", "参考资料", "训练设置", "参数设置", "结果", "摘要"}:
        return False
    if re.fullmatch(r"(?:谢谢(?:你的提问)?|不客气|希望(?:这|以上).{0,20}(?:帮助|有用)|thank you|you're welcome)[!！。.]?", plain, re.I):
        return False
    if re.match(r"^(?:(?:现有|本次|所给|提供的)\s*)?(?:证据|资料).{0,15}(?:不足|未提供|没有|无法确定)", plain):
        return False
    cjk = len(re.findall(r"[\u3400-\u9fff]", plain))
    words = len(re.findall(r"[a-zA-Z]{2,}", plain))
    return cjk >= (4 if listed else 8) or words >= (3 if listed else 5)


def _contexts(text: str) -> list[str]:
    # Decimal points stay intact. Explicit contrast clauses cannot lend a role
    # to each other's numbers, even when they are in the same source sentence.
    return [part.strip(" ,，:：") for part in _CONTEXT_BREAK.split(text) if part.strip(" ,，:：")]


def _relation_mentions(text: str):
    for name, pattern in _RELATIONS.items():
        for match in pattern.finditer(text):
            before = text[max(0, match.start() - 35):match.start()]
            negative = bool(re.search(r"(?:\b(?:not|no|never)\s+(?!only\b)(?:a\s+|an\s+|the\s+)?|不是|并非|不属于|非)\s*$", before, re.I))
            if name in {"stability", "divergence"}:
                # Outcome negation stays local and explicit. In particular,
                # "no divergence" is not an affirmative divergence match, and
                # it does not automatically become evidence of stability.
                negative = negative or bool(re.search(
                    r"(?:\b(?:no|without)\s+(?:(?:model|training)\s+)?|"
                    r"\b(?:not|never)\s+(?!only\b)(?:(?:show|exhibit|cause|experience|observe)\s+)?|"
                    r"没有(?:出现|发生|表现出)?|未(?:出现|发生|表现出)?|不)\s*$", before, re.I))
                after = text[match.end():match.end() + 45]
                negative = negative or bool(re.match(
                    r"\s+(?:(?:is|was|were)\s+)?not\s+(?:observed|found|seen|detected|present)\b|"
                    r"\s*(?:未被观察到|没有出现)", after, re.I))
                intrinsic_negative = match[0].lower() in {"unstable", "instability", "不稳定"}
                negative = bool(negative) != intrinsic_negative
            yield name, "negative" if negative else "positive"


def _quantity_literal(match) -> str:
    return f"{match[1]}{match[2].upper()}" if match[2] in {"k", "m", "b", "t"} else f"{match[1]} {match[2]}"


def _relation_checks(plain: str, valid: list[int], sources: list[dict], normalized: list[str]) -> list[dict]:
    """Record local relation-word matches per source, never an entailment verdict.

    Only sources containing the exact anchor are inspected for its role. A
    relation elsewhere in that source, or in another cited source, cannot fill
    the gap. No numeric anchor means the referent is still not identified.
    """
    checks, seen = [], set()
    for claim_context in _contexts(plain):
        relations = sorted(set(_relation_mentions(claim_context)), key=lambda item: (list(_RELATIONS).index(item[0]), item[1]))
        if not relations:
            continue
        anchors = [(_quantity_literal(m), (_number_value(m[1]), _unit_value(m[2]))) for m in _QUANTITY.finditer(claim_context)]
        if not anchors:
            anchors = [(m[0], (_number_value(m[0]), None)) for m in _NUMBER.finditer(claim_context)]
        for relation, polarity in relations:
            for literal, anchor in anchors or [(None, None)]:
                for n in valid:
                    key = (relation, polarity, literal, n)
                    if key in seen:
                        continue
                    seen.add(key)
                    source_text = normalized[n - 1]
                    contexts = _contexts(source_text)
                    if anchor:
                        value, unit = anchor
                        contexts = [context for context in contexts if
                                    (value, unit) in {(_number_value(m[1]), _unit_value(m[2])) for m in _QUANTITY.finditer(context)}
                                    or (unit is None and value in _numbers(context))]
                        if not contexts:
                            # The numeric diagnostic already reports absence;
                            # do not invent a role target in an unrelated source.
                            continue
                    matching = [context for context in contexts if (relation, polarity) in set(_relation_mentions(context))]
                    local = bool(matching) and anchor is not None
                    if local:
                        reason = "relation_word_near_quantity"
                    elif anchor is None:
                        reason = "referent_not_explicit"
                    elif any(name == relation for context in contexts for name, _ in _relation_mentions(context)):
                        reason = "relation_polarity_unverified"
                    elif any(name == relation for name, _ in _relation_mentions(source_text)):
                        reason = "relation_term_in_other_context"
                    else:
                        reason = "relation_term_absent"
                    excerpt = (matching or contexts or [source_text])[0]
                    # Put a late anchor inside the bounded diagnostic excerpt.
                    offset = 0
                    if anchor and len(excerpt) > 300:
                        candidates = list(_QUANTITY.finditer(excerpt)) if anchor[1] else list(_NUMBER.finditer(excerpt))
                        found = next((m for m in candidates if _number_value(m[1] if anchor[1] else m[0]) == anchor[0]), None)
                        offset = max(0, found.start() - 100) if found else 0
                    checks.append({"relation": relation, "quantity": literal, "source_index": n,
                                   "page": sources[n - 1].get("page"), "claim_polarity": polarity,
                                   "status": "local_relation_literal" if local else "relation_unverified",
                                   "reason": reason, "context_excerpt": excerpt[offset:offset + 300]})
    return checks


def check_citation_consistency(answer: str, sources: list[dict]) -> dict:
    """Return review flags without changing the answer or verifying its meaning."""
    normalized = [_literal_text(_without_citations(str(s.get("text", "")))) for s in sources]
    quote_sources = [_literal_text(str(s.get("text", ""))) for s in sources]
    source_numbers = [_numbers(text) for text in normalized]
    source_quantities = [{(_number_value(m[1]), _unit_value(m[2])) for m in _QUANTITY.finditer(text)} for text in normalized]
    claims = []
    for fragment, listed, prose in _fragments(answer):
        binding = _citation_binding(prose)
        refs = binding["source_indices"]
        valid = [n for n in refs if 1 <= n <= len(sources)]
        invalid = sorted({n for _, _, indices in citation_markers(prose) for n in indices if not 1 <= n <= len(sources)})
        plain = _literal_text(re.sub(r"[*`]|(?<!\w)_(?!\w)", "", _without_citations(prose)))

        def finding(literal, matches):
            return {"literal": literal, "status": "matched_literal" if matches else "not_found_in_cited_sources", "matched_source_indices": matches}

        numbers = []
        seen_numbers = set()
        for match in list(_NUMBER.finditer(plain)) + list(_QUANTITY.finditer(plain)):
            literal = match[1] if match.re is _QUANTITY else match[0]
            value = _number_value(literal)
            if value not in seen_numbers:
                numbers.append(finding(literal, [n for n in valid if value in source_numbers[n-1]]))
                seen_numbers.add(value)
        quantities = []
        for match in _QUANTITY.finditer(plain):
            value = (_number_value(match[1]), _unit_value(match[2]))
            literal = _quantity_literal(match)
            quantities.append(finding(literal, [n for n in valid if value in source_quantities[n-1]]))
        quotes = []
        for match in _QUOTE.finditer(prose):
            quote = match[1] or match[2]
            if len(quote.strip()) >= 4:
                quotes.append(finding(quote, [n for n in valid if _literal_text(quote) in quote_sources[n-1]]))
        findings = numbers + quantities + quotes
        if not refs and not _is_fact_candidate(prose, listed, findings):
            continue
        risks = []
        if binding["conflict"]:
            risks.append("citation_binding_conflict")
        if invalid:
            risks.append("invalid_citation")
        if not valid:
            risks.append("missing_citation")
        if any(f["status"] != "matched_literal" for f in findings):
            risks.append("literal_not_found")
        joint = set(valid)
        for item in findings:
            joint &= set(item["matched_source_indices"])
        if len(valid) > 1 and findings and all(f["matched_source_indices"] for f in findings) and not joint:
            risks.append("split_source_support")
        locations = {(sources[n-1].get("paper_id"), sources[n-1].get("page")) for n in valid}
        if len(locations) > 1 and len(quantities) >= 2:
            risks.append("multiple_source_contexts")
        if valid and _ATTRIBUTION.search(prose):
            risks.append("context_attribution_unverified")
        if _SUPPLEMENT.search(prose):
            risks.append("supplement_scope_review")
        # A verbatim quotation is checked as a whole above. Do not reinterpret
        # an isolated quoted sentence as an unanchored experimental claim.
        quote_intro = _without_quotes(plain).strip(" ,，:：。.!?！？")
        quotation_only = bool(quotes) and bool(re.fullmatch(
            r"(?:原文(?:为|说|写道|指出)|(?:the\s+)?(?:source|paper|text)\s+(?:states|says|reports|reads)|quote|quotation)?", quote_intro, re.I))
        relation_checks = [] if quotation_only else _relation_checks(plain, valid, sources, normalized)
        if any(item["status"] == "relation_unverified" for item in relation_checks):
            risks.append("relation_unverified")
        status = "needs_review" if risks else "matched_literals" if findings else "no_checkable_literals"
        claims.append({"text": fragment, "source_indices": valid, "invalid_source_indices": invalid,
                       "numbers": numbers, "quantities": quantities, "quotes": quotes,
                       "citation_binding": binding, "relation_checks": relation_checks,
                       "joint_literal_source_indices": sorted(joint) if findings else [],
                       "risk_codes": risks, "status": status})
    return {"method": "lexical_rules", "version": "claim-local-v4.1", "semantic_entailment_verified": False,
            "claims": claims, "limitations": LIMITATIONS}


def concise_review_warnings(checks: dict, limit: int = 3) -> list[str]:
    """Up to three actionable warnings, with overflow counted in the last one."""
    issues = []
    def add(priority, text):
        if not any(previous == text for _, previous in issues):
            issues.append((priority, text))
    for claim in checks.get("claims", []):
        refs = "".join(f"[{i}]" for i in claim.get("source_indices", [])) or "未标来源"
        risks = set(claim.get("risk_codes", []))
        if "invalid_citation" in risks:
            invalid = "".join(f"[{i}]" for i in claim["invalid_source_indices"])
            add(0, f"引用 {invalid} 超出本次来源范围；该处需要核对。")
        if "citation_binding_conflict" in risks:
            prefix = "".join(f"[{i}]" for i in claim["citation_binding"]["prefix_source_indices"])
            add(0, f"同一陈述的前置来源 {prefix} 与后置来源 {refs} 不一致；仅用后置来源核对，不能拼接两组证据。")
        missing = [f for f in claim.get("quantities", []) + claim.get("quotes", []) if not f.get("matched_source_indices")]
        covered_numbers = {m[0] for f in missing for m in _NUMBER.finditer(_literal_text(f["literal"]))}
        missing += [f for f in claim.get("numbers", []) if not f.get("matched_source_indices") and f["literal"] not in covered_numbers]
        if missing and claim.get("source_indices"):
            literals = "、".join(f'“{f["literal"][:65]}”' for f in missing[:3])
            suffix = f"等 {len(missing)} 项" if len(missing) > 3 else ""
            add(1, f"{literals}{suffix} 未在所引片段 {refs} 中逐项匹配；计算、换算或释义也可能触发，请核对原文。")
        elif "missing_citation" in risks:
            excerpt = re.sub(r"[*`>|]", "", claim["text"])[:65]
            add(2, f"“{excerpt}”未紧跟有效来源；后文引用不会自动支持这一项。")
        if "split_source_support" in risks:
            details = [f'“{f["literal"][:35]}”→' + "".join(f"[{n}]" for n in f["matched_source_indices"]) for f in claim.get("quantities", []) + claim.get("quotes", []) if f.get("matched_source_indices")]
            add(3, f"同一陈述的字面信息分散在不同来源 {refs}（{'；'.join(details[:3]) or '没有一条来源包含全部数字'}）；不能据此认定属于同一实验。")
        if "context_attribution_unverified" in risks:
            add(1, f"“{claim['text'][:65]}”用 {refs} 解释前文数字的归属；请核对是否为同一表格、实验和配置。")
        grouped_relations = {}
        for item in claim.get("relation_checks", []):
            if item["status"] != "relation_unverified":
                continue
            key = (item["relation"], item["source_index"], item.get("page"), item["reason"])
            grouped_relations.setdefault(key, []).append(item.get("quantity"))
        for (relation, index, page, reason), literals in grouped_relations.items():
            label = _RELATION_LABELS[relation]
            values = "、".join(dict.fromkeys(value for value in literals if value)) or "该陈述"
            position = f"（第 {page} 页）" if page is not None else ""
            detail = {"relation_term_absent": "未出现对应关系词",
                      "relation_term_in_other_context": "关系词出现在其他句子或对比从句",
                      "relation_polarity_unverified": "关系词的肯定/否定表述不一致",
                      "referent_not_explicit": "没有明确数字对象可供就近绑定"}.get(reason, "缺少局部关系依据")
            add(1, f"{values[:65]} 的“{label}”关系在来源 [{index}]{position} 中未核验：{detail}；其他来源不能代为支持，请核对原文。")
        if "multiple_source_contexts" in risks and "split_source_support" not in risks:
            add(4, f"同一条数量陈述同时引用不同位置 {refs}；即使数字均匹配，也需核对实验、表头和配置是否一致。")
        if "supplement_scope_review" in risks:
            add(5, f"补充内容“{claim['text'][:55]}”需核对是否为回答问题所必需，以及来源 {refs} 是否支持。")
    issues.sort(key=lambda issue: issue[0])
    limit = max(0, min(3, limit))
    selected = [text for _, text in issues[:limit]]
    if selected and len(issues) > limit:
        selected[-1] += f" 另有 {len(issues)-limit} 项规则风险，请逐条核对原文引用。"
    return selected
