"""备份告警单元测试:失败/过期时发信,未启用或未配置收件人时不发信。"""

import logging

import pytest

from app import alerts as alerts_mod
from app import maintenance as maintenance_mod
from app.backup import BackupError
from app.config import Settings
from app.mailer import MailMessage


def _settings(**overrides) -> Settings:
    values = {
        "email_mode": "smtp",
        "email_host": "smtp.example.com",
        "email_from": "ops@example.com",
        "alert_email_to": "admin@example.com",
        "backup_enabled": False,
    }
    values.update(overrides)
    return Settings(**values)


class RecordingMailer:
    def __init__(self, result: bool = True) -> None:
        self.sent: list[MailMessage] = []
        self.result = result

    def send(self, message: MailMessage) -> bool:
        self.sent.append(message)
        return self.result


def test_recipients_split_on_comma_and_semicolon():
    settings = _settings(alert_email_to="a@example.com, b@example.com; c@example.com")
    assert alerts_mod.alert_recipients(settings) == [
        "a@example.com",
        "b@example.com",
        "c@example.com",
    ]


def test_failure_alert_sends_to_all_recipients(monkeypatch):
    mailer = RecordingMailer()
    monkeypatch.setattr(alerts_mod, "build_mailer", lambda _settings: mailer)
    settings = _settings(alert_email_to="a@example.com,b@example.com")

    ok = alerts_mod.notify_backup_failure(settings, "pg_dump 失败: exit 1", "scheduled")

    assert ok is True
    assert [m.to for m in mailer.sent] == ["a@example.com", "b@example.com"]
    assert "备份失败" in mailer.sent[0].subject
    assert "每日定时" in mailer.sent[0].body
    assert "pg_dump" in mailer.sent[0].body


def test_alert_skipped_when_disabled_or_no_recipients(monkeypatch):
    mailer = RecordingMailer()
    monkeypatch.setattr(alerts_mod, "build_mailer", lambda _settings: mailer)

    assert alerts_mod.notify_backup_failure(_settings(email_mode="disabled"), "x", "manual") is False
    assert alerts_mod.notify_backup_failure(_settings(alert_email_to=""), "x", "manual") is False
    assert mailer.sent == []


def test_stale_alert_contains_last_success(monkeypatch):
    mailer = RecordingMailer()
    monkeypatch.setattr(alerts_mod, "build_mailer", lambda _settings: mailer)

    ok = alerts_mod.notify_backup_stale(_settings(), "2026-10-26T02:00:00+00:00", 50.5)

    assert ok is True
    assert "26 小时" in mailer.sent[0].subject
    assert "2026-10-26T02:00:00+00:00" in mailer.sent[0].body


def test_send_failure_is_reported_not_raised(monkeypatch):
    mailer = RecordingMailer(result=False)
    monkeypatch.setattr(alerts_mod, "build_mailer", lambda _settings: mailer)
    assert alerts_mod.notify_backup_failure(_settings(), "x", "manual") is False


def test_config_rejects_alert_without_smtp():
    with pytest.raises(ValueError):
        Settings(alert_email_to="admin@example.com", email_mode="disabled").validate_backup_config()


def test_maintenance_alerts_on_backup_failure(monkeypatch, caplog):
    """定时备份抛异常时发一次告警,且维护流程继续(清理仍执行)。"""
    calls: list[tuple] = []

    class FakeDB:
        def rollback(self):
            pass

        def commit(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(maintenance_mod, "SessionLocal", lambda: FakeDB())
    monkeypatch.setattr(
        maintenance_mod, "run_backup", lambda *a, **k: (_ for _ in ()).throw(BackupError("磁盘已满"))
    )
    monkeypatch.setattr(maintenance_mod, "record_audit", lambda *a, **k: None)
    monkeypatch.setattr(maintenance_mod, "prune_audit_events", lambda *a, **k: 0)
    monkeypatch.setattr(maintenance_mod, "prune_login_attempts", lambda *a, **k: 0)
    monkeypatch.setattr(
        maintenance_mod,
        "prune_chat",
        lambda *a, **k: {"messages_deleted": 0, "invocations_deleted": 0, "conversations_deleted": 0},
    )
    monkeypatch.setattr(
        maintenance_mod,
        "notify_backup_failure",
        lambda settings, reason, trigger: calls.append((reason, trigger)) or True,
    )

    with caplog.at_level(logging.ERROR, logger="opssop.maintenance"):
        results = maintenance_mod.run_daily_maintenance(_settings(backup_enabled=True))

    assert results["backup"] == "failed"
    assert calls and calls[0][1] == "scheduled"
    assert "磁盘已满" in calls[0][0]
    assert "audit_deleted" in results  # 告警/maintenance 失败没有中断后续清理


def test_maintenance_stale_alert_when_backups_disabled(monkeypatch):
    sent: list[tuple] = []
    monkeypatch.setattr(
        maintenance_mod,
        "SessionLocal",
        lambda: type(
            "FakeDB",
            (),
            {"rollback": lambda self: None, "commit": lambda self: None, "close": lambda self: None},
        )(),
    )
    monkeypatch.setattr(maintenance_mod, "record_audit", lambda *a, **k: None)
    monkeypatch.setattr(maintenance_mod, "prune_audit_events", lambda *a, **k: 0)
    monkeypatch.setattr(maintenance_mod, "prune_login_attempts", lambda *a, **k: 0)
    monkeypatch.setattr(
        maintenance_mod,
        "prune_chat",
        lambda *a, **k: {"messages_deleted": 0, "invocations_deleted": 0, "conversations_deleted": 0},
    )
    monkeypatch.setattr(
        maintenance_mod,
        "backup_status",
        lambda settings: {"last_success_at": "2026-10-01T00:00:00+00:00", "backup_age_hours": 100.0},
    )
    monkeypatch.setattr(
        maintenance_mod,
        "notify_backup_stale",
        lambda settings, last, age: sent.append((last, age)) or True,
    )

    results = maintenance_mod.run_daily_maintenance(_settings(backup_enabled=False))

    assert sent == [("2026-10-01T00:00:00+00:00", 100.0)]
    assert results["stale_alert"] == "sent"