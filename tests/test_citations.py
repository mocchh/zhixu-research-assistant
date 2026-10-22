"""Citation precision, explicit-version constraints and original evidence."""
import copy

import pytest

from backend import citations


def paper(identifier="target", **changes):
    return {"id": identifier, "title": "Attention Is All You Need", "family_id": identifier,
            "version": 1, "authors": [], **changes}


def block(text, identifier="r", **changes):
    return {"id": identifier, "paper_id": "source", "page": 8, "bbox": [12, 34, 500, 100],
            "text": text, "kind": "reference", "section": "References", **changes}


def linked(text, papers, **changes):
    return citations.link_references([block(text, **changes)], papers)[0]


@pytest.mark.parametrize(("value", "expected"), [
    ("1706.03762v7", ("1706.03762", 7)),
    ("arXiv:1706.03762v1.", ("1706.03762", 1)),
    ("https://arxiv.org/abs/1706.03762v7", ("1706.03762", 7)),
    ("http://arxiv.org/pdf/1706.03762v1.pdf", ("1706.03762", 1)),
    ("https://export.arxiv.org/pdf/hep-th/9901001v2.pdf", ("hep-th/9901001", 2)),
    ("arXiv:math.GT/0309136v2", ("math.gt/0309136", 2)),
    ("https://arxiv.org/abs/\n1706.03762", ("1706.03762", None)),
    ("arXiv:1706.03762v0", None),
    ("arXiv:1713.03762", None),
    ("https://fakearxiv.org/pdf/1706.03762v7.pdf", None),
    ("https://other.example/arxiv.org/pdf/1706.03762v7.pdf", None),
])
def test_arxiv_identifiers(value, expected):
    assert citations.parse_arxiv(value) == expected


@pytest.mark.parametrize("location", ["arXiv:1706.03762v1", "https://arxiv.org/pdf/1706.03762v1.pdf", "http://arxiv.org/abs/1706.03762v1", "arXiv preprint 1706.03762v1"])
def test_reference_version_is_exact_and_never_falls_back_to_title(location):
    v1 = paper("v1", arxiv_id="1706.03762", version=1, family_id="family")
    v7 = paper("v7", arxiv_id="1706.03762", version=7, family_id="family")
    text = f"[1] A. Author. Attention Is All You Need. {location}."
    hit = linked(text, [v7, v1])
    assert hit["linked_paper_id"] == "v1"
    assert hit["version"] == hit["linked_version"] == 1
    assert hit["match_rule"] == "arxiv_id_and_version_exact"
    missing = linked(text, [v7])
    assert "linked_paper_id" not in missing
    assert missing["match_rule"] == "specified_version_not_found"


def test_unversioned_arxiv_selects_latest_local_version_and_keeps_note():
    papers = [paper("v1", arxiv_id="1706.03762", version=1, family_id="f"), paper("v7", arxiv_id="1706.03762", version=7, family_id="f")]
    ref = linked("[1] Example. https://arxiv.org/abs/1706.03762", papers)
    assert ref["linked_paper_id"] == "v7"
    assert ref["version"] is None and ref["linked_version"] == 7
    assert ref["match_rule"] == "arxiv_id_latest_local_version"
    assert "未指定版本" in ref["match_note"]


def test_legacy_metadata_embedded_version_is_accepted():
    ref = linked("[1] Example. arXiv:hep-th/9901001v2.", [paper(arxiv_id="hep-th/9901001v2", version=2)])
    assert ref["linked_paper_id"] == "target" and ref["linked_version"] == 2
    inconsistent = linked("[1] Example. arXiv:hep-th/9901001v2.", [paper(arxiv_id="hep-th/9901001v2", version=1)])
    assert "linked_paper_id" not in inconsistent
    bare = linked("[1] Example. hep-th/9901001v2.", [paper(arxiv_id="hep-th/9901001", version=2)])
    assert bare["linked_paper_id"] == "target" and bare["linked_version"] == 2


@pytest.mark.parametrize("text", [
    "[1] Attention Is All You Need. arXiv:1706.03762v1 and arXiv:1706.03762v7.",
    "[1] Attention Is All You Need. arXiv:1706.03762 and arXiv:1607.06450.",
    "[1] Attention Is All You Need. doi:10.1000/one and doi:10.1000/two.",
])
def test_conflicting_identifiers_do_not_choose_arbitrarily(text):
    ref = linked(text, [paper(arxiv_id="1706.03762")])
    assert "linked_paper_id" not in ref
    assert ref["match_rule"] == "conflicting_identifiers"


def test_title_normalizes_wrapped_hyphen_and_punctuation_without_partial_titles():
    target = paper(title="A Comprehensive Study of Representation-Learning for Scientific Documents")
    ref = linked("[1] A. Author. A comprehensive study of repre-\nsentation learning for scientific documents. 2020.", [target])
    assert ref["linked_paper_id"] == "target"
    assert ref["match_rule"] == "title_latest_local_version" and ref["confidence"] == 0.9
    for text in ("A. Author. Attention Is All You Need Revisited. 2019.",
                 "A. Author. Beyond Attention Is All You Need. 2019.",
                 "A. Author. Attention Is All You Need: A Revision. 2019."):
        assert "linked_paper_id" not in linked(text, [paper()])


def test_same_title_different_families_is_ambiguous_unless_authors_and_year_disambiguate():
    a = paper("a", authors=["Alice Able"], year=2020)
    b = paper("b", authors=["Bob Baker"], year=2021)
    ambiguous = linked("[1] Attention Is All You Need.", [a, b])
    assert ambiguous["match_rule"] == "ambiguous_local_match"
    assert "linked_paper_id" not in ambiguous
    hit = linked("[1] Alice Able. Attention Is All You Need. 2020.", [b, a])
    assert hit["linked_paper_id"] == "a"
    assert hit["cross_evidence"] == [{"kind": "author", "value": "able"}, {"kind": "year", "value": 2020}]


def test_title_versions_in_one_family_still_collapse():
    a = paper("a", family_id="f", version=1)
    b = paper("b", family_id="f", version=7)
    assert linked("[1] Attention Is All You Need.", [a, b])["linked_paper_id"] == "b"


def test_title_family_is_not_proof_when_unknown_id_papers_have_conflicting_authors():
    a = paper("a", family_id="title:same", authors=["Alice Able"], year=2020, version=None)
    b = paper("b", family_id="title:same", authors=["Bob Baker"], year=2021, version=None)
    explicit = linked("[1] Alice Able. Attention Is All You Need. 2020.", [a, b])
    assert explicit["linked_paper_id"] == "a"
    for text in ("[1] Attention Is All You Need.",
                 "[1] Alice Able and Bob Baker. Attention Is All You Need. 2020, 2021."):
        ref = linked(text, [a, b])
        assert "linked_paper_id" not in ref
        assert ref["match_rule"] == "ambiguous_local_match"


def test_known_family_versions_and_shared_author_duplicates_still_select_latest():
    a = paper("a", family_id="f", authors=["Alice Able"], version=1)
    b = paper("b", family_id="f", authors=["A. Able"], version=2)
    assert linked("[1] Alice Able. Attention Is All You Need.", [a, b])["linked_paper_id"] == "b"
    a.update(arxiv_id="1706.03762")
    b.update(arxiv_id="1706.03762", authors=["Bob Baker"])
    assert linked("[1] Attention Is All You Need. arXiv:1706.03762v1.", [a, b])["linked_paper_id"] == "a"


def test_graph_splits_conflicting_title_family_and_targets_supported_author():
    source = paper("source", title="Another source study")
    a = paper("a", family_id="title:same", authors=["Alice Able"], year=2020, version=1)
    a2 = paper("a2", family_id="title:same", authors=["A. Able"], year=2020, version=2)
    b = paper("b", family_id="title:same", authors=["Bob Baker"], year=2021, version=7)
    refs = [block("[1] Alice Able. Attention Is All You Need. 2020.")]
    graph = citations.citation_graph([source, a, a2, b], refs)
    assert {n["id"] for n in graph["nodes"]} == {"source", "a2", "b"}
    assert len(graph["edges"]) == 1
    edge = graph["edges"][0]
    assert edge["target"] == edge["evidence"][0]["linked_paper_id"] == "a2"
    assert next(n for n in graph["nodes"] if n["id"] == "b")["citations"] == 0


def test_long_approximate_title_requires_author_and_known_year():
    target = paper(title="A Comprehensive Study of Neural Representation Learning for Scientific Documents", authors=["Alice Able"], year=2020)
    text = "[1] Alice Able. A comprehensive study of neural represantation learning for scientific documents. 2020."
    ref = linked(text, [target])
    assert ref["linked_paper_id"] == "target"
    assert ref["match_rule"] == "title_approximate_with_bibliographic_evidence"
    assert ref["confidence"] == 0.8
    for weak in (text.replace("Alice Able", "B. Other"), text.replace("2020", "2021")):
        assert "linked_paper_id" not in linked(weak, [target])
    assert "linked_paper_id" not in linked("[1] Alice Able. Attention Is All You Nead. 2020.", [paper(authors=["Alice Able"], year=2020)])
    venue_name = text.replace("Alice Able", "Other Author").replace("2020.", "Journal of Able Research, 2020.")
    assert "linked_paper_id" not in linked(venue_name, [target])


@pytest.mark.parametrize("identifier", ["arXiv:9999.99999v1", "arXiv:1706.03762v0", "https://arxiv.org/abs/invalid"])
def test_invalid_explicit_identifier_blocks_title_fallback(identifier):
    ref = linked(f"[1] Attention Is All You Need. {identifier}.", [paper()])
    assert "linked_paper_id" not in ref
    assert ref["match_rule"] == "invalid_arxiv_identifier"


def test_doi_uses_real_target_metadata_and_rejects_conflict():
    ref = "[1] Any other bibliographic title. https://doi.org/10.1234/ABC."
    hit = linked(ref, [paper(doi="10.1234/abc")])
    assert hit["match_rule"] == "doi_exact" and hit["linked_paper_id"] == "target"
    missing = linked(ref, [paper(abstract="See https://doi.org/10.1234/abc")])
    assert "linked_paper_id" not in missing
    conflict = linked("[1] Attention Is All You Need. doi:10.1234/abc.", [paper(doi="10.1234/other")])
    assert "linked_paper_id" not in conflict
    title_only = linked("[1] Attention Is All You Need. doi:10.1234/abc.", [paper()])
    assert title_only["match_rule"] == "title_latest_local_version"
    identifiers_conflict = linked("[1] Attention Is All You Need. arXiv:1706.03762. doi:10.1234/abc.", [paper(arxiv_id="1706.03762", doi="10.1234/other")])
    assert identifiers_conflict["match_rule"] == "conflicting_identifiers"


def test_body_mentions_and_similarly_named_sections_are_ignored():
    texts = [block("Attention Is All You Need. arXiv:1706.03762.", kind="paragraph", section=section) for section in ("Methods", "References to prior results", "")]
    assert citations.link_references(texts, [paper(arxiv_id="1706.03762")]) == []
    ref = linked("[1] Attention Is All You Need.", [paper()], kind="paragraph", section="9 References")
    assert ref["linked_paper_id"] == "target"


@pytest.mark.parametrize("delimiter", ["\n[2] ", "\n[2]", " [2] ", "\n2. ", "\n\n"])
def test_separate_entries_inside_a_block_cannot_make_a_title(delimiter):
    target = paper(title="A Full Title Formed By Joining Two Separate Entries")
    blocks = [block("[1] A Full Title Formed" + delimiter + "By Joining Two Separate Entries.")]
    refs = citations.link_references(blocks, [target])
    assert len(refs) == 2
    assert not any(r.get("linked_paper_id") for r in refs)
    for ref in refs:
        assert ref["block_text"][ref["start"]:ref["end"]] == ref["text"]
        assert ref["bbox"] == [12, 34, 500, 100]


@pytest.mark.parametrize("second_author", ["Dzmitry Bahdanau", "张三"])
def test_ocr_compact_reference_labels_keep_entries_and_exact_evidence(second_author):
    first = "[1]Jimmy Lei Ba. Layer normalization. arXiv:1607.06450, 2016."
    second = f"[2]{second_author}. Neural machine translation. arXiv:1409.0473, 2014."
    source = block("  " + first + "\n\t" + second + "  ")
    original = copy.deepcopy(source)
    refs = citations.reference_entries([source])
    assert [(ref["label"], ref["arxiv_id"], ref["year"]) for ref in refs] == [
        ("1", "1607.06450", 2016), ("2", "1409.0473", 2014)]
    assert [ref["text"] for ref in refs] == [first, second]
    assert source == original
    for ref in refs:
        assert source["text"][ref["start"]:ref["end"]] == ref["text"]
        assert ref["bbox"] == source["bbox"] and ref["block_id"] == source["id"]


@pytest.mark.parametrize("mention", ["This result extends [2]Author's method.",
                                    "1.Introduction presents prior evidence.",
                                    "[2]+x denotes the indexed quantity."])
def test_compact_reference_support_does_not_split_inline_citations_or_numbered_text(mention):
    raw = "[1] A. Author. An earlier paper. 2020.\n" + mention
    refs = citations.reference_entries([block(raw)])
    assert len(refs) == 1 and refs[0]["text"] == raw
    assert refs[0]["label"] == "1"
    assert citations.reference_entries([block(raw, kind="paragraph", section="Methods")]) == []


def test_separate_blocks_never_get_concatenated():
    blocks = [block("[1] A Full Title Formed", identifier="one"), block("By Joining Two Separate Entries.", identifier="two")]
    result = citations.citation_graph([paper("source"), paper(title="A Full Title Formed By Joining Two Separate Entries")], blocks)
    assert result["edges"] == []


def test_graph_preserves_original_evidence_and_exact_target_version_when_families_collapse():
    papers = [paper("source1", title="Source study of language models", family_id="sourcefamily", version=1), paper("source7", title="Source study of language models", family_id="sourcefamily", version=7),
              paper("target1", arxiv_id="1706.03762", family_id="targetfamily", version=1), paper("target7", arxiv_id="1706.03762", family_id="targetfamily", version=7)]
    blocks = [block("[1] A. Author. Attention Is All You Need. arXiv:1706.03762v1.", paper_id="source1"),
              block("[1] Attention Is All You Need.", identifier="r2", paper_id="source7", page=9)]
    original = copy.deepcopy((papers, blocks))
    result = citations.citation_graph(papers, blocks)
    assert (papers, blocks) == original
    assert {n["id"] for n in result["nodes"]} == {"source7", "target7"}
    assert len(result["edges"]) == 1
    edge = result["edges"][0]
    assert {key: edge[key] for key in ("source", "target", "confidence")} == {"source": "source7", "target": "target7", "confidence": 1.0}
    assert len(edge["evidence"]) == 2
    first = edge["evidence"][0]
    assert first["source_paper_id"] == "source1" and first["source_version"] == 1
    assert first["linked_paper_id"] == "target1" and first["linked_version"] == 1
    assert first["block_id"] == "r" and first["page"] == 8
    assert first["bbox"] == blocks[0]["bbox"] and first["block_text"] == blocks[0]["text"]
    assert first["match_rule"] == "arxiv_id_and_version_exact"
    assert next(n for n in result["nodes"] if n["id"] == "target7")["citations"] == 1
    assert "局部" in result["scope"]


def test_graph_no_self_family_edges_duplicate_evidence_or_missing_source_ids():
    source = paper("source", family_id="f", arxiv_id="1706.03762")
    refs = [block("[1] Attention Is All You Need. arXiv:1706.03762.")]
    assert citations.citation_graph([source], refs)["edges"] == []
    target = paper(arxiv_id="1706.03762")
    result = citations.citation_graph([paper("source", title="Another source article"), target], refs + refs + [block("[1] Attention Is All You Need.", paper_id="missing")])
    assert len(result["edges"]) == len(result["edges"][0]["evidence"]) == 1
