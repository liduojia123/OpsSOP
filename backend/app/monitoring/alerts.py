"""Prometheus 告警转故障。

- 只读取 `/api/v1/alerts`,不修改 Prometheus;
- 同一告警(alertname + 标签集合)指纹在未解决期间只生成一条故障;
  已解决后再次触发视为新事件,生成新故障;
- 故障正文只保留标签/注解摘要,先做脱敏再入库;告警内容视为不可信数据,
  不会被执行,也不会改变 AI 规则;
- 告警负责人生成故障并留审计,同时通知审核人员;
- 同步节流:默认 60 秒最多一次(强制同步由管理员显式触发)。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.sanitize import sanitize_text
from app.database import SessionLocal
from app.deps import record_audit
from app.models import Fault, FaultStatus, MonitoringConfig, Role, User
from app.monitoring.defaults import resolve_client
from app.monitoring.prometheus import MonitoringError
from app.notifications import notify, reviewer_ids

logger = logging.getLogger("opssop.monitoring.alerts")

SYNC_INTERVAL_SECONDS = 60
MAX_NEW_FAULTS = 20
MAX_ALERTS = 50


def alert_fingerprint(labels: dict[str, str], alertname: str) -> str:
    payload = json.dumps(
        {"alertname": alertname, "labels": {k: v for k, v in sorted(labels.items())}},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _short(value: str, limit: int) -> str:
    return sanitize_text(str(value or "").strip())[:limit]


def _parse_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _owner(db: Session, config: MonitoringConfig) -> User | None:
    if config.alert_owner_id:
        owner = db.get(User, config.alert_owner_id)
        if owner is not None and owner.is_active:
            return owner
    return db.scalar(
        select(User)
        .where(User.is_active.is_(True), User.role == Role.admin)
        .order_by(User.id)
        .limit(1)
    )


def normalize_alert(item: dict, service_label: str) -> dict:
    """把 Prometheus 告警整理成页面/故障可用的安全摘要。"""
    labels = {str(k): str(v) for k, v in (item.get("labels") or {}).items()}
    annotations = {
        str(k): str(v) for k, v in (item.get("annotations") or {}).items()
    }
    name = labels.get("alertname") or annotations.get("summary") or "Prometheus 告警"
    return {
        "name": _short(name, 200),
        "state": str(item.get("state", "")),
        "severity": _short(labels.get("severity", ""), 32),
        "instance": _short(labels.get("instance", ""), 128),
        "service": _short(labels.get(service_label, "") or labels.get("service", ""), 128),
        "summary": _short(
            annotations.get("summary") or annotations.get("description", ""), 500
        ),
        "active_at": str(item.get("active_at", "")),
        "labels": {k: _short(v, 200) for k, v in labels.items()},
        "annotations": {k: _short(v, 500) for k, v in annotations.items()},
        "value": _short(str(item.get("value", "")), 200),
    }


def preview_alerts(db: Session, config: MonitoringConfig | None = None) -> dict:
    """预览当前告警(不写库),用于管理员确认告警格式与标签。"""
    config = config or db.get(MonitoringConfig, 1)
    if config is None or not config.enabled or not config.prometheus_url:
        return {"ok": False, "enabled": False, "items": [], "error": "监控未启用或未配置"}
    client = resolve_client(db)
    if client is None:
        return {"ok": False, "enabled": False, "items": [], "error": "无法连接 Prometheus"}
    try:
        items = client.alerts()
    except MonitoringError as exc:
        return {"ok": False, "enabled": True, "items": [], "error": exc.message}
    label = config.alert_service_label or "service"
    return {
        "ok": True,
        "enabled": True,
        "items": [normalize_alert(item, label) for item in items[:MAX_ALERTS]],
        "error": "",
    }


def sync_alerts(db: Session, *, force: bool = False) -> dict:
    """同步告警并生成故障;返回本次结果(不抛连接错误)。"""
    config = db.get(MonitoringConfig, 1)
    if config is None or not config.enabled or not config.prometheus_url:
        return {"ok": False, "skipped": "monitoring_disabled", "error": "监控未启用或未配置"}
    if not config.alerts_enabled:
        return {"ok": False, "skipped": "alerts_disabled", "error": "告警转故障未开启"}

    now = datetime.now(timezone.utc)
    last = config.alert_last_sync_at
    if last is not None and last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    if not force and last is not None and (now - last).total_seconds() < SYNC_INTERVAL_SECONDS:
        return {"ok": True, "skipped": "throttled", "created": [], "synced_at": last.isoformat()}

    client = resolve_client(db)
    if client is None:
        return {"ok": False, "skipped": "connect", "error": "无法连接 Prometheus"}
    try:
        raw_items = client.alerts()
    except MonitoringError as exc:
        # 不同步时间戳:下次页面刷新可重试
        return {"ok": False, "skipped": "upstream", "error": exc.message}

    label = config.alert_service_label or "service"
    items = [normalize_alert(item, label) for item in raw_items]
    firing = [item for item in items if item["state"] == "firing"][:MAX_NEW_FAULTS]

    owner = _owner(db, config)
    if firing and owner is None:
        return {
            "ok": False,
            "skipped": "no_owner",
            "error": "没有可用的管理员账号作为告警故障负责人,请先在监控设置中指定",
        }

    created: list[dict] = []
    for item in firing:
        fingerprint = alert_fingerprint(item["labels"], item["name"])
        exists = db.scalar(
            select(Fault.id)
            .where(
                Fault.alert_fingerprint == fingerprint,
                Fault.status != FaultStatus.resolved,
            )
            .limit(1)
        )
        if exists is not None:
            continue
        detail_lines = [item["summary"]] if item["summary"] else []
        detail_lines.append(
            "Prometheus 告警: "
            + ", ".join(f"{k}={v}" for k, v in sorted(item["labels"].items()) if k != "alertname")
        )
        if item["value"]:
            detail_lines.append(f"触发值: {item['value']}")
        fault = Fault(
            title=f"[告警] {item['name']}"[:255],
            description="\n".join(line for line in detail_lines if line)[:4000],
            occurred_at=_parse_time(item["active_at"]),
            environment=_short(item["labels"].get("env", "") or item["labels"].get("environment", ""), 64),
            service=item["service"],
            tags=[tag for tag in ("alert", item["severity"]) if tag],
            source="alert",
            alert_fingerprint=fingerprint,
            status=FaultStatus.open,
            created_by=owner.id,
        )
        db.add(fault)
        db.flush()
        created.append({"id": fault.id, "title": fault.title, "service": fault.service})
        record_audit(
            db,
            None,
            "fault.create_from_alert",
            "fault",
            fault.id,
            {
                "alertname": item["name"][:200],
                "fingerprint": fingerprint[:16],
                "severity": item["severity"],
            },
        )

    if created:
        notify(
            db,
            [owner.id],
            kind="fault.alert",
            title=f"新增 {len(created)} 条监控告警故障",
            body="; ".join(item["title"] for item in created[:5]),
            link="/faults",
            entity_type="fault",
            entity_id=created[0]["id"],
        )
        notify(
            db,
            [user_id for user_id in reviewer_ids(db) if user_id != owner.id],
            kind="fault.alert",
            title=f"监控告警生成 {len(created)} 条故障",
            body="; ".join(item["title"] for item in created[:5]),
            link="/faults",
            entity_type="fault",
            entity_id=created[0]["id"],
        )

    config.alert_last_sync_at = now
    record_audit(
        db,
        None,
        "monitoring.alert.sync",
        "monitoring",
        "",
        {
            "created": [item["id"] for item in created],
            "alerts": len(items),
            "firing": len(firing),
            "forced": force,
        },
    )
    db.commit()
    return {
        "ok": True,
        "created": created,
        "firing": len(firing),
        "total": len(items),
        "synced_at": now.isoformat(timespec="seconds"),
        "error": "",
    }


def sync_alerts_once() -> dict:
    """在独立会话中同步一次告警,供后台任务使用。"""
    db = SessionLocal()
    try:
        return sync_alerts(db)
    finally:
        db.close()


async def alerts_loop(interval: float = 60.0) -> None:
    """生产模式后台告警同步:每 60 秒一次。

    告警转故障未开启、监控未配置或上游不可达时只跳过/记日志,不中断循环;
    与页面触发的同步共用节流与指纹去重,不会重复建单。
    """
    while True:
        try:
            await asyncio.sleep(max(10.0, float(interval)))
            result = await asyncio.to_thread(sync_alerts_once)
            if result.get("created"):
                logger.info("告警同步生成 %s 条故障", len(result["created"]))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 单轮失败不能让后台任务停摆
            logger.exception("告警同步失败,下一轮继续")