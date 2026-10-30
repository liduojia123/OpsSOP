"""监控与业务联动:服务映射、故障实时指标裁剪、告警转故障与通知。"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.models import (
    DashboardPanel,
    Fault,
    FaultStatus,
    MonitoredServer,
    MonitoringConfig,
    Notification,
    Role,
)
from app.monitoring import alerts as alerts_module
from app.monitoring.alerts import alert_fingerprint, normalize_alert, sync_alerts
from app.monitoring.prometheus import QueryResult, Series
from tests.conftest import make_user


def _config(db, admin, **overrides) -> MonitoringConfig:
    row = db.get(MonitoringConfig, 1)
    if row is None:
        row = MonitoringConfig(id=1)
        db.add(row)
    row.enabled = overrides.pop("enabled", True)
    row.prometheus_url = overrides.pop("prometheus_url", "http://192.168.196.30:9090")
    row.timeout_seconds = overrides.pop("timeout_seconds", 5)
    row.ai_tools_enabled = overrides.pop("ai_tools_enabled", True)
    row.alerts_enabled = overrides.pop("alerts_enabled", True)
    row.alert_service_label = overrides.pop("alert_service_label", "service")
    row.alert_owner_id = overrides.pop("alert_owner_id", admin.id)
    row.updated_by = admin.id
    for key, value in overrides.items():
        setattr(row, key, value)
    db.commit()
    return row


def _server(db, **overrides) -> MonitoredServer:
    row = MonitoredServer(
        name=overrides.pop("name", "ubuntu3"),
        host=overrides.pop("host", "10.0.0.5"),
        port=22,
        username="ops",
        prometheus_instance=overrides.pop("prometheus_instance", "10.0.0.5:9100"),
        service_names=overrides.pop("service_names", ["order-service"]),
        **overrides,
    )
    db.add(row)
    db.commit()
    return row


class _FakeMetricsClient:
    def query_range(self, expr, minutes=60, step=60):
        return QueryResult(
            series=[
                Series(labels={"instance": "10.0.0.5:9100"}, points=[(1, 42.0), (2, 44.0)]),
                Series(labels={"instance": "10.0.0.9:9100"}, points=[(1, 90.0), (2, 91.0)]),
            ]
        )

    def query(self, expr):
        return QueryResult(series=[Series(labels={"instance": "10.0.0.5:9100"}, points=[(1, 1.0)])])


def test_fault_metrics_filter_by_mapped_instance(client, db, operator, monkeypatch):
    _server(db)
    for title, kind in (("CPU", "line"), ("负载", "stat")):
        db.add(DashboardPanel(title=title, promql="up", kind=kind, sort_order=1, enabled=True))
    fault = Fault(title="下单超时", service="order-service", created_by=operator.id)
    db.add(fault)
    db.commit()

    monkeypatch.setattr(
        "app.monitoring.service.resolve_client", lambda db: _FakeMetricsClient()
    )
    resp = client.get(f"/api/monitoring/faults/{fault.id}/metrics", headers=_headers(client, operator))
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is True and body["mapped"] is True
    assert body["instances"] == ["10.0.0.5:9100"]
    assert body["servers"][0]["service_names"] == ["order-service"]
    for panel in body["panels"]:
        instances = {point["labels"].get("instance") for point in panel["series"]}
        assert instances == {"10.0.0.5:9100"}


def _headers(client, user) -> dict:
    resp = client.post(
        "/api/auth/login",
        json={"username": user.username, "password": "test-password-1"},
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def test_fault_metrics_reports_unmapped_and_hides_other_fault(client, db, operator, monkeypatch):
    _server(db)
    fault = Fault(title="未知服务故障", service="payment", created_by=operator.id)
    db.add(fault)
    other = make_user(db, "op_other_metrics", Role.operator)
    db.commit()

    monkeypatch.setattr(
        "app.monitoring.service.resolve_client", lambda db: _FakeMetricsClient()
    )
    headers = _headers(client, other)
    # 无权访问他人故障:不返回任何映射信息
    assert (
        client.get(f"/api/monitoring/faults/{fault.id}/metrics", headers=headers).status_code
        == 403
    )

    resp = client.get(
        f"/api/monitoring/faults/{fault.id}/metrics", headers=_headers(client, operator)
    )
    body = resp.json()
    assert body["mapped"] is False
    assert "未映射" in body["error"]


class _FakeAlertsClient:
    def __init__(self, items):
        self.items = items
        self.calls = 0

    def alerts(self):
        self.calls += 1
        return self.items


def _alert(**overrides) -> dict:
    item = {
        "state": "firing",
        "labels": {
            "alertname": "HighCPU",
            "severity": "critical",
            "instance": "10.0.0.5:9100",
            "service": "order-service",
            "env": "prod",
        },
        "annotations": {"summary": "CPU 使用率超过 90%", "description": "持续 5 分钟"},
        "active_at": datetime.now(timezone.utc).isoformat(),
        "value": "0.95",
    }
    item.update(overrides)
    return item


def test_alert_sync_creates_dedupes_and_notifies(db, admin, reviewer, monkeypatch):
    _config(db, admin)
    fake = _FakeAlertsClient([_alert()])
    monkeypatch.setattr("app.monitoring.alerts.resolve_client", lambda db: fake)

    first = sync_alerts(db)
    assert first["ok"] is True and len(first["created"]) == 1
    fault = db.scalar(select(Fault).where(Fault.source == "alert"))
    assert fault is not None
    assert fault.status == FaultStatus.open
    assert fault.service == "order-service"
    assert fault.environment == "prod"
    assert "CPU 使用率超过 90%" in fault.description
    assert fault.created_by == admin.id

    # 同一告警未解决期间重复同步不重复建单
    second = sync_alerts(db, force=True)
    assert second["created"] == []
    assert len(db.scalars(select(Fault)).all()) == 1

    # 告警负责人与审核人都收到通知(负责人不重复)
    titles = [row.title for row in db.scalars(select(Notification)).all()]
    assert any("监控告警" in title or "新增" in title for title in titles)
    assert db.scalar(select(Notification).where(Notification.user_id == admin.id)) is not None
    assert db.scalar(select(Notification).where(Notification.user_id == reviewer.id)) is not None

    # 已解决后再次触发视为新事件
    fault.status = FaultStatus.resolved
    db.commit()
    third = sync_alerts(db, force=True)
    assert len(third["created"]) == 1
    assert len(db.scalars(select(Fault).where(Fault.source == "alert")).all()) == 2


def test_alert_sync_skips_when_disabled_and_throttles(db, admin, monkeypatch):
    _config(db, admin, alerts_enabled=False)
    fake = _FakeAlertsClient([_alert()])
    monkeypatch.setattr("app.monitoring.alerts.resolve_client", lambda db: fake)
    result = sync_alerts(db)
    assert result["ok"] is False and result["skipped"] == "alerts_disabled"
    assert fake.calls == 0

    _config(db, admin, alerts_enabled=True, alert_last_sync_at=datetime.now(timezone.utc))
    monkeypatch.setattr("app.monitoring.alerts.resolve_client", lambda db: fake)
    throttled = sync_alerts(db)
    assert throttled["skipped"] == "throttled"
    assert fake.calls == 0


def test_alert_fingerprint_is_stable_and_normalize_sanitizes():
    labels = {"alertname": "HighCPU", "service": "order", "severity": "critical"}
    reordered = {"severity": "critical", "service": "order", "alertname": "HighCPU"}
    assert alert_fingerprint(labels, "HighCPU") == alert_fingerprint(reordered, "HighCPU")
    item = normalize_alert(
        {
            "state": "firing",
            "labels": {"alertname": "HighCPU", "service": "order"},
            "annotations": {"summary": "token=super-secret-value cpu high"},
            "active_at": "2026-10-30T10:00:00Z",
        },
        "service",
    )
    assert item["service"] == "order"
    assert "super-secret-value" not in item["summary"]


def test_admin_alert_config_and_preview(client, db, adm_headers, op_headers, admin, monkeypatch):
    # 未启用监控时不能开启告警转故障
    bad = client.put(
        "/api/admin/monitoring",
        json={
            "enabled": False,
            "prometheus_url": "",
            "alerts_enabled": True,
        },
        headers=adm_headers,
    )
    assert bad.status_code == 400

    ok = client.put(
        "/api/admin/monitoring",
        json={
            "enabled": True,
            "prometheus_url": "http://192.168.196.30:9090",
            "alerts_enabled": True,
            "alert_service_label": "app",
            "alert_owner_id": admin.id,
        },
        headers=adm_headers,
    )
    assert ok.status_code == 200
    assert ok.json()["alerts_enabled"] is True
    assert ok.json()["alert_service_label"] == "app"

    monkeypatch.setattr(
        "app.monitoring.alerts.resolve_client",
        lambda db: _FakeAlertsClient([_alert(labels={"alertname": "HighCPU", "app": "order"})]),
    )
    preview = client.get("/api/admin/monitoring/alerts", headers=adm_headers)
    assert preview.status_code == 200
    assert preview.json()["items"][0]["service"] == "order"
    assert client.get("/api/admin/monitoring/alerts", headers=op_headers).status_code == 403
    assert (
        client.post("/api/admin/monitoring/alerts/sync", headers=op_headers).status_code
        == 403
    )