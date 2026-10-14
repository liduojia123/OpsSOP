"""回归:生产默认账号检查(质量检查 P1 #3)与首次建号语义。"""

import pytest

from app.config import Settings
from app.models import Role, User
from app.security import hash_password
from app.seed import ensure_seed, find_insecure_accounts
from tests.conftest import TestingSessionLocal


def _prod_settings(**overrides) -> Settings:
    base = dict(
        opssop_env="production",
        secret_key="strong-random-secret-value-123",
        opssop_admin_password="strong-admin-pass-123",
        opssop_seed_demo=False,
    )
    base.update(overrides)
    return Settings(**base)


def test_first_time_admin_created_with_env_password(db, monkeypatch):
    monkeypatch.setattr("app.seed.get_settings", lambda: _prod_settings())
    ensure_seed(session_factory=TestingSessionLocal)
    admin = db.query(User).filter(User.username == "admin").one()
    assert admin.role == Role.admin


def test_existing_admin_password_never_overwritten(db, monkeypatch):
    # 首次建号
    monkeypatch.setattr("app.seed.get_settings", lambda: _prod_settings())
    ensure_seed(session_factory=TestingSessionLocal)
    admin = db.query(User).filter(User.username == "admin").one()
    old_hash = admin.password_hash

    # 换新口令再初始化:已有账号的口令不得被覆盖(启动时静默改密是被禁止的)
    monkeypatch.setattr(
        "app.seed.get_settings",
        lambda: _prod_settings(opssop_admin_password="brand-new-password-456"),
    )
    ensure_seed(session_factory=TestingSessionLocal)
    db.expire_all()
    admin = db.query(User).filter(User.username == "admin").one()
    assert admin.password_hash == old_hash


def test_production_blocks_leftover_default_password_accounts(db, monkeypatch):
    """质量检查 P1 #3 复现场景:旧默认 admin 口令的账号在生产启动必须被发现。"""
    db.add(
        User(
            username="admin",
            display_name="管理员",
            password_hash=hash_password("change-me-admin-password"),
            role=Role.admin,
        )
    )
    db.commit()

    problems = find_insecure_accounts(db)
    assert any("admin" in p and "默认口令" in p for p in problems)

    monkeypatch.setattr("app.seed.get_settings", lambda: _prod_settings())
    with pytest.raises(RuntimeError, match="不安全账号"):
        ensure_seed(session_factory=TestingSessionLocal)


def test_production_blocks_demo_seed_accounts(db, monkeypatch):
    """旧演示账号(种子指纹)在生产启动必须被发现,即使口令已轮换。"""
    db.add(
        User(
            username="operator",
            display_name="演示运维",
            password_hash=hash_password("rotated-strong-pass-1"),
            role=Role.operator,
        )
    )
    db.commit()

    problems = find_insecure_accounts(db)
    assert any("演示" in p for p in problems)

    monkeypatch.setattr("app.seed.get_settings", lambda: _prod_settings())
    with pytest.raises(RuntimeError, match="不安全账号"):
        ensure_seed(session_factory=TestingSessionLocal)


def test_production_passes_after_rotation(db, monkeypatch):
    db.add(
        User(
            username="admin",
            display_name="管理员",
            password_hash=hash_password("strong-admin-pass-123"),
            role=Role.admin,
        )
    )
    db.commit()
    monkeypatch.setattr("app.seed.get_settings", lambda: _prod_settings())
    ensure_seed(session_factory=TestingSessionLocal)  # 不应抛出
    assert find_insecure_accounts(db) == []


def test_production_rejects_weak_env_values(monkeypatch):
    monkeypatch.setattr(
        "app.seed.get_settings",
        lambda: _prod_settings(opssop_admin_password="change-me-admin-password"),
    )
    with pytest.raises(RuntimeError, match="示例默认值"):
        ensure_seed(session_factory=_db_that_never_opens)


def test_production_rejects_seed_demo_flag(monkeypatch):
    monkeypatch.setattr("app.seed.get_settings", lambda: _prod_settings(opssop_seed_demo=True))
    with pytest.raises(RuntimeError, match="SEED_DEMO"):
        ensure_seed(session_factory=_db_that_never_opens)


def _db_that_never_opens():
    raise AssertionError("配置校验失败不应打开数据库连接")


def test_development_seed_creates_demo_users(db, monkeypatch):
    monkeypatch.setattr(
        "app.seed.get_settings",
        lambda: Settings(opssop_env="development", opssop_seed_demo=True),
    )
    ensure_seed(session_factory=TestingSessionLocal)
    names = {u.username for u in db.query(User).all()}
    assert {"admin", "operator", "reviewer"} <= names
