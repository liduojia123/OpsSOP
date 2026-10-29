"""第四阶段第二批复审回归:新库迁移/manifest 失败后不得留下无主库。

独立复审 P2 #5:只清理本轮确实 CREATE 的库;陌生既有库仍按 manifest
防护拒绝接管/删除。
"""

import uuid

import pytest
from sqlalchemy import text

from app.config import get_settings
from app.eval import db as eval_db_module


def _db_exists(admin, name: str) -> bool:
    with admin.connect() as conn:
        return bool(
            conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": name}
            ).scalar()
        )


def test_migration_failure_drops_only_the_database_created_this_run(monkeypatch):
    settings = get_settings()
    run_id = "migratefail" + uuid.uuid4().hex[:8]
    target = eval_db_module.default_target_db(run_id)
    admin = eval_db_module._admin_engine(settings)
    try:
        def fail_upgrade(*_args, **_kwargs):
            raise RuntimeError("independent migration failure")

        monkeypatch.setattr(eval_db_module.command, "upgrade", fail_upgrade)
        with pytest.raises(RuntimeError, match="independent migration failure") as excinfo:
            eval_db_module.create_or_reuse_database(
                settings,
                target,
                run_id=run_id,
                dataset_version="review",
                dataset_hash="review",
            )
        cleanup = getattr(excinfo.value, "eval_cleanup", None)
        assert cleanup is not None
        assert cleanup["dropped"] is True
        assert cleanup["database"] == target
        assert _db_exists(admin, target) is False
    finally:
        assert eval_db_module.validate_target_db(target, settings, run_id) == target
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{target}"'))
        admin.dispose()


def test_migration_failure_cleanup_failure_is_reported_with_exact_name(monkeypatch):
    settings = get_settings()
    run_id = "migratecleanupfail" + uuid.uuid4().hex[:8]
    target = eval_db_module.default_target_db(run_id)
    admin = eval_db_module._admin_engine(settings)
    try:
        def fail_upgrade(*_args, **_kwargs):
            raise RuntimeError("independent migration failure")

        def fail_cleanup(_settings, name):
            return {
                "dropped": False,
                "status": "failed",
                "database": name,
                "ownership": "created_this_run",
                "error": "synthetic cleanup failure",
            }

        monkeypatch.setattr(eval_db_module.command, "upgrade", fail_upgrade)
        monkeypatch.setattr(
            eval_db_module, "_force_drop_created_database", fail_cleanup
        )
        with pytest.raises(RuntimeError, match="independent migration failure") as excinfo:
            eval_db_module.create_or_reuse_database(
                settings,
                target,
                run_id=run_id,
                dataset_version="review",
                dataset_hash="review",
            )
        cleanup = getattr(excinfo.value, "eval_cleanup", None)
        assert cleanup is not None
        assert cleanup["dropped"] is False
        assert cleanup["database"] == target
        assert cleanup["ownership"] == "created_this_run"
        assert "synthetic cleanup failure" in cleanup["error"]
        # 清理失败时库仍在;本次测试按精确名称手动删除,证明报告名称可操作。
        assert _db_exists(admin, target) is True
    finally:
        assert eval_db_module.validate_target_db(target, settings, run_id) == target
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{target}"'))
        admin.dispose()


def test_cancelled_migration_drops_new_database_before_propagating(monkeypatch):
    settings = get_settings()
    run_id = "cancelmigration" + uuid.uuid4().hex[:8]
    target = eval_db_module.default_target_db(run_id)
    admin = eval_db_module._admin_engine(settings)
    try:
        def cancel_upgrade(*_args, **_kwargs):
            raise KeyboardInterrupt("independent migration cancellation")

        monkeypatch.setattr(eval_db_module.command, "upgrade", cancel_upgrade)
        with pytest.raises(KeyboardInterrupt) as excinfo:
            eval_db_module.create_or_reuse_database(
                settings,
                target,
                run_id=run_id,
                dataset_version="review",
                dataset_hash="review",
            )
        cleanup = getattr(excinfo.value, "eval_cleanup", None)
        assert cleanup is not None
        assert cleanup["dropped"] is True
        assert cleanup["database"] == target
        assert _db_exists(admin, target) is False
    finally:
        assert eval_db_module.validate_target_db(target, settings, run_id) == target
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{target}"'))
        admin.dispose()


def test_migration_failure_does_not_drop_a_foreign_existing_database(monkeypatch):
    settings = get_settings()
    run_id = "foreignfail" + uuid.uuid4().hex[:8]
    target = eval_db_module.default_target_db(run_id)
    admin = eval_db_module._admin_engine(settings)
    try:
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{target}"'))

        def fail_upgrade(*_args, **_kwargs):
            raise AssertionError("foreign database must not reach migration")

        monkeypatch.setattr(eval_db_module.command, "upgrade", fail_upgrade)
        with pytest.raises(eval_db_module.EvalSetupError, match="manifest"):
            eval_db_module.create_or_reuse_database(
                settings,
                target,
                run_id=run_id,
                dataset_version="review",
                dataset_hash="review",
                reuse=True,
            )
        assert _db_exists(admin, target) is True
    finally:
        assert eval_db_module.validate_target_db(target, settings, run_id) == target
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{target}"'))
        admin.dispose()
