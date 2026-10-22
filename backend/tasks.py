"""Small durable task runner for a single-process local installation."""
from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import documents, intelligence
from .storage import TaskConflict


def page_text(page):
    return "\n\n".join(block["text"] for block in page["blocks"])


def translation_chunks(text):
    # Preserve every character; split at paragraph boundaries where possible.
    chunks = []
    while len(text) > 6500:
        split = text.rfind("\n", 0, 6500)
        if split < 3000:
            split = 6500
        chunks.append(text[:split])
        text = text[split:]
    if text:
        chunks.append(text)
    return chunks


async def translate_long_text(text, language, settings):
    chunks = translation_chunks(text)
    return "\n\n".join([await intelligence.translate_text(chunk, language, settings) for chunk in chunks])


class TaskRunner:
    def __init__(self, store, config):
        self.store = store
        self.config = config
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="research-task")
        self.stopping = threading.Event()
        self.lock = threading.Lock()
        self.active = {}
        self.pending = {}
        self.recovered = False

    def recover(self):
        with self.lock:
            if self.recovered:
                return
            self.recovered = True
        self.store.cleanup_interrupted_imports()
        # A requested pause/cancel survives a crash. Only queued work restarts.
        for existing in reversed(self.store.jobs(limit=-1)):
            job = self.store.recover_job(existing["id"])
            if job and job["status"] == "queued" and self.store.paper(job["paper_id"]):
                self.submit(job["id"], job["kind"], job["paper_id"], job["options"])

    def submit(self, job_id, kind, paper_id, options=None):
        job = self.store.job(job_id)
        if not job or job["status"] != "queued" or self.stopping.is_set():
            return False
        if job["kind"] != kind or job["paper_id"] != paper_id:
            raise ValueError("任务类型或文献不匹配")
        with self.lock:
            key = (kind, paper_id)
            if key in self.active:
                if self.active[key] == job_id:
                    # A resume may arrive after a checkpoint but before finally.
                    self.pending[key] = (job_id, kind, paper_id, options)
                    return True
                previous = self.store.job(self.active[key])
                if previous and previous["status"] in {"paused", "cancelled", "error", "done"}:
                    self.pending[key] = (job_id, kind, paper_id, options)
                    return True
                self.store.update_job(job_id, status="cancelled", message="已有同类任务，重复任务已取消")
                return False
            self.active[key] = job_id
        try:
            self.pool.submit(self._execute, job_id, kind, paper_id, options or {})
        except RuntimeError:
            with self.lock:
                self.active.pop(key, None)
            return False
        return True

    def control(self, job_id, action, run=True):
        with self.lock:
            existing = self.store.job(job_id)
            if existing and (existing['kind'], existing['paper_id']) not in self.active:
                # A worker may have exited while storage was unavailable and
                # could not persist its final state. Recover only unowned work.
                self.store.recover_job(job_id)
            job = self.store.control_job(job_id, action)
        if action == "resume" and job["status"] == "queued" and run:
            self.submit(job["id"], job["kind"], job["paper_id"], job["options"])
        return self.store.job(job_id)

    def _boundary(self, job_id):
        return self.store.settle_job(job_id, shutdown=self.stopping.is_set())

    def _execute(self, job_id, kind, paper_id, options):
        key = (kind, paper_id)
        with self.lock:
            if key in self.active and self.active[key] != job_id:
                return
            # Direct executions used by recovery checks own their slot too.
            self.active[key] = job_id
        try:
            if not self.store.start_job(job_id):
                return
            saved = self.store.job(job_id)
            options = saved["options"] or options
            if kind == "parse":
                self._parse(job_id, paper_id)
            elif kind == "translate":
                self._translate(job_id, paper_id, options.get("target_language", "zh"),
                                options.get("start_page", 1), options.get("end_page"))
            else:
                raise ValueError("不支持的任务类型")
        except Exception as exc:
            # Provider clients redact upstream messages. Never log raw responses.
            if not self._boundary(job_id):
                self.store.fail_job(job_id, str(exc)[:500] or "任务处理失败，请重试")
        finally:
            # Storage itself can fail (full disk, lock, revoked permission).
            # Always release in-memory ownership so a later retry can proceed.
            key = (kind, paper_id)
            try:
                self._boundary(job_id)
            finally:
                with self.lock:
                    if self.active.get(key) == job_id:
                        self.active.pop(key, None)
                    pending = self.pending.pop(key, None)
            if not self.stopping.is_set():
                if pending:
                    self.submit(*pending)
                else:
                    saved = self.store.job(job_id)
                    if saved and saved["status"] == "queued":
                        self.submit(job_id, kind, paper_id, saved["options"])

    def _parse(self, job_id, paper_id):
        if self._boundary(job_id):
            return
        paper = self.store.paper(paper_id, private=True)
        if paper is None:
            return
        if not self.store.prepare_parser(paper_id, job_id, getattr(documents, "PARSER_VERSION", "legacy")):
            return
        self.store.update_job(job_id, message="识别文献与版本")
        meta = documents.inspect_pdf(Path(paper["path"]), paper["filename"])
        if self._boundary(job_id):
            return
        fields = {k: v for k, v in meta.items() if k in {"title", "authors", "abstract", "arxiv_id", "version", "page_count", "tags", "warnings"}}
        # The custom-field and task-state checks share the metadata write's
        # transaction: a user edit after inspection must win over extraction.
        if not self.store.apply_extracted_metadata(paper_id, job_id, fields):
            return
        self.store.reconcile_family(paper_id, job_id=job_id)
        warnings = list(meta.get("warnings", []))
        with self.store.connect() as db:
            for stored in db.execute("SELECT page,warnings FROM pages WHERE paper_id=?", (paper_id,)):
                warnings.extend(f"第 {stored['page']} 页：{w}" for w in json.loads(stored["warnings"]))
        warnings = list(dict.fromkeys(warnings))
        completed = self.store.completed_pages(paper_id)
        first_missing = next((n for n in range(1, meta["page_count"] + 1) if n not in completed), meta["page_count"] + 1)
        iterator = documents.iter_pages(Path(paper["path"]), start_page=first_missing)
        try:
            while not self._boundary(job_id):
                try:
                    page = next(iterator)
                except StopIteration:
                    break
                if page["page"] not in completed:
                    # A requested pause/cancel lets the in-flight page commit.
                    # A deletion or terminal cancellation cannot write it back.
                    if not self.store.add_page(paper_id, page, job_id=job_id):
                        return
                    completed.add(page["page"])
                for warning in page.get("warnings", []):
                    item = f"第 {page['page']} 页：{warning}"
                    if item not in warnings:
                        warnings.append(item)
                self.store.update_paper(paper_id, warnings=warnings)
                self.store.checkpoint_job(job_id, page["page"], round(len(completed) / meta["page_count"] * 100, 1),
                                          f"已解析 {len(completed)} / {meta['page_count']} 页", processed_pages=len(completed))
        finally:
            if hasattr(iterator, "close"):
                iterator.close()
        if self._boundary(job_id):
            return
        final = self.store.paper(paper_id)
        if not final:
            return
        with self.store.connect() as db:
            empty_warned = db.execute("""SELECT COUNT(*) FROM pages p WHERE paper_id=? AND warnings != '[]'
                AND NOT EXISTS(SELECT 1 FROM blocks b WHERE b.paper_id=p.paper_id AND b.page=p.page)""", (paper_id,)).fetchone()[0]
        incomplete = final["text_chars"] == 0 or empty_warned > 0 or final["processed_pages"] < final["page_count"]
        status = "partial" if incomplete else "ready"
        self.store.finish_job(job_id, "解析完成，部分页面需检查" if incomplete else "解析完成，可阅读和检索",
                              {"pages": final["processed_pages"], "status": status}, paper_status=status, warnings=warnings)

    def translation_estimate(self, paper_id, language="zh", start_page=1, end_page=None):
        paper = self.store.paper(paper_id)
        if paper is None:
            raise KeyError("未找到该文献")
        end_page = paper["page_count"] if end_page is None else end_page
        if language not in {"zh", "en"} or not 1 <= start_page <= end_page <= paper["page_count"]:
            raise ValueError("翻译语言或页码范围无效")
        total, pending, cached, blank, ascii_chars, non_ascii = 0, 0, 0, 0, 0, 0
        for number in range(start_page, end_page + 1):
            page = self.store.page(paper_id, number)
            if page is None:
                raise ValueError(f"第 {number} 页尚未解析")
            text = page_text(page)
            total += len(text)
            if not text.strip():
                if page.get("warnings"):
                    raise ValueError(f"第 {number} 页解析未获得正文且有警告，请检查 OCR 后再翻译")
                blank += 1
            elif self.store.translation(paper_id, number, language, text) is not None:
                cached += 1
            else:
                for index, chunk in enumerate(translation_chunks(text)):
                    if self.store.translation_chunk(paper_id, number, language, text, index) is None:
                        pending += len(chunk)
                        ascii_chars += sum(ord(c) < 128 for c in chunk)
                        non_ascii += sum(ord(c) >= 128 for c in chunk)
        import math
        return {"paper_id": paper_id, "target_language": language, "start_page": start_page, "end_page": end_page,
                "pages": end_page-start_page+1, "total_characters": total, "pending_characters": pending,
                "estimated_input_tokens": math.ceil(ascii_chars/4 + non_ascii), "cached_pages": cached, "blank_pages": blank,
                "warning": "Token 数按待翻译正文粗估，未含提示词和输出；实际用量取决于模型分词与回复长度，不代表费用。"}

    def start_translation(self, paper_id, language="zh", start_page=1, end_page=None, run=True):
        """Validate once and share deduplication across both translation routes.

        KeyError means missing paper, ValueError invalid range or unparsed page,
        TaskConflict incompatible active job, ConfigError missing provider key.
        """
        estimate = self.translation_estimate(paper_id, language, start_page, end_page)
        if not self.config.get()["api_key"]:
            raise intelligence.ConfigError("请先在模型设置中配置 API 密钥，再使用翻译")
        options = {key: estimate[key] for key in ("target_language", "start_page", "end_page")}
        job = self.store.ensure_job("translate", paper_id, options)
        saved = job["options"] or job["result"]
        paper = self.store.paper(paper_id)
        if paper is None:
            raise KeyError("未找到该文献")
        existing_options = {"target_language": saved.get("target_language", "zh"),
                            "start_page": saved.get("start_page", 1),
                            "end_page": saved.get("end_page") or paper["page_count"]}
        if existing_options != options:
            raise TaskConflict("该文献已有另一语言或页码范围的翻译任务，请完成或取消后重试")
        if job["status"] == "cancel_requested":
            raise TaskConflict("翻译任务正在取消，请等待当前分块保存后重试")
        if run and job["status"] == "queued":
            self.submit(job["id"], "translate", paper_id, options)
        return self.store.job(job["id"])

    def _translate(self, job_id, paper_id, language, start_page=1, end_page=None):
        if self._boundary(job_id):
            return
        settings = self.config.get()
        if not settings["api_key"]:
            raise intelligence.ConfigError("请先在模型设置中配置 API 密钥，再使用翻译")
        paper = self.store.paper(paper_id)
        if paper is None:
            return
        end_page = paper["page_count"] if end_page is None else end_page
        if language not in {"zh", "en"} or not 1 <= start_page <= end_page <= paper["page_count"]:
            raise ValueError("翻译语言或页码范围无效")
        self.store.update_job(job_id, message="开始逐页翻译")
        translated = 0
        for number in range(start_page, end_page + 1):
            if self._boundary(job_id):
                return
            page = self.store.page(paper_id, number)
            if page is None:
                raise ValueError(f"第 {number} 页尚未解析，请完成解析后再翻译")
            original = page_text(page)
            chunk_count = len(translation_chunks(original)) if original.strip() else 0
            if not original.strip() and page.get("warnings"):
                raise ValueError(f"第 {number} 页解析未获得正文且有警告，请检查 OCR 后再翻译")
            if original.strip():
                cached = self.store.translation(paper_id, number, language, original)
                if cached is None:
                    chunks = translation_chunks(original)
                    outputs = []
                    for index, chunk in enumerate(chunks):
                        if self._boundary(job_id):
                            return
                        output = self.store.translation_chunk(paper_id, number, language, original, index)
                        if output is None:
                            output = asyncio.run(intelligence.translate_text(chunk, language, settings))
                            # Save the in-flight response before honoring a control.
                            if not self.store.save_translation_chunk(paper_id, number, language, original, index, output, job_id):
                                return
                        outputs.append(output)
                        if index + 1 < len(chunks):
                            progress = (number-start_page + (index+1)/len(chunks))/(end_page-start_page+1)*100
                            self.store.checkpoint_job(job_id, 0, round(progress, 1),
                                f"第 {number} 页已保存 {index+1} / {len(chunks)} 个翻译分块",
                                current_page=number, completed_chunks=index+1, total_chunks=len(chunks))
                            if self._boundary(job_id):
                                return
                    translated_text = "\n\n".join(outputs)
                    if not self.store.save_translation(paper_id, number, language, original, translated_text, job_id=job_id):
                        if self._boundary(job_id):
                            return
                        raise ValueError(f"第 {number} 页原文已更新，旧译文未保存，请重新开始翻译")
                translated += 1
            self.store.checkpoint_job(job_id, number, round((number-start_page+1)/(end_page-start_page+1)*100, 1),
                                      f"已处理第 {number} 页（范围 {start_page}–{end_page}）", target_language=language,
                                      start_page=start_page, end_page=end_page, translated_pages=translated,
                                      current_page=number, completed_chunks=chunk_count, total_chunks=chunk_count)
        if self._boundary(job_id):
            return
        self.store.finish_job(job_id, "所选页翻译完成", {"target_language": language, "start_page": start_page,
                              "end_page": end_page, "translated_pages": translated})

    def close(self):
        self.stopping.set()
        self.pool.shutdown(wait=False, cancel_futures=True)
