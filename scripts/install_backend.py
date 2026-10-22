#!/usr/bin/env python3
"""Install locked Python dependencies when the manifest contents change."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


MARKER = ".research-deps-installed.json"


def dependency_fingerprint(root: Path) -> dict:
    return {"version": 1, **{
        name: hashlib.sha256((root / name).read_bytes()).hexdigest()
        for name in ("requirements.txt", "requirements.lock")
    }}


def ensure_dependencies(root: Path, venv: Path | None = None) -> bool:
    root = root.resolve()
    venv = (venv or root / ".venv").resolve()
    python = venv / "bin" / "python"
    if not python.is_file():
        raise RuntimeError("请先创建 Python 3.12 虚拟环境。")
    subprocess.run([str(python), "-c", "import sys; sys.exit(0 if sys.version_info[:2] == (3, 12) else '当前虚拟环境需要 Python 3.12。')"], check=True)
    expected = dependency_fingerprint(root)
    marker = venv / MARKER
    try:
        previous = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = None
    if previous == expected:
        return False

    # A failed install can leave a partially updated environment. Never trust
    # its previous marker, including when the user restores an older lockfile.
    marker.unlink(missing_ok=True)
    print("正在按锁文件安装后端依赖…", flush=True)
    uv = shutil.which("uv")
    command = ([uv, "pip", "install", "--python", str(python)] if uv else
               [str(python), "-m", "pip", "install"])
    subprocess.run([*command, "-r", str(root / "requirements.lock")], cwd=root, check=True)
    if dependency_fingerprint(root) != expected:
        raise RuntimeError("安装期间后端依赖文件发生变化，请重新运行启动脚本。")
    pending = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=venv,
                                         prefix=MARKER + ".", delete=False) as handle:
            pending = Path(handle.name)
            json.dump(expected, handle, sort_keys=True)
            handle.write("\n")
        os.replace(pending, marker)
    finally:
        if pending is not None:
            pending.unlink(missing_ok=True)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="按 requirements 文件内容指纹安装后端依赖。")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    options = parser.parse_args()
    try:
        ensure_dependencies(options.root)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"后端依赖安装未完成：{error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
