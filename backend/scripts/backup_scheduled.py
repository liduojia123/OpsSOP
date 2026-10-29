# -*- coding: utf-8 -*-
"""定时备份包装器：调用 ops_cli.py 备份、清理旧备份、写日志。

由 cron / systemd timer / Windows 任务计划程序调用。只依赖 Python 3.10+
标准库和 Docker CLI；备份内容与校验仍由 backend/scripts/ops_cli.py 完成。

用法（Linux，每天 02:30）:
  python3 backend/scripts/backup_scheduled.py \
    --project opssop-prod --env-file .env.production \
    --out-dir /var/backups/opssop --keep 14

设计约束:
- 同一输出目录同一时间只允许一个实例（锁文件）；
- 备份成功后最多保留 --keep 份，只删除含 opsop-*.json 元信息的目录；
- 失败时保留现场并返回非零，便于监控告警；
- 不打印任何口令或密钥。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

LOCK_STALE_SECONDS = 6 * 3600


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def log(path: Path, message: str) -> None:
    line = f"[{now()}] {message}"
    print(line)
    try:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError as exc:
        print(f"[backup] 写日志失败: {exc}", file=sys.stderr)


def acquire_lock(out_dir: Path) -> Path | None:
    out_dir.mkdir(parents=True, exist_ok=True)
    lock = out_dir / ".backup.lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        age = time.time() - lock.stat().st_mtime
        if age < LOCK_STALE_SECONDS:
            return None
        lock.unlink(missing_ok=True)
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(f"pid={os.getpid()} started={now()}\n")
    return lock


def prune(out_dir: Path, keep: int, log_path: Path) -> list[str]:
    if keep <= 0:
        return []
    candidates: list[Path] = []
    for child in out_dir.iterdir():
        if not child.is_dir():
            continue
        dumps = list(child.glob("*.dump"))
        metadata = list(child.glob("opssop-*.json"))
        if dumps and metadata:
            candidates.append(child)
    candidates.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    removed: list[str] = []
    for old in candidates[keep:]:
        for file in sorted(old.iterdir(), key=lambda item: item.name):
            if file.is_file():
                file.unlink()
        try:
            old.rmdir()
            removed.append(old.name)
        except OSError as exc:
            log(log_path, f"清理旧备份目录失败 {old.name}: {exc}")
    return removed


def main() -> int:
    parser = argparse.ArgumentParser(description="OpsSOP 定时备份包装器")
    parser.add_argument("--project", required=True, help="Compose 项目名，例如 opssop-prod")
    parser.add_argument("--env-file", required=True, help="生产环境文件路径")
    parser.add_argument("--out-dir", required=True, help="备份根目录")
    parser.add_argument("--keep", type=int, default=14, help="保留最近 N 份（默认 14）")
    parser.add_argument(
        "--compose-file",
        action="append",
        default=None,
        help="Compose 文件，可重复；默认 docker-compose.yml + docker-compose.prod.yml",
    )
    parser.add_argument("--repo-root", default=str(Path(__file__).resolve().parents[2]))
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    out_dir = Path(args.out_dir).resolve()
    log_path = out_dir / "backup.log"
    env_file = Path(args.env_file).resolve()
    compose_files = args.compose_file or ["docker-compose.yml", "docker-compose.prod.yml"]
    for item in [env_file, *(repo_root / name for name in compose_files)]:
        if not item.exists():
            log(log_path, f"缺少文件: {item}")
            return 2

    lock = acquire_lock(out_dir)
    if lock is None:
        log(log_path, "已有备份进程运行，跳过本次")
        return 0

    run_id = "backup" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    backup_dir = out_dir / run_id
    backup_dir.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        str(repo_root / "backend" / "scripts" / "ops_cli.py"),
        "--repo-root",
        str(repo_root),
        "backup",
        "--project",
        args.project,
        "--env-file",
        str(env_file),
        "--run-id",
        run_id,
        "--out-dir",
        str(backup_dir),
        "--name",
        f"opssop-{run_id}.dump",
    ]
    for name in compose_files:
        command.extend(["--compose-file", name])

    log(log_path, f"开始备份 run_id={run_id} project={args.project}")
    try:
        completed = subprocess.run(
            command,
            cwd=str(repo_root),
            text=True,
            capture_output=True,
            timeout=60 * 60,
        )
    except subprocess.TimeoutExpired:
        log(log_path, f"备份超时 run_id={run_id}")
        return 1
    finally:
        lock.unlink(missing_ok=True)

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()[-3:]
        log(log_path, f"备份失败 run_id={run_id} exit={completed.returncode} {' | '.join(detail)}")
        return completed.returncode or 1

    removed = prune(out_dir, args.keep, log_path)
    summary = {
        "status": "ok",
        "run_id": run_id,
        "project": args.project,
        "backup_dir": str(backup_dir),
        "pruned": removed,
        "finished_at": now(),
    }
    log(log_path, json.dumps(summary, ensure_ascii=False))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
