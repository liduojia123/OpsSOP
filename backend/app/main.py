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
    audit,
    auth,
    backups,
    cases,
    categories,
    faults,
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
    if settings.is_production:
        from app.maintenance import maintenance_loop

        maintenance_task = asyncio.create_task(maintenance_loop(settings))
    try:
        yield
    finally:
        if maintenance_task is not None:
            maintenance_task.cancel()
            with suppress(asyncio.CancelledError):
                await maintenance_task


app = FastAPI(
    title="OpsSOP",
    description="运维故障知识库与 SOP 管理系统",
    version="2.3.0",
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
app.include_router(admin_embedding.router)
