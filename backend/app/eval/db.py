"""评测数据库隔离:连接前硬校验、按 run_id 建库、正式迁移与精确清理。

安全顺序:
1. 先校验目标库名/环境/与业务库、测试库、demo/系统库冲突,再打开任何连接;
2. 只允许 `opssop_eval_<run_id>` 形式的专用库;生产环境直接拒绝;
3. 新库用正式 Alembic 迁移初始化(不以 create_all 替代);
4. `eval_run_manifest` 记录 run_id/数据集版本/哈希/夹具映射;
5. 复用既有库必须显式 --reuse 且 manifest 匹配;清理只按 manifest 归属删除该库。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, Engine
from sqlalchemy.orm import sessionmaker

from app.config import Settings

BACKEND_DIR = Path(__file__).resolve().parents[2]
SAFE_DB_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
SAFE_RUN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_]{5,31}$")
DENY_NAMES = {
    "opssop",
    "opssop_test",
    "opssop_eval",
    "opssop_demo",
    "opssop_phase4_app",
    "review_app",
    "review_demo",
    "postgres",
    "template0",
    "template1",
}


class EvalSetupError(Exception):
    """评测配置/准备/数据库归属错误:对应退出状态 2,不输出误导性指标。"""


@dataclass
class EvalDatabase:
    name: str
    url: str
    engine: Engine
    session_factory: sessionmaker
    manifest: dict
    created: bool


def validate_run_id(run_id: str) -> str:
    value = (run_id or "").strip()
    if not SAFE_RUN_ID_RE.match(value):
        raise EvalSetupError(
            "run_id 必须是 6-32 位小写字母/数字/下划线;不使用路径或任意字符串"
        )
    return value


def default_target_db(run_id: str) -> str:
    return f"opssop_eval_{validate_run_id(run_id)}"


def validate_target_db(name: str, settings: Settings, run_id: str) -> str:
    """连接前硬校验;返回规范化名称。"""
    if settings.is_production:
        raise EvalSetupError("production 环境禁止创建/准备评测库")
    value = (name or "").strip().lower()
    if not value:
        raise EvalSetupError("评测目标库不能为空")
    if not SAFE_DB_RE.match(value):
        raise EvalSetupError(
            "评测库名必须匹配 ^[a-z][a-z0-9_]{0,62}$(安全 SQL 标识符)"
        )
    if not value.startswith("opssop_eval_"):
        raise EvalSetupError(
            "评测库名必须以 opssop_eval_ 开头;禁止指向业务/测试/demo/系统库"
        )
    rid = validate_run_id(run_id)
    if rid not in value:
        raise EvalSetupError("评测库名必须包含本轮 run_id,防止误接管其他运行")
    denied = {settings.postgres_db.strip().lower(), settings.postgres_test_db.strip().lower()}
    denied |= DENY_NAMES
    if value in denied:
        raise EvalSetupError(f"拒绝使用受保护数据库名: {value}")
    return value


def _url_for(settings: Settings, database: str) -> str:
    return URL.create(
        "postgresql+psycopg",
        username=settings.postgres_user,
        password=settings.postgres_password,
        host=settings.postgres_host,
        port=settings.postgres_port,
        database=database,
    ).render_as_string(hide_password=False)


def _admin_engine(settings: Settings) -> Engine:
    return create_engine(
        _url_for(settings, "postgres"), isolation_level="AUTOCOMMIT", pool_pre_ping=True
    )


def _manifest_table_sql() -> str:
    return """
        CREATE TABLE IF NOT EXISTS eval_run_manifest (
            run_id TEXT PRIMARY KEY,
            dataset_version TEXT NOT NULL,
            dataset_hash TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            data JSONB NOT NULL DEFAULT '{}'::jsonb
        )
    """


def _read_manifest(engine: Engine, run_id: str) -> dict | None:
    with engine.connect() as conn:
        exists = conn.execute(
            text("SELECT to_regclass('public.eval_run_manifest')")
        ).scalar()
        if not exists:
            return None
        row = conn.execute(
            text(
                "SELECT run_id, dataset_version, dataset_hash, data "
                "FROM eval_run_manifest WHERE run_id = :run_id"
            ),
            {"run_id": run_id},
        ).first()
        if row is None:
            return None
        return {
            "run_id": row[0],
            "dataset_version": row[1],
            "dataset_hash": row[2],
            "data": row[3] or {},
        }


def _force_drop_created_database(settings: Settings, name: str) -> dict:
    """只清理本函数本轮 CREATE 出来的库;名称已通过 validate_target_db。

    迁移/manifest 初始化失败时没有 manifest,不能走正常 drop_eval_database
    的归属校验;因此保存 created 标志,并只在 created=True 时调用本函数。
    陌生既有库永远不会进入这个分支。
    """
    admin = None
    try:
        admin = _admin_engine(settings)
        with admin.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": name}
            ).scalar()
            if not exists:
                return {"dropped": False, "reason": "database_not_found", "database": name}
            conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :name AND pid <> pg_backend_pid()"
                ),
                {"name": name},
            )
            conn.execute(text(f'DROP DATABASE "{name}"'))
        return {"dropped": True, "database": name, "ownership": "created_this_run"}
    except Exception as exc:  # noqa: BLE001
        return {
            "dropped": False,
            "status": "failed",
            "database": name,
            "ownership": "created_this_run",
            "error": f"{type(exc).__name__}: {str(exc)[:300]}",
        }
    finally:
        if admin is not None:
            admin.dispose()


def create_or_reuse_database(
    settings: Settings,
    target_db: str,
    *,
    run_id: str,
    dataset_version: str,
    dataset_hash: str,
    reuse: bool = False,
) -> EvalDatabase:
    """校验后创建/复用本轮评测库,执行正式迁移并读取/写入 manifest。"""
    name = validate_target_db(target_db, settings, run_id)
    rid = validate_run_id(run_id)

    admin = _admin_engine(settings)
    created = False
    try:
        with admin.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": name}
            ).scalar()
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{name}"'))
                created = True
            elif not reuse:
                raise EvalSetupError(
                    f"评测库 {name} 已存在;复跑请显式 --reuse,并确认 manifest 归属/数据集版本"
                )
    finally:
        admin.dispose()

    url = _url_for(settings, name)
    engine = create_engine(url, pool_pre_ping=True)

    try:
        # 既有库先只读 manifest 校验归属/数据集版本,再做任何迁移 DDL;
        # 不匹配时拒绝接管,避免对陌生库执行迁移或覆盖。
        if not created:
            manifest_before = _read_manifest(engine, rid)
            if manifest_before is None:
                raise EvalSetupError(
                    "--reuse 目标缺少 eval_run_manifest,或不是本评测工具创建的库;拒绝接管"
                )
            if manifest_before["dataset_version"] != dataset_version or (
                manifest_before["dataset_hash"] != dataset_hash
            ):
                raise EvalSetupError(
                    "既有评测库的数据集版本/哈希与当前数据集不一致;拒绝复用或覆盖"
                )

        # 正式迁移,不使用 create_all 替代迁移验证
        cfg = Config(str(BACKEND_DIR / "alembic.ini"))
        cfg.attributes["migration_url"] = url
        command.upgrade(cfg, "head")

        with engine.begin() as conn:
            conn.execute(text(_manifest_table_sql()))

        manifest = _read_manifest(engine, rid)
        if manifest is None:
            if not created:
                raise EvalSetupError("既有评测库缺少 manifest,拒绝接管")
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO eval_run_manifest "
                        "(run_id, dataset_version, dataset_hash, data) "
                        "VALUES (:run_id, :version, :hash, CAST(:data AS jsonb))"
                    ),
                    {
                        "run_id": rid,
                        "version": dataset_version,
                        "hash": dataset_hash,
                        "data": json.dumps({"fixture_map": {}, "analysis_faults": {}}),
                    },
                )
            manifest = _read_manifest(engine, rid)
        else:
            if not reuse:
                raise EvalSetupError(
                    f"评测库 {name} 已有 run_id {manifest['run_id']};复跑请显式 --reuse"
                )
            if manifest["dataset_version"] != dataset_version or manifest["dataset_hash"] != dataset_hash:
                raise EvalSetupError(
                    "既有评测库的数据集版本/哈希与当前数据集不一致;拒绝复用或覆盖"
                )
    except BaseException as exc:
        engine.dispose()
        # 只清理本轮 CREATE 的库;既有库(created=False)保持原样,绝不接管/删除。
        # BaseException 覆盖可捕获的 KeyboardInterrupt/取消路径。
        if created:
            cleanup = _force_drop_created_database(settings, name)
            try:
                exc.eval_cleanup = cleanup
            except Exception:  # noqa: BLE001
                pass
        raise

    return EvalDatabase(
        name=name,
        url=url,
        engine=engine,
        session_factory=sessionmaker(bind=engine, autoflush=False, expire_on_commit=False),
        manifest=manifest or {},
        created=created,
    )


def drop_eval_database(settings: Settings, target_db: str, run_id: str) -> dict:
    """只删除 manifest 归属匹配的本轮评测库;不匹配则拒绝。"""
    name = validate_target_db(target_db, settings, run_id)
    rid = validate_run_id(run_id)
    admin = _admin_engine(settings)
    try:
        with admin.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": name}
            ).scalar()
            if not exists:
                return {"dropped": False, "reason": "database_not_found", "database": name}
        target_engine = create_engine(_url_for(settings, name), pool_pre_ping=True)
        try:
            manifest = _read_manifest(target_engine, rid)
        finally:
            target_engine.dispose()
        if manifest is None or manifest.get("run_id") != rid:
            raise EvalSetupError(
                "评测库 manifest 缺失或 run_id 不匹配;拒绝删除,避免误清理其他运行/业务数据"
            )
        with admin.connect() as conn:
            conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :name AND pid <> pg_backend_pid()"
                ),
                {"name": name},
            )
            conn.execute(text(f'DROP DATABASE "{name}"'))
        return {"dropped": True, "database": name}
    finally:
        admin.dispose()


def database_info(settings: Settings, name: str) -> dict:
    """报告用非敏感数据库标识(不含口令/URL)。"""
    return {
        "host": settings.postgres_host,
        "port": settings.postgres_port,
        "database": name,
    }
