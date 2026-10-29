"""同版本逻辑恢复:只允许新建目标库,先校验归档/元信息,再创建和恢复。

安全顺序:
1. 校验归档/元信息/大小/SHA-256/pg_restore 可读性;
2. 校验目标名、目标不存在、目标 != 来源、服务端主版本一致;
3. 才创建目标库并执行 pg_restore --exit-on-error;
4. 用快照比对计数/状态/业务摘要和 Alembic 版本;
5. 失败保留部分目标(默认)或只清理本轮确切创建的目标(显式开关)。
"""

from __future__ import annotations

import json
import re
import secrets
from pathlib import Path

from .common import (
    OpsCancelled,
    OpsError,
    atomic_write_json,
    code_info,
    now_iso,
    read_json,
    sha256_file,
    validate_run_id,
    validate_target_database,
)
from .dockerops import ComposeStack, compose_version, image_info
from .snapshot import TABLES, compare_snapshots, take_snapshot

RESTORE_SCHEMA = "opssop-restore/1"


def _major(version: str) -> str:
    return str(version or "").split(".", 1)[0]


def _container_temp(run_id: str) -> str:
    return f"/tmp/opssop-restore-{run_id}-{secrets.token_hex(4)}.dump"


def _validate_metadata(metadata: dict) -> dict:
    """创建目标库之前完成元信息结构/快照完整性校验。

    返回 source_snapshot;任何缺失或畸形字段都抛 OpsError,绝不先写目标。
    """
    problems: list[str] = []
    if not isinstance(metadata, dict):
        raise OpsError("备份元信息不是 JSON 对象", stage="precheck")
    if metadata.get("schema_version") != "opssop-backup/1":
        problems.append("schema_version 不是 opssop-backup/1")
    if metadata.get("format") != "pg_dump-Fc":
        problems.append("format 不是 pg_dump-Fc")
    archive = metadata.get("archive")
    if not isinstance(archive, dict):
        problems.append("archive 不是对象")
    else:
        if (
            not isinstance(archive.get("bytes"), int)
            or isinstance(archive.get("bytes"), bool)
            or archive["bytes"] <= 0
        ):
            problems.append("archive.bytes 不是正整数")
        if not re.fullmatch(r"[0-9a-f]{64}", str(archive.get("sha256") or "")):
            problems.append("archive.sha256 不是 SHA-256")
        if not isinstance(archive.get("name"), str) or not archive["name"]:
            problems.append("archive.name 缺失")
    database = metadata.get("database")
    if not isinstance(database, dict):
        problems.append("database 不是对象")
        database = {}
    source_db = database.get("name")
    if not isinstance(source_db, str) or not source_db:
        problems.append("database.name 缺失")
    if not isinstance(database.get("server_version"), str) or not database.get(
        "server_version"
    ):
        problems.append("database.server_version 缺失")

    snapshot = metadata.get("source_snapshot")
    if not isinstance(snapshot, dict):
        problems.append("source_snapshot 缺失或不是对象")
        snapshot = {}
    if not isinstance(snapshot.get("database"), str) or not snapshot.get("database"):
        problems.append("source_snapshot.database 缺失")
    elif source_db and snapshot.get("database") != source_db:
        problems.append("source_snapshot.database 与来源库不一致")
    if not isinstance(snapshot.get("server_version"), str) or not snapshot.get(
        "server_version"
    ):
        problems.append("source_snapshot.server_version 缺失")
    if not isinstance(snapshot.get("alembic_version"), str) or not snapshot.get(
        "alembic_version"
    ):
        problems.append("source_snapshot.alembic_version 缺失")
    # 恢复后比较依赖 vector_version;缺失/畸形必须在创建目标库前拒绝
    if "vector_version" not in snapshot:
        problems.append("source_snapshot.vector_version 缺失")
    elif snapshot["vector_version"] is not None and not (
        isinstance(snapshot["vector_version"], str) and snapshot["vector_version"]
    ):
        problems.append("source_snapshot.vector_version 不是非空字符串/null")
    counts = snapshot.get("counts")
    if not isinstance(counts, dict):
        problems.append("source_snapshot.counts 缺失或不是对象")
    else:
        for table in TABLES:
            value = counts.get(table)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                problems.append(f"source_snapshot.counts.{table} 不是非负整数")
    statuses = snapshot.get("statuses")
    if not isinstance(statuses, dict):
        problems.append("source_snapshot.statuses 缺失或不是对象")
    else:
        for key in (
            "users_by_role",
            "cases_by_status",
            "sops_by_status",
            "faults_by_status",
        ):
            # 合法 null(该状态无行,json_object_agg 返回 NULL)与键缺失必须区分
            if key not in statuses:
                problems.append(f"source_snapshot.statuses.{key} 缺失")
                continue
            value = statuses[key]
            if value is None:
                continue
            if not isinstance(value, dict):
                problems.append(f"source_snapshot.statuses.{key} 不是对象/null")
                continue
            for name, count in value.items():
                if (
                    not isinstance(name, str)
                    or not name
                    or not isinstance(count, int)
                    or isinstance(count, bool)
                    or count < 0
                ):
                    problems.append(
                        f"source_snapshot.statuses.{key}.{name} 不是非负整数"
                    )
    digest = snapshot.get("business_digest")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        problems.append("source_snapshot.business_digest 不是 SHA-256")
    if snapshot.get("table_order") != TABLES:
        problems.append("source_snapshot.table_order 与当前表清单不一致")

    if problems:
        raise OpsError(
            "备份元信息预检失败: " + "; ".join(problems[:12]), stage="precheck"
        )
    return snapshot


def run_restore(
    stack: ComposeStack,
    *,
    archive_path: Path,
    metadata_path: Path,
    target_db: str,
    run_id: str,
    out_dir: Path,
    cleanup_failed_target: bool = False,
    allow_version_mismatch: bool = False,
    repo_root: Path | None = None,
    image: str = "",
) -> dict:
    run_id = validate_run_id(run_id)
    target_db = validate_target_database(target_db)
    archive_path = archive_path.resolve()
    metadata_path = metadata_path.resolve()
    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    result_path = out_dir / f"{target_db}-restore.json"
    if result_path.exists():
        raise OpsError(f"恢复结果已存在,拒绝覆盖: {result_path.name}", stage="restore")

    container_path = _container_temp(run_id)
    created = False
    result: dict = {
        "schema_version": RESTORE_SCHEMA,
        "run_id": run_id,
        "target_database": target_db,
        "success": False,
        "cancelled": False,
        "created_at": now_iso(),
        "stages": {},
        "target_created": False,
        "target_preserved": False,
        "error": None,
    }

    def _write_result() -> None:
        result["finished_at"] = now_iso()
        atomic_write_json(result_path, result)

    try:
        result["stages"]["metadata"] = "running"
        metadata = read_json(metadata_path)
        # 完整结构/快照校验必须发生在 CREATE DATABASE / pg_restore 之前
        source_snapshot = _validate_metadata(metadata)
        archive_meta = metadata["archive"]
        if not archive_path.exists():
            raise OpsError("备份归档不存在", stage="precheck")
        actual_bytes = archive_path.stat().st_size
        actual_sha = sha256_file(archive_path)
        if actual_bytes != archive_meta.get("bytes"):
            raise OpsError("归档大小与元信息不一致", stage="precheck")
        if actual_sha != archive_meta.get("sha256"):
            raise OpsError("归档 SHA-256 与元信息不一致", stage="precheck")
        source_db = metadata["database"]["name"]
        if source_db == target_db:
            raise OpsError("目标库不能与来源库相同", stage="precheck")
        if stack.database_exists(target_db):
            raise OpsError(f"目标数据库已存在,拒绝覆盖: {target_db}", stage="precheck")
        if not allow_version_mismatch:
            current = stack.psql_json(
                "SELECT json_build_object('server_version', current_setting('server_version'))::text"
            )
            if _major(current.get("server_version")) != _major(
                (metadata.get("database") or {}).get("server_version")
            ):
                raise OpsError(
                    "来源/目标 PostgreSQL 主版本不一致;本工具只声明同版本恢复,"
                    "请人工评估或显式 --allow-version-mismatch",
                    stage="precheck",
                )
        result["stages"]["metadata"] = "success"
        result["stages"]["archive_readable"] = "running"
        stack.copy_to_container(archive_path, container_path)
        stack.pg_restore_list(container_path)
        result["stages"]["archive_readable"] = "success"

        result["stages"]["create_target"] = "running"
        stack.create_database(target_db)
        created = True
        result["target_created"] = True
        result["stages"]["create_target"] = "success"

        result["stages"]["pg_restore"] = "running"
        stack.pg_restore_to_database(container_path, target_db)
        result["stages"]["pg_restore"] = "success"

        result["stages"]["verify"] = "running"
        restored_snapshot = take_snapshot(stack, database=target_db)
        source_snapshot = metadata.get("source_snapshot")
        if not source_snapshot:
            raise OpsError(
                "备份元信息缺少 source_snapshot,无法完成恢复后计数/摘要核验",
                stage="verify",
            )
        comparison = compare_snapshots(source_snapshot, restored_snapshot)
        result["restored_snapshot"] = restored_snapshot
        result["source_snapshot"] = source_snapshot
        result["comparison"] = comparison
        if comparison.get("equal") is False:
            raise OpsError(
                "恢复后快照与备份时点不一致: "
                + json.dumps(comparison.get("differences"), ensure_ascii=False)[:1000],
                stage="verify",
            )
        result["stages"]["verify"] = "success"
        result.update(
            {
                "success": True,
                "core_success": True,
                "archive_sha256": actual_sha,
                "archive_bytes": actual_bytes,
                "alembic_version": restored_snapshot.get("alembic_version"),
                "vector_version": restored_snapshot.get("vector_version"),
                "tools": {
                    "pg_restore": stack.pg_tool_version("pg_restore"),
                    "docker_compose": compose_version(),
                },
                "image": image_info(image) if image else None,
                "code": code_info(repo_root or Path.cwd()),
            }
        )
    except KeyboardInterrupt as exc:
        result["cancelled"] = True
        result["error"] = "恢复被用户取消"
        result["stages"]["cancelled"] = "cancelled"
        if created:
            if cleanup_failed_target:
                result["cleanup_after_failure"] = stack.drop_database_exact(target_db)
            else:
                result["target_preserved"] = True
        raise OpsCancelled("恢复被用户取消", stage="restore") from exc
    except Exception as exc:
        result["error"] = str(exc) if isinstance(exc, OpsError) else f"{type(exc).__name__}: {exc}"
        if created:
            if cleanup_failed_target:
                try:
                    result["cleanup_after_failure"] = stack.drop_database_exact(target_db)
                except Exception as cleanup_exc:  # noqa: BLE001
                    result["cleanup_after_failure"] = {
                        "dropped": False,
                        "database": target_db,
                        "error": f"{type(cleanup_exc).__name__}: {cleanup_exc}"[:300],
                    }
            else:
                result["target_preserved"] = True
        if isinstance(exc, OpsError):
            raise
        raise OpsError(f"恢复失败: {type(exc).__name__}", stage="restore") from exc
    finally:
        try:
            cleanup = stack.remove_container_file(container_path)
        except Exception as cleanup_exc:  # noqa: BLE001
            cleanup = {
                "removed": False,
                "path": container_path,
                "error": f"{type(cleanup_exc).__name__}: {cleanup_exc}"[:300],
            }
        result["container_temp_cleanup"] = cleanup
        cleanup_ok = isinstance(cleanup, dict) and cleanup.get("removed") is True
        result["cleanup_complete"] = cleanup_ok
        if result.get("success"):
            if cleanup_ok:
                result["status"] = "success"
            else:
                result["success"] = False
                result["core_success"] = True
                result["status"] = "cleanup_incomplete"
                result["warning"] = "容器内临时归档清理失败,请按精确路径人工处理"
        elif result.get("cancelled"):
            result["status"] = "cancelled"
        else:
            result["status"] = "failed"
        _write_result()

    return result
