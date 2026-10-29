"""平台内置备份：pg_dump 归档、元信息、保留清理。

单实例应用，进程内锁避免手动与定时任务重叠；数据库侧仍由 pg_dump 保证
一致性快照。备份目录与文件权限收窄，元信息不包含数据库口令。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import SessionLocal
from app.ops.snapshot import (
    TABLES,
    _business_sql,
    _canonical_digest,
    _counts_sql,
    _status_sql,
)

logger = logging.getLogger("opssop.backup")

_NAME_RE = re.compile(r"^opssop-\d{8}T\d{6}Z$")
_LOCK = threading.Lock()
_SCHEMA = "opssop-backup/1"
# 最近一次备份成功/失败状态(管理员可见;失败也要能被发现,而不是只埋在容器日志里)
_STATUS_FILE = "backup-status.json"


class BackupError(RuntimeError):
    pass


class BackupBusy(BackupError):
    pass


def _dir(settings: Settings) -> Path:
    path = Path(settings.backup_dir)
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        raise BackupError(f"备份目录不可用: {path}") from exc
    if not path.is_dir():
        raise BackupError(f"备份路径不是目录: {path}")
    return path


def read_backup_status(settings: Settings) -> dict:
    """读取最近备份成功/失败状态;文件缺失或损坏时返回空状态(不抛异常)。"""
    try:
        directory = _dir(settings)
    except BackupError:
        return {}
    try:
        data = json.loads(_status_path(directory).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def record_backup_success(settings: Settings, name: str, created_at: str) -> None:
    _write_status(settings, {"last_success_at": created_at, "last_success_name": name})


def record_backup_failure(settings: Settings, reason: str) -> None:
    _write_status(
        settings,
        {
            "last_failure_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "last_failure_reason": reason[:300],
        },
    )


def _write_status(settings: Settings, update: dict) -> None:
    try:
        directory = _dir(settings)
        current = read_backup_status(settings)
        current.update(update)
        tmp = directory / f".{_STATUS_FILE}.tmp"
        tmp.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, _status_path(directory))
    except (BackupError, OSError):
        # 状态记录失败只降级可见性,绝不影响备份/业务,也不能掩盖原始失败原因
        logger.warning("写入备份状态文件失败", exc_info=True)


def _status_path(directory: Path) -> Path:
    return directory / _STATUS_FILE


def backup_status(settings: Settings) -> dict:
    """最近成功/失败状态与备份年龄(小时),供接口、告警和维护任务共用。

    老版本升级后状态文件不存在时,用最新备份时间兜底,避免误报"从未成功"。
    """
    status = read_backup_status(settings)
    last_success_at = status.get("last_success_at")
    if not last_success_at:
        try:
            items = list_backups(settings)
            if items:
                last_success_at = items[0].get("created_at")
        except BackupError:
            pass
    age_hours = None
    if last_success_at:
        try:
            created = datetime.fromisoformat(str(last_success_at))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            age_hours = round((datetime.now(timezone.utc) - created).total_seconds() / 3600, 1)
        except ValueError:
            age_hours = None
    return {
        "last_success_at": last_success_at,
        "last_failure_at": status.get("last_failure_at"),
        "last_failure_reason": status.get("last_failure_reason"),
        "backup_age_hours": age_hours,
    }


def valid_name(name: str) -> bool:
    return bool(_NAME_RE.fullmatch(name))


def _metadata_path(directory: Path, name: str) -> Path:
    return directory / f"{name}.json"


def _dump_path(directory: Path, name: str) -> Path:
    return directory / f"{name}.dump"


def list_backups(settings: Settings) -> list[dict]:
    directory = _dir(settings)
    items: list[dict] = []
    for meta_file in sorted(directory.glob("opssop-*.json"), reverse=True):
        name = meta_file.stem
        if not valid_name(name):
            continue
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        dump = _dump_path(directory, name)
        if not dump.is_file():
            continue
        items.append(
            {
                "name": name,
                "created_at": meta.get("created_at"),
                "size_bytes": dump.stat().st_size,
                "sha256": meta.get("sha256", ""),
                "trigger": meta.get("trigger", ""),
                "retention_days": meta.get("retention_days"),
            }
        )
    items.sort(key=lambda item: item.get("created_at") or "", reverse=True)
    return items


def prune_backups(settings: Settings, now: datetime | None = None) -> list[str]:
    """删除超过保留期的备份对;同时清理没有配对元信息的孤立 .dump。

    元信息缺失的孤立 dump 不会出现在备份列表里,只按文件修改时间判断年龄,
    否则会永久占用磁盘直到写满。
    """
    directory = _dir(settings)
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=settings.backup_retention_days)
    removed: list[str] = []
    paired: set[str] = set()
    for meta_file in directory.glob("opssop-*.json"):
        name = meta_file.stem
        if not valid_name(name):
            continue
        paired.add(name)
        dump = _dump_path(directory, name)
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            created = datetime.fromisoformat(str(meta.get("created_at")))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
        except (OSError, ValueError, TypeError):
            # 元信息损坏时按文件修改时间判断，避免永久占用空间
            try:
                created = datetime.fromtimestamp(meta_file.stat().st_mtime, tz=timezone.utc)
            except OSError:
                continue
        if created >= cutoff:
            continue
        for path in (dump, meta_file):
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("删除过期备份失败 %s: %s", path.name, exc)
        removed.append(name)
    for dump in directory.glob("opssop-*.dump"):
        name = dump.name[: -len(".dump")]
        if not valid_name(name) or name in paired:
            continue
        try:
            created = datetime.fromtimestamp(dump.stat().st_mtime, tz=timezone.utc)
            if created >= cutoff:
                continue
            dump.unlink()
        except OSError as exc:
            logger.warning("清理孤立备份失败 %s: %s", dump.name, exc)
            continue
        removed.append(name)
    _prune_stale_temp_files(directory, cutoff)
    return removed


def _prune_stale_temp_files(directory: Path, cutoff: datetime) -> None:
    """崩溃残留的临时文件(以 . 开头,不出现在列表里)超过保留期后一并清理。"""
    for tmp in directory.glob(".opssop-*.tmp"):
        try:
            if datetime.fromtimestamp(tmp.stat().st_mtime, tz=timezone.utc) < cutoff:
                tmp.unlink()
        except OSError as exc:
            logger.warning("清理备份临时文件失败 %s: %s", tmp.name, exc)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_metadata(directory: Path, name: str, metadata: dict) -> None:
    """元信息与 dump 成对落盘;写入失败时删除对应 dump,不留没有元信息的孤立归档。"""
    meta_file = _metadata_path(directory, name)
    meta_tmp = directory / f".{name}.json.tmp"
    dump = _dump_path(directory, name)
    try:
        meta_tmp.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        os.chmod(meta_tmp, 0o600)
        os.replace(meta_tmp, meta_file)
    except OSError as exc:
        dump.unlink(missing_ok=True)
        meta_tmp.unlink(missing_ok=True)
        raise BackupError(f"写入备份元信息失败: {type(exc).__name__}") from exc


def _snapshot(db: Session) -> dict:
    """采集与 ops_cli 恢复工具兼容的计数/状态/内容摘要。"""
    try:
        counts = json.loads(db.execute(text(_counts_sql())).scalar_one())
        statuses = json.loads(db.execute(text(_status_sql())).scalar_one())
        business = json.loads(db.execute(text(_business_sql())).scalar_one())
        meta = json.loads(
            db.execute(
                text(
                    """
                    SELECT json_build_object(
                      'database', current_database(),
                      'server_version', current_setting('server_version'),
                      'alembic_version', (SELECT version_num FROM alembic_version),
                      'vector_version', (SELECT extversion FROM pg_extension WHERE extname='vector')
                    )::text
                    """
                )
            ).scalar_one()
        )
    except Exception as exc:  # noqa: BLE001 - 快照失败即中止备份
        raise BackupError(f"数据库快照失败: {type(exc).__name__}") from exc
    return {
        "database": meta.get("database"),
        "server_version": meta.get("server_version"),
        "alembic_version": meta.get("alembic_version"),
        "vector_version": meta.get("vector_version"),
        "counts": counts,
        "statuses": statuses,
        "business_digest": _canonical_digest(business),
        "table_order": TABLES,
    }


def run_backup(settings: Settings, trigger: str, db: Session | None = None) -> dict:
    if not settings.backup_enabled:
        raise BackupError("备份功能未启用(BACKUP_ENABLED=0)")
    if not _LOCK.acquire(blocking=False):
        raise BackupBusy("已有备份任务在进行")
    owned_session = db is None
    session = db or SessionLocal()
    try:
        snapshot = _snapshot(session)
        directory = _dir(settings)
        pg_dump = shutil.which("pg_dump")
        if not pg_dump:
            raise BackupError("容器内未找到 pg_dump，无法创建备份")
        now = datetime.now(timezone.utc)
        name = f"opssop-{now.strftime('%Y%m%dT%H%M%SZ')}"
        dump = _dump_path(directory, name)
        temp = directory / f".{name}.dump.tmp"
        env = os.environ.copy()
        env["PGPASSWORD"] = settings.postgres_password
        env["PGCONNECT_TIMEOUT"] = "15"
        command = [
            pg_dump,
            "--format=custom",
            "--host",
            settings.postgres_host,
            "--port",
            str(settings.postgres_port),
            "--username",
            settings.postgres_user,
            "--dbname",
            settings.postgres_db,
        ]
        try:
            with temp.open("wb") as handle:
                completed = subprocess.run(
                    command,
                    stdout=handle,
                    stderr=subprocess.PIPE,
                    env=env,
                    timeout=600,
                    check=False,
                )
        except (OSError, subprocess.TimeoutExpired) as exc:
            temp.unlink(missing_ok=True)
            raise BackupError(f"执行 pg_dump 失败: {type(exc).__name__}") from exc
        if completed.returncode != 0:
            temp.unlink(missing_ok=True)
            detail = (completed.stderr or b"").decode("utf-8", "replace").strip().splitlines()
            reason = detail[-1][:300] if detail else f"exit {completed.returncode}"
            raise BackupError(f"pg_dump 失败: {reason}")
        os.replace(temp, dump)
        os.chmod(dump, 0o600)
        size = dump.stat().st_size
        digest = _sha256(dump)
        metadata = {
            # 平台管理字段
            "name": name,
            "created_at": now.isoformat(),
            "size_bytes": size,
            "sha256": digest,
            "trigger": trigger,
            "retention_days": settings.backup_retention_days,
            # 与 ops_cli 恢复工具兼容的归档元信息
            "schema_version": _SCHEMA,
            "run_id": name,
            "format": "pg_dump-Fc",
            "archive": {"name": dump.name, "bytes": size, "sha256": digest},
            "database": {
                "name": snapshot.get("database"),
                "server_version": snapshot.get("server_version"),
                "alembic_version": snapshot.get("alembic_version"),
                "vector_version": snapshot.get("vector_version"),
            },
            "source_snapshot": snapshot,
            "notes": [
                "由平台内置备份任务生成;归档含业务数据、账号哈希和索引状态。",
                "恢复请使用 backend/scripts/ops_cli.py restore 并成对提供 .dump/.json。",
            ],
        }
        _write_metadata(directory, name, metadata)
        record_backup_success(settings, name, metadata["created_at"])
        removed = prune_backups(settings)
        logger.info("备份完成 name=%s size=%s pruned=%s", name, metadata["size_bytes"], len(removed))
        return {**metadata, "path": str(dump), "pruned": removed}
    finally:
        if owned_session:
            session.close()
        _LOCK.release()
