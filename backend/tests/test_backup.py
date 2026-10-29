"""平台备份、保留与邮件接口的单元测试（不执行 pg_dump）。"""

from datetime import datetime, timedelta, timezone
import json

import pytest

from app.backup import list_backups, prune_backups, valid_name
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
    Settings(email_mode="disabled", backup_enabled=False).validate_backup_config()


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
