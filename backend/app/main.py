import asyncio
import logging
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.logging_setup import setup_file_logging
from app.net import TrustedProxyMiddleware, parse_trusted_cidrs
from app.routers import (
    admin_embedding,
    ai,
    ai_admin,
    ai_settings,
    audit,
    auth,
    backups,
    chat,
    chat_admin,
    cases,
    categories,
    faults,
    monitoring,
    monitoring_admin,
    notifications,
    search,
    sops,
    system,
    users,
    workbench,
)
from app.seed import ensure_seed

logger = logging.getLogger("opssop")

# 生命周期初始化入口;测试通过 monkeypatch 替换为指向测试库的实现
seed_fn = ensure_seed


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    try:
        settings.validate_ai_config()
        settings.validate_backup_config()
    except ValueError as exc:
        logger.error("配置校验失败: %s", exc)
        raise
    setup_file_logging(settings)
    seed_fn()
    maintenance_task: asyncio.Task | None = None
    alerts_task: asyncio.Task | None = None
    if settings.is_production:
        from app.maintenance import maintenance_loop
        from app.monitoring.alerts import alerts_loop

        maintenance_task = asyncio.create_task(maintenance_loop(settings))
        # 告警转故障未开启时循环只做一次配置查询;开启后每 60 秒同步
        alerts_task = asyncio.create_task(alerts_loop())
    try:
        yield
    finally:
        for task in (maintenance_task, alerts_task):
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task


app = FastAPI(
    title="OpsSOP",
    description="运维故障知识库与 SOP 管理系统",
    version="3.6.4",
    lifespan=lifespan,
)

# 本地开发时允许 Vite dev server;容器内由 nginx 同源代理,不需要跨域
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 可信代理必须显式配置;配置非法时在启动阶段直接失败
app.add_middleware(
    TrustedProxyMiddleware,
    trusted_cidrs=parse_trusted_cidrs(get_settings().trusted_proxy_cidrs),
)

app.include_router(auth.router)
app.include_router(users.router)
app.include_router(categories.router)
app.include_router(faults.router)
app.include_router(cases.router)
app.include_router(sops.router)
app.include_router(workbench.router)
app.include_router(audit.router)
app.include_router(backups.router)
app.include_router(system.router)
app.include_router(ai.router)
app.include_router(ai_settings.router)
app.include_router(ai_admin.router)
app.include_router(monitoring.router)
app.include_router(monitoring_admin.router)
app.include_router(notifications.router)
app.include_router(chat.router)
app.include_router(chat_admin.router)
app.include_router(search.router)
app.include_router(admin_embedding.router)
