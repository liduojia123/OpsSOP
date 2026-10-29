"""异地副本每日任务的单元测试(不连 docker/ssh/真实主机)。"""

import subprocess
from pathlib import Path

import pytest

from app.ops import offsite_daily as daily


def _config(tmp_path: Path, **overrides) -> daily.OffsiteDailyConfig:
    values = {
        "project": "opssop-test",
        "backup_dir": "/data/OpsSop/BackUp",
        "container": "test-backend-1",
        "host": "192.168.1.30",
        "user": "backup",
        "remote_dir": "/srv/opssop-offsite",
        "identity": "",
        "keep": 3,
        "max_age_hours": 30,
        "state_file": str(tmp_path / "state.json"),
        "report_dir": str(tmp_path / "reports"),
    }
    values.update(overrides)
    return daily.OffsiteDailyConfig(**values)


def test_load_env_file_parses_quotes_and_skips_comments(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "# 注释\nOFFSITE_HOST=192.168.1.30\nOFFSITE_USER=\"backup\"\n\nEMPTY=\nBAD LINE\n",
        encoding="utf-8",
    )
    values = daily.load_env_file(path)
    assert values["OFFSITE_HOST"] == "192.168.1.30"
    assert values["OFFSITE_USER"] == "backup"
    assert values["EMPTY"] == ""
    assert "BAD LINE" not in values


def test_load_env_file_missing_path_is_empty(tmp_path):
    assert daily.load_env_file(tmp_path / "nope.env") == {}


def test_resolve_precedence_cli_over_env_over_file(monkeypatch):
    monkeypatch.delenv("OFFSITE_HOST", raising=False)
    file_env = {"OFFSITE_HOST": "from-file", "OFFSITE_USER": "from-file"}
    assert daily.resolve("OFFSITE_HOST", "from-cli", file_env) == "from-cli"
    assert daily.resolve("OFFSITE_HOST", None, file_env) == "from-file"
    monkeypatch.setenv("OFFSITE_HOST", "from-process")
    assert daily.resolve("OFFSITE_HOST", None, file_env) == "from-process"
    assert daily.resolve("OFFSITE_MISSING", None, file_env, "fallback") == "fallback"


def test_find_newest_pair_parses_container_listing(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, timeout=300):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "opssop-20261029T020000Z.dump\n", "")

    monkeypatch.setattr(daily, "run", fake_run)
    assert daily.find_newest_pair("c1", "/data/OpsSop/BackUp") == (
        "opssop-20261029T020000Z.dump",
        "opssop-20261029T020000Z.json",
    )
    assert "ls -1t" in calls[0][-1]


def test_find_newest_pair_returns_none_without_backups(monkeypatch):
    monkeypatch.setattr(
        daily,
        "run",
        lambda cmd, timeout=300: subprocess.CompletedProcess(cmd, 0, "", ""),
    )
    assert daily.find_newest_pair("c1", "/data/OpsSop/BackUp") is None


def test_backup_age_hours_from_stat(monkeypatch):
    import time

    now = int(time.time())
    monkeypatch.setattr(
        daily,
        "run",
        lambda cmd, timeout=300: subprocess.CompletedProcess(cmd, 0, f"{now - 3600}\n", ""),
    )
    age = daily.backup_age_hours("c1", "/data/OpsSop/BackUp", "opssop-20261029T020000Z.dump")
    assert age is not None and 0.9 < age < 1.2


def test_send_mail_skipped_when_disabled_or_without_recipients():
    assert daily.send_mail({"EMAIL_MODE": "disabled"}, "s", "b") is False
    assert daily.send_mail({"EMAIL_MODE": "smtp", "EMAIL_HOST": "h", "EMAIL_FROM": "f"}, "s", "b") is False


def test_execute_skips_when_target_not_configured(tmp_path):
    result = daily.execute(_config(tmp_path, host="", remote_dir=""))
    assert result["status"] == "noop"


def test_execute_skips_already_copied_backup(tmp_path, monkeypatch):
    state = tmp_path / "state.json"
    state.write_text('{"last_copied": "opssop-20261029T020000Z.dump"}', encoding="utf-8")
    monkeypatch.setattr(daily, "find_container", lambda project, explicit="": "c1")
    monkeypatch.setattr(
        daily,
        "find_newest_pair",
        lambda container, backup_dir: ("opssop-20261029T020000Z.dump", "opssop-20261029T020000Z.json"),
    )
    result = daily.execute(_config(tmp_path))
    assert result["status"] == "skipped"
    assert result["reason"] == "已复制过"


def test_execute_skips_stale_backup(tmp_path, monkeypatch):
    monkeypatch.setattr(daily, "find_container", lambda project, explicit="": "c1")
    monkeypatch.setattr(
        daily,
        "find_newest_pair",
        lambda container, backup_dir: ("opssop-20261001T020000Z.dump", "opssop-20261001T020000Z.json"),
    )
    monkeypatch.setattr(daily, "backup_age_hours", lambda *a: 100.0)
    result = daily.execute(_config(tmp_path))
    assert result["status"] == "skipped"
    assert result["reason"] == "备份过旧"


def test_execute_copies_and_records_state(tmp_path, monkeypatch):
    def fake_run_offsite(archive, metadata, target, *, keep=14, out_dir=None):
        assert archive.name.endswith(".dump")
        assert metadata.name.endswith(".json")
        assert target.host == "192.168.1.30"
        return {
            "success": True,
            "sha256_verified": True,
            "remote_removed": [],
            "completed_at": "2026-10-29T04:00:00+00:00",
        }

    monkeypatch.setattr(daily, "find_container", lambda project, explicit="": "c1")
    monkeypatch.setattr(
        daily,
        "find_newest_pair",
        lambda container, backup_dir: ("opssop-20261029T020000Z.dump", "opssop-20261029T020000Z.json"),
    )
    monkeypatch.setattr(daily, "backup_age_hours", lambda *a: 1.0)

    def fake_run(cmd, timeout=300):
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(daily, "run", fake_run)
    monkeypatch.setattr(daily, "run_offsite", fake_run_offsite)

    result = daily.execute(_config(tmp_path))

    assert result["status"] == "copied"
    state = daily.load_state(tmp_path / "state.json")
    assert state["last_copied"] == "opssop-20261029T020000Z.dump"


def test_execute_raises_when_docker_cp_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(daily, "find_container", lambda project, explicit="": "c1")
    monkeypatch.setattr(
        daily,
        "find_newest_pair",
        lambda container, backup_dir: ("opssop-20261029T020000Z.dump", "opssop-20261029T020000Z.json"),
    )
    monkeypatch.setattr(daily, "backup_age_hours", lambda *a: 1.0)
    monkeypatch.setattr(
        daily,
        "run",
        lambda cmd, timeout=300: subprocess.CompletedProcess(cmd, 1, "", "copy failed"),
    )
    with pytest.raises(daily.OpsError):
        daily.execute(_config(tmp_path))
