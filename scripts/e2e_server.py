"""Isolated real application server and deterministic PDFs for browser tests.

This entry point never reads the user's corpus. The slow fixture only delays
real parser yields, so pause/cancel checkpoints can be exercised reliably.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import socket
import sys
import time

from starlette.requests import Request

ROOT = Path(__file__).resolve().parents[1]


def model_stub_content(body: dict, question_output_rules: str) -> str:
    """Serve a local protocol fixture, including deliberately invalid claims."""
    messages = body["messages"]
    source = messages[-1]["content"]
    if any(message.get("role") == "system" and question_output_rules in message.get("content", "")
           for message in messages):
        # Choose the real retrieved fixture block; never invent a source index.
        match = next((match for match in re.finditer(
            r"SOURCE \[(\d+)\][^\n]*\n(.*?)\nEND SOURCE \[\1\]", source, re.S)
            if "8 NVIDIA P100 GPUs" in match[2] and "12 hours" in match[2]), None)
        if match is None:
            return json.dumps({"claims": [], "insufficient_evidence": True})
        index = int(match[1])
        gpu_count = 999 if body.get("model") == "e2e-incorrect-answer" else 8
        return json.dumps({"claims": [
            {"text": f"The calibration run used {gpu_count} NVIDIA P100 GPUs.", "source_indices": [index]},
            {"text": "The calibration run lasted 12 hours.", "source_indices": [index]},
        ], "insufficient_evidence": False})
    try:
        segments = json.loads(source)
    except ValueError:
        segments = None
    if isinstance(segments, list):
        return json.dumps({"translations": [{"id": item["id"], "text": "测试服务译文：" + item["source"]}
                                             for item in reversed(segments)]}, ensure_ascii=False)
    return "测试服务译文：" + source


def fixtures(directory: Path) -> None:
    import pymupdf

    directory.mkdir()
    pdf = pymupdf.open()
    first = pdf.new_page(width=595, height=842)
    first.insert_text((50, 65), "Deterministic Research Fixture", fontsize=22)
    first.insert_text((50, 100), "Alice Example and Bob Example", fontsize=11)
    first.insert_text((50, 160), "1 Introduction", fontsize=16, fontname="hebo")
    first.insert_textbox(pymupdf.Rect(50, 190, 545, 300),
                         "This synthetic document tests a research reading workspace. "
                         "The checkpoint method follows the study [1]. "
                         "The evaluation measures reliable evidence navigation and saved reading progress.", fontsize=12)
    second = pdf.new_page(width=595, height=842)
    second.insert_text((50, 65), "2 Experiments", fontsize=16, fontname="hebo")
    second.insert_textbox(pymupdf.Rect(50, 110, 545, 240),
                          "A reproducible calibration experiment evaluates checkpoint persistence. "
                          "The procedure records hardware, duration, and batch configuration. "
                          "All reported numbers belong to this synthetic test fixture.", fontsize=12)
    second.insert_textbox(pymupdf.Rect(50, 660, 545, 760),
                          "The calibration run used 8 NVIDIA P100 GPUs for 12 hours. "
                          "The batch size was 64. Checkpoints were written every 30 minutes. "
                          "These measurements verify exact evidence retrieval and page positioning.", fontsize=12)
    third = pdf.new_page(width=595, height=842)
    third.insert_text((50, 65), "References", fontsize=16, fontname="hebo")
    third.insert_textbox(pymupdf.Rect(50, 120, 545, 210),
                         "[1] Alice Example and Bob Example. Checkpoint Control Study. "
                         "Synthetic Research Proceedings, 2024.", fontsize=12)
    pdf.set_metadata({"title": "Deterministic Research Fixture", "author": "Alice Example; Bob Example", "subject": "e2e-native-fixture"})
    pdf.save(directory / "research.pdf")
    pdf.close()
    (directory / "duplicate.pdf").write_bytes((directory / "research.pdf").read_bytes())
    slow = pymupdf.open()
    for number in range(48):
        page = slow.new_page(width=595, height=842)
        page.insert_text((50, 65), "Slow Control Fixture" if number == 0 else f"Checkpoint Page {number + 1}", fontsize=18)
        page.insert_textbox(pymupdf.Rect(50, 140, 545, 300),
                            f"This is synthetic checkpoint page {number + 1}. "
                            "The actual PDF parser stores this text in SQLite. "
                            "A bounded test-only delay allows pause, resume, cancel, and retry actions.", fontsize=12)
    slow.set_metadata({"title": "Slow Control Fixture", "subject": "e2e-slow-control"})
    slow.save(directory / "slow.pdf")
    slow.close()
    (directory / "broken.pdf").write_bytes(b"%PDF-1.7\nThis file deliberately has no valid PDF objects.\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    if not (workspace / ".e2e-isolated").is_file() or workspace == ROOT or ROOT in workspace.parents:
        parser.error("Use a new marked temporary directory outside the project.")
    if args.port in (8000, 5173) or not 0 <= args.port <= 65535:
        parser.error("The production and development ports are forbidden.")
    data = workspace / "data"
    if data.exists():
        parser.error("The temporary database directory must not already exist.")
    fixture_dir = workspace / "fixtures"
    fixtures(fixture_dir)
    # Set these before importing app: its module-level app and dotenv loader must
    # also use the isolated workspace, with external model requests disabled.
    os.environ["RESEARCH_DATA_DIR"] = str(data)
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    os.environ["LLM_API_KEY"] = ""
    os.environ["LLM_BASE_URL"] = "http://127.0.0.1:1/v1"
    os.environ["LLM_MODEL"] = "e2e-extractive-only"
    sys.path.insert(0, str(ROOT))
    import pymupdf
    import uvicorn
    from fastapi.responses import JSONResponse
    from backend import documents
    from backend.answer_contract import QUESTION_OUTPUT_RULES
    from backend.app import create_app

    original_pages = documents.iter_pages

    def controlled_pages(path, *values, **kwargs):
        with documents.PYMUPDF_LOCK, pymupdf.open(path) as document:
            slow = document.metadata.get("subject") == "e2e-slow-control"
        for result in original_pages(path, *values, **kwargs):
            if slow:
                time.sleep(0.18)
            yield result

    documents.iter_pages = controlled_pages
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", args.port))
    port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    app = create_app(data_dir=data, sample_dir=fixture_dir, extra_local_origins=[url])
    production_route_count = len(app.router.routes)

    # Real routes keep the request receive channel intact, so the application's
    # disconnect checks also work while running in this isolated service.
    @app.post("/__e2e__/reset-model")
    async def reset_model():
        app.state.config.path.unlink(missing_ok=True)
        return {"ok": True}

    @app.post("/__e2e__/model/chat/completions")
    async def local_model(request: Request):
        # A local protocol fixture tests the real HTTP client, not model quality.
        if request.headers.get("authorization") != "Bearer e2e-contract-only":
            return JSONResponse({"error": "test credential required"}, status_code=403)
        body = await request.json()
        content = model_stub_content(body, QUESTION_OUTPUT_RULES)
        return {"choices": [{"message": {"content": content}, "finish_reason": "stop"}]}

    # The production app mounts the built UI at '/', which must remain after
    # the test-only endpoints in route matching order.
    app.router.routes[:] = (app.router.routes[production_route_count:]
                           + app.router.routes[:production_route_count])

    class ControlledChatLatency:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            # Only a marked request opts into this bounded delay. Pass receive
            # through unchanged instead of creating a BaseHTTPMiddleware layer.
            if (scope["type"] == "http" and scope["path"] == "/api/chat"
                    and dict(scope.get("headers", [])).get(b"x-e2e-delay") == b"yes"):
                await asyncio.sleep(1.5)
            await self.app(scope, receive, send)

    app.add_middleware(ControlledChatLatency)

    (workspace / "server.json").write_text(json.dumps({"url": url, "fixtures": str(fixture_dir)}), encoding="utf-8")
    try:
        uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")).run(sockets=[sock])
    finally:
        sock.close()


if __name__ == "__main__":
    main()
