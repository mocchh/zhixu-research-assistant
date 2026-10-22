"""HTTP interface for the local, single-user research workspace."""
from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import sqlite3
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Literal

import httpx
import pymupdf
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import documents, intelligence
from .config import ModelConfig
from .storage import Store, TaskConflict, uid
from .tasks import TaskRunner, page_text, translate_long_text
from .document_routes import create_document_router
from .translation_routes import create_translation_router
from .request_control import LocalWriteGuard, while_connected
from .workspace_routes import create_workspace_router
from .workspace_lock import WorkspaceLock

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
MAX_UPLOAD = 100 * 1024 * 1024
DEFAULT_SAMPLES = ["1706.03762v7.pdf", "1706.03762v1.pdf", "N19-1423.pdf", "2201.11903v6.pdf", "2005.14165v4.pdf"]


class PaperPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=500)
    is_favorite: bool | None = None
    tags: list[str] | None = Field(default=None, max_length=20)


class SampleImport(BaseModel):
    filenames: list[str] | None = Field(default=None, max_length=100)


class HistoryItem(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=12000)


class ChatInput(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    paper_ids: list[str] = Field(default_factory=list, max_length=100)
    history: list[HistoryItem] = Field(default_factory=list, max_length=20)


class TranslateInput(BaseModel):
    paper_id: str
    page: int | None = Field(default=None, ge=1)
    text: str | None = Field(default=None, max_length=30000)
    target_language: Literal["zh", "en"] = "zh"


class FullTranslation(BaseModel):
    target_language: Literal["zh", "en"] = "zh"
    start_page: int = Field(default=1, ge=1)
    end_page: int | None = Field(default=None, ge=1)


class ResearchInput(BaseModel):
    paper_ids: list[str] = Field(default_factory=list, max_length=100)
    task: Literal["summary", "review", "ideas", "outline"]
    idea: str = Field(default="", max_length=6000)


class ImportArxiv(BaseModel):
    arxiv_id: str = Field(min_length=1, max_length=80)


class SettingsInput(BaseModel):
    model: str = Field(min_length=1, max_length=200)
    base_url: str = Field(min_length=1, max_length=1000)
    api_key: str | None = Field(default=None, max_length=2000)
    clear_api_key: bool = False


def create_app(data_dir: Path | None = None, sample_dir: Path | None = None, run_jobs=True,
               extra_local_origins: list[str] | None = None):
    directory = data_dir or Path(os.getenv("RESEARCH_DATA_DIR", str(ROOT / "data")))
    workspace_lock = WorkspaceLock(directory)
    # Reject duplicate startup before schema initialization can touch a live
    # workspace. Module import alone does not reserve an unused workspace.
    with workspace_lock:
        store = Store(directory)
    config = ModelConfig(store.directory)
    runner = TaskRunner(store, config)
    samples = (sample_dir or ROOT / "pdf").resolve()

    @asynccontextmanager
    async def lifespan(app):
        with workspace_lock:
            try:
                if run_jobs:
                    runner.recover()
                yield
            finally:
                runner.close()
                # An in-flight worker may still commit its final checkpoint.
                # Keep ownership until it stops, even if shutdown is cancelled.
                joined = asyncio.create_task(asyncio.to_thread(runner.pool.shutdown, wait=True))
                try:
                    await asyncio.shield(joined)
                except asyncio.CancelledError:
                    await joined
                    raise

    app = FastAPI(title="知序 · 科研助手", version="0.1.0", lifespan=lifespan)
    app.state.store = store
    app.state.runner = runner
    app.state.config = config
    local_origins = [f"http://{host}:{port}" for host in ("localhost", "127.0.0.1") for port in (5173, 8000)]
    # Isolated test servers may use another loopback port without weakening the
    # default application's protection against writes from other websites.
    if extra_local_origins:
        from urllib.parse import urlsplit
        for origin in extra_local_origins:
            parsed = urlsplit(origin)
            if (parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1"}
                    or parsed.username or parsed.password or parsed.path or parsed.query
                    or parsed.fragment or not parsed.port):
                raise ValueError("额外来源必须是带端口的本机 HTTP 地址")
        local_origins.extend(extra_local_origins)
    app.add_middleware(CORSMiddleware, allow_origins=local_origins, allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"], allow_headers=["Content-Type"])
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "testserver"])

    app.add_middleware(LocalWriteGuard, allowed_origins=local_origins)

    @app.exception_handler(sqlite3.OperationalError)
    async def database_unavailable(request, exc):
        return JSONResponse({"detail": "本地数据库暂时不可用，请检查磁盘空间与权限，稍后重试。"}, status_code=503)

    @app.exception_handler(intelligence.ConfigError)
    async def config_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=503)

    @app.exception_handler(intelligence.UpstreamError)
    async def upstream_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=502)

    def get_paper(paper_id, private=False):
        paper = store.paper(paper_id, private=private)
        if not paper:
            raise HTTPException(404, "未找到该文献")
        return paper

    def select_papers(ids):
        if ids:
            papers = [get_paper(x) for x in dict.fromkeys(ids)]
        else:
            # Default to the newest usable version of each work, while explicit IDs remain exact.
            papers = []
            families = set()
            for p in sorted(store.papers(), key=lambda p: p["version"] or 0, reverse=True):
                if p["status"] in {"ready", "partial"} and p["family_id"] not in families:
                    papers.append(p)
                    families.add(p["family_id"])
        if not papers:
            raise HTTPException(400, "请先导入并解析至少一篇论文")
        if any(p["status"] not in {"ready", "partial"} for p in papers):
            raise HTTPException(409, "所选文献仍在解析或解析失败，请等待完成或重试")
        return papers, store.blocks([p["id"] for p in papers])

    def add_file(path, filename, source):
        # Called with an application-owned staging file. Original samples are never removed.
        committed = False
        try:
            if path.stat().st_size > MAX_UPLOAD:
                raise HTTPException(413, "每份 PDF 最大支持 100 MB")
            with path.open("rb") as f:
                prefix = f.read(1024)
                if b"%PDF-" not in prefix:
                    raise HTTPException(400, f"{filename} 不是有效 PDF")
            with path.open("rb") as f:
                sha = hashlib.file_digest(f, "sha256").hexdigest()
            existing = store.by_sha(sha)
            if existing:
                path.unlink(missing_ok=True)
                return None, {"filename": filename, "paper_id": existing["id"], "title": existing["title"]}
            try:
                with documents.PYMUPDF_LOCK, pymupdf.open(path) as doc:
                    if doc.needs_pass:
                        raise HTTPException(400, "暂不支持加密 PDF，请先解除密码后上传")
                    count = len(doc)
                    if count < 1 or count > documents.MAX_PAGES:
                        raise HTTPException(400, f"PDF 页数必须在 1–{documents.MAX_PAGES} 页之间")
            except HTTPException:
                raise
            except Exception as exc:
                raise HTTPException(400, "PDF 文件损坏或无法打开") from exc
            try:
                paper, job = store.create_import(filename, path, sha, source, count)
            except sqlite3.IntegrityError:
                existing = store.by_sha(sha)
                path.unlink(missing_ok=True)
                if existing:
                    return None, {"filename": filename, "paper_id": existing["id"], "title": existing["title"]}
                raise
            committed = True
            if run_jobs:
                try:
                    if not runner.submit(job["id"], "parse", paper["id"]):
                        raise RuntimeError("parse dispatch unavailable")
                except Exception:
                    # The durable import already succeeded. Never remove its
                    # PDF because the in-memory executor could not start it.
                    with suppress(Exception):
                        store.fail_queued_import(job["id"])
                        paper = store.paper(paper["id"]) or paper
            return paper, None
        except Exception:
            if not committed:
                path.unlink(missing_ok=True)
            raise

    def import_error(filename, exc):
        status = exc.status_code if isinstance(exc, HTTPException) else 500
        detail = str(exc.detail) if isinstance(exc, HTTPException) else "导入未完成，请重试；若仍失败，请检查本地磁盘与数据库"
        return {"filename": filename, "status": status, "detail": detail}

    def import_response(papers, duplicates, errors):
        result = {"papers": papers, "duplicates": duplicates, "errors": errors,
                  "status": "partial" if errors and (papers or duplicates) else "failed" if errors else "ok"}
        if not errors:
            return result
        result["detail"] = "；".join(f"{item['filename']}：{item['detail']}" for item in errors)
        if papers or duplicates:
            status = 207
        else:
            codes = {item["status"] for item in errors}
            status = 500 if any(code >= 500 for code in codes) else next(iter(codes)) if len(codes) == 1 else 400
        return JSONResponse(result, status_code=status)

    @app.get("/api/health")
    def health():
        return {"status": "ok", "model_configured": config.public()["has_api_key"], "model": config.public()["model"], "parser": "PyMuPDF + OCR", "parser_version": getattr(documents, "PARSER_VERSION", "legacy"), "ocr_available": documents.get_ocr_status()["available"]}

    @app.get("/api/stats")
    def stats():
        return store.stats()

    @app.get("/api/papers")
    def list_papers(q: str = "", status: str | None = None, favorite: bool = False):
        return {"papers": store.papers(q=q, status=status, favorite=favorite)}

    @app.post("/api/papers/upload")
    async def upload(files: list[UploadFile] = File(...)):
        if len(files) > 30:
            raise HTTPException(400, "一次最多上传 30 份 PDF")
        papers, duplicates, errors = [], [], []
        for file in files:
            name = Path((file.filename or "document.pdf").replace("\\", "/")).name[:250]
            path = store.files / f"{uid()}.pdf"
            handed_off = False
            try:
                if not name.lower().endswith(".pdf"):
                    raise HTTPException(400, "仅支持 PDF 文件")
                size = 0
                with path.open("wb") as output:
                    while chunk := await file.read(1024 * 1024):
                        size += len(chunk)
                        if size > MAX_UPLOAD:
                            raise HTTPException(413, "每份 PDF 最大支持 100 MB")
                        output.write(chunk)
                handed_off = True
                p, duplicate = await asyncio.to_thread(add_file, path, name, "upload")
                if p:
                    papers.append(p)
                if duplicate:
                    duplicates.append(duplicate)
            except Exception as exc:
                errors.append(import_error(name, exc))
            finally:
                if not handed_off:
                    path.unlink(missing_ok=True)
                await file.close()
        return import_response(papers, duplicates, errors)

    @app.get("/api/samples")
    def sample_files():
        return {"files": [{"filename": p.name, "size": p.stat().st_size} for p in sorted(samples.glob("*.pdf")) if p.is_file()]}

    @app.post("/api/samples/import")
    def import_samples(body: SampleImport):
        names = body.filenames if body.filenames is not None else DEFAULT_SAMPLES
        papers, duplicates, errors = [], [], []
        for name in names:
            dest = store.files / f"{uid()}.pdf"
            handed_off = False
            try:
                source = (samples / name).resolve()
                if source.parent != samples or source.suffix.lower() != ".pdf" or not source.is_file():
                    raise HTTPException(400, f"无效的样例文件：{name}")
                shutil.copyfile(source, dest)
                handed_off = True
                p, duplicate = add_file(dest, source.name, "sample")
                if p:
                    papers.append(p)
                if duplicate:
                    duplicates.append(duplicate)
            except Exception as exc:
                errors.append(import_error(name, exc))
            finally:
                if not handed_off:
                    dest.unlink(missing_ok=True)
        return import_response(papers, duplicates, errors)

    @app.get("/api/papers/{paper_id}")
    def paper_detail(paper_id: str):
        paper = get_paper(paper_id)
        blocks = store.blocks([paper_id])
        return {"paper": paper, "sections": documents.sections_from_blocks(blocks), "versions": store.versions(paper["family_id"])}

    @app.patch("/api/papers/{paper_id}")
    def patch_paper(paper_id: str, body: PaperPatch):
        get_paper(paper_id)
        fields = body.model_dump(exclude_none=True)
        if "title" in fields:
            fields["title"] = fields["title"].strip()
            if not fields["title"]:
                raise HTTPException(400, "标题不能为空")
            fields["title_custom"] = True
        if "tags" in fields:
            fields["tags"] = list(dict.fromkeys(t.strip()[:50] for t in fields["tags"] if t.strip()))
            fields["tags_custom"] = True
        return store.update_paper(paper_id, **fields)

    @app.delete("/api/papers/{paper_id}")
    def delete_paper(paper_id: str):
        paper = get_paper(paper_id, private=True)
        store.delete_paper(paper_id)
        path = Path(paper["path"]).resolve()
        if path.parent == store.files:
            path.unlink(missing_ok=True)
        return {"ok": True}

    @app.post("/api/papers/{paper_id}/retry")
    def retry(paper_id: str):
        paper = get_paper(paper_id)
        active = [j for j in store.jobs(limit=10000) if j["paper_id"] == paper_id and j["kind"] == "parse" and j["status"] in {"queued", "processing", "paused", "pause_requested", "cancel_requested"}]
        if paper["status"] in {"queued", "processing", "paused", "pause_requested", "cancel_requested"} or active:
            raise HTTPException(409, "该文献已有解析任务，请在任务面板继续或取消")
        if paper["status"] == "partial":
            # Re-run empty pages after an OCR engine becomes available.
            with store.connect() as db:
                db.execute("DELETE FROM pages WHERE paper_id=? AND NOT EXISTS(SELECT 1 FROM blocks WHERE blocks.paper_id=pages.paper_id AND blocks.page=pages.page)", (paper_id,))
        store.update_paper(paper_id, status="queued", error=None)
        job = store.ensure_job("parse", paper_id)["id"]
        if run_jobs:
            runner.submit(job, "parse", paper_id)
        return {"job_id": job}

    @app.post("/api/papers/{paper_id}/reparse")
    def reparse(paper_id: str):
        try:
            job = store.create_reparse(paper_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc.args[0])) from exc
        except TaskConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        if run_jobs:
            runner.submit(job["id"], "parse", paper_id)
        return {"job_id": job["id"]}

    @app.get("/api/papers/{paper_id}/file")
    def pdf_file(paper_id: str):
        paper = get_paper(paper_id, private=True)
        return FileResponse(paper["path"], media_type="application/pdf", filename=paper["filename"], content_disposition_type="inline")

    @app.get("/api/papers/{paper_id}/pages/{page}")
    def get_page(paper_id: str, page: int, target_language: Literal["zh", "en"] = "zh"):
        paper = get_paper(paper_id)
        if not 1 <= page <= paper["page_count"]:
            raise HTTPException(404, "页码超出范围")
        result = store.page(paper_id, page)
        if not result:
            raise HTTPException(409, "该页尚未解析，请稍后重试")
        result["translation"] = store.translation(paper_id, page, target_language, page_text(result))
        return result

    @app.get("/api/papers/{paper_id}/pages/{page}/image")
    def page_image(paper_id: str, page: int, scale: float = 1.5):
        paper = get_paper(paper_id, private=True)
        if not 1 <= page <= paper["page_count"]:
            raise HTTPException(404, "页码超出范围")
        png = documents.render_page(Path(paper["path"]), page, max(0.5, min(scale, 2.5)))
        return Response(png, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})

    @app.get("/api/papers/{paper_id}/text")
    def export_text(paper_id: str):
        paper = get_paper(paper_id)
        blocks = store.blocks([paper_id])
        text = paper["title"] + "\n\n" + "\n\n".join(f"[PDF 第 {b['page']} 页]\n{b['text']}" for b in blocks)
        return Response(text, media_type="text/plain; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="{paper_id}.txt"'})

    @app.get("/api/jobs")
    def jobs(limit: int = Query(20, ge=1, le=100), cursor: str | None = Query(None, max_length=300)):
        try:
            return store.job_listing(limit, cursor)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/chat")
    async def chat(body: ChatInput, request: Request):
        if not body.question.strip():
            raise HTTPException(400, "请输入问题")
        papers, blocks = select_papers(body.paper_ids)
        return await while_connected(request, intelligence.answer_question(body.question, papers, blocks, [m.model_dump() for m in body.history], config.get()))

    @app.post("/api/translate")
    async def translate(body: TranslateInput, request: Request):
        paper = get_paper(body.paper_id)
        if not config.get()["api_key"]:
            raise intelligence.ConfigError("请先在模型设置中配置 API 密钥，再使用翻译")
        source = body.text
        cache_page = body.page is not None and body.text is None
        if source is None:
            if body.page is None or not 1 <= body.page <= paper["page_count"]:
                raise HTTPException(400, "请指定有效页码或要翻译的文字")
            page = store.page(body.paper_id, body.page)
            if page is None:
                raise HTTPException(409, "该页尚未解析")
            source = page_text(page)
        if not source.strip():
            raise HTTPException(400, "没有可翻译的文本，该页可能需要 OCR")
        if cache_page:
            cached = store.translation(body.paper_id, body.page, body.target_language, source)
            if cached:
                return {"translation": cached, "mode": "model", "cached": True}
        result = await while_connected(request, translate_long_text(source, body.target_language, config.get()))
        if cache_page:
            if not store.save_translation(body.paper_id, body.page, body.target_language, source, result):
                raise HTTPException(409, "翻译期间原文已重新解析或删除，结果未保存，请刷新后重试")
        return {"translation": result, "mode": "model", "cached": False}

    @app.post("/api/papers/{paper_id}/translate")
    def translate_document(paper_id: str, body: FullTranslation):
        get_paper(paper_id)
        try:
            job = runner.start_translation(paper_id, body.target_language, body.start_page, body.end_page, run=run_jobs)
        except intelligence.ConfigError:
            raise
        except TaskConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(404, str(exc.args[0])) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"job_id": job["id"]}

    @app.get("/api/graph")
    def graph():
        papers = [p for p in store.papers() if p["status"] in {"ready", "partial"}]
        return intelligence.graph(papers, store.blocks([p["id"] for p in papers]))

    @app.post("/api/research")
    async def research(body: ResearchInput, request: Request):
        papers, blocks = select_papers(body.paper_ids)
        try:
            return await while_connected(request, intelligence.research(body.task, body.idea, papers, blocks, config.get()))
        except intelligence.ConfigError:
            raise
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/charts")
    async def charts(file: UploadFile = File(...), kind: str = Form("line"), x: str | None = Form(None), y: str | None = Form(None)):
        from .charts import render_csv
        try:
            content = await file.read(2 * 1024 * 1024 + 1)
            if len(content) > 2 * 1024 * 1024:
                raise HTTPException(413, "CSV 文件不能超过 2 MB")
            return await asyncio.to_thread(render_csv, content, kind, x, y)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        finally:
            await file.close()

    @app.get("/api/discover")
    async def discover(q: str, limit: int = 8):
        if not q.strip() or len(q) > 500:
            raise HTTPException(400, "请输入 1–500 字符的检索词")
        result = await intelligence.discover(q, max(1, min(limit, 20)))
        return result if isinstance(result, dict) else {"papers": result}

    @app.post("/api/discover/import")
    async def discover_import(body: ImportArxiv):
        try:
            arxiv_id = intelligence.validate_arxiv_id(body.arxiv_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        dest = store.files / f"{uid()}.pdf"
        handed_off = False
        try:
            await intelligence.fetch_arxiv_pdf(arxiv_id, dest)
            handed_off = True
            paper, duplicate = await asyncio.to_thread(add_file, dest, arxiv_id.replace("/", "_") + ".pdf", "arxiv")
        except Exception:
            if not handed_off:
                dest.unlink(missing_ok=True)
            raise
        return {"papers": [paper] if paper else [], "duplicates": [duplicate] if duplicate else []}

    @app.get("/api/settings")
    def settings():
        return config.public()

    @app.put("/api/settings")
    def put_settings(body: SettingsInput):
        try:
            return config.save(body.model, body.base_url, body.api_key, clear_api_key=body.clear_api_key)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/settings/test")
    async def test_settings():
        values = config.get()
        if not values["api_key"]:
            raise intelligence.ConfigError("请先保存 API 密钥")
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
                base = values["base_url"].rstrip("/")
                endpoint = base if base.endswith("/chat/completions") else base + "/chat/completions"
                response = await client.post(endpoint, headers={"Authorization": "Bearer " + values["api_key"]}, json={"model": values["model"], "messages": [{"role": "user", "content": "Reply with OK."}], "max_tokens": 8})
            if response.status_code != 200:
                raise HTTPException(502, f"模型服务返回 HTTP {response.status_code}，请检查地址、模型名与密钥")
            data = response.json()
            if not data.get("choices"):
                raise HTTPException(502, "模型服务未返回兼容的回答结构")
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(502, "无法连接模型服务，请检查地址和网络") from exc
        return {"ok": True, "message": "模型连接成功"}

    app.include_router(create_document_router(store))
    app.include_router(create_translation_router(store, config))
    app.include_router(create_workspace_router(store, runner, config, run_jobs=run_jobs))
    dist = ROOT / "frontend" / "dist"
    if dist.is_dir():
        app.mount("/", StaticFiles(directory=dist, html=True), name="frontend")
    else:
        @app.get("/")
        def index():
            return {"message": "前端开发地址 http://localhost:5173；构建前端后可从此地址使用完整应用。", "docs": "/docs"}
    return app


app = create_app()
