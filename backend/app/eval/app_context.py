"""在隔离评测库上复用真实 FastAPI 路由的最小进程内上下文。

只替换模型/embedding 边界适配器与 get_db/get_settings,不复制诊断、检索、
引用校验或来源复核实现;所有业务路由、Schema、校验与持久化仍走原代码。
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.ai import service as ai_service
from app.config import Settings


def synthetic_settings(base: Settings) -> Settings:
    """显式合成配置:不读取真实 AI/embedding 密钥,不使用真实模型配置。"""
    return Settings(
        postgres_user=base.postgres_user,
        postgres_password=base.postgres_password,
        postgres_db=base.postgres_db,
        postgres_test_db=base.postgres_test_db,
        postgres_host=base.postgres_host,
        postgres_port=base.postgres_port,
        opssop_env="development",
        secret_key="eval-only-synthetic-session-secret",
        opssop_admin_password="eval-only-synthetic-admin-password",
        opssop_admin_username="admin",
        opssop_seed_demo=False,
        ai_mode="cloud",
        ai_base_url="http://127.0.0.1:9/v1",
        ai_api_key="",
        ai_model="synthetic-chat-eval",
        ai_supports_json_mode=True,
        ai_supports_usage=True,
        ai_timeout_seconds=1,
        ai_rate_limit_per_minute=10000,
        ai_max_concurrent_per_user=1000,
        embedding_mode="cloud",
        embedding_base_url="http://127.0.0.1:9/v1",
        embedding_api_key="",
        embedding_model="synthetic-embed-eval",
        embedding_timeout_seconds=1,
        embedding_rate_limit_per_minute=10000,
        embedding_max_concurrent_per_user=1000,
        local_only=False,
    )


def reset_service_runtime() -> None:
    """清理评测进程内的缓存/限流计数(不改变产品默认配置)。"""
    ai_service._cache.clear()
    ai_service._rate_buckets.clear()
    ai_service._inflight.clear()
    ai_service._embed_rate_buckets.clear()
    ai_service._embed_inflight.clear()


@contextmanager
def eval_app_context(session_factory: sessionmaker, settings: Settings, chat_adapter, embedding_adapter):
    """在评测库上启动真实 app;models 边界使用注入的合成适配器。"""
    from app.database import get_db
    from app.main import app

    def _override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    with ExitStack() as stack:
        stack.enter_context(patch("app.main.get_settings", lambda: settings))
        stack.enter_context(patch("app.main.seed_fn", lambda: None))
        stack.enter_context(patch("app.routers.ai.get_settings", lambda: settings))
        stack.enter_context(patch("app.routers.system.get_settings", lambda: settings))
        stack.enter_context(patch("app.security.get_settings", lambda: settings))
        stack.enter_context(patch("app.ai.service.build_adapter", lambda _settings: chat_adapter))
        stack.enter_context(
            patch("app.search.hybrid.build_embedding_adapter", lambda _settings: embedding_adapter)
        )
        app.dependency_overrides[get_db] = _override_get_db
        try:
            with TestClient(app, raise_server_exceptions=False) as client:
                yield client
        finally:
            app.dependency_overrides.clear()


def login(client: TestClient, username: str, password: str) -> dict:
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    if resp.status_code != 200:
        raise RuntimeError(f"评测账号登录失败: {username} HTTP {resp.status_code}")
    return {"Authorization": f"Bearer {resp.json()['token']}"}
