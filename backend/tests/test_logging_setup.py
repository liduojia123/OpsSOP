"""应用日志文件输出(可选功能)的单元测试:不依赖真实时间轮转。"""

import logging
from datetime import datetime, timedelta
from pathlib import Path

from app.config import Settings
from app.logging_setup import build_file_handler, setup_file_logging


def _settings(tmp_path: Path, **overrides) -> Settings:
    values = {"log_file_enabled": True, "log_dir": str(tmp_path), "log_retention_days": 30}
    values.update(overrides)
    return Settings(**values)


def test_disabled_by_default_writes_nothing(tmp_path):
    assert setup_file_logging(Settings(log_file_enabled=False, log_dir=str(tmp_path))) is False
    assert not (tmp_path / "opssop.log").exists()


def test_enabled_writes_records_to_file(tmp_path):
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        assert setup_file_logging(_settings(tmp_path)) is True
        logging.getLogger("opssop.logging_test").info("文件日志测试记录")
        for handler in root.handlers:
            handler.flush()
        content = (tmp_path / "opssop.log").read_text(encoding="utf-8")
        assert "文件日志测试记录" in content
        assert "opssop.logging_test" in content
        assert root.level <= logging.INFO
    finally:
        for handler in list(root.handlers):
            if handler not in before:
                handler.close()
                root.removeHandler(handler)
        root.setLevel(logging.NOTSET)


def test_rotation_keeps_only_retention_days(tmp_path):
    """超过保留天数的旧轮转文件应被识别删除;不真实等待跨天。"""
    handler = build_file_handler(_settings(tmp_path, log_retention_days=30))
    try:
        now = datetime.now()
        for days_ago in range(1, 41):
            day = now - timedelta(days=days_ago)
            (tmp_path / f"opssop.log.{day.strftime('%Y-%m-%d')}").write_text("x", encoding="utf-8")
        expired = handler.getFilesToDelete()
        assert len(expired) == 10
        # 删除的是最旧的 10 个(第 31~40 天前);第 30 天前的应保留
        oldest = now - timedelta(days=40)
        assert any(Path(p).name.endswith(oldest.strftime("%Y-%m-%d")) for p in expired)
        kept_boundary = now - timedelta(days=30)
        assert not any(Path(p).name.endswith(kept_boundary.strftime("%Y-%m-%d")) for p in expired)
    finally:
        handler.close()


def test_unwritable_log_dir_degrades_without_raising(tmp_path):
    """目录不可用只告警降级为 stdout,不阻断启动(与备份目录不同,日志是辅助能力)。"""
    blocked = tmp_path / "blocked"
    blocked.write_text("占用为文件,无法作为目录创建", encoding="utf-8")
    assert setup_file_logging(_settings(blocked)) is False
