"""One application process owns a data directory until its workers stop."""
from __future__ import annotations

import errno
import os
from pathlib import Path


class WorkspaceInUseError(RuntimeError):
    pass


class WorkspaceLock:
    """An OS-owned, nonblocking lock; process death releases it automatically.

    Keep the file after releasing it. Unlinking a locked inode would let another
    process create a different file and acquire an unrelated lock at that path.
    """
    def __init__(self, directory: Path):
        self.directory = directory.resolve()
        self.path = self.directory / ".workspace.lock"
        self.descriptor: int | None = None

    def acquire(self):
        if self.descriptor is not None:
            raise RuntimeError("工作区锁已被当前对象持有")
        self.directory.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise RuntimeError("工作区锁文件不能是符号链接")
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            if os.name == "nt":
                import msvcrt
                if os.fstat(descriptor).st_size == 0:
                    os.write(descriptor, b"\0")
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            os.close(descriptor)
            if error.errno in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}:
                raise WorkspaceInUseError("此数据目录已由另一个应用实例使用。请先关闭已运行的实例，或使用不同的数据目录。") from None
            raise
        except BaseException:
            os.close(descriptor)
            raise
        self.descriptor = descriptor
        return self

    def release(self):
        if self.descriptor is not None:
            descriptor, self.descriptor = self.descriptor, None
            # Closing the descriptor releases flock / the Windows byte lock.
            os.close(descriptor)

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *_):
        self.release()
