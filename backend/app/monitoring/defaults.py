"""监控默认配置、内置模板与面板种子,以及数据源客户端构造。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.monitoring.prometheus import MonitoringError, PrometheusClient, validate_prometheus_url
from app.monitoring.templates import BUILTIN_TEMPLATES
from app.models import (
    DashboardPanel,
    MonitoredServer,
    MonitoringConfig,
    SshCommandTemplate,
    User,
)

BUILTIN_PANELS: list[dict] = [
    {
        "title": "CPU 使用率",
        "promql": '100 - (avg by(instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)',
        "unit": "percent",
        "kind": "line",
        "sort_order": 10,
    },
    {
        "title": "内存使用率",
        "promql": "100 * (1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)",
        "unit": "percent",
        "kind": "line",
        "sort_order": 20,
    },
    {
        "title": "磁盘使用率(/)",
        "promql": (
            "100 - (node_filesystem_avail_bytes{mountpoint=\"/\",fstype!~\"tmpfs|overlay\"}"
            " / node_filesystem_size_bytes{mountpoint=\"/\",fstype!~\"tmpfs|overlay\"} * 100)"
        ),
        "unit": "percent",
        "kind": "line",
        "sort_order": 30,
    },
    {
        "title": "系统负载(1m)",
        "promql": "node_load1",
        "unit": "load",
        "kind": "line",
        "sort_order": 40,
    },
    {
        "title": "网络入流量",
        "promql": 'sum by(instance) (rate(node_network_receive_bytes_total{device!="lo"}[5m]))',
        "unit": "bytes",
        "kind": "line",
        "sort_order": 50,
    },
]


def get_config(db: Session) -> MonitoringConfig:
    row = db.get(MonitoringConfig, 1)
    if row is None:
        row = MonitoringConfig(id=1)
        db.add(row)
        db.flush()
    return row


def ensure_defaults(db: Session) -> MonitoringConfig:
    """幂等写入默认配置、内置模板和默认面板;不覆盖已有启用状态。"""
    config = get_config(db)
    existing_keys = set(db.scalars(select(SshCommandTemplate.key)).all())
    for item in BUILTIN_TEMPLATES:
        if item["key"] in existing_keys:
            continue
        db.add(
            SshCommandTemplate(
                key=item["key"],
                name=item["name"],
                command=item["command"],
                param_schema=item["param_schema"],
                description=item["description"],
                builtin=True,
                enabled=True,
            )
        )
    existing_titles = set(db.scalars(select(DashboardPanel.title)).all())
    for panel in BUILTIN_PANELS:
        if panel["title"] in existing_titles:
            continue
        db.add(DashboardPanel(builtin=True, enabled=True, **panel))
    db.commit()
    return config


def save_config(
    db: Session,
    admin: User,
    *,
    enabled: bool,
    prometheus_url: str,
    timeout_seconds: int = 10,
    ai_tools_enabled: bool = True,
    alerts_enabled: bool = False,
    alert_service_label: str = "service",
    alert_owner_id: int | None = None,
) -> MonitoringConfig:
    url = validate_prometheus_url(prometheus_url) if prometheus_url.strip() else ""
    if enabled and not url:
        raise MonitoringError("config", "启用监控前请填写 Prometheus 地址")
    timeout = max(3, min(int(timeout_seconds), 30))
    label = (alert_service_label or "service").strip() or "service"
    if len(label) > 64:
        raise MonitoringError("config", "告警服务标签过长")
    if alert_owner_id is not None:
        owner = db.get(User, alert_owner_id)
        if owner is None or not owner.is_active:
            raise MonitoringError("config", "告警故障负责人不存在或已停用")
    if alerts_enabled and not enabled:
        raise MonitoringError("config", "启用告警转故障前请先启用监控")
    row = get_config(db)
    row.enabled = enabled
    row.prometheus_url = url
    row.timeout_seconds = timeout
    row.ai_tools_enabled = ai_tools_enabled
    row.alerts_enabled = alerts_enabled
    row.alert_service_label = label
    row.alert_owner_id = alert_owner_id
    row.updated_by = admin.id
    from datetime import datetime, timezone

    row.updated_at = datetime.now(timezone.utc)
    return row


def client_from_config(config: MonitoringConfig) -> PrometheusClient:
    if not config.prometheus_url:
        raise MonitoringError("config", "尚未配置 Prometheus 地址")
    return PrometheusClient(config.prometheus_url, timeout=float(config.timeout_seconds))


def resolve_client(db: Session) -> PrometheusClient | None:
    config = db.get(MonitoringConfig, 1)
    if config is None or not config.enabled or not config.prometheus_url:
        return None
    try:
        return client_from_config(config)
    except MonitoringError:
        return None


def ai_tools_configured(db: Session) -> bool:
    """AI 工具是否可用:管理员未关闭,且至少配置了 Prometheus 或一台 SSH 服务器。"""
    config = db.get(MonitoringConfig, 1)
    if config is not None and not config.ai_tools_enabled:
        return False
    has_prometheus = bool(config and config.enabled and config.prometheus_url)
    has_server = db.scalar(
        select(MonitoredServer.id).where(
            MonitoredServer.enabled.is_(True),
            MonitoredServer.private_key_encrypted != "",
        )
    ) is not None
    return has_prometheus or has_server