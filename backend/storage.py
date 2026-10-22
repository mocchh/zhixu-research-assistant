"""Durable local storage. Each operation owns its SQLite connection."""
from __future__ import annotations

import hashlib
import base64
import json
import re
import sqlite3
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def uid() -> str:
    return uuid.uuid4().hex


class RevisionConflict(ValueError):
    """A client attempted to overwrite a newer saved revision."""


class TaskConflict(ValueError):
    """The requested transition is incompatible with the durable job state."""


def family_key(title: str, arxiv_id: str | None, fallback: str) -> str:
    if arxiv_id:
        return "arxiv:" + re.sub(r"v\d+$", "", arxiv_id)
    normalized = "".join(c for c in unicodedata.normalize("NFKC", title).casefold() if c.isalnum())
    return "title:" + hashlib.sha256((normalized or fallback).encode()).hexdigest()[:24]


class Store:
    def __init__(self, directory: Path):
        self.directory = directory.resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.files = self.directory / "files"
        self.files.mkdir(exist_ok=True)
        self.path = self.directory / "research.sqlite3"
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS papers (
                    id TEXT PRIMARY KEY, filename TEXT NOT NULL, title TEXT NOT NULL,
                    path TEXT NOT NULL, sha256 TEXT UNIQUE NOT NULL, family_id TEXT NOT NULL,
                    authors TEXT NOT NULL DEFAULT '[]', abstract TEXT NOT NULL DEFAULT '',
                    arxiv_id TEXT, version INTEGER, page_count INTEGER NOT NULL DEFAULT 0,
                    processed_pages INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'queued',
                    error TEXT, created_at TEXT NOT NULL, tags TEXT NOT NULL DEFAULT '[]',
                    is_favorite INTEGER NOT NULL DEFAULT 0, duplicate_of TEXT,
                    title_custom INTEGER NOT NULL DEFAULT 0, tags_custom INTEGER NOT NULL DEFAULT 0,
                    warnings TEXT NOT NULL DEFAULT '[]', source TEXT NOT NULL,
                    text_chars INTEGER NOT NULL DEFAULT 0, ocr_pages INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS paper_family ON papers(family_id);
                CREATE TABLE IF NOT EXISTS pages (
                    paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
                    page INTEGER NOT NULL, width REAL NOT NULL, height REAL NOT NULL,
                    ocr INTEGER NOT NULL DEFAULT 0, warnings TEXT NOT NULL DEFAULT '[]',
                    page_label TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (paper_id, page)
                );
                CREATE TABLE IF NOT EXISTS blocks (
                    id TEXT PRIMARY KEY, paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
                    page INTEGER NOT NULL, text TEXT NOT NULL, kind TEXT NOT NULL,
                    bbox TEXT NOT NULL, section TEXT NOT NULL, order_index INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS block_document ON blocks(paper_id, page, order_index);
                CREATE TABLE IF NOT EXISTS translations (
                    paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
                    page INTEGER NOT NULL, language TEXT NOT NULL, source_hash TEXT NOT NULL,
                    text TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY (paper_id, page, language)
                );
                CREATE TABLE IF NOT EXISTS translation_chunks (
                    paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
                    page INTEGER NOT NULL, language TEXT NOT NULL, source_hash TEXT NOT NULL,
                    chunk_index INTEGER NOT NULL, text TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY (paper_id, page, language, source_hash, chunk_index)
                );
                CREATE TABLE IF NOT EXISTS aligned_translations (
                    paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
                    page INTEGER NOT NULL, language TEXT NOT NULL, source_hash TEXT NOT NULL,
                    pairs TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY(paper_id, page, language)
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, paper_id TEXT,
                    status TEXT NOT NULL, progress REAL NOT NULL DEFAULT 0, message TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    result TEXT NOT NULL DEFAULT '{}', options TEXT NOT NULL DEFAULT '{}',
                    last_page INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS active_jobs ON jobs(paper_id,kind,status);
                CREATE INDEX IF NOT EXISTS job_history_order ON jobs(created_at DESC,id DESC);
                CREATE TABLE IF NOT EXISTS chat_sessions (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, paper_ids TEXT NOT NULL DEFAULT '[]',
                    messages TEXT NOT NULL DEFAULT '[]', revision INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS research_notes (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, content TEXT NOT NULL,
                    paper_ids TEXT NOT NULL DEFAULT '[]', sources TEXT NOT NULL DEFAULT '[]',
                    kind TEXT NOT NULL DEFAULT 'note', revision INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS note_versions (
                    note_id TEXT NOT NULL REFERENCES research_notes(id) ON DELETE CASCADE,
                    revision INTEGER NOT NULL, title TEXT NOT NULL, content TEXT NOT NULL,
                    paper_ids TEXT NOT NULL, sources TEXT NOT NULL, kind TEXT NOT NULL,
                    created_at TEXT NOT NULL, PRIMARY KEY(note_id,revision)
                );
                CREATE TABLE IF NOT EXISTS reading_positions (
                    paper_id TEXT PRIMARY KEY REFERENCES papers(id) ON DELETE CASCADE,
                    page INTEGER NOT NULL, scroll_ratio REAL NOT NULL DEFAULT 0,
                    revision INTEGER NOT NULL, updated_at TEXT NOT NULL
                );
            """)
            if "page_label" not in {row[1] for row in db.execute("PRAGMA table_info(pages)")}:
                db.execute("ALTER TABLE pages ADD COLUMN page_label TEXT NOT NULL DEFAULT ''")
            paper_columns = {row[1] for row in db.execute("PRAGMA table_info(papers)")}
            if "parser_version" not in paper_columns:
                db.execute("ALTER TABLE papers ADD COLUMN parser_version TEXT NOT NULL DEFAULT ''")
            for field in ("title_custom", "tags_custom"):
                if field not in paper_columns:
                    db.execute(f"ALTER TABLE papers ADD COLUMN {field} INTEGER NOT NULL DEFAULT 0")
            job_columns = {row[1] for row in db.execute("PRAGMA table_info(jobs)")}
            if "options" not in job_columns:
                db.execute("ALTER TABLE jobs ADD COLUMN options TEXT NOT NULL DEFAULT '{}'")
                db.execute("UPDATE jobs SET options=result WHERE kind='translate'")
            if "last_page" not in job_columns:
                db.execute("ALTER TABLE jobs ADD COLUMN last_page INTEGER NOT NULL DEFAULT 0")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def paper_dict(row, private=False):
        if row is None:
            return None
        result = dict(row)
        for key in ("authors", "tags", "warnings"):
            result[key] = json.loads(result[key])
        result["is_favorite"] = bool(result["is_favorite"])
        if not private:
            result.pop("path", None)
            result.pop("sha256", None)
            result.pop("title_custom", None)
            result.pop("tags_custom", None)
        return result

    def paper(self, paper_id, private=False):
        with self.connect() as db:
            return self.paper_dict(db.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone(), private)

    def papers(self, q="", status=None, favorite=False):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM papers ORDER BY created_at DESC").fetchall()
        result = [self.paper_dict(r) for r in rows]
        if q:
            query = q.casefold()
            result = [p for p in result if query in " ".join([p["title"], p["filename"], *p["authors"], *p["tags"]]).casefold()]
        if status:
            result = [p for p in result if p["status"] == status]
        if favorite:
            result = [p for p in result if p["is_favorite"]]
        return result

    def by_sha(self, sha):
        with self.connect() as db:
            return self.paper_dict(db.execute("SELECT * FROM papers WHERE sha256=?", (sha,)).fetchone())

    def create_paper(self, filename, path, sha, source, page_count):
        paper_id = uid()
        with self.connect() as db:
            db.execute("""INSERT INTO papers
                (id,filename,title,path,sha256,family_id,source,created_at,page_count)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (paper_id, filename, Path(filename).stem, str(path), sha, paper_id, source, now(), page_count))
        return self.paper(paper_id)

    def create_import(self, filename, path, sha, source, page_count):
        """Commit the paper and its recoverable parse job together."""
        paper_id, job_id, timestamp = uid(), uid(), now()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""INSERT INTO papers
                (id,filename,title,path,sha256,family_id,source,created_at,page_count)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (paper_id, filename, Path(filename).stem, str(path), sha, paper_id, source, timestamp, page_count))
            db.execute("""INSERT INTO jobs
                (id,kind,paper_id,status,progress,message,created_at,updated_at,result,options)
                VALUES (?,?,?,'queued',0,'等待处理',?,?,'{}','{}')""",
                (job_id, "parse", paper_id, timestamp, timestamp))
            paper = self.paper_dict(db.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone())
            job = self.job_dict(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
        return paper, job

    def cleanup_interrupted_imports(self):
        """Remove only application staging names with no committed paper.

        Called once before startup accepts uploads in the single-process app.
        Other names and symlinks are retained; committed PDFs are never removed.
        """
        with self.connect() as db:
            referenced = {Path(row[0]).resolve() for row in db.execute('SELECT path FROM papers')}
        removed = 0
        for path in self.files.iterdir():
            if (re.fullmatch(r'[0-9a-f]{32}\.pdf', path.name) and not path.is_symlink()
                    and path.is_file() and path.resolve() not in referenced):
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    # An inaccessible leftover must not prevent reading saved work.
                    continue
        return removed

    def fail_queued_import(self, job_id):
        """Keep committed PDFs and make a dispatch failure retryable in the UI."""
        message = "文献已保存，但解析任务未能启动；请重试解析"
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            job = db.execute("SELECT paper_id FROM jobs WHERE id=? AND kind='parse' AND status='queued'", (job_id,)).fetchone()
            if job:
                db.execute("UPDATE jobs SET status='error',message=?,updated_at=? WHERE id=?", (message, now(), job_id))
                db.execute("UPDATE papers SET status='error',error=? WHERE id=?", (message, job["paper_id"]))

    def update_paper(self, paper_id, **fields):
        allowed = {"title", "authors", "abstract", "arxiv_id", "version", "family_id", "page_count",
                   "processed_pages", "status", "error", "tags", "is_favorite", "duplicate_of", "warnings",
                   "text_chars", "ocr_pages", "title_custom", "tags_custom"}
        if not set(fields).issubset(allowed):
            raise ValueError("Unknown paper fields")
        values = {k: json.dumps(v, ensure_ascii=False) if k in {"authors", "tags", "warnings"} else v for k, v in fields.items()}
        if values:
            with self.connect() as db:
                db.execute(f"UPDATE papers SET {','.join(k + '=?' for k in values)} WHERE id=?", (*values.values(), paper_id))
        return self.paper(paper_id)

    def versions(self, family_id):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM papers WHERE family_id=? ORDER BY version DESC,created_at DESC", (family_id,)).fetchall()
        return [self.paper_dict(r) for r in rows]

    def apply_extracted_metadata(self, paper_id, job_id, metadata):
        """Apply parser fields atomically without overwriting concurrent edits."""
        allowed = {"title", "authors", "abstract", "arxiv_id", "version", "page_count", "tags", "warnings"}
        if not set(metadata).issubset(allowed):
            raise ValueError("Unknown extracted metadata fields")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            paper = db.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
            if not paper or not db.execute("SELECT 1 FROM jobs WHERE id=? AND paper_id=? AND kind='parse' AND status='processing'", (job_id, paper_id)).fetchone():
                return False
            fields = dict(metadata)
            if paper["title_custom"]:
                fields.pop("title", None)
            if paper["tags_custom"]:
                fields.pop("tags", None)
            family = family_key(metadata.get("title", paper["title"]), metadata.get("arxiv_id"), paper["sha256"])
            other = db.execute("SELECT id FROM papers WHERE family_id=? AND id!=? ORDER BY version DESC,created_at DESC LIMIT 1", (family, paper_id)).fetchone()
            fields.update(family_id=family, duplicate_of=other[0] if other else None)
            values = {key: json.dumps(value, ensure_ascii=False) if key in {"authors", "tags", "warnings"} else value for key, value in fields.items()}
            db.execute(f"UPDATE papers SET {','.join(key + '=?' for key in values)} WHERE id=?", (*values.values(), paper_id))
            return True

    def reconcile_family(self, paper_id, job_id=None):
        """Match an unidentified publisher copy to its identified arXiv work.

        Require the same normalized title and an overlapping author when both
        author lists exist. Conflicting explicit identifiers are never merged.
        """
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if job_id and not db.execute("SELECT 1 FROM jobs WHERE id=? AND paper_id=? AND kind='parse' AND status='processing'", (job_id, paper_id)).fetchone():
                return None
            rows = [self.paper_dict(r) for r in db.execute("SELECT * FROM papers")]
            current = next((p for p in rows if p["id"] == paper_id), None)
            if not current:
                return None
            fingerprint = family_key(current["title"], None, paper_id)
            normalize = lambda s: "".join(c for c in unicodedata.normalize("NFKC", s).casefold() if c.isalpha())
            current_authors = {normalize(a) for a in current["authors"]}
            matching_titles = [p for p in rows if family_key(p["title"], None, p["id"]) == fingerprint]
            explicit_ids = {p["arxiv_id"] for p in matching_titles if p["arxiv_id"]}
            ambiguous = len(explicit_ids) > 1
            if ambiguous and not current["arxiv_id"]:
                db.execute("UPDATE papers SET family_id=?,duplicate_of=NULL WHERE id=?", (fingerprint, paper_id))
                return fingerprint
            candidates = []
            for p in rows:
                if p["id"] == paper_id or family_key(p["title"], None, p["id"]) != fingerprint:
                    continue
                if current["arxiv_id"] and p["arxiv_id"] and current["arxiv_id"] != p["arxiv_id"]:
                    continue
                if ambiguous and not p["arxiv_id"]:
                    continue
                others = {normalize(a) for a in p["authors"]}
                if current_authors and others and not current_authors.intersection(others):
                    continue
                if (not current_authors or not others) and len(normalize(current["title"])) < 24:
                    continue
                candidates.append(p)
            if not candidates:
                return current["family_id"]
            identified = current["arxiv_id"] or next((p["arxiv_id"] for p in candidates if p["arxiv_id"]), None)
            canonical = family_key(current["title"], identified, paper_id)
            ids = [paper_id, *[p["id"] for p in candidates]]
            db.execute(f"UPDATE papers SET family_id=? WHERE id IN ({','.join('?' for _ in ids)})", (canonical, *ids))
            db.execute("UPDATE papers SET duplicate_of=? WHERE id=?", (candidates[0]["id"], paper_id))
            return canonical

    def add_page(self, paper_id, page, job_id=None):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM papers WHERE id=?", (paper_id,)).fetchone():
                return False
            if job_id and not db.execute("SELECT 1 FROM jobs WHERE id=? AND paper_id=? AND status IN ('processing','pause_requested','cancel_requested')", (job_id, paper_id)).fetchone():
                return False
            db.execute("DELETE FROM blocks WHERE paper_id=? AND page=?", (paper_id, page["page"]))
            db.execute("INSERT OR REPLACE INTO pages (paper_id,page,width,height,ocr,warnings,page_label) VALUES (?,?,?,?,?,?,?)",
                       (paper_id, page["page"], page["width"], page["height"], int(page.get("ocr", False)), json.dumps(page.get("warnings", []), ensure_ascii=False), str(page.get("page_label", ""))))
            for i, b in enumerate(page["blocks"]):
                block_id = hashlib.sha256(f"{paper_id}:{page['page']}:{i}:{b['text']}".encode()).hexdigest()[:24]
                db.execute("INSERT INTO blocks VALUES (?,?,?,?,?,?,?,?)", (block_id, paper_id, page["page"], b["text"], b.get("kind", "paragraph"), json.dumps(b["bbox"]), b.get("section", ""), b.get("order_index", i)))
            db.execute("""UPDATE papers SET
                processed_pages=(SELECT COUNT(*) FROM pages WHERE paper_id=?),
                text_chars=(SELECT COALESCE(SUM(LENGTH(text)),0) FROM blocks WHERE paper_id=?),
                ocr_pages=(SELECT COALESCE(SUM(ocr),0) FROM pages WHERE paper_id=?)
                WHERE id=?""", (paper_id, paper_id, paper_id, paper_id))
        return True

    def page(self, paper_id, number):
        with self.connect() as db:
            row = db.execute("SELECT * FROM pages WHERE paper_id=? AND page=?", (paper_id, number)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["warnings"] = json.loads(result["warnings"])
        result["blocks"] = self.blocks([paper_id], page=number)
        return result

    def completed_pages(self, paper_id):
        with self.connect() as db:
            return {r[0] for r in db.execute("SELECT page FROM pages WHERE paper_id=?", (paper_id,))}

    def blocks(self, paper_ids, page=None):
        if not paper_ids:
            return []
        placeholders = ",".join("?" for _ in paper_ids)
        sql = f"SELECT * FROM blocks WHERE paper_id IN ({placeholders})"
        params = list(paper_ids)
        if page is not None:
            sql += " AND page=?"
            params.append(page)
        sql += " ORDER BY paper_id,page,order_index"
        with self.connect() as db:
            rows = db.execute(sql, params).fetchall()
        return [{**dict(r), "bbox": json.loads(r["bbox"])} for r in rows]

    def translation(self, paper_id, page, language, source_text):
        digest = hashlib.sha256(source_text.encode()).hexdigest()
        with self.connect() as db:
            row = db.execute("SELECT text FROM translations WHERE paper_id=? AND page=? AND language=? AND source_hash=?", (paper_id, page, language, digest)).fetchone()
        return row[0] if row else None

    def save_translation(self, paper_id, page, language, source_text, text, job_id=None):
        digest = hashlib.sha256(source_text.encode()).hexdigest()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM papers WHERE id=?", (paper_id,)).fetchone():
                return False
            if job_id and not db.execute("SELECT 1 FROM jobs WHERE id=? AND paper_id=? AND status IN ('processing','pause_requested','cancel_requested')", (job_id, paper_id)).fetchone():
                return False
            if not db.execute("SELECT 1 FROM pages WHERE paper_id=? AND page=?", (paper_id, page)).fetchone():
                return False
            # Compare and replace within one write transaction. A response for
            # a previous parse must never evict a translation of the new text.
            rows = db.execute("SELECT text FROM blocks WHERE paper_id=? AND page=? ORDER BY order_index", (paper_id, page)).fetchall()
            current_source = "\n\n".join(row["text"] for row in rows)
            if hashlib.sha256(current_source.encode()).hexdigest() != digest:
                return False
            db.execute("INSERT OR REPLACE INTO translations VALUES (?,?,?,?,?,?)", (paper_id, page, language, digest, text, now()))
        return True

    def translation_chunk(self, paper_id, page, language, source_text, chunk_index):
        digest = hashlib.sha256(source_text.encode()).hexdigest()
        with self.connect() as db:
            row = db.execute("SELECT text FROM translation_chunks WHERE paper_id=? AND page=? AND language=? AND source_hash=? AND chunk_index=?", (paper_id, page, language, digest, chunk_index)).fetchone()
        return row[0] if row else None

    def save_translation_chunk(self, paper_id, page, language, source_text, chunk_index, text, job_id):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM papers WHERE id=?", (paper_id,)).fetchone():
                return False
            if not db.execute("SELECT 1 FROM jobs WHERE id=? AND paper_id=? AND status IN ('processing','pause_requested','cancel_requested')", (job_id, paper_id)).fetchone():
                return False
            db.execute("INSERT OR REPLACE INTO translation_chunks VALUES (?,?,?,?,?,?,?)", (paper_id, page, language, hashlib.sha256(source_text.encode()).hexdigest(), chunk_index, text, now()))
        return True

    @staticmethod
    def job_dict(row):
        if row is None:
            return None
        result = dict(row)
        for field in ("result", "options"):
            result[field] = json.loads(result.get(field) or "{}")
        return result

    def job(self, job_id):
        with self.connect() as db:
            return self.job_dict(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def create_job(self, kind, paper_id, result=None):
        job_id = uid()
        with self.connect() as db:
            options = json.dumps(result or {}, ensure_ascii=False)
            db.execute("INSERT INTO jobs (id,kind,paper_id,status,progress,message,created_at,updated_at,result,options) VALUES (?,?,?,?,?,?,?,?,?,?)", (job_id, kind, paper_id, "queued", 0, "等待处理", now(), now(), options, options))
        return job_id

    def ensure_job(self, kind, paper_id, result=None):
        """Atomically reuse an active job or create one, including across requests."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM papers WHERE id=?", (paper_id,)).fetchone():
                raise KeyError("未找到该文献")
            row = db.execute("SELECT * FROM jobs WHERE paper_id=? AND kind=? AND status IN ('queued','processing','pause_requested','paused','cancel_requested') ORDER BY created_at LIMIT 1", (paper_id, kind)).fetchone()
            if row:
                return self.job_dict(row)
            job_id = uid()
            timestamp = now()
            options = json.dumps(result or {}, ensure_ascii=False)
            db.execute("INSERT INTO jobs (id,kind,paper_id,status,progress,message,created_at,updated_at,result,options) VALUES (?,?,?,?,?,?,?,?,?,?)", (job_id, kind, paper_id, "queued", 0, "等待处理", timestamp, timestamp, options, options))
            return self.job_dict(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def create_reparse(self, paper_id):
        """Reset derived pages atomically; keep the PDF, identity and research history."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM papers WHERE id=?", (paper_id,)).fetchone():
                raise KeyError("未找到该文献")
            if db.execute("SELECT 1 FROM jobs WHERE paper_id=? AND status IN ('queued','processing','pause_requested','paused','cancel_requested')", (paper_id,)).fetchone():
                raise TaskConflict("该文献还有解析或翻译任务，请先完成或取消任务再重建解析")
            db.execute("DELETE FROM blocks WHERE paper_id=?", (paper_id,))
            db.execute("DELETE FROM pages WHERE paper_id=?", (paper_id,))
            db.execute("UPDATE papers SET status='queued',processed_pages=0,text_chars=0,ocr_pages=0,error=NULL,warnings='[]',parser_version='' WHERE id=?", (paper_id,))
            job_id, timestamp = uid(), now()
            options = json.dumps({"rebuild": True})
            db.execute("INSERT INTO jobs (id,kind,paper_id,status,progress,message,created_at,updated_at,result,options) VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (job_id, "parse", paper_id, "queued", 0, "从首页重建解析，原文与笔记保留", timestamp, timestamp, "{}", options))
            return self.job_dict(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def prepare_parser(self, paper_id, job_id, version):
        """Never mix checkpoints produced by different layout parsers."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            paper = db.execute("SELECT parser_version FROM papers WHERE id=?", (paper_id,)).fetchone()
            if not paper or not db.execute("SELECT 1 FROM jobs WHERE id=? AND paper_id=? AND status='processing'", (job_id, paper_id)).fetchone():
                return False
            if paper[0] != version:
                db.execute("DELETE FROM blocks WHERE paper_id=?", (paper_id,))
                db.execute("DELETE FROM pages WHERE paper_id=?", (paper_id,))
                db.execute("UPDATE papers SET processed_pages=0,text_chars=0,ocr_pages=0,parser_version=? WHERE id=?", (version, paper_id))
                db.execute("UPDATE jobs SET last_page=0,progress=0,message='解析器已更新，从首页重建解析' WHERE id=?", (job_id,))
            return True

    def update_job(self, job_id, **fields):
        allowed = {"status", "progress", "message", "result"}
        if not set(fields).issubset(allowed):
            raise ValueError("Unknown job fields")
        fields["updated_at"] = now()
        if "result" in fields:
            fields["result"] = json.dumps(fields["result"], ensure_ascii=False)
        with self.connect() as db:
            cursor = db.execute(f"UPDATE jobs SET {','.join(k + '=?' for k in fields)} WHERE id=? AND status NOT IN ('cancelled','paused','pause_requested','cancel_requested')", (*fields.values(), job_id))
            return bool(cursor.rowcount)

    def jobs(self, limit=100):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [self.job_dict(r) for r in rows]

    def job_listing(self, limit=20, cursor=None):
        """All actionable jobs plus a stable, bounded page of terminal history."""
        if not 1 <= limit <= 100:
            raise ValueError("历史任务每页数量必须为 1–100")
        position = None
        if cursor:
            try:
                position = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
                if not isinstance(position, list) or len(position) != 2 or any(not isinstance(v, str) or not v for v in position):
                    raise ValueError()
            except (ValueError, TypeError, UnicodeError) as exc:
                raise ValueError("历史任务分页位置无效，请重新加载") from exc
        states = "('queued','processing','pause_requested','paused','cancel_requested','error')"
        with self.connect() as db:
            # One read snapshot keeps a finishing job from falling between the
            # active and history queries in this response.
            db.execute("BEGIN")
            active = db.execute(f"SELECT * FROM jobs WHERE status IN {states} ORDER BY created_at DESC,id DESC").fetchall()
            where, params = f"status NOT IN {states}", []
            if position:
                where += " AND (created_at,id)<(?,?)"
                params.extend(position)
            history = db.execute(f"SELECT * FROM jobs WHERE {where} ORDER BY created_at DESC,id DESC LIMIT ?", (*params, limit + 1)).fetchall()
            total = db.execute(f"SELECT COUNT(*) FROM jobs WHERE status NOT IN {states}").fetchone()[0]
        more = len(history) > limit
        history = history[:limit]
        next_cursor = base64.urlsafe_b64encode(json.dumps([history[-1]["created_at"], history[-1]["id"]]).encode()).decode() if more else None
        active_jobs = [self.job_dict(row) for row in active]
        history_jobs = [self.job_dict(row) for row in history]
        return {"jobs": active_jobs + history_jobs, "active_jobs": active_jobs, "history_jobs": history_jobs,
                "next_cursor": next_cursor, "history_total": total}

    def control_job(self, job_id, action):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            job = self.job_dict(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
            if not job:
                raise KeyError("未找到该任务")
            if not db.execute("SELECT 1 FROM papers WHERE id=?", (job["paper_id"],)).fetchone():
                raise TaskConflict("文献已删除，无法继续任务")
            state = job["status"]
            if action == "pause":
                target = {"queued": "paused", "processing": "pause_requested", "paused": "paused", "pause_requested": "pause_requested"}.get(state)
                message = "已暂停，页面进度已保存" if target == "paused" else "将在当前页完成后暂停"
            elif action == "cancel":
                target = {"queued": "cancelled", "paused": "cancelled", "error": "cancelled", "processing": "cancel_requested", "pause_requested": "cancel_requested", "cancel_requested": "cancel_requested", "cancelled": "cancelled"}.get(state)
                message = "已取消，已完成页面保留" if target == "cancelled" else "将在当前页完成后取消"
            elif action == "resume":
                target = {"paused": "queued", "error": "queued", "queued": "queued", "processing": "processing"}.get(state)
                message = "等待继续任务" if target == "queued" else job["message"]
                other = db.execute("SELECT id FROM jobs WHERE paper_id=? AND kind=? AND id!=? AND status IN ('queued','processing','paused','pause_requested','cancel_requested') LIMIT 1", (job["paper_id"], job["kind"], job_id)).fetchone()
                if other:
                    raise TaskConflict("该文献已有同类任务，请先完成或取消它")
            else:
                raise ValueError("不支持的任务操作")
            if target is None:
                raise TaskConflict(f"当前任务状态 {state} 不支持此操作")
            if job["kind"] == "translate" and target in {"pause_requested", "cancel_requested"}:
                message = "将在当前翻译分块返回并保存后" + ("暂停" if target == "pause_requested" else "取消") + "；当前请求可能已提交给服务方，费用以服务方实际用量为准"
            db.execute("UPDATE jobs SET status=?,message=?,updated_at=? WHERE id=?", (target, message, now(), job_id))
            if job["kind"] == "parse" and target in {"paused", "cancelled", "queued"}:
                db.execute("UPDATE papers SET status=?,error=NULL WHERE id=?", (target, job["paper_id"]))
            return self.job_dict(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def start_job(self, job_id):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            job = self.job_dict(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
            if not job or job["status"] != "queued" or not db.execute("SELECT 1 FROM papers WHERE id=?", (job["paper_id"],)).fetchone():
                return False
            db.execute("UPDATE jobs SET status='processing',message='开始处理',updated_at=? WHERE id=?", (now(), job_id))
            if job["kind"] == "parse":
                db.execute("UPDATE papers SET status='processing',error=NULL WHERE id=?", (job["paper_id"],))
            return True

    def settle_job(self, job_id, shutdown=False):
        """Observe controls at a page/chunk checkpoint; return True to stop."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            job = self.job_dict(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
            if not job or job["status"] in {"paused", "cancelled", "error", "done"}:
                return True
            target = {"pause_requested": "paused", "cancel_requested": "cancelled"}.get(job["status"])
            if not target and shutdown:
                target = "queued"
            if not target:
                return job["status"] != "processing"
            message = {"paused": "已暂停，页面进度已保存", "cancelled": "已取消，已完成页面保留", "queued": "已保存进度，下次启动继续"}[target]
            if job["kind"] == "translate" and target in {"paused", "cancelled"}:
                message = ("已暂停" if target == "paused" else "已取消") + "，已完成页面与分块保留；已提交请求的费用以服务方实际用量为准"
            db.execute("UPDATE jobs SET status=?,message=?,updated_at=? WHERE id=?", (target, message, now(), job_id))
            if job["kind"] == "parse":
                db.execute("UPDATE papers SET status=? WHERE id=?", (target, job["paper_id"]))
            return True

    def checkpoint_job(self, job_id, page, progress, message, **result_fields):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            job = self.job_dict(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
            if not job or job["status"] not in {"processing", "pause_requested", "cancel_requested"}:
                return False
            result = {**job["result"], **job["options"], **result_fields}
            if job["status"] != "processing":
                message = job["message"]
            db.execute("UPDATE jobs SET last_page=MAX(last_page,?),progress=?,message=?,result=?,updated_at=? WHERE id=?", (page, progress, message, json.dumps(result, ensure_ascii=False), now(), job_id))
            return True

    def finish_job(self, job_id, message, result, paper_status=None, warnings=None):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            job = self.job_dict(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
            if not job or job["status"] != "processing" or not db.execute("SELECT 1 FROM papers WHERE id=?", (job["paper_id"],)).fetchone():
                return False
            merged = {**job["result"], **job["options"], **result}
            db.execute("UPDATE jobs SET status='done',progress=100,message=?,result=?,updated_at=? WHERE id=?", (message, json.dumps(merged, ensure_ascii=False), now(), job_id))
            if paper_status:
                db.execute("UPDATE papers SET status=?,warnings=?,error=NULL WHERE id=?", (paper_status, json.dumps(warnings or [], ensure_ascii=False), job["paper_id"]))
            return True

    def fail_job(self, job_id, message):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            job = self.job_dict(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
            if not job or job["status"] != "processing":
                return False
            db.execute("UPDATE jobs SET status='error',message=?,updated_at=? WHERE id=?", (message, now(), job_id))
            if job["kind"] == "parse":
                db.execute("UPDATE papers SET status='error',error=? WHERE id=?", (message, job["paper_id"]))
            return True

    def recover_job(self, job_id):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            job = self.job_dict(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
            if not job:
                return None
            target = {"processing": "queued", "pause_requested": "paused", "cancel_requested": "cancelled"}.get(job["status"])
            if target:
                db.execute("UPDATE jobs SET status=?,message=?,updated_at=? WHERE id=?", (target, "已恢复已保存的任务状态", now(), job_id))
                if job["kind"] == "parse":
                    db.execute("UPDATE papers SET status=? WHERE id=?", (target, job["paper_id"]))
            return self.job_dict(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def delete_paper(self, paper_id):
        with self.connect() as db:
            db.execute("UPDATE jobs SET status='cancelled',message='文献已删除',updated_at=? WHERE paper_id=? AND status IN ('queued','processing','pause_requested','paused','cancel_requested','error')", (now(), paper_id))
            db.execute("UPDATE papers SET duplicate_of=NULL WHERE duplicate_of=?", (paper_id,))
            db.execute("DELETE FROM papers WHERE id=?", (paper_id,))

    @staticmethod
    def workspace_dict(row):
        if row is None:
            return None
        item = dict(row)
        for key in ("paper_ids", "messages", "sources"):
            if key in item:
                item[key] = json.loads(item[key])
        return item

    def sessions(self):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM chat_sessions ORDER BY updated_at DESC LIMIT 200").fetchall()
        result = []
        for row in rows:
            session = self.workspace_dict(row)
            messages = session.pop("messages")
            session["message_count"] = len(messages)
            session["preview"] = messages[-1].get("content", "")[:160] if messages else ""
            result.append(session)
        return result

    def session(self, session_id):
        with self.connect() as db:
            return self.workspace_dict(db.execute("SELECT * FROM chat_sessions WHERE id=?", (session_id,)).fetchone())

    def create_session(self, title="新对话", paper_ids=None, messages=None):
        session_id, timestamp = uid(), now()
        with self.connect() as db:
            db.execute("INSERT INTO chat_sessions VALUES (?,?,?,?,?,?,?)", (session_id, title, json.dumps(paper_ids or [], ensure_ascii=False), json.dumps(messages or [], ensure_ascii=False, allow_nan=False), 1, timestamp, timestamp))
        return self.session(session_id)

    def save_session(self, session_id, revision, title, paper_ids, messages):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT revision FROM chat_sessions WHERE id=?", (session_id,)).fetchone()
            if row is None:
                raise KeyError("未找到该会话")
            if row["revision"] != revision:
                raise RevisionConflict("会话已有更新，请重新载入后合并保存")
            db.execute("UPDATE chat_sessions SET title=?,paper_ids=?,messages=?,revision=revision+1,updated_at=? WHERE id=?", (title, json.dumps(paper_ids, ensure_ascii=False), json.dumps(messages, ensure_ascii=False, allow_nan=False), now(), session_id))
            return self.workspace_dict(db.execute("SELECT * FROM chat_sessions WHERE id=?", (session_id,)).fetchone())

    def delete_session(self, session_id):
        with self.connect() as db:
            return bool(db.execute("DELETE FROM chat_sessions WHERE id=?", (session_id,)).rowcount)

    def notes(self):
        with self.connect() as db:
            rows = db.execute("SELECT id,title,paper_ids,kind,revision,created_at,updated_at FROM research_notes ORDER BY updated_at DESC LIMIT 200").fetchall()
        return [self.workspace_dict(row) for row in rows]

    def note(self, note_id):
        with self.connect() as db:
            result = self.workspace_dict(db.execute("SELECT * FROM research_notes WHERE id=?", (note_id,)).fetchone())
            if result:
                result["versions"] = [dict(row) for row in db.execute("SELECT revision,title,created_at FROM note_versions WHERE note_id=? ORDER BY revision DESC", (note_id,))]
            return result

    def note_version(self, note_id, revision):
        with self.connect() as db:
            return self.workspace_dict(db.execute("SELECT * FROM note_versions WHERE note_id=? AND revision=?", (note_id, revision)).fetchone())

    def create_note(self, title, content, paper_ids=None, sources=None, kind="note"):
        note_id, timestamp = uid(), now()
        ids, evidence = json.dumps(paper_ids or [], ensure_ascii=False), json.dumps(sources or [], ensure_ascii=False, allow_nan=False)
        with self.connect() as db:
            db.execute("INSERT INTO research_notes VALUES (?,?,?,?,?,?,?,?,?)", (note_id, title, content, ids, evidence, kind, 1, timestamp, timestamp))
            db.execute("INSERT INTO note_versions VALUES (?,?,?,?,?,?,?,?)", (note_id, 1, title, content, ids, evidence, kind, timestamp))
        return self.note(note_id)

    def save_note(self, note_id, revision, title, content, paper_ids, sources, kind="note"):
        timestamp = now()
        ids, evidence = json.dumps(paper_ids, ensure_ascii=False), json.dumps(sources, ensure_ascii=False, allow_nan=False)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT revision FROM research_notes WHERE id=?", (note_id,)).fetchone()
            if row is None:
                raise KeyError("未找到该笔记")
            if row["revision"] != revision:
                raise RevisionConflict("笔记已有新版本，请重新载入后合并保存")
            new_revision = revision + 1
            db.execute("INSERT INTO note_versions VALUES (?,?,?,?,?,?,?,?)", (note_id, new_revision, title, content, ids, evidence, kind, timestamp))
            db.execute("UPDATE research_notes SET title=?,content=?,paper_ids=?,sources=?,kind=?,revision=?,updated_at=? WHERE id=?", (title, content, ids, evidence, kind, new_revision, timestamp, note_id))
            result = self.workspace_dict(db.execute("SELECT * FROM research_notes WHERE id=?", (note_id,)).fetchone())
            result["versions"] = [dict(row) for row in db.execute("SELECT revision,title,created_at FROM note_versions WHERE note_id=? ORDER BY revision DESC", (note_id,))]
            return result

    def delete_note(self, note_id):
        with self.connect() as db:
            return bool(db.execute("DELETE FROM research_notes WHERE id=?", (note_id,)).rowcount)

    def reading_position(self, paper_id):
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM papers WHERE id=?", (paper_id,)).fetchone():
                raise KeyError("未找到该文献")
            row = db.execute("SELECT * FROM reading_positions WHERE paper_id=?", (paper_id,)).fetchone()
        return dict(row) if row else {"paper_id": paper_id, "page": 1, "scroll_ratio": 0.0, "revision": 0, "updated_at": None}

    def save_reading_position(self, paper_id, revision, page, scroll_ratio):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            paper = db.execute("SELECT page_count FROM papers WHERE id=?", (paper_id,)).fetchone()
            if paper is None:
                raise KeyError("未找到该文献")
            if not 1 <= page <= paper["page_count"] or not 0 <= scroll_ratio <= 1:
                raise ValueError("阅读页码或滚动位置超出范围")
            row = db.execute("SELECT revision FROM reading_positions WHERE paper_id=?", (paper_id,)).fetchone()
            current = row["revision"] if row else 0
            if current != revision:
                raise RevisionConflict("阅读位置已有更新，请使用最新进度保存")
            db.execute("INSERT OR REPLACE INTO reading_positions VALUES (?,?,?,?,?)", (paper_id, page, scroll_ratio, revision+1, now()))
            return dict(db.execute("SELECT * FROM reading_positions WHERE paper_id=?", (paper_id,)).fetchone())

    def stats(self):
        with self.connect() as db:
            row = db.execute("""SELECT COUNT(*) papers, COALESCE(SUM(status='ready'),0) ready,
                COALESCE(SUM(processed_pages),0) pages, COALESCE(SUM(status IN ('queued','processing')),0) processing,
                COUNT(DISTINCT family_id) families FROM papers""").fetchone()
        return dict(row)
