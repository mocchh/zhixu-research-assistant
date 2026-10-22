"""Versioned table matching must preserve the actual row and column relation."""
from copy import deepcopy

import pytest

from scripts import evaluate_retrieval as subject
from tests.test_retrieval_evaluation import frozen_holdout_report


def source(text, kind="table", page=2):
    return {"filename": "synthetic.pdf", "page": page, "kind": kind, "text": text}


def target(**extra):
    return {"filename": "synthetic.pdf", "page": 2, **extra}


def test_strict_protocol_stays_unchanged_and_v2_accepts_cell_separators():
    block = source("Group | A | B\n64 | 31.4 | 16.6\n128 | 30.7 | 15.8")
    gold = target(terms=["64 31.4", "31.4 16.6"])
    assert not subject.matches(block, gold)
    assert subject.matches(block, gold, "table_cells_v2")


@pytest.mark.parametrize("text", [
    "64 | 31.4\n16.6 | 1.88",  # cannot combine separate rows
    "164 | 31.45 | 16.6",      # numeric substring is not equality
    "64 | 16.6 | 31.4",        # wrong column order
    "64 | 31.4 | 0 | 16.6",    # a missing cell cannot be skipped
])
def test_v2_does_not_assemble_numeric_rows(text):
    assert not subject.matches(source(text), target(terms=["64 31.4 16.6"]), "table_cells_v2")


def test_v2_does_not_strip_bars_from_prose_or_move_evidence_between_pages():
    gold = target(terms=["64 31.4"])
    assert not subject.matches(source("64 | 31.4", kind="paragraph"), gold, "table_cells_v2")
    assert not subject.matches(source("64 | 31.4", page=3), gold, "table_cells_v2")


def test_explicit_table_cells_require_correct_row_column_and_header():
    gold = target(terms=["Latency"], table_row={"start_column": 0, "cells": ["small", "64", "31.4"]})
    block = source("Model | Size | Latency\nsmall | 64 | 31.4\nbig | 128 | 60.2")
    assert subject.matches(block, gold, "table_cells_v2")
    assert not subject.matches(block, gold, "strict_v1")
    assert not subject.matches(source("Model | Size | Accuracy\nsmall | 64 | 31.4"), gold, "table_cells_v2")
    assert not subject.matches(source("Model | Size | Latency\nlabel | small | 64 | 31.4"), gold, "table_cells_v2")
    assert not subject.matches(source("Model | Size | Latency\nsmall | 128 | 60.2\nbig | 64 | 31.4"), gold, "table_cells_v2")


def test_baseline_refuses_comparison_between_scoring_versions():
    baseline = frozen_holdout_report()
    result = deepcopy(baseline)
    result["match_protocol"] = "table_cells_v2"
    checked = subject.compare_baseline(result, baseline)
    assert checked["regression_check"] == "failed"
    assert any("match_protocol" in item for item in checked["regressions"])
