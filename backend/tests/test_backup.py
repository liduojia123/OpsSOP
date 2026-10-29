"""平台备份、保留与邮件接口的单元测试（不执行 pg_dump）。"""

from datetime import datetime, timedelta, timezone
import json
import os

import pytest

from app import backup as backup_mod
from app.backup import (
    list_backups,
    prune_backups,
    read_backup_status,
    record_backup_failure,
    record_backup_success,
    valid_name,
)
from app.config import Settings
from app.mailer import DisabledMailer, MailMessage, build_mailer
from app.maintenance import next_run_at
from app.models import AuditEvent
from app.retention import prune_audit_events


def settings_for(tmp_path, **overrides) -> Settings:
    values = {
        "backup_dir": str(tmp_path),
        "backup_enabled": True,
        "backup_retention_days": 30,
        "audit_retention_days": 30,
        "backup_hour": 2,
    }
    values.update(overrides)
    return Settings(**values)


def make_backup(directory, name: str, created_at: datetime, size: int = 32) -> None:
    (directory / f"{name}.dump").write_bytes(b"x" * size)
    (directory / f"{name}.json").write_text(
        json.dumps(
            {
                "name": name,
                "created_at": created_at.isoformat(),
                "sha256": "a" * 64,
                "trigger": "scheduled",
                "retention_days": 30,
            }
        ),
        encoding="utf-8",
    )


def test_valid_backup_name():
    assert valid_name("opssop-20261028T020000Z")
    assert not valid_name("opssop-20261028T020000Z.dump")
    assert not valid_name("../opssop-20261028T020000Z")
    assert not valid_name("other-20261028T020000Z")


def test_prune_removes_only_expired_pairs(tmp_path):
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=31)
    fresh = now - timedelta(days=2)
    make_backup(tmp_path, "opssop-20260101T000000Z", old)
    make_backup(tmp_path, "opssop-20261027T000000Z", fresh)
    (tmp_path / "unrelated.txt").write_text("keep", encoding="utf-8")

    removed = prune_backups(settings_for(tmp_path), now=now)

    assert removed == ["opssop-20260101T000000Z"]
    assert not (tmp_path / "opssop-20260101T000000Z.dump").exists()
    assert not (tmp_path / "opssop-20260101T000000Z.json").exists()
    assert (tmp_path / "opssop-20261027T000000Z.dump").exists()
    assert (tmp_path / "unrelated.txt").exists()


def test_list_ignores_corrupt_or_incomplete_entries(tmp_path):
    now = datetime.now(timezone.utc)
    make_backup(tmp_path, "opssop-20261027T000000Z", now)
    (tmp_path / "opssop-20261028T000000Z.json").write_text("not-json", encoding="utf-8")
    (tmp_path / "opssop-20261029T000000Z.json").write_text(
        json.dumps({"name": "opssop-20261029T000000Z", "created_at": now.isoformat()}),
        encoding="utf-8",
    )

    items = list_backups(settings_for(tmp_path))

    assert [item["name"] for item in items] == ["opssop-20261027T000000Z"]
    assert items[0]["size_bytes"] == 32


def test_prune_removes_orphan_dumps_past_retention(tmp_path):
    """没有配对元信息的孤立 dump 不进列表,只能靠清理回收空间。"""
    now = datetime.now(timezone.utc)
    old_ts = (now - timedelta(days=31)).timestamp()
    make_backup(tmp_path, "opssop-20261027T000000Z", now - timedelta(days=2))
    orphan_old = tmp_path / "opssop-20250901T000000Z.dump"
    orphan_old.write_bytes(b"x" * 16)
    os.utime(orphan_old, (old_ts, old_ts))
    orphan_fresh = tmp_path / "opssop-20261020T000000Z.dump"
    orphan_fresh.write_bytes(b"x" * 16)

    removed = prune_backups(settings_for(tmp_path), now=now)

    assert not orphan_old.exists()
    assert orphan_fresh.exists()
    assert (tmp_path / "opssop-20261027T000000Z.dump").exists()
    assert "opssop-20250901T000000Z" in removed


def test_metadata_write_failure_leaves_no_orphan_dump(tmp_path, monkeypatch):
    """元信息写入失败时删除对应 dump,避免孤立归档逐渐占满磁盘。"""
    name = "opssop-20261028T000000Z"
    dump = tmp_path / f"{name}.dump"
    dump.write_bytes(b"x" * 16)
    meta_tmp = tmp_path / f".{name}.json.tmp"
    meta_tmp.write_text("{}", encoding="utf-8")

    def boom(src, dst, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(backup_mod.os, "replace", boom)
    with pytest.raises(backup_mod.BackupError):
        backup_mod._write_metadata(tmp_path, name, {"name": name})
    assert not dump.exists()
    assert not meta_tmp.exists()


def test_backup_status_records_success_and_failure(tmp_path):
    settings = settings_for(tmp_path)
    assert read_backup_status(settings) == {}
    record_backup_success(settings, "opssop-20261028T020000Z", "2026-10-28T02:00:00+00:00")
    record_backup_failure(settings, "pg_dump 失败: exit 2")

    status = read_backup_status(settings)

    assert status["last_success_name"] == "opssop-20261028T020000Z"
    assert status["last_success_at"] == "2026-10-28T02:00:00+00:00"
    assert "pg_dump" in status["last_failure_reason"]
    assert status["last_failure_at"]
    # 状态文件与临时文件都不应出现在备份列表里
    assert list_backups(settings) == []


def test_backup_status_write_never_raises_when_dir_unusable(tmp_path):
    """备份目录不可用时(CI 非 root 无法创建 /data/OpsSop/BackUp)状态写入只告警。

    否则记录失败状态本身抛异常会掩盖原始备份失败,并中断维护任务。
    """
    blocked = tmp_path / "blocked"
    blocked.write_text("占用为文件,不能作为目录", encoding="utf-8")
    settings = settings_for(blocked)

    record_backup_failure(settings, "pg_dump 失败")
    record_backup_success(settings, "opssop-20261028T020000Z", "2026-10-28T02:00:00+00:00")

    assert read_backup_status(settings) == {}


def test_backup_config_api_reports_last_status(client, adm_headers, tmp_path, monkeypatch):
    """/api/admin/backups/config 暴露最近成功/失败与备份年龄,备份失败可被发现。"""
    settings = settings_for(tmp_path)
    record_backup_failure(settings, "pg_dump 失败: exit 1")
    record_backup_success(settings, "opssop-20261028T020000Z", "2026-10-28T02:00:00+00:00")
    monkeypatch.setattr("app.routers.backups.get_settings", lambda: settings)

    resp = client.get("/api/admin/backups/config", headers=adm_headers)

    assert resp.status_code == 200
    data = resp.json()
    assert data["last_success_at"] == "2026-10-28T02:00:00+00:00"
    assert "pg_dump" in data["last_failure_reason"]
    assert data["last_failure_at"]
    assert data["backup_age_hours"] is not None


def test_disabled_mailer_does_not_send():
    assert isinstance(build_mailer(Settings(email_mode="disabled")), DisabledMailer)
    assert build_mailer(Settings(email_mode="disabled")).send(MailMessage("a@example.com", "s", "b")) is False


def test_backup_config_validation():
    with pytest.raises(ValueError):
        Settings(backup_hour=24).validate_backup_config()
    with pytest.raises(ValueError):
        Settings(backup_retention_days=0).validate_backup_config()
    with pytest.raises(ValueError):
        Settings(email_mode="smtp", email_host="", email_from="").validate_backup_config()
    # 显式清空告警收件人:本机环境文件可能配置了 ALERT_EMAIL_TO,测试不依赖宿主环境
    Settings(email_mode="disabled", backup_enabled=False, alert_email_to="").validate_backup_config()


def test_next_run_is_tomorrow_after_hour():
    now = datetime(2026, 10, 28, 3, 0, 0)
    assert next_run_at(2, now) == datetime(2026, 10, 29, 2, 0, 0)
    assert next_run_at(5, now) == datetime(2026, 10, 28, 5, 0, 0)


def test_prune_audit_events_only_removes_expired(db):
    now = datetime.now(timezone.utc)
    db.add(AuditEvent(action="old.event", created_at=now - timedelta(days=31)))
    db.add(AuditEvent(action="fresh.event", created_at=now - timedelta(days=1)))
    db.commit()

    removed = prune_audit_events(db, 30, now=now)

    assert removed == 1
    assert db.query(AuditEvent).filter(AuditEvent.action == "old.event").count() == 0
    assert db.query(AuditEvent).filter(AuditEvent.action == "fresh.event").count() == 1
