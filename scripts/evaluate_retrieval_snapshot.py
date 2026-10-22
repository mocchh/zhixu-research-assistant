#!/usr/bin/env python3
"""Run the existing evaluator against an explicitly frozen intelligence module.

Usage: python scripts/evaluate_retrieval_snapshot.py --retriever-snapshot PATH
       --expected-retriever-sha256 SHA [evaluate_retrieval.py arguments]
No workspace retriever module is imported or modified. This supports an isolated
pre-change baseline while another process edits the live implementation.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
from pathlib import Path
import runpy
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--retriever-snapshot", type=Path, required=True)
    parser.add_argument("--expected-retriever-sha256", required=True)
    parser.add_argument("--output", type=Path, help="New report path; existing reports cannot be overwritten")
    options, remaining = parser.parse_known_args()
    if options.output:
        if options.output.exists():
            parser.error("Output already exists; preserve the first run and choose another report path")
        remaining += ["--output", str(options.output)]
    snapshot = options.retriever_snapshot.resolve()
    actual = hashlib.sha256(snapshot.read_bytes()).hexdigest()
    if actual != options.expected_retriever_sha256:
        parser.error("Retriever snapshot SHA256 mismatch; refusing to evaluate")
    sys.path.insert(0, str(ROOT))
    import backend
    name = "backend.intelligence"
    specification = importlib.util.spec_from_file_location(name, snapshot)
    if specification is None or specification.loader is None:
        parser.error("Could not load frozen retriever snapshot")
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    backend.intelligence = module
    sys.argv = [str(ROOT / "scripts/evaluate_retrieval.py"), *remaining]
    runpy.run_path(sys.argv[0], run_name="__main__")


if __name__ == "__main__":
    main()
