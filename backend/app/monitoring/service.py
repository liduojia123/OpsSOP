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
        "service_names": list(row.service_names or []),
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


def servers_for_service(db: Session, service: str) -> list[MonitoredServer]:
    """按故障/案例的 service 字段匹配承载服务器(忽略大小写,空服务不匹配)。"""
    key = (service or "").strip().lower()
    if not key:
        return []
    rows = db.scalars(
        select(MonitoredServer)
        .where(MonitoredServer.enabled.is_(True))
        .order_by(MonitoredServer.id)
    ).all()
    return [
        row
        for row in rows
        if key in {str(name).strip().lower() for name in (row.service_names or []) if str(name).strip()}
    ]


def instance_keys(server: MonitoredServer) -> set[str]:
    """服务器在 Prometheus 里的实例标识。

    管理员显式配置了 prometheus_instance 时只认它(避免把 host 误当实例);
    未配置时按 node_exporter 默认端口推断。
    """
    configured = server.prometheus_instance.strip()
    if configured:
        return {configured}
    host = server.host.strip()
    return {key for key in (host, f"{host}:9100") if key}


def _trim_series(series, instances: set[str], hosts: set[str]) -> tuple[list, bool]:
    """按实例裁剪序列;返回 (序列, 是否已按实例裁剪)。

    内置面板都保留 instance 标签;若自定义 PromQL 已聚合掉实例,
    无法安全裁剪,则返回全局序列并由前端标注"全局值",避免张冠李戴。
    """
    with_instance = [item for item in series if item.labels.get("instance")]
    if not with_instance:
        return list(series), False
    return (
        [
            item
            for item in with_instance
            if item.labels.get("instance") in instances
            or item.labels.get("host") in hosts
        ],
        True,
    )


def fault_metrics(
    db: Session, fault, *, minutes: int = 60, step: int = 60
) -> dict:
    """故障页实时指标:无服务/无映射时返回可解释的空结果,不伪造数据。"""
    client = resolve_client(db)
    servers = servers_for_service(db, fault.service or "")
    instances = sorted({key for server in servers for key in instance_keys(server)})
    payload: dict = {
        "enabled": client is not None,
        "mapped": bool(servers),
        "fault_id": fault.id,
        "service": fault.service or "",
        "servers": [server_public(row) for row in servers],
        "instances": instances,
        "panels": [],
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "error": "",
    }
    if client is None:
        payload["error"] = "监控未启用或未配置 Prometheus 地址"
        return payload
    if not servers:
        payload["error"] = (
            "该故障的服务未映射到服务器:请在「系统管理 → 监控」中为服务器填写承载服务名"
            if (fault.service or "").strip()
            else "该故障未填写服务名,无法关联实时指标"
        )
        return payload

    hosts = {server.host.strip() for server in servers if server.host.strip()}
    panels = db.scalars(
        select(DashboardPanel)
        .where(DashboardPanel.enabled.is_(True))
        .order_by(DashboardPanel.sort_order, DashboardPanel.id)
    ).all()
    for panel in panels:
        item = {
            "id": panel.id,
            "title": panel.title,
            "unit": panel.unit,
            "kind": panel.kind,
            "promql": panel.promql,
            "series": [],
            "error": "",
            "scoped": True,
        }
        try:
            result = (
                client.query(panel.promql)
                if panel.kind == "stat"
                else client.query_range(panel.promql, minutes=minutes, step=step)
            )
            trimmed, scoped = _trim_series(list(result.series), set(instances), hosts)
            item["series"] = [
                {"labels": s.labels, "points": s.points} for s in trimmed
            ]
            item["scoped"] = scoped
            item["truncated"] = result.truncated
        except MonitoringError as exc:
            item["error"] = exc.message
        payload["panels"].append(item)
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