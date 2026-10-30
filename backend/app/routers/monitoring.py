"""监控概览与人工触发的只读诊断命令。"""

from __future__ import annotations

import threading
import time

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.access import can_view_fault
from app.config import get_settings
from app.database import get_db
from app.deps import get_current_user, record_audit, require_operator
from app.models import Fault, MonitoredServer, SshCommandTemplate, User
from app.monitoring.alerts import sync_alerts
from app.monitoring.defaults import ensure_defaults
from app.monitoring.service import (
    execute_server_template,
    fault_metrics,
    overview,
    server_public,
    template_public,
)
from app.monitoring.ssh import SshError
from app.monitoring.templates import TemplateError
from app.schemas import (
    FaultMetricsOut,
    MonitoredServerOut,
    MonitoringOverviewOut,
    RunCommandOut,
    RunCommandRequest,
    SshTemplateOut,
)

router = APIRouter(prefix="/api/monitoring", tags=["monitoring"])

# 人工执行限流:每用户每分钟 20 次(单实例内存计数)
_RATE_LIMIT = 20
_RATE_WINDOW = 60
_rate_lock = threading.Lock()
_rate_buckets: dict[int, list[float]] = {}


def _check_rate(user_id: int) -> None:
    now = time.monotonic()
    with _rate_lock:
        bucket = [t for t in _rate_buckets.get(user_id, []) if now - t < _RATE_WINDOW]
        if len(bucket) >= _RATE_LIMIT:
            _rate_buckets[user_id] = bucket
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "诊断命令执行过于频繁,请稍后再试")
        bucket.append(now)
        _rate_buckets[user_id] = bucket


@router.get("/overview", response_model=MonitoringOverviewOut)
def monitoring_overview(
    minutes: int = Query(default=60, ge=1, le=1440),
    step: int = Query(default=60, ge=15, le=3600),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> MonitoringOverviewOut:
    ensure_defaults(db)
    # 告警转故障:开启时按 60 秒节流同步,页面查看即可落在故障列表
    sync_alerts(db)
    return MonitoringOverviewOut(**overview(db, minutes=minutes, step=step))


@router.get("/faults/{fault_id}/metrics", response_model=FaultMetricsOut)
def fault_live_metrics(
    fault_id: int,
    minutes: int = Query(default=60, ge=1, le=1440),
    step: int = Query(default=60, ge=15, le=3600),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> FaultMetricsOut:
    """故障详情页的实时指标:按故障服务匹配承载服务器,并按实例裁剪序列。"""
    fault = db.get(Fault, fault_id)
    if fault is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "故障不存在")
    if not can_view_fault(user, fault):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "无权查看该故障")
    return FaultMetricsOut(**fault_metrics(db, fault, minutes=minutes, step=step))


@router.get("/servers", response_model=list[MonitoredServerOut])
def list_servers(
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> list[MonitoredServerOut]:
    rows = db.scalars(
        select(MonitoredServer).where(MonitoredServer.enabled.is_(True)).order_by(MonitoredServer.id)
    ).all()
    return [MonitoredServerOut(**server_public(row)) for row in rows]


@router.get("/templates", response_model=list[SshTemplateOut])
def list_templates(
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> list[SshTemplateOut]:
    ensure_defaults(db)
    rows = db.scalars(
        select(SshCommandTemplate)
        .where(SshCommandTemplate.enabled.is_(True))
        .order_by(SshCommandTemplate.id)
    ).all()
    return [SshTemplateOut(**template_public(row)) for row in rows]


@router.post("/servers/{server_id}/run", response_model=RunCommandOut)
def run_server_command(
    server_id: int,
    body: RunCommandRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_operator),
) -> RunCommandOut:
    """人工触发只读模板命令;命令固定、参数强校验,执行记录写审计。"""
    _check_rate(user.id)
    server = db.get(MonitoredServer, server_id)
    if server is None or not server.enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "服务器不存在或已停用")
    ensure_defaults(db)  # 内置模板幂等种子,避免首次执行时模板尚未写入
    settings = get_settings()
    try:
        result = execute_server_template(db, server, body.template, body.args, settings)
    except TemplateError as exc:
        record_audit(db, user, "monitoring.ssh.run", "server", server.host, {
            "template": body.template, "status": "denied", "reason": exc.message[:200]
        })
        db.commit()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, exc.message) from exc
    except SshError as exc:
        record_audit(db, user, "monitoring.ssh.run", "server", server.host, {
            "template": body.template, "status": "error", "reason": exc.message[:200]
        })
        db.commit()
        code = status.HTTP_400_BAD_REQUEST if exc.code in ("auth", "config", "host_key_mismatch", "host_key_missing") else status.HTTP_502_BAD_GATEWAY
        raise HTTPException(code, exc.message) from exc
    record_audit(db, user, "monitoring.ssh.run", "server", server.host, {
        "template": body.template,
        "status": "ok",
        "exit_status": result["exit_status"],
        "latency_ms": result["latency_ms"],
    })
    db.commit()
    return RunCommandOut(**result)