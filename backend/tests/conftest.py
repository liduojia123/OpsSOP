"""测试夹具:使用 PostgreSQL 测试库(opssop_test),每个会话建表/清表。

隔离保证(质量检查 P1 #4):
- 连接与删表前强制校验 POSTGRES_TEST_DB != POSTGRES_DB,防止误删业务表;
- TestClient 生命周期中的 ensure_seed 被替换为指向测试库的实现,不触碰业务库;
- 迁移测试(test_migrations.py)使用一次性数据库,不经过此夹具。
"""

import os
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

# 保证容器内测试可导入 app
os.environ.setdefault("POSTGRES_HOST", "db")
os.environ.setdefault("OPSSOP_ENV", "development")
# 测试与部署环境隔离:不继承真实 .env 的 AI 限流配置(联调时可能为 2/分钟),
# 需要验证限流的用例用显式 Settings(ai_rate_limit_per_minute=...) 覆盖
os.environ["AI_RATE_LIMIT_PER_MINUTE"] = "1000"
os.environ["AI_MAX_CONCURRENT_PER_USER"] = "100"

from app.config import get_settings  # noqa: E402
from app.database import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Role, User  # noqa: E402
from app.security import hash_password  # noqa: E402
from app.seed import ensure_seed  # noqa: E402

settings = get_settings()


class SnapshotTestClient(TestClient):
    """兼容既有业务测试：未显式指定时，先按当前身份读取版本再提交。"""

    def request(self, method, url, **kwargs):
        match = re.fullmatch(r"/api/(cases|sops)/(\d+)(?:/(submit|approve|return|deprecate|new-version))?", str(url))
        if match and ((method.upper() == "PATCH" and match.group(3) is None) or (method.upper() == "POST" and match.group(3))):
            headers = dict(kwargs.get("headers") or {})
            if not any(key.lower() == "x-expected-updated-at" for key in headers):
                response = super().request("GET", f"/api/{match.group(1)}/{match.group(2)}", headers=headers)
                if response.status_code == 200:
                    headers["X-Expected-Updated-At"] = response.json()["updated_at"]
                    kwargs["headers"] = headers
        return super().request(method, url, **kwargs)


def _assert_database_isolation() -> None:
    """连测试库/删表前的硬性隔离校验。"""
    if not settings.postgres_test_db:
        raise RuntimeError("POSTGRES_TEST_DB 未配置,拒绝运行测试")
    if settings.postgres_test_db == settings.postgres_db:
        raise RuntimeError(
            f"POSTGRES_TEST_DB({settings.postgres_test_db!r})与 POSTGRES_DB"
            f"({settings.postgres_db!r})相同,拒绝连接/删表以防破坏业务数据。"
        )
    if settings.postgres_test_db in ("postgres", "template0", "template1"):
        raise RuntimeError(f"POSTGRES_TEST_DB 不允许使用系统库 {settings.postgres_test_db!r}")


_assert_database_isolation()


def _ensure_test_database() -> None:
    admin_url = settings.database_url.rsplit("/", 1)[0] + "/postgres"
    engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with engine.connect() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM pg_database WHERE datname = :name"),
            {"name": settings.postgres_test_db},
        ).scalar()
        if not exists:
            conn.execute(text(f'CREATE DATABASE "{settings.postgres_test_db}"'))
    engine.dispose()


_ensure_test_database()
engine = create_engine(settings.test_database_url, pool_pre_ping=True)
TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

# pgvector 扩展:embedding 索引表的向量列需要(镜像须为 pgvector/pgvector 系列)。
# 失败时给出明确原因,不以普通数组替代。
try:
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS vector")
except Exception as exc:  # pragma: no cover - 环境问题
    raise RuntimeError(
        "测试库无法启用 pgvector 扩展:请确认数据库镜像为 pgvector/pgvector:pg16 "
        "且用户有 CREATE EXTENSION 权限"
    ) from exc


@pytest.fixture(scope="session", autouse=True)
def prepare_database():
    _assert_database_isolation()
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    _assert_database_isolation()
    Base.metadata.drop_all(bind=engine)


@pytest.fixture(autouse=True)
def clean_tables():
    yield
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())


@pytest.fixture
def db():
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client(db, monkeypatch):
    def _override_get_db():
        yield db

    # 生命周期初始化必须指向测试库,不触碰业务库
    monkeypatch.setattr("app.main.seed_fn", lambda: ensure_seed(session_factory=TestingSessionLocal))

    app.dependency_overrides[get_db] = _override_get_db
    with SnapshotTestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def make_user(db, username: str, role: Role, password: str = "test-password-1") -> User:
    user = User(
        username=username,
        display_name=username,
        password_hash=hash_password(password),
        role=role,
    )
    db.add(user)
    db.commit()
    return user


@pytest.fixture
def operator(db):
    return make_user(db, "op_user", Role.operator)


@pytest.fixture
def reviewer(db):
    return make_user(db, "rev_user", Role.reviewer)


@pytest.fixture
def admin(db):
    return make_user(db, "adm_user", Role.admin)


def login(client, username: str, password: str = "test-password-1") -> dict:
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


@pytest.fixture
def op_headers(client, operator):
    return login(client, "op_user")


@pytest.fixture
def rev_headers(client, reviewer):
    return login(client, "rev_user")


@pytest.fixture
def adm_headers(client, admin):
    return login(client, "adm_user")
