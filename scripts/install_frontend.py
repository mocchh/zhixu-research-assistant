#!/usr/bin/env python3
"""Install the locked frontend dependencies when their verified state changes."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile


MARKER = ".research-deps-installed.json"


def dependency_fingerprint(frontend: Path) -> dict:
    return {"version": 1, **{
        name: hashlib.sha256((frontend / name).read_bytes()).hexdigest()
        for name in ("package.json", "package-lock.json")
    }}


def ensure_dependencies(frontend: Path) -> bool:
    """Return whether npm ci ran; only a successful unchanged install is trusted."""
    frontend = frontend.resolve()
    expected = dependency_fingerprint(frontend)
    modules = frontend / "node_modules"
    marker = modules / MARKER
    try:
        previous = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = None
    if modules.is_dir() and previous == expected:
        return False

    # npm ci may partially replace node_modules before failing. An older marker
    # must not remain trustworthy even if the user later restores an old lock.
    marker.unlink(missing_ok=True)
    print("正在按锁文件安装前端依赖…", flush=True)
    subprocess.run(["npm", "ci"], cwd=frontend, check=True)
    if dependency_fingerprint(frontend) != expected:
        raise RuntimeError("安装期间前端依赖文件发生变化，请重新运行启动脚本。")
    if not modules.is_dir():
        raise RuntimeError("npm ci 未生成 node_modules，未记录安装成功。")

    pending = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=modules,
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
    parser = argparse.ArgumentParser(description="按 package.json 和锁文件指纹安装前端依赖。")
    parser.add_argument("--frontend", type=Path, default=Path(__file__).resolve().parents[1] / "frontend")
    options = parser.parse_args()
    try:
        ensure_dependencies(options.frontend)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"前端依赖安装未完成：{error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
