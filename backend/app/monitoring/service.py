"""监控概览与只读诊断命令执行。"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.keys import KeyError_, decrypt_secret
from app.config import Settings
from app.monitoring.defaults import resolve_client
from app.monitoring.prometheus import MonitoringError
from app.monitoring.ssh import SshError, SshTarget, run_command
from app.monitoring.templates import TemplateError, render_command
from app.models import (
    DashboardPanel,
    MonitoringConfig,
    MonitoredServer,
    SshCommandTemplate,
)


def server_public(row: MonitoredServer) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "host": row.host,
        "port": row.port,
        "username": row.username,
        "prometheus_instance": row.prometheus_instance,
        "enabled": row.enabled,
        "notes": row.notes,
        "has_key": bool(row.private_key_encrypted),
        "host_key_fingerprint": row.host_key_fingerprint,
    }


def template_public(row: SshCommandTemplate) -> dict:
    return {
        "id": row.id,
        "key": row.key,
        "name": row.name,
        "command": row.command,
        "param_schema": row.param_schema or {},
        "description": row.description,
        "enabled": row.enabled,
        "builtin": row.builtin,
    }


def overview(db: Session, *, minutes: int = 60, step: int = 60) -> dict:
    client = resolve_client(db)
    panels = db.scalars(
        select(DashboardPanel)
        .where(DashboardPanel.enabled.is_(True))
        .order_by(DashboardPanel.sort_order, DashboardPanel.id)
    ).all()
    servers = db.scalars(
        select(MonitoredServer).where(MonitoredServer.enabled.is_(True)).order_by(MonitoredServer.id)
    ).all()
    config = db.get(MonitoringConfig, 1)
    payload: dict = {
        "enabled": client is not None,
        "config": {"prometheus_url": config.prometheus_url if config else ""},
        "panels": [],
        "targets": [],
        "servers": [server_public(row) for row in servers],
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "error": "",
    }
    if client is None:
        payload["error"] = "监控未启用或未配置 Prometheus 地址"
        return payload

    for panel in panels:
        item = {
            "id": panel.id,
            "title": panel.title,
            "unit": panel.unit,
            "kind": panel.kind,
            "promql": panel.promql,
            "series": [],
            "error": "",
        }
        try:
            result = (
                client.query(panel.promql)
                if panel.kind == "stat"
                else client.query_range(panel.promql, minutes=minutes, step=step)
            )
            item["series"] = [
                {"labels": s.labels, "points": s.points} for s in result.series
            ]
            item["truncated"] = result.truncated
        except MonitoringError as exc:
            item["error"] = exc.message
        payload["panels"].append(item)

    try:
        payload["targets"] = client.targets()
    except MonitoringError as exc:
        payload["targets_error"] = exc.message
    return payload


def execute_server_template(
    db: Session,
    server: MonitoredServer,
    template_key: str,
    args: dict | None,
    settings: Settings,
    *,
    timeout: float = 15,
) -> dict:
    """执行一条内置只读模板命令;失败抛 SshError/TemplateError。"""
    if not server.enabled:
        raise TemplateError("服务器已停用")
    template = db.scalar(
        select(SshCommandTemplate).where(
            SshCommandTemplate.key == template_key,
            SshCommandTemplate.enabled.is_(True),
        )
    )
    if template is None:
        raise TemplateError(f"未知或已停用的命令模板: {template_key}")
    try:
        private_key = decrypt_secret(server.private_key_encrypted, settings.secret_key)
    except KeyError_ as exc:
        raise SshError("config", str(exc)) from exc
    command = render_command(template.command, template.param_schema or {}, args)
    target = SshTarget(
        host=server.host,
        port=server.port,
        username=server.username,
        private_key_pem=private_key,
        host_key_fingerprint=server.host_key_fingerprint,
    )
    result = run_command(target, command, timeout=timeout)
    return {
        "server": server.name,
        "template": template.key,
        "command": command,
        **result,
    }