"""Persistent workspace and durable job-control routes.

Generation remains on /chat and /research. Clients save their result snapshots
with a revision-checked PUT, so a late model response cannot overwrite a newer
conversation or a different document scope.
"""
from __future__ import annotations

import json
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator

from . import intelligence
from .storage import RevisionConflict, TaskConflict, now


class AnswerAudit(BaseModel):
    status: Literal["checked", "fallback", "insufficient"]
    raw_response: str = Field(max_length=100000)
    draft: str = Field(max_length=100000)
    semantic_entailment_verified: Literal[False] = False
    issues: list[str] = Field(default_factory=list, max_length=100)
    citation_checks: dict = Field(default_factory=dict)


class ChatMessage(BaseModel):
    id: str = Field(min_length=1, max_length=100)
    role: Literal["user", "assistant"]
    content: str = Field(default="", max_length=100000)
    paper_ids: list[str] = Field(default_factory=list, max_length=100)
    sources: list[dict] = Field(default_factory=list, max_length=100)
    mode: str | None = Field(default=None, max_length=40)
    warnings: list[str] = Field(default_factory=list, max_length=100)
    answer_audit: AnswerAudit | None = None
    status: Literal["pending", "completed", "error"] = "completed"
    request_id: str | None = Field(default=None, max_length=100)
    created_at: str | None = Field(default=None, max_length=80)


class SessionCreate(BaseModel):
    title: str = Field(default="新对话", min_length=1, max_length=500)
    paper_ids: list[str] = Field(default_factory=list, max_length=100)
    messages: list[ChatMessage] = Field(default_factory=list, max_length=500)

    @field_validator("title")
    @classmethod
    def title_is_not_whitespace(cls, value):
        if not value.strip():
            raise ValueError("标题不能为空")
        return value.strip()

    @model_validator(mode="after")
    def bounded_unique_messages(self):
        ids = [message.id for message in self.messages]
        if len(ids) != len(set(ids)):
            raise ValueError("消息 ID 不可重复")
        if len(json.dumps(self.model_dump(), ensure_ascii=False, allow_nan=False).encode()) > 4*1024*1024:
            raise ValueError("单个会话最大支持 4 MiB，请新建会话继续")
        return self


class SessionSave(SessionCreate):
    revision: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=500)
    paper_ids: list[str] = Field(max_length=100)
    messages: list[ChatMessage] = Field(max_length=500)


class NoteCreate(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    content: str = Field(default="", max_length=500000)
    paper_ids: list[str] = Field(default_factory=list, max_length=100)
    sources: list[dict] = Field(default_factory=list, max_length=100)
    kind: Literal["note", "summary", "review", "ideas", "outline"] = "note"

    @field_validator("title")
    @classmethod
    def title_is_not_whitespace(cls, value):
        if not value.strip():
            raise ValueError("标题不能为空")
        return value.strip()

    @model_validator(mode="after")
    def bounded_snapshot(self):
        if len(json.dumps(self.model_dump(), ensure_ascii=False, allow_nan=False).encode()) > 4*1024*1024:
            raise ValueError("单个笔记版本最大支持 4 MiB")
        return self


class NoteSave(NoteCreate):
    revision: int = Field(ge=1)


class ReadingSave(BaseModel):
    revision: int = Field(ge=0)
    page: int = Field(ge=1)
    scroll_ratio: float = Field(default=0, ge=0, le=1, allow_inf_nan=False)


class TranslationRange(BaseModel):
    target_language: Literal["zh", "en"] = "zh"
    start_page: int = Field(default=1, ge=1)
    end_page: int | None = Field(default=None, ge=1)


def create_workspace_router(store, runner, config, run_jobs=True):
    router = APIRouter(prefix="/api")

    def call(function, *args, **kwargs):
        try:
            return function(*args, **kwargs)
        except KeyError as exc:
            raise HTTPException(404, str(exc.args[0])) from exc
        except (RevisionConflict, TaskConflict) as exc:
            raise HTTPException(409, str(exc)) from exc
        except intelligence.ConfigError as exc:
            raise HTTPException(503, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    def messages(body):
        result = []
        for message in body.messages:
            item = message.model_dump(exclude_none=True)
            item.setdefault("created_at", now())
            result.append(item)
        return result

    @router.get("/sessions")
    def list_sessions():
        return {"sessions": store.sessions()}

    @router.post("/sessions", status_code=201)
    def new_session(body: SessionCreate):
        return call(store.create_session, body.title, body.paper_ids, messages(body))

    @router.get("/sessions/{session_id}")
    def get_session(session_id: str):
        result = store.session(session_id)
        if result is None:
            raise HTTPException(404, "未找到该会话")
        return result

    @router.put("/sessions/{session_id}")
    def put_session(session_id: str, body: SessionSave):
        return call(store.save_session, session_id, body.revision, body.title, body.paper_ids, messages(body))

    @router.delete("/sessions/{session_id}")
    def remove_session(session_id: str):
        if not store.delete_session(session_id):
            raise HTTPException(404, "未找到该会话")
        return {"ok": True}

    @router.get("/notes")
    def list_notes():
        return {"notes": store.notes()}

    @router.post("/notes", status_code=201)
    def new_note(body: NoteCreate):
        return call(store.create_note, **body.model_dump())

    @router.get("/notes/{note_id}")
    def get_note(note_id: str):
        result = store.note(note_id)
        if result is None:
            raise HTTPException(404, "未找到该笔记")
        return result

    @router.put("/notes/{note_id}")
    def put_note(note_id: str, body: NoteSave):
        return call(store.save_note, note_id, **body.model_dump())

    @router.get("/notes/{note_id}/versions/{revision}")
    def get_note_version(note_id: str, revision: int):
        result = store.note_version(note_id, revision)
        if result is None:
            raise HTTPException(404, "未找到该笔记版本")
        return result

    @router.delete("/notes/{note_id}")
    def remove_note(note_id: str):
        if not store.delete_note(note_id):
            raise HTTPException(404, "未找到该笔记")
        return {"ok": True}

    @router.get("/papers/{paper_id}/reading")
    def reading(paper_id: str):
        return call(store.reading_position, paper_id)

    @router.put("/papers/{paper_id}/reading")
    def save_reading(paper_id: str, body: ReadingSave):
        return call(store.save_reading_position, paper_id, **body.model_dump())

    @router.get("/jobs/{job_id}")
    def get_job(job_id: str):
        result = store.job(job_id)
        if result is None:
            raise HTTPException(404, "未找到该任务")
        return result

    @router.post("/jobs/{job_id}/{action}")
    def control_job(job_id: str, action: Literal["pause", "resume", "cancel"]):
        return call(runner.control, job_id, action, run=run_jobs)

    @router.post("/papers/{paper_id}/translation-estimate")
    def estimate_translation(paper_id: str, body: TranslationRange):
        return call(runner.translation_estimate, paper_id, body.target_language, body.start_page, body.end_page)

    @router.post("/papers/{paper_id}/translation-jobs")
    def start_translation(paper_id: str, body: TranslationRange):
        job = call(runner.start_translation, paper_id, body.target_language, body.start_page, body.end_page, run=run_jobs)
        return {"job_id": job["id"], "job": job}

    return router
