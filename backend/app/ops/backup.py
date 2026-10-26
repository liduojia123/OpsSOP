"""逻辑备份:pg_dump -Fc + 元信息 + SHA-256 + 可读性校验。

二进制归档只通过 `docker cp` 传输,不经过文本管道。默认拒绝覆盖已有输出;
任何一步失败都非零,不把临时文件/半截元信息标成有效备份。
"""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

from .common import (
    OpsCancelled,
    OpsError,
    code_info,
    now_iso,
    sha256_file,
    validate_run_id,
)
from .dockerops import ComposeStack, compose_version, image_info
from .snapshot import take_snapshot

BACKUP_SCHEMA = "opssop-backup/1"


def _safe_archive_base(value: str) -> str:
    value = (value or "").strip()
    if not value or "/" in value or "\\" in value or ".." in value:
        raise OpsError("备份文件名不能包含路径分隔符", stage="backup")
    if any(ord(ch) < 32 for ch in value):
        raise OpsError("备份文件名不能包含控制字符", stage="backup")
    if len(value) > 120:
        raise OpsError("备份文件名过长", stage="backup")
    if not value.endswith(".dump"):
        value += ".dump"
    return value


def _container_temp(run_id: str) -> str:
    return f"/tmp/opssop-{run_id}-{secrets.token_hex(4)}.dump"


def run_backup(
    stack: ComposeStack,
    *,
    out_dir: Path,
    run_id: str,
    archive_name: str = "",
    repo_root: Path | None = None,
    image: str = "",
) -> dict:
    run_id = validate_run_id(run_id)
    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    base = _safe_archive_base(archive_name or f"opssop-backup-{run_id}")
    archive_path = out_dir / base
    metadata_path = out_dir / (base[:-5] + ".json") if base.endswith(".dump") else out_dir / (base + ".json")
    if archive_path.exists() or metadata_path.exists():
        raise OpsError(
            f"输出已存在,拒绝覆盖: {archive_path.name} / {metadata_path.name}",
            stage="backup",
        )

    container_path = _container_temp(run_id)
    archive_tmp = out_dir / f".{base}.tmp-{os.getpid()}-{secrets.token_hex(4)}"
    metadata_tmp = out_dir / f".{metadata_path.name}.tmp-{os.getpid()}-{secrets.token_hex(4)}"
    result: dict = {
        "schema_version": BACKUP_SCHEMA,
        "run_id": run_id,
        "success": False,
        "created_at": now_iso(),
        "archive_name": archive_path.name,
        "metadata_name": metadata_path.name,
        "archive_path": str(archive_path),
        "metadata_path": str(metadata_path),
        "container_temp": container_path,
        "container_temp_cleanup": None,
        "error": None,
    }
    try:
        source_snapshot = take_snapshot(stack)
        db_info = stack.psql_json(
            """
            SELECT json_build_object(
              'database', current_database(),
              'server_version', current_setting('server_version'),
              'alembic_version', (SELECT version_num FROM alembic_version),
              'vector_version', (SELECT extversion FROM pg_extension WHERE extname='vector')
            )::text
            """
        )
        stack.pg_dump_to_container(container_path)
        # 归档必须先能被 pg_restore 读取,才允许落盘为正式备份
        stack.pg_restore_list(container_path)
        stack.copy_from_container(container_path, archive_tmp)
        if not archive_tmp.exists() or archive_tmp.stat().st_size <= 0:
            raise OpsError("备份归档为空或未落盘", stage="backup")
        archive_bytes = archive_tmp.stat().st_size
        archive_sha = sha256_file(archive_tmp)

        metadata = {
            "schema_version": BACKUP_SCHEMA,
            "run_id": run_id,
            "created_at": result["created_at"],
            "format": "pg_dump-Fc",
            "archive": {
                "name": archive_path.name,
                "bytes": archive_bytes,
                "sha256": archive_sha,
            },
            "database": {
                "name": db_info.get("database"),
                "server_version": db_info.get("server_version"),
                "alembic_version": db_info.get("alembic_version"),
                "vector_version": db_info.get("vector_version"),
            },
            "tools": {
                "pg_dump": stack.pg_tool_version("pg_dump"),
                "pg_restore": stack.pg_tool_version("pg_restore"),
                "docker_compose": compose_version(),
            },
            "image": image_info(image) if image else None,
            "code": code_info(repo_root or Path.cwd()),
            "source_snapshot": source_snapshot,
            "notes": [
                "归档包含业务 schema、数据、序列、Alembic 版本和索引状态;不包含 PostgreSQL 角色/集群级配置。",
                "备份含账号哈希与业务内容,按私有数据保存,不得提交公开仓库。",
                "本文件仅记录计数/摘要,不包含业务全文或口令哈希。",
            ],
        }
        metadata_tmp.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        # 正式产物:先归档,再元信息;任一步失败都清理本次创建的文件
        os.replace(archive_tmp, archive_path)
        try:
            os.replace(metadata_tmp, metadata_path)
        except Exception:
            archive_path.unlink(missing_ok=True)
            raise

        result.update(
            {
                "success": True,
                "core_success": True,
                "archive_bytes": archive_bytes,
                "archive_sha256": archive_sha,
                "alembic_version": db_info.get("alembic_version"),
                "vector_version": db_info.get("vector_version"),
                "metadata": metadata,
            }
        )
    except KeyboardInterrupt as exc:
        archive_tmp.unlink(missing_ok=True)
        metadata_tmp.unlink(missing_ok=True)
        if archive_path.exists():
            archive_path.unlink(missing_ok=True)
        raise OpsCancelled("备份被用户取消", stage="backup") from exc
    except Exception as exc:
        archive_tmp.unlink(missing_ok=True)
        metadata_tmp.unlink(missing_ok=True)
        # 只清理本次运行可能已替换出的正式归档;元信息不存在时归档不完整
        if archive_path.exists() and not metadata_path.exists():
            archive_path.unlink(missing_ok=True)
        if isinstance(exc, OpsError):
            result["error"] = str(exc)
            raise
        raise OpsError(f"备份失败: {type(exc).__name__}", stage="backup") from exc
    finally:
        try:
            result["container_temp_cleanup"] = stack.remove_container_file(container_path)
        except Exception as exc:  # noqa: BLE001
            result["container_temp_cleanup"] = {
                "removed": False,
                "path": container_path,
                "error": str(exc)[:300],
            }

    cleanup = result.get("container_temp_cleanup") or {}
    cleanup_ok = isinstance(cleanup, dict) and cleanup.get("removed") is True
    result["cleanup_complete"] = cleanup_ok
    if result.get("success") and not cleanup_ok:
        # 归档/元信息已落盘,但临时文件清理失败:整轮不能返回完整成功
        result["success"] = False
        result["core_success"] = True
        result["status"] = "cleanup_incomplete"
        result["warning"] = "容器内临时归档清理失败,请按精确路径人工处理"
    elif result.get("success"):
        result["status"] = "success"
    else:
        result["status"] = "failed"
    return result
