"""第四阶段第三批:备份/恢复工具的失败边界与安全校验(不依赖真实 Docker)。"""

import json
from pathlib import Path

import pytest

from app.ops import backup as backup_module
from app.ops import restore as restore_module
from app.ops.backup import run_backup
from app.ops.common import OpsCancelled, OpsError
from app.ops.restore import run_restore


class FakeStack:
    def __init__(self, *, fail_restore=False, cancel_restore=False):
        self.created: list[str] = []
        self.dropped: list[str] = []
        self.fail_restore = fail_restore
        self.cancel_restore = cancel_restore
        self.copied = None

    def psql_json(self, sql, database=None):
        text = sql.lower()
        if "current_database" in text:
            return {
                "database": "src_db",
                "server_version": "16.10",
                "alembic_version": "abc123",
                "vector_version": "0.8.7",
            }
        if "server_version" in text:
            return {"server_version": "16.10"}
        if "users_by_role" in text:
            return {
                "users_by_role": {"admin": 1},
                "cases_by_status": {"approved": 1},
                "sops_by_status": {"published": 1},
                "faults_by_status": {"resolved": 1},
            }
        if "json_agg" in text:
            return {"cases": [], "sops": [], "audit": [], "feedback": []}
        return {
            "users": 1,
            "categories": 8,
            "faults": 1,
            "fault_records": 1,
            "cases": 1,
            "sops": 1,
            "sop_feedback": 0,
            "audit_events": 3,
            "ai_analyses": 0,
            "sop_ai_generation_runs": 0,
            "embedding_index_state": 0,
            "embedding_index_units": 0,
            "demo_seed_records": 0,
        }

    def pg_dump_to_container(self, path):
        self.container_path = path
        return path

    def pg_restore_list(self, path):
        return "archive listing"

    def copy_from_container(self, path, host_path: Path):
        host_path.write_bytes(b"fake-pg-dump-archive")
        self.copied = host_path

    def copy_to_container(self, host_path: Path, path):
        self.copied = host_path

    def remove_container_file(self, path):
        return {"removed": True, "path": path}

    def pg_tool_version(self, tool):
        return f"{tool} (PostgreSQL) 16.10"

    def db_service_config(self):
        return {"user": "opssop", "database": "src_db"}

    def database_exists(self, name):
        return False

    def create_database(self, name):
        if name in self.created:
            raise OpsError("目标数据库已存在", stage="restore")
        self.created.append(name)

    def pg_restore_to_database(self, path, target_db):
        if self.cancel_restore:
            raise KeyboardInterrupt("synthetic restore cancel")
        if self.fail_restore:
            raise OpsError("synthetic pg_restore failure", stage="restore")

    def drop_database_exact(self, name):
        self.dropped.append(name)
        return {"dropped": True, "database": name}


@pytest.fixture(autouse=True)
def _no_docker(monkeypatch):
    monkeypatch.setattr(backup_module, "compose_version", lambda: "Docker Compose test")
    monkeypatch.setattr(restore_module, "compose_version", lambda: "Docker Compose test")


def test_backup_writes_archive_and_metadata_and_refuses_overwrite(tmp_path):
    stack = FakeStack()
    result = run_backup(
        stack,
        out_dir=tmp_path,
        run_id="backupabc123",
        archive_name="backup.dump",
    )
    archive = tmp_path / "backup.dump"
    metadata = tmp_path / "backup.json"
    assert result["success"] is True
    assert archive.exists() and metadata.exists()
    meta = json.loads(metadata.read_text(encoding="utf-8"))
    assert meta["format"] == "pg_dump-Fc"
    assert meta["archive"]["sha256"] == result["archive_sha256"]
    assert meta["database"]["alembic_version"] == "abc123"
    assert "src_db" in meta["database"]["name"]
    # 不泄露口令/连接串
    rendered = metadata.read_text(encoding="utf-8")
    assert "password" not in rendered.lower()

    with pytest.raises(OpsError, match="拒绝覆盖"):
        run_backup(
            stack,
            out_dir=tmp_path,
            run_id="backupabc124",
            archive_name="backup.dump",
        )


def test_backup_with_space_and_chinese_path(tmp_path):
    out = tmp_path / "备份 目录"
    stack = FakeStack()
    result = run_backup(
        stack,
        out_dir=out,
        run_id="backupabc123",
        archive_name="带空格 的备份.dump",
    )
    assert result["success"] is True
    assert (out / "带空格 的备份.dump").read_bytes() == b"fake-pg-dump-archive"


def test_backup_cancellation_does_not_leave_valid_output(tmp_path):
    class CancelStack(FakeStack):
        def pg_dump_to_container(self, path):
            raise KeyboardInterrupt("synthetic backup cancel")

    with pytest.raises(OpsCancelled):
        run_backup(
            CancelStack(),
            out_dir=tmp_path,
            run_id="backupabc123",
            archive_name="cancel.dump",
        )
    assert not (tmp_path / "cancel.dump").exists()
    assert not (tmp_path / "cancel.json").exists()


def _make_backup(tmp_path):
    stack = FakeStack()
    result = run_backup(
        stack,
        out_dir=tmp_path,
        run_id="restoreabc123",
        archive_name="source.dump",
    )
    return result, tmp_path / "source.dump", tmp_path / "source.json"


def test_restore_requires_new_target_and_valid_archive(tmp_path):
    _result, archive, metadata = _make_backup(tmp_path)

    class Existing(FakeStack):
        def database_exists(self, name):
            return True

    with pytest.raises(OpsError, match="已存在|拒绝覆盖"):
        run_restore(
            Existing(),
            archive_path=archive,
            metadata_path=metadata,
            target_db="opssop_restore_target",
            run_id="restoreabc123",
            out_dir=tmp_path / "out",
        )

    tampered = tmp_path / "tampered.dump"
    tampered.write_bytes(archive.read_bytes() + b"x")
    with pytest.raises(OpsError, match="大小|SHA"):
        run_restore(
            FakeStack(),
            archive_path=tampered,
            metadata_path=metadata,
            target_db="opssop_restore_target",
            run_id="restoreabc124",
            out_dir=tmp_path / "out2",
        )


def test_restore_success_and_failure_policy(tmp_path):
    _result, archive, metadata = _make_backup(tmp_path)
    target = "opssop_restore_ok"
    out = tmp_path / "restore-ok"
    result = run_restore(
        FakeStack(),
        archive_path=archive,
        metadata_path=metadata,
        target_db=target,
        run_id="restoreabc123",
        out_dir=out,
    )
    assert result["success"] is True
    assert result["comparison"]["equal"] is True
    assert result["target_created"] is True
    assert (out / f"{target}-restore.json").exists()

    failing = FakeStack(fail_restore=True)
    with pytest.raises(OpsError, match="synthetic pg_restore failure"):
        run_restore(
            failing,
            archive_path=archive,
            metadata_path=metadata,
            target_db="opssop_restore_fail",
            run_id="restoreabc125",
            out_dir=tmp_path / "restore-fail",
        )
    failed_result = json.loads(
        (tmp_path / "restore-fail" / "opssop_restore_fail-restore.json").read_text(
            encoding="utf-8"
        )
    )
    assert failed_result["success"] is False
    assert failed_result["target_preserved"] is True

    cleanup = FakeStack(fail_restore=True)
    with pytest.raises(OpsError):
        run_restore(
            cleanup,
            archive_path=archive,
            metadata_path=metadata,
            target_db="opssop_restore_cleanup",
            run_id="restoreabc126",
            out_dir=tmp_path / "restore-cleanup",
            cleanup_failed_target=True,
        )
    assert cleanup.dropped == ["opssop_restore_cleanup"]


def test_restore_cancellation_records_and_preserves_target(tmp_path):
    _result, archive, metadata = _make_backup(tmp_path)
    stack = FakeStack(cancel_restore=True)
    with pytest.raises(OpsCancelled):
        run_restore(
            stack,
            archive_path=archive,
            metadata_path=metadata,
            target_db="opssop_restore_cancel",
            run_id="restoreabc127",
            out_dir=tmp_path / "restore-cancel",
        )
    data = json.loads(
        (tmp_path / "restore-cancel" / "opssop_restore_cancel-restore.json").read_text(
            encoding="utf-8"
        )
    )
    assert data["cancelled"] is True
    assert data["target_preserved"] is True
