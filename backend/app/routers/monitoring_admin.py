"""管理员监控配置:Prometheus 数据源、服务器与只读命令模板。

- 只有管理员可以修改数据源、服务器和模板启停;
- SSH 私钥加密保存,接口只回传 has_key;
- 主机指纹首次通过测试连接获取,由管理员确认后保存,后续连接强校验。
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.keys import KeyError_, decrypt_secret, encrypt_secret
from app.config import get_settings
from app.database import get_db
from app.deps import record_audit, require_admin
from app.models import (
    DashboardPanel,
    MonitoredServer,
    SshCommandTemplate,
    User,
)
from app.monitoring.alerts import preview_alerts, sync_alerts
from app.monitoring.defaults import client_from_config, ensure_defaults, get_config, save_config
from app.monitoring.prometheus import MonitoringError, PrometheusClient, validate_prometheus_url
from app.monitoring.service import server_public, template_public
from app.monitoring.ssh import SshError, SshTarget, probe_fingerprint
from app.schemas import (
    AlertPreviewOut,
    AlertSyncOut,
    DashboardPanelOut,
    ServerTestRequest,
    MonitoredServerOut,
    MonitoredServerRequest,
    MonitoringConfigOut,
    MonitoringConfigRequest,
    MonitoringTargetOut,
    MonitoringTestOut,
    MonitoringTestRequest,
    ServerTestOut,
    SshTemplateOut,
)

router = APIRouter(prefix="/api/admin", tags=["monitoring-admin"])

_HOST_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,255}$")
_USER_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def _config_out(row) -> MonitoringConfigOut:
    return MonitoringConfigOut(
        enabled=row.enabled,
        prometheus_url=row.prometheus_url,
        timeout_seconds=row.timeout_seconds,
        ai_tools_enabled=row.ai_tools_enabled,
        alerts_enabled=row.alerts_enabled,
        alert_service_label=row.alert_service_label,
        alert_owner_id=row.alert_owner_id,
        alert_last_sync_at=row.alert_last_sync_at,
        updated_at=row.updated_at,
    )


@router.get("/monitoring", response_model=MonitoringConfigOut)
def read_monitoring(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> MonitoringConfigOut:
    return _config_out(ensure_defaults(db))


@router.put("/monitoring", response_model=MonitoringConfigOut)
def update_monitoring(
    body: MonitoringConfigRequest,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> MonitoringConfigOut:
    ensure_defaults(db)  # 首次保存时写入内置模板/面板,保证配置后即可执行
    try:
        row = save_config(
            db,
            admin,
            enabled=body.enabled,
            prometheus_url=body.prometheus_url,
            timeout_seconds=body.timeout_seconds,
            ai_tools_enabled=body.ai_tools_enabled,
            alerts_enabled=body.alerts_enabled,
            alert_service_label=body.alert_service_label,
            alert_owner_id=body.alert_owner_id,
        )
    except MonitoringError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, exc.message) from exc
    record_audit(
        db,
        admin,
        "monitoring.save",
        "monitoring",
        "",
        {
            "enabled": row.enabled,
            "url": row.prometheus_url,
            "ai_tools": row.ai_tools_enabled,
            "alerts": row.alerts_enabled,
        },
    )
    db.commit()
    return _config_out(row)


@router.post("/monitoring/test", response_model=MonitoringTestOut)
def test_monitoring(
    body: MonitoringTestRequest,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> MonitoringTestOut:
    row = get_config(db)
    url = body.prometheus_url.strip() or row.prometheus_url
    try:
        client = PrometheusClient(validate_prometheus_url(url), timeout=float(row.timeout_seconds))
    except MonitoringError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, exc.message) from exc
    if not client.ready():
        return MonitoringTestOut(ok=False, message="Prometheus 未就绪或地址不可达")
    try:
        targets = client.targets()
    except MonitoringError as exc:
        return MonitoringTestOut(ok=False, message=exc.message)
    up = sum(1 for t in targets if t.get("health") == "up")
    return MonitoringTestOut(
        ok=True,
        message=f"连接成功,{up}/{len(targets)} 个抓取目标正常",
        target_count=len(targets),
        up_count=up,
    )


@router.get("/monitoring/targets", response_model=list[MonitoringTargetOut])
def list_targets(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> list[MonitoringTargetOut]:
    row = get_config(db)
    try:
        client = client_from_config(row)
        return [MonitoringTargetOut(**t) for t in client.targets()]
    except MonitoringError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, exc.message) from exc


@router.get("/monitoring/alerts", response_model=AlertPreviewOut)
def preview_prometheus_alerts(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> AlertPreviewOut:
    """预览 Prometheus 当前告警(只读),用于确认标签与服务映射。"""
    ensure_defaults(db)
    return AlertPreviewOut(**preview_alerts(db))


@router.post("/monitoring/alerts/sync", response_model=AlertSyncOut)
def sync_prometheus_alerts(
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> AlertSyncOut:
    """立即同步告警并生成故障(按指纹去重);不受 60 秒节流限制。"""
    ensure_defaults(db)
    result = sync_alerts(db, force=True)
    record_audit(
        db,
        admin,
        "monitoring.alert.sync_manual",
        "monitoring",
        "",
        {"created": [item["id"] for item in result.get("created", [])]},
    )
    db.commit()
    return AlertSyncOut(**result)


# ===== 服务器 =====

@router.get("/servers", response_model=list[MonitoredServerOut])
def list_servers(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> list[MonitoredServerOut]:
    rows = db.scalars(select(MonitoredServer).order_by(MonitoredServer.id)).all()
    return [MonitoredServerOut(**server_public(row)) for row in rows]


def _validate_server(body: MonitoredServerRequest) -> None:
    if not _HOST_RE.fullmatch(body.host.strip()):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "服务器地址格式不合法")
    if not _USER_RE.fullmatch(body.username.strip()):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "SSH 用户名格式不合法")
    if not 1 <= body.port <= 65535:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "SSH 端口不合法")
    if len(body.service_names) > 50:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "承载服务最多 50 个")


def _clean_services(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values or []:
        name = str(value).strip()[:128]
        key = name.lower()
        if name and key not in seen:
            seen.add(key)
            out.append(name)
    return out


@router.post("/servers", response_model=MonitoredServerOut, status_code=status.HTTP_201_CREATED)
def create_server(
    body: MonitoredServerRequest,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> MonitoredServerOut:
    _validate_server(body)
    if not body.private_key.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "创建服务器时必须填写 SSH 私钥")
    settings = get_settings()
    row = MonitoredServer(
        name=body.name.strip(),
        host=body.host.strip(),
        port=body.port,
        username=body.username.strip(),
        private_key_encrypted=encrypt_secret(body.private_key.strip(), settings.secret_key),
        host_key_fingerprint=body.host_key_fingerprint.strip(),
        prometheus_instance=body.prometheus_instance.strip(),
        service_names=_clean_services(body.service_names),
        enabled=body.enabled,
        notes=body.notes.strip(),
    )
    db.add(row)
    record_audit(db, admin, "server.save", "server", body.host, {"name": row.name})
    db.commit()
    return MonitoredServerOut(**server_public(row))


@router.patch("/servers/{server_id}", response_model=MonitoredServerOut)
def update_server(
    server_id: int,
    body: MonitoredServerRequest,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> MonitoredServerOut:
    _validate_server(body)
    row = db.get(MonitoredServer, server_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "服务器不存在")
    row.name = body.name.strip()
    row.host = body.host.strip()
    row.port = body.port
    row.username = body.username.strip()
    row.prometheus_instance = body.prometheus_instance.strip()
    row.service_names = _clean_services(body.service_names)
    row.enabled = body.enabled
    row.notes = body.notes.strip()
    if body.private_key.strip():
        settings = get_settings()
        row.private_key_encrypted = encrypt_secret(body.private_key.strip(), settings.secret_key)
    if body.host_key_fingerprint.strip():
        row.host_key_fingerprint = body.host_key_fingerprint.strip()
    record_audit(db, admin, "server.save", "server", row.host, {"name": row.name})
    db.commit()
    return MonitoredServerOut(**server_public(row))


@router.delete(
    "/servers/{server_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
def delete_server(
    server_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> Response:
    row = db.get(MonitoredServer, server_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "服务器不存在")
    record_audit(db, admin, "server.delete", "server", row.host, {"name": row.name})
    db.delete(row)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/servers/{server_id}/test", response_model=ServerTestOut)
def test_server(
    server_id: int,
    body: ServerTestRequest | None = None,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> ServerTestOut:
    row = db.get(MonitoredServer, server_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "服务器不存在")
    settings = get_settings()
    pem = (body.private_key.strip() if body else "") or ""
    if not pem:
        try:
            pem = decrypt_secret(row.private_key_encrypted, settings.secret_key)
        except KeyError_ as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    target = SshTarget(
        host=(body.host.strip() if body and body.host.strip() else row.host),
        port=(body.port if body else row.port),
        username=(body.username.strip() if body and body.username.strip() else row.username),
        private_key_pem=pem,
    )
    try:
        fingerprint = probe_fingerprint(target)
    except SshError as exc:
        return ServerTestOut(ok=False, message=exc.message)
    return ServerTestOut(
        ok=True,
        fingerprint=fingerprint,
        message="连接成功;确认指纹后保存到服务器配置",
    )


# ===== 命令模板与面板 =====

@router.get("/ssh-templates", response_model=list[SshTemplateOut])
def list_templates(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> list[SshTemplateOut]:
    ensure_defaults(db)
    rows = db.scalars(select(SshCommandTemplate).order_by(SshCommandTemplate.id)).all()
    return [SshTemplateOut(**template_public(row)) for row in rows]


@router.patch("/ssh-templates/{template_id}", response_model=SshTemplateOut)
def update_template(
    template_id: int,
    enabled: bool,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> SshTemplateOut:
    row = db.get(SshCommandTemplate, template_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "模板不存在")
    row.enabled = enabled
    record_audit(db, admin, "server.template", "ssh_template", row.key, {"enabled": enabled})
    db.commit()
    return SshTemplateOut(**template_public(row))


@router.get("/dashboard-panels", response_model=list[DashboardPanelOut])
def list_panels(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> list[DashboardPanelOut]:
    ensure_defaults(db)
    rows = db.scalars(select(DashboardPanel).order_by(DashboardPanel.sort_order, DashboardPanel.id)).all()
    return [DashboardPanelOut.model_validate(row, from_attributes=True) for row in rows]