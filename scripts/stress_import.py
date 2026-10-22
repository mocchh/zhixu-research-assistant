#!/usr/bin/env python3
"""Exercise real upload, parsing, deduplication and reads in a disposable workspace.

This measures synthetic, native-text PDFs on this machine, not OCR or LLM speed.
No application data, model credentials or network services are used.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import resource
import statistics
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def percentile(values, fraction):
    values = sorted(values)
    return round(values[min(len(values) - 1, max(0, int((len(values) - 1) * fraction)))], 2) if values else None


def make_pdf(index, pages):
    import pymupdf
    with pymupdf.open() as doc:
        for page_number in range(1, pages + 1):
            page = doc.new_page()
            page.insert_text((50, 55), f"Batch Experiment {index:04d}", fontsize=20)
            page.insert_text((50, 80), "Alice Example and Bob Researcher", fontsize=11)
            page.insert_text((50, 110), f"arXiv:2401.{index + 10000:05d}v1", fontsize=10)
            page.insert_text((50, 155), "1 Controlled Experiment", fontsize=14)
            paragraph = (f"ExperimentCode QX{index:04d} uses {index + 1} GPUs and {100 + index} training steps. "
                         f"The physical PDF page is {page_number}. This text is synthetic test data. "
                         "The study records a fixed hardware configuration and deterministic measurements. "
                         "No language model or remote API is needed to parse this paragraph or find its evidence.")
            page.insert_textbox((50, 180, 540, 430), paragraph, fontsize=12)
        return doc.tobytes(garbage=4, deflate=True)


def run(args):
    started = time.perf_counter()
    report = {"kind": "isolated_synthetic_import_stress", "started_at": datetime.now(timezone.utc).isoformat(),
              "configuration": {"papers": args.papers, "pages_each": args.pages, "concurrent_requests": args.concurrency,
                                "batch_size": args.batch_size, "parse_workers": 2},
              "limits": ["Native-text synthetic PDFs only; does not measure OCR or model generation.",
                         "Timing and peak memory depend on the local machine; no production capacity claim.",
                         "Uses the real ASGI application and SQLite through TestClient, not a TCP load generator."],
              "checks": {}, "errors": []}
    try:
        with tempfile.TemporaryDirectory(prefix="research-import-stress-") as directory:
            root = Path(directory)
            # Set before importing app.py, whose module exports a default application.
            os.environ["RESEARCH_DATA_DIR"] = str(root / "data")
            os.environ["LLM_API_KEY"] = ""
            os.environ["PYTHON_DOTENV_DISABLED"] = "1"
            from backend.app import create_app
            from fastapi.testclient import TestClient
            samples = root / "samples"
            samples.mkdir()
            app = create_app(root / "data", samples, run_jobs=True)
            payloads = [make_pdf(i, args.pages) for i in range(args.papers)]
            expected_hashes = {hashlib.sha256(p).hexdigest() for p in payloads}
            report["input_bytes"] = sum(map(len, payloads))
            metrics_lock = threading.Lock()
            latencies = {"upload": [], "duplicate_upload": [], "read": [], "chat": []}
            deadline = time.monotonic() + args.timeout

            def checked(response, expected=200):
                if response.status_code != expected:
                    raise AssertionError(f"HTTP {response.status_code}: {response.text[:400]}")
                return response.json()

            with TestClient(app) as client:
                assert not checked(client.get("/api/health"))["model_configured"]

                def upload(batch, metric):
                    tick = time.perf_counter()
                    result = checked(client.post("/api/papers/upload", files=[
                        ("files", (f"experiment-{i:04d}.pdf", payloads[i], "application/pdf")) for i in batch]))
                    with metrics_lock:
                        latencies[metric].append((time.perf_counter() - tick) * 1000)
                    return result

                batches = [list(range(i, min(i + args.batch_size, args.papers)))
                           for i in range(0, args.papers, args.batch_size)]
                with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
                    results = [future.result() for future in as_completed([pool.submit(upload, b, "upload") for b in batches])]
                imported = [p for result in results for p in result["papers"]]
                assert len(imported) == args.papers
                report["checks"]["all_unique_pdfs_imported"] = True

                while True:
                    papers = checked(client.get("/api/papers"))["papers"]
                    errors = [p for p in papers if p["status"] in {"error", "partial", "cancelled", "paused"}]
                    if errors:
                        raise AssertionError(f"Parsing did not finish cleanly: {[(p['filename'], p['status'], p['error']) for p in errors]}")
                    if len(papers) == args.papers and all(p["status"] == "ready" for p in papers):
                        break
                    if time.monotonic() > deadline:
                        raise TimeoutError(f"Parsing exceeded {args.timeout}s: {sum(p['status'] == 'ready' for p in papers)}/{args.papers} ready")
                    time.sleep(.05)
                report["import_and_parse_seconds"] = round(time.perf_counter() - started, 3)
                report["checks"]["all_pages_parsed"] = all(p["processed_pages"] == args.pages for p in papers)
                assert report["checks"]["all_pages_parsed"]

                # Send every PDF again, and race identical copies within concurrent requests.
                with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
                    futures = [pool.submit(upload, b, "duplicate_upload") for b in batches]
                    futures += [pool.submit(upload, [0], "duplicate_upload") for _ in range(args.concurrency * 2)]
                    duplicate_results = [f.result() for f in as_completed(futures)]
                assert not any(r["papers"] for r in duplicate_results)
                assert sum(len(r["duplicates"]) for r in duplicate_results) == args.papers + args.concurrency * 2
                assert checked(client.get("/api/stats"))["papers"] == args.papers
                report["checks"]["concurrent_duplicates_add_no_rows"] = True

                def read_paper(paper):
                    tick = time.perf_counter()
                    page = checked(client.get(f"/api/papers/{paper['id']}/pages/{args.pages}"))
                    assert page["blocks"] and page["page"] == args.pages
                    with metrics_lock:
                        latencies["read"].append((time.perf_counter() - tick) * 1000)
                    tick = time.perf_counter()
                    answer = checked(client.post("/api/chat", json={"question": "ExperimentCode GPUs training steps", "paper_ids": [paper["id"]]}))
                    assert answer["mode"] == "extractive" and answer["sources"]
                    blocks = {b["id"]: b for b in app.state.store.blocks([paper["id"]])}
                    for source in answer["sources"]:
                        original = blocks[source["id"]]
                        assert source["paper_id"] == paper["id"] and source["text"] == original["text"]
                        assert source["bbox"] == original["bbox"] and source["page"] == original["page"]
                    with metrics_lock:
                        latencies["chat"].append((time.perf_counter() - tick) * 1000)

                with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
                    for f in as_completed([pool.submit(read_paper, p) for p in papers]):
                        f.result()
                report["checks"]["parallel_reads_and_evidence_integrity"] = True

                with app.state.store.connect() as db:
                    assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                    assert not db.execute("PRAGMA foreign_key_check").fetchall()
                    assert db.execute("SELECT count(*) FROM pages").fetchone()[0] == args.papers * args.pages
                    assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == args.papers
                    assert set(r[0] for r in db.execute("SELECT sha256 FROM papers")) == expected_hashes
                    assert set(r[0] for r in db.execute("SELECT status FROM jobs")) == {"done"}
                    paths = [Path(r[0]) for r in db.execute("SELECT path FROM papers")]
                assert set(paths) == set(app.state.store.files.glob("*.pdf"))
                assert {hashlib.sha256(p.read_bytes()).hexdigest() for p in paths} == expected_hashes
                report["checks"]["sqlite_integrity_and_file_hashes"] = True
                report["checks"]["no_orphan_files_or_duplicate_jobs"] = True
                report["final_stats"] = checked(client.get("/api/stats"))
                report["workspace_bytes"] = sum(p.stat().st_size for p in root.rglob("*") if p.is_file())
            report["latency_ms"] = {name: {"count": len(v), "median": round(statistics.median(v), 2),
                                           "p95": percentile(v, .95), "max": round(max(v), 2)} for name, v in latencies.items() if v}
        report["checks"]["temporary_workspace_removed"] = not root.exists()
    except Exception as exc:
        report["errors"].append(f"{type(exc).__name__}: {exc}")
    report["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    report["process_peak_rss_mib"] = round(rss / (1024 ** 2 if sys.platform == "darwin" else 1024), 2)
    report["passed"] = not report["errors"] and all(report["checks"].values())
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--papers", type=int, default=60)
    parser.add_argument("--pages", type=int, default=4)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--output", type=Path, default=ROOT / "test-results/import-stress.json")
    args = parser.parse_args()
    if not (1 <= args.papers <= 500 and 1 <= args.pages <= 50 and 1 <= args.concurrency <= 16
            and 1 <= args.batch_size <= 30 and 1 <= args.timeout <= 1800):
        parser.error("papers 1..500, pages 1..50, concurrency 1..16, batch-size 1..30, timeout 1..1800")
    report = run(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
