"""Portable, verified backups of the local workspace; no model or server needed.

Archives contain only a consistent SQLite snapshot and its referenced PDFs.
Checksums detect corruption; pin the returned archive SHA256 separately when
verification must also detect a rewritten manifest.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import zipfile
import zlib
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath


FORMAT = "research-assistant-backup"
VERSION = 1
DATABASE = "research.sqlite3"
MANIFEST = "manifest.json"
CHUNK = 1024 * 1024
MAX_MANIFEST = 16 * CHUNK
MAX_PDF = 100 * CHUNK
MAX_DATABASE = 20 * 1024 * CHUNK
MAX_ARCHIVE_CONTENT = 100 * 1024 * CHUNK
TABLES = {"papers", "pages", "blocks", "translations", "translation_chunks", "aligned_translations", "jobs",
          "chat_sessions", "research_notes", "note_versions", "reading_positions"}


class BackupError(ValueError):
    """A backup cannot be safely created, verified or restored."""


def _timestamp():
    return datetime.now(timezone.utc).isoformat()


def _digest(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _regular(path):
    try:
        info = path.lstat()
    except OSError as exc:
        raise BackupError(f"文件不存在或无法读取：{path.name}") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise BackupError(f"拒绝链接或非普通文件：{path.name}")
    return info


def _signature(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


@contextmanager
def _open_regular(path):
    before = _regular(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise BackupError(f"无法安全读取文件：{path.name}") from exc
    with os.fdopen(descriptor, "rb") as stream:
        current = os.fstat(stream.fileno())
        if _signature(current) != _signature(before) or not stat.S_ISREG(current.st_mode) or current.st_nlink != 1:
            raise BackupError(f"读取前文件发生变化：{path.name}")
        yield stream, before
        if _signature(os.fstat(stream.fileno())) != _signature(before) or _signature(_regular(path)) != _signature(before):
            raise BackupError(f"读取期间文件被删除、替换或修改：{path.name}")


def _directory(path):
    path = Path(path).expanduser().absolute()
    if path.is_symlink() or not path.is_dir():
        raise BackupError(f"目录不存在或为符号链接：{path}")
    return path.resolve(strict=True)


def _new_target(path):
    path = Path(path).expanduser().absolute()
    parent = _directory(path.parent)
    return parent / path.name


def _source_pdf(root, stored_path):
    path = Path(stored_path)
    if not path.is_absolute():
        raise BackupError("数据库中的 PDF 路径必须为绝对路径")
    # Resolve the trusted root, then reject links in every relative component.
    try:
        relative = path.relative_to(root / "files")
    except ValueError as exc:
        raise BackupError("PDF 指向数据目录 files 之外，备份已拒绝") from exc
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise BackupError("PDF 路径含不安全的目录分量")
    current = root / "files"
    if current.is_symlink() or not current.is_dir():
        raise BackupError("PDF 文件目录不存在或为符号链接")
    for part in relative.parts[:-1]:
        current /= part
        if current.is_symlink() or not current.is_dir():
            raise BackupError("PDF 路径中包含链接或无效目录")
    _regular(path)
    return path


@contextmanager
def _readonly(path, immutable=False):
    uri = path.resolve().as_uri() + "?mode=ro" + ("&immutable=1" if immutable else "")
    db = sqlite3.connect(uri, uri=True, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    db.execute("PRAGMA trusted_schema=OFF")
    try:
        yield db
    finally:
        db.close()


@contextmanager
def _writable(path):
    db = sqlite3.connect(path)
    db.execute("PRAGMA trusted_schema=OFF")
    try:
        with db:
            yield db
    finally:
        db.close()


def _database_papers(path):
    try:
        with _readonly(path, immutable=True) as db:
            schema = db.execute("SELECT type,name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
            if any(row["type"] in {"view", "trigger"} for row in schema):
                raise BackupError("备份数据库不允许视图或触发器")
            tables = {row["name"] for row in schema if row["type"] == "table"}
            if "papers" not in tables or not tables.issubset(TABLES):
                raise BackupError("数据库表结构不属于支持的科研工作区")
            if [row[0] for row in db.execute("PRAGMA integrity_check")] != ["ok"]:
                raise BackupError("SQLite 完整性检查失败")
            if db.execute("PRAGMA foreign_key_check").fetchone():
                raise BackupError("SQLite 外键完整性检查失败")
            papers = [dict(row) for row in db.execute("SELECT id,filename,path,sha256 FROM papers ORDER BY id")]
            for paper in papers:
                if any(not isinstance(paper[key], str) or not paper[key] for key in ("id", "filename", "path")) or not _digest(paper["sha256"]):
                    raise BackupError("文献标识或 SHA256 无效")
            return papers
    except sqlite3.Error as exc:
        raise BackupError("SQLite 快照损坏或表结构不受支持") from exc


def _copy_and_hash(source, target=None, expected_size=None, limit=None):
    digest, size = hashlib.sha256(), 0
    while chunk := source.read(CHUNK):
        size += len(chunk)
        if limit is not None and size > limit:
            raise BackupError("文件超过备份允许的大小")
        digest.update(chunk)
        if target is not None:
            target.write(chunk)
    if expected_size is not None and size != expected_size:
        raise BackupError("文件实际大小与清单不一致")
    return digest.hexdigest(), size


def _file_hash(path):
    with _open_regular(path) as (stream, _):
        return _copy_and_hash(stream)[0]


def _sync_directory(path):
    if os.name == "posix":
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _snapshot_readonly(root, snapshot):
    """Use SQLite's online backup API, including committed WAL transactions.

    The source connection is read-only. SQLite may maintain WAL/SHM auxiliary
    files for its locking protocol; business records and PDFs are not changed.
    """
    source_path = root / DATABASE
    before = _regular(source_path)
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = root / (DATABASE + suffix)
        if sidecar.exists() or sidecar.is_symlink():
            _regular(sidecar)
    try:
        with _readonly(source_path) as source, _writable(snapshot) as destination:
            source.backup(destination, pages=256, sleep=0.01)
            destination.execute("PRAGMA journal_mode=DELETE")
    except sqlite3.Error as exc:
        raise BackupError("无法生成 SQLite 一致快照") from exc
    after = _regular(source_path)
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        raise BackupError("生成快照期间源数据库被替换")


def _recheck_sources(root, papers, signatures, original_db):
    current_db = _regular(root / DATABASE)
    if (current_db.st_dev, current_db.st_ino) != (original_db.st_dev, original_db.st_ino):
        raise BackupError("备份期间源数据库被替换")
    with tempfile.TemporaryDirectory(prefix="research-recheck-") as work:
        snapshot = Path(work) / DATABASE
        _snapshot_readonly(root, snapshot)
        current = {paper["id"]: (paper["path"], paper["sha256"]) for paper in _database_papers(snapshot)}
    for paper in papers:
        if current.get(paper["id"]) != (paper["path"], paper["sha256"]):
            raise BackupError("备份期间文献被删除或引用发生变化，请重试")
        path = _source_pdf(root, paper["path"])
        if _signature(_regular(path)) != signatures[paper["id"]]:
            raise BackupError("备份期间 PDF 被删除、替换或修改，请重试")


def create_backup(source_dir, archive_path):
    """Create a new ZIP without changing source business records or PDFs."""
    root, output = _directory(source_dir), _new_target(archive_path)
    if output == root or root in output.parents:
        raise BackupError("备份输出必须放在源数据目录之外")
    if output.exists() or output.is_symlink():
        raise BackupError("备份输出已存在，拒绝覆盖")
    original_db = _regular(root / DATABASE)
    descriptor, staging_name = tempfile.mkstemp(prefix=".research-backup-", suffix=".tmp", dir=output.parent)
    os.close(descriptor)
    staging = Path(staging_name)
    try:
        with tempfile.TemporaryDirectory(prefix="research-backup-") as work:
            snapshot = Path(work) / DATABASE
            _snapshot_readonly(root, snapshot)
            papers = _database_papers(snapshot)
            records, signatures = [], {}
            with zipfile.ZipFile(staging, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                for paper in papers:
                    path = _source_pdf(root, paper["path"])
                    member = f"files/{paper['sha256']}.pdf"
                    with _open_regular(path) as (stream, before), archive.open(member, "w", force_zip64=True) as entry:
                        signatures[paper["id"]] = _signature(before)
                        digest, size = _copy_and_hash(stream, entry, expected_size=before.st_size, limit=MAX_PDF)
                    if digest != paper["sha256"]:
                        raise BackupError("PDF 内容与数据库记录的 SHA256 不一致")
                    records.append({"id": paper["id"], "filename": paper["filename"], "path": member, "sha256": digest, "size": size})
                # Make the snapshot portable and avoid retaining source absolute paths.
                with _writable(snapshot) as db:
                    db.executemany("UPDATE papers SET path=? WHERE id=?", [(item["path"], item["id"]) for item in records])
                db_record = {"path": DATABASE, "size": snapshot.stat().st_size, "sha256": _file_hash(snapshot)}
                manifest = {"format": FORMAT, "version": VERSION, "created_at": _timestamp(),
                            "database": db_record, "papers": records, "excluded": ["settings.json", ".env"]}
                archive.write(snapshot, DATABASE)
                archive.writestr(MANIFEST, json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
            report = verify_backup(staging)
            _recheck_sources(root, papers, signatures, original_db)
        with staging.open("rb") as stream:
            os.fsync(stream.fileno())
        # Exclusive publication: a simultaneous creator cannot be overwritten.
        os.link(staging, output)
        staging.unlink()
        _sync_directory(output.parent)
        return {**report, "archive": str(output)}
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise BackupError("备份失败：文件被更改、删除、目标已存在或无法写入") from exc
    finally:
        staging.unlink(missing_ok=True)


def _manifest(archive):
    members = archive.infolist()
    names = [member.filename for member in members]
    if len(names) != len(set(names)) or MANIFEST not in names or DATABASE not in names:
        raise BackupError("备份包缺少必要文件或有重复路径")
    total = 0
    for member in members:
        name = member.filename
        path = PurePosixPath(name)
        mode = member.external_attr >> 16
        if name != str(path) or path.is_absolute() or "\\" in name or "\x00" in member.orig_filename or any(part in {".", ".."} for part in path.parts):
            raise BackupError("备份包包含路径穿越或非规范路径")
        if member.is_dir() or stat.S_IFMT(mode) not in {0, stat.S_IFREG} or member.flag_bits & 1:
            raise BackupError("备份包不允许链接、目录或加密条目")
        if member.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
            raise BackupError("备份包压缩格式不受支持")
        limit = MAX_MANIFEST if name == MANIFEST else MAX_DATABASE if name == DATABASE else MAX_PDF
        total += member.file_size
        if member.file_size > limit or total > MAX_ARCHIVE_CONTENT:
            raise BackupError("备份包超过大小限制")
    try:
        manifest = json.loads(archive.read(MANIFEST))
    except (ValueError, UnicodeError) as exc:
        raise BackupError("备份清单不是有效的 JSON") from exc
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT or manifest.get("version") != VERSION:
        raise BackupError("备份格式或版本不受支持")
    database, papers = manifest.get("database"), manifest.get("papers")
    if not isinstance(database, dict) or database.get("path") != DATABASE or not isinstance(papers, list):
        raise BackupError("备份清单结构无效")
    expected, ids = {DATABASE, MANIFEST}, set()
    for record in [database, *papers]:
        if not isinstance(record, dict) or not _digest(record.get("sha256")) or type(record.get("size")) is not int or record["size"] < 0:
            raise BackupError("备份清单的摘要或大小无效")
    for paper in papers:
        paper_id = paper.get("id")
        if not isinstance(paper_id, str) or not paper_id or paper_id in ids or not isinstance(paper.get("filename"), str):
            raise BackupError("文献清单标识或文件名无效")
        if paper.get("path") != f"files/{paper['sha256']}.pdf" or paper["path"] in expected:
            raise BackupError("PDF 清单路径重复或不符合规范")
        ids.add(paper_id)
        expected.add(paper["path"])
    if set(names) != expected:
        raise BackupError("备份包文件与清单不一致，或包含未允许的额外文件")
    for record in [database, *papers]:
        if archive.getinfo(record["path"]).file_size != record["size"]:
            raise BackupError("备份包文件大小与清单不一致")
    return manifest


@contextmanager
def _verified(archive_path, expected_sha256=None):
    path = Path(archive_path).expanduser().absolute()
    if expected_sha256 is not None and not _digest(expected_sha256):
        raise BackupError("指定的 SHA256 应为 64 位小写十六进制字符串")
    try:
        with _open_regular(path) as (stream, _), tempfile.TemporaryDirectory(prefix="research-verify-") as work:
            archive_sha256 = _copy_and_hash(stream)[0]
            if expected_sha256 is not None and archive_sha256 != expected_sha256:
                raise BackupError("备份包 SHA256 与指定摘要不一致")
            stream.seek(0)
            with zipfile.ZipFile(stream, "r") as archive:
                manifest = _manifest(archive)
                snapshot = Path(work) / DATABASE
                for record in [manifest["database"], *manifest["papers"]]:
                    with archive.open(record["path"]) as entry:
                        if record["path"] == DATABASE:
                            with snapshot.open("xb") as output:
                                digest, _ = _copy_and_hash(entry, output, record["size"], MAX_DATABASE)
                        else:
                            digest, _ = _copy_and_hash(entry, expected_size=record["size"], limit=MAX_PDF)
                    if digest != record["sha256"]:
                        raise BackupError(f"SHA256 校验失败：{record['path']}")
                papers = _database_papers(snapshot)
                entries = {item["id"]: item for item in manifest["papers"]}
                if {paper["id"] for paper in papers} != set(entries):
                    raise BackupError("数据库文献与备份清单不一致")
                for paper in papers:
                    item = entries[paper["id"]]
                    if any(paper[key] != item[key] for key in ("filename", "path", "sha256")):
                        raise BackupError("数据库 PDF 引用与清单不一致")
                report = {"ok": True, "archive": str(path), "archive_sha256": archive_sha256,
                          "format_version": VERSION, "created_at": manifest.get("created_at"),
                          "paper_count": len(papers), "file_count": len(entries) + 2,
                          "total_bytes": sum(item["size"] for item in [manifest["database"], *manifest["papers"]]),
                          "settings_included": False}
                yield archive, manifest, snapshot, report
    except (OSError, sqlite3.Error, zipfile.BadZipFile, RuntimeError, EOFError, zlib.error) as exc:
        raise BackupError("备份包损坏、被修改或无法读取") from exc


def verify_backup(archive_path, expected_sha256=None):
    """Independently check archive paths, all hashes, SQLite and PDF references."""
    with _verified(archive_path, expected_sha256) as (_, _, _, report):
        return report


def _pause_restored_jobs(snapshot):
    with _writable(snapshot) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "jobs" in tables:
            db.execute("""UPDATE papers SET status='paused' WHERE id IN (
                SELECT paper_id FROM jobs WHERE kind='parse' AND status IN ('queued','processing','pause_requested','paused'))""")
            db.execute("""UPDATE papers SET status='cancelled' WHERE id IN (
                SELECT paper_id FROM jobs WHERE kind='parse' AND status='cancel_requested')""")
            db.execute("""UPDATE jobs SET status='paused',message='从备份恢复，已暂停；请检查配置后手动继续',updated_at=?
                WHERE status IN ('queued','processing','pause_requested','paused')""", (_timestamp(),))
            db.execute("UPDATE jobs SET status='cancelled',message='从备份恢复，已保留取消请求',updated_at=? WHERE status='cancel_requested'", (_timestamp(),))
        db.execute("UPDATE papers SET status='paused' WHERE status IN ('queued','processing','pause_requested','paused')")
        db.execute("UPDATE papers SET status='cancelled' WHERE status='cancel_requested'")


def restore_backup(archive_path, target_dir, expected_sha256=None):
    """Restore into a new/empty directory, never merge with running app data."""
    target = _new_target(target_dir)
    if target.is_symlink() or target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise BackupError("恢复目标必须为新的或空目录，拒绝覆盖现有数据")
    initial = target.stat() if target.exists() else None
    staging = Path(tempfile.mkdtemp(prefix=".research-restore-", dir=target.parent))
    try:
        with _verified(archive_path, expected_sha256) as (archive, manifest, snapshot, report):
            (staging / "files").mkdir()
            shutil.copyfile(snapshot, staging / DATABASE)
            for record in manifest["papers"]:
                output = staging / record["path"]
                with archive.open(record["path"]) as entry, output.open("xb") as stream:
                    digest, _ = _copy_and_hash(entry, stream, record["size"], MAX_PDF)
                    stream.flush()
                    os.fsync(stream.fileno())
                if digest != record["sha256"]:
                    raise BackupError("恢复读取期间 PDF 摘要发生变化")
            with _writable(staging / DATABASE) as db:
                db.executemany("UPDATE papers SET path=? WHERE id=?", [(str(target / paper["path"]), paper["id"]) for paper in manifest["papers"]])
            _pause_restored_jobs(staging / DATABASE)
            _database_papers(staging / DATABASE)
            with (staging / DATABASE).open("rb") as stream:
                os.fsync(stream.fileno())
            _sync_directory(staging / "files")
            _sync_directory(staging)
        # Recheck only after all archive reads have completed successfully.
        if initial is not None:
            current = target.lstat()
            if target.is_symlink() or (current.st_dev, current.st_ino) != (initial.st_dev, initial.st_ino):
                raise BackupError("恢复目标在处理期间发生变化")
            target.rmdir()  # Refuses a target populated while verification ran.
        elif target.exists() or target.is_symlink():
            raise BackupError("恢复目标在处理期间已被创建")
        os.rename(staging, target)
        _sync_directory(target.parent)
        return {**report, "target": str(target), "active_tasks_paused": True}
    except (OSError, sqlite3.Error) as exc:
        raise BackupError("恢复失败：目标被占用、发生变化或无法写入") from exc
    finally:
        if staging.exists():
            shutil.rmtree(staging)
