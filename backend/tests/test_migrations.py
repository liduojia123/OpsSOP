"""迁移测试:在一次性数据库上验证 空库升级 / 升级-回滚-再升级 往返。

不使用 conftest 的 opssop_test,也不触碰业务库;每个测试会话用独立库并在结束时删除。
"""

import os
import uuid

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from app.config import get_settings

settings = get_settings()


@pytest.fixture(scope="module")
def disposable_db_url():
    """一次性数据库:名称随机,测试结束删除。"""
    if settings.postgres_test_db == settings.postgres_db:
        pytest.skip("POSTGRES_TEST_DB 与 POSTGRES_DB 相同,拒绝迁移测试以防破坏业务库")
    name = f"opssop_migrate_{uuid.uuid4().hex[:8]}"
    admin_url = settings.database_url.rsplit("/", 1)[0] + "/postgres"
    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    admin_engine.dispose()

    from sqlalchemy.engine import URL

    url = URL.create(
        "postgresql+psycopg",
        username=settings.postgres_user,
        password=settings.postgres_password,
        host=settings.postgres_host,
        port=settings.postgres_port,
        database=name,
    ).render_as_string(hide_password=False)
    yield url

    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        conn.execute(
            text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = :name AND pid <> pg_backend_pid()"
            ),
            {"name": name},
        )
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
    admin_engine.dispose()


def _alembic_config(url: str) -> Config:
    cfg = Config(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.attributes["migration_url"] = url
    return cfg


def test_upgrade_from_empty_db(disposable_db_url):
    cfg = _alembic_config(disposable_db_url)
    command.upgrade(cfg, "head")
    engine = create_engine(disposable_db_url)
    with engine.connect() as conn:
        tables = {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
                )
            )
        }
    engine.dispose()
    assert {
        "users",
        "faults",
        "cases",
        "sops",
        "sop_feedback",
        "audit_events",
        "ai_analyses",
    } <= tables


def test_upgrade_downgrade_reupgrade_roundtrip(disposable_db_url):
    """质量检查 P2 #9 回归:回滚后不得遗留枚举类型,再次升级必须成功。"""
    cfg = _alembic_config(disposable_db_url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")  # 修复前此处抛 type "role" already exists
    engine = create_engine(disposable_db_url)
    with engine.connect() as conn:
        enums = {
            row[0]
            for row in conn.execute(
                text("SELECT typname FROM pg_type WHERE typtype = 'e'")
            )
        }
        rev = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    engine.dispose()
    # 回滚到 base 后枚举被清理,再升级后正常重建(存在是允许的)
    assert rev is not None
    assert "role" in enums  # 重建成功
