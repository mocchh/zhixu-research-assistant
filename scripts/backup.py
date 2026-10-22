#!/usr/bin/env python3
"""本地科研助手的独立备份工具。"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.backup import BackupError, create_backup, restore_backup, verify_backup


def main(argv=None):
    parser = argparse.ArgumentParser(description="备份、校验和恢复科研助手数据；不复制模型密钥设置。",
        epilog="业务数据与 PDF 只读；SQLite 在线快照可能维护 WAL/SHM 辅助文件。请另存输出的 archive_sha256 用于校验。")
    commands = parser.add_subparsers(dest="command", required=True, title="命令")
    create = commands.add_parser("create", help="创建 SQLite 一致快照与所引用 PDF 的 ZIP 备份")
    create.add_argument("--data", type=Path, default=Path("data"), help="源数据目录（默认：data，只读）")
    create.add_argument("--output", type=Path, required=True, help="新 ZIP 文件路径，须位于源数据目录之外")
    verify = commands.add_parser("verify", help="独立校验清单、SHA256、SQLite 与全部 PDF")
    verify.add_argument("archive", type=Path, help="备份 ZIP 文件")
    verify.add_argument("--sha256", help="另行保存的备份包 SHA256；同时检测清单被整体改写")
    restore = commands.add_parser("restore", help="恢复到新/空目录；活动任务保持暂停，不自动调用模型")
    restore.add_argument("archive", type=Path, help="备份 ZIP 文件")
    restore.add_argument("--target", type=Path, required=True, help="新的或空的恢复目录；拒绝覆盖现有数据")
    restore.add_argument("--sha256", help="另行保存的备份包 SHA256")
    args = parser.parse_args(argv)
    try:
        if args.command == "create":
            report = create_backup(args.data, args.output)
        elif args.command == "verify":
            report = verify_backup(args.archive, args.sha256)
        else:
            report = restore_backup(args.archive, args.target, args.sha256)
    except BackupError as exc:
        print(f"失败：{exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
