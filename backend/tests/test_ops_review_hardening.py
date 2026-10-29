"""第四阶段第三批复审修复:环境锁定、启动前归属、清理状态与报告边界。"""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.ops import backup as backup_module
from app.ops import dockerops, drill
from app.ops import restore as restore_module
from app.ops.common import OpsError
from app.ops.dockerops import ComposeStack
from tests.test_ops_backup_restore import FakeStack


@pytest.fixture(autouse=True)
def _no_docker_versions(monkeypatch):
    monkeypatch.setattr(backup_module, "compose_version", lambda: "test compose")
    monkeypatch.setattr(restore_module, "compose_version", lambda: "test compose")
    monkeypatch.setattr(drill, "compose_version", lambda: "test compose")
    monkeypatch.setattr(drill, "code_info", lambda *_a: {})


def _stack(tmp_path: Path, **kwargs) -> ComposeStack:
    env = tmp_path / "drill.env"
    env.write_text(
        "POSTGRES_DB=expected_db\nAI_MODE=disabled\nOPS_ENV_FILE="
        + str(env).replace("\\", "/")
        + "\n",
        encoding="utf-8",
    )
    compose = tmp_path / "compose.yml"
    compose.write_text(
        "services: {}\n",
        encoding="utf-8",
    )
    return ComposeStack(
        tmp_path,
        "opssop-review-hardening",
        env,
        [compose],
        run_id="reviewhard1",
        **kwargs,
    )


def test_controlled_compose_env_removes_host_interpolation_vars(tmp_path, monkeypatch):
    stack = _stack(tmp_path, controlled_env=True)
    stack.compose_files[0].write_text(
        "services:\n  db:\n    image: x\n    environment:\n"
        "      POSTGRES_DB: ${POSTGRES_DB}\n"
        "      AI_MODE: ${AI_MODE}\n"
        "      AI_API_KEY: ${AI_API_KEY}\n"
        "      LOCAL_ONLY: ${LOCAL_ONLY}\n"
        "      OPS_ENV_FILE: ${OPS_ENV_FILE}\n",
        encoding="utf-8",
    )
    for key, value in {
        "POSTGRES_DB": "host_sentinel",
        "AI_MODE": "cloud",
        "AI_API_KEY": "host-sentinel-key",
        "LOCAL_ONLY": "0",
        "OPS_ENV_FILE": "C:/host/other.env",
        "COMPOSE_FILE": "C:/host/other-compose.yml",
        "COMPOSE_PROJECT_NAME": "host-project",
    }.items():
        monkeypatch.setenv(key, value)
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(cmd, 0, "{}", "")

    monkeypatch.setattr(dockerops, "run", fake_run)
    stack.run("config", "--format", "json")
    env = captured["env"]
    assert env is not None
    for key in (
        "POSTGRES_DB",
        "AI_MODE",
        "AI_API_KEY",
        "LOCAL_ONLY",
        "OPS_ENV_FILE",
        "COMPOSE_FILE",
        "COMPOSE_PROJECT_NAME",
    ):
        assert key not in env, key
    assert "PATH" in env


def test_preflight_fresh_rejects_existing_volume_before_up(tmp_path, monkeypatch):
    stack = _stack(tmp_path)
    monkeypatch.setattr(
        stack,
        "config",
        lambda: {
            "services": {},
            "volumes": {"pgdata": {"name": "opssop-review-hardening_pgdata"}},
            "networks": {"default": {"name": "opssop-review-hardening_default"}},
        },
    )
    calls: list[list[str]] = []

    def fake_docker(args, **kwargs):
        calls.append([str(x) for x in args])
        if args[:1] == ["ps"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:2] == ["volume", "inspect"] and args[2].endswith("_pgdata"):
            return subprocess.CompletedProcess(args, 0, "{}", "")
        return subprocess.CompletedProcess(
            args, 1, "", "Error: no such volume or network"
        )

    monkeypatch.setattr(dockerops, "docker", fake_docker)
    monkeypatch.setattr(
        stack,
        "up",
        lambda *_a, **_k: pytest.fail("启动前拒绝时不能调用 up"),
    )
    with pytest.raises(OpsError, match="已存在的volume|拒绝"):
        stack.preflight_fresh()
    assert not any(call[:2] == ["rm", "-f"] for call in calls)


def test_preflight_fresh_rejects_existing_project_container(tmp_path, monkeypatch):
    stack = _stack(tmp_path)
    monkeypatch.setattr(
        stack,
        "config",
        lambda: {"services": {}, "volumes": {}, "networks": {}},
    )

    def fake_docker(args, **kwargs):
        if args[:1] == ["ps"]:
            return subprocess.CompletedProcess(args, 0, "container-id\n", "")
        return subprocess.CompletedProcess(args, 1, "", "no such")

    monkeypatch.setattr(dockerops, "docker", fake_docker)
    with pytest.raises(OpsError, match="已存在容器|拒绝"):
        stack.preflight_fresh()


def test_cleanup_refuses_foreign_volume_with_same_run_id(tmp_path, monkeypatch):
    stack = _stack(tmp_path)
    monkeypatch.setattr(stack, "volume_name", lambda: "foreign-mounted-volume")
    monkeypatch.setattr(stack, "network_name", lambda: "own-network")
    monkeypatch.setattr(stack, "run", lambda *_a, **_k: "")
    monkeypatch.setattr(
        stack,
        "network_labels",
        lambda *_a: {
            "com.opssop.run_id": stack.run_id,
            "com.docker.compose.project": stack.project,
            "com.opssop.purpose": "drill",
        },
    )
    monkeypatch.setattr(
        stack,
        "volume_labels",
        lambda *_a: {
            "com.opssop.run_id": stack.run_id,
            "com.docker.compose.project": "unrelated-project",
            "com.opssop.purpose": "unrelated",
        },
    )
    removals: list[list[str]] = []
    monkeypatch.setattr(
        dockerops,
        "docker",
        lambda args, **_k: removals.append([str(x) for x in args])
        or subprocess.CompletedProcess(args, 0, "", ""),
    )
    stack.manifest = {
        "project": stack.project,
        "run_id": stack.run_id,
        "containers": [],
        "volumes": [{"key": "pgdata", "name": "foreign-mounted-volume"}],
        "networks": [{"key": "default", "name": "own-network"}],
    }
    result = stack.cleanup()
    assert ["volume", "rm", "foreign-mounted-volume"] not in removals
    assert result["volume"]["status"] == "refused"
    assert result["failed"] is True


def test_cleanup_network_remove_nonzero_is_not_reported_removed(tmp_path, monkeypatch):
    stack = _stack(tmp_path)
    monkeypatch.setattr(stack, "volume_name", lambda: "review-volume")
    monkeypatch.setattr(stack, "network_name", lambda: "review-network")
    monkeypatch.setattr(stack, "run", lambda *_a, **_k: "")
    monkeypatch.setattr(
        stack,
        "network_labels",
        lambda *_a: {
            "com.opssop.run_id": stack.run_id,
            "com.docker.compose.project": stack.project,
            "com.opssop.purpose": "drill",
        },
    )
    monkeypatch.setattr(
        stack,
        "volume_labels",
        lambda *_a: {
            "com.opssop.run_id": stack.run_id,
            "com.docker.compose.project": stack.project,
            "com.opssop.purpose": "drill",
        },
    )

    def command(args, **_kwargs):
        return subprocess.CompletedProcess(
            args,
            1 if args[:2] == ["network", "rm"] else 0,
            "",
            "network has active endpoints",
        )

    monkeypatch.setattr(dockerops, "docker", command)
    stack.manifest = {
        "project": stack.project,
        "run_id": stack.run_id,
        "containers": [],
        "volumes": [{"key": "pgdata", "name": "review-volume"}],
        "networks": [{"key": "default", "name": "review-network"}],
    }
    result = stack.cleanup()
    assert result["network"]["removed"] is False
    assert result["network"]["status"] == "failed"
    assert result["failed"] is True


def test_cleanup_without_manifest_never_adopts_existing_resources(tmp_path, monkeypatch):
    """预检拒绝时没有本轮清单:同名项目/相同标签不足以授权删除。"""
    stack = _stack(tmp_path)
    calls: list[object] = []
    monkeypatch.setattr(
        dockerops,
        "docker",
        lambda args, **_k: calls.append(args)
        or subprocess.CompletedProcess(args, 0, "deadbeef\n", ""),
    )
    monkeypatch.setattr(stack, "run", lambda *_a, **_k: "deadbeef\n")
    monkeypatch.setattr(stack, "volume_name", lambda: "previous-run-pgdata")
    monkeypatch.setattr(stack, "network_name", lambda: "previous-run-network")
    result = stack.cleanup()
    assert calls == [], calls
    assert result["status"] == "retained"
    assert result["reason"] == "never_created"
    assert result["failed"] is False


def test_cleanup_after_failed_resource_record_keeps_scene(tmp_path, monkeypatch):
    """up 已尝试但清单记录失败:不清删,标明状态未知。"""
    stack = _stack(tmp_path)
    calls: list[object] = []
    monkeypatch.setattr(
        dockerops,
        "docker",
        lambda args, **_k: calls.append(args)
        or subprocess.CompletedProcess(args, 0, "", ""),
    )
    stack.creation_attempted = True
    result = stack.cleanup()
    assert calls == [], calls
    assert result["status"] == "retained"
    assert result["reason"] == "creation_unknown"


def test_registered_b_frontend_without_created_id_is_not_removed(tmp_path, monkeypatch):
    """名称先登记、容器未创建:失败清理不得按名称删除旧同名容器。"""
    state = drill.DrillState(
        result={"cleanup": {}},
        config=drill.DrillConfig(
            repo_root=tmp_path,
            work_dir=tmp_path / "work",
            run_id="reviewhard1",
            cleanup_on_failure=True,
        ),
        b_frontend_name="opssop-drill-reviewhard1-b-frontend",
        b_frontend="",
    )
    calls: list[object] = []
    monkeypatch.setattr(drill, "docker_cmd", lambda args, **_k: calls.append(args))
    drill._cleanup_after_run(state, success=False)
    assert calls == [], calls


def test_partial_up_with_resource_record_error_is_not_nothing_to_clean(tmp_path):
    """开始启动但清单记录失败:保留现场并标未知,不宣告无资源。"""
    state = drill.DrillState(
        result={"cleanup": {}, "resource_record_errors": ["source: OpsError"]},
        config=drill.DrillConfig(
            repo_root=tmp_path, work_dir=tmp_path / "work", run_id="reviewhard1"
        ),
    )
    drill._cleanup_after_run(state, success=False)
    assert state.result["cleanup"]["status"] == "retained"
    assert state.result["cleanup"]["reason"] == "resource_state_unknown"


def test_cleanup_continues_when_network_already_removed(tmp_path, monkeypatch):
    """网络被外部删除时应报 not_found 并继续删卷,而不是整体抛错留下卷。"""
    stack = _stack(tmp_path)
    stack.manifest = {
        "project": stack.project,
        "run_id": stack.run_id,
        "containers": [],
        "volumes": [{"key": "pgdata", "name": "review-volume"}],
        "networks": [{"key": "default", "name": "review-network"}],
    }

    def missing_network(_name):
        raise OpsError("Error response from daemon: network not found", stage="docker")

    monkeypatch.setattr(stack, "network_labels", missing_network)
    monkeypatch.setattr(
        stack,
        "volume_labels",
        lambda _n: {
            "com.docker.compose.project": stack.project,
            "com.opssop.run_id": stack.run_id,
            "com.opssop.purpose": "drill",
        },
    )
    calls: list[object] = []
    monkeypatch.setattr(
        dockerops,
        "docker",
        lambda args, **_k: calls.append(args)
        or subprocess.CompletedProcess(args, 0, "", ""),
    )
    result = stack.cleanup()
    assert result["network"]["status"] == "not_found"
    assert result["volume"]["status"] == "removed"
    assert ["volume", "rm", "review-volume"] in calls


@pytest.mark.parametrize("resource", ["containers", "network", "volume"])
def test_drill_nested_cleanup_refusal_is_failed(tmp_path, resource):
    refused = {"containers": [], "network": None, "volume": None}
    item = {"removed": False, "reason": "run_id_mismatch"}
    refused[resource] = [item] if resource == "containers" else item
    state = drill.DrillState(
        result={"cleanup": {}},
        config=drill.DrillConfig(
            repo_root=tmp_path,
            work_dir=tmp_path / "work",
            run_id="reviewhard1",
        ),
    )
    state.source_stack = SimpleNamespace(cleanup=lambda **_k: refused)
    drill._cleanup_after_run(state, success=True)
    assert state.result["cleanup"]["failed"] is True
    assert state.result["cleanup"]["status"] == "failed"


def _metadata_without_snapshot(tmp_path: Path):
    result = backup_module.run_backup(
        FakeStack(),
        out_dir=tmp_path,
        run_id="reviewhard1",
        archive_name="source.dump",
    )
    metadata = tmp_path / "source.json"
    data = json.loads(metadata.read_text(encoding="utf-8"))
    return result, tmp_path / "source.dump", metadata, data


@pytest.mark.parametrize(
    "mutate",
    [
        lambda data: data.pop("source_snapshot"),
        lambda data: data.__setitem__("source_snapshot", {}),
        lambda data: data["source_snapshot"].__setitem__("counts", {"users": "1"}),
        lambda data: data["source_snapshot"].pop("business_digest"),
        lambda data: data["source_snapshot"]["statuses"].pop("users_by_role"),
        lambda data: data["source_snapshot"]["statuses"]["users_by_role"].__setitem__(
            "admin", "not-an-integer"
        ),
        lambda data: data["source_snapshot"].pop("vector_version"),
        lambda data: data["archive"].__setitem__("bytes", True),
        lambda data: data["source_snapshot"].__setitem__(
            "table_order", ["users"]
        ),
    ],
)
def test_restore_rejects_bad_snapshot_before_creating_target(tmp_path, mutate):
    _result, archive, metadata, data = _metadata_without_snapshot(tmp_path)
    mutate(data)
    metadata.write_text(json.dumps(data), encoding="utf-8")
    stack = FakeStack()
    with pytest.raises(OpsError, match="元信息预检失败|source_snapshot"):
        restore_module.run_restore(
            stack,
            archive_path=archive,
            metadata_path=metadata,
            target_db="opssop_review_restore_badmeta",
            run_id="reviewhard1",
            out_dir=tmp_path / "restore-badmeta",
        )
    assert stack.created == []
    assert stack.copied is None


def test_backup_temp_cleanup_failure_is_not_success(tmp_path):
    class CleanupFailure(FakeStack):
        def remove_container_file(self, path):
            return {"removed": False, "path": path, "error": "denied"}

    result = backup_module.run_backup(
        CleanupFailure(),
        out_dir=tmp_path,
        run_id="reviewhard1",
        archive_name="cleanup.dump",
    )
    assert result["success"] is False
    assert result["core_success"] is True
    assert result["status"] == "cleanup_incomplete"


def test_restore_temp_cleanup_failure_is_not_success(tmp_path):
    _result, archive, metadata, _data = _metadata_without_snapshot(tmp_path)

    class CleanupFailure(FakeStack):
        def remove_container_file(self, path):
            return {"removed": False, "path": path, "error": "denied"}

    result = restore_module.run_restore(
        CleanupFailure(),
        archive_path=archive,
        metadata_path=metadata,
        target_db="opssop_review_restore_cleanup",
        run_id="reviewhard1",
        out_dir=tmp_path / "restore-cleanup",
    )
    assert result["success"] is False
    assert result["core_success"] is True
    assert result["status"] == "cleanup_incomplete"


def _locked_config_fixture(tmp_path: Path):
    repo = tmp_path / "repo"
    for rel in ("backend/app", "backend/alembic", "backend/scripts", "frontend/dist"):
        (repo / rel).mkdir(parents=True, exist_ok=True)
    (repo / "backend" / "alembic.ini").write_text("", encoding="utf-8")
    (repo / "frontend" / "nginx.conf").write_text("", encoding="utf-8")
    env = tmp_path / "source.env"
    env.write_text("OPS_ENV_FILE=" + str(env), encoding="utf-8")
    backend_env = {
        "POSTGRES_DB": "drill_db",
        "POSTGRES_USER": "opssop",
        "POSTGRES_PASSWORD": "synthetic-password",
        "SECRET_KEY": "synthetic-secret",
        "POSTGRES_HOST": "db",
        "OPSSOP_ENV": "development",
        "OPSSOP_SEED_DEMO": "0",
        "AI_MODE": "disabled",
        "AI_BASE_URL": "",
        "AI_API_KEY": "",
        "AI_MODEL": "",
        "EMBEDDING_MODE": "disabled",
        "EMBEDDING_BASE_URL": "",
        "EMBEDDING_API_KEY": "",
        "EMBEDDING_MODEL": "",
        "LOCAL_ONLY": "1",
        "OPS_ENV_FILE": str(env),
    }
    cfg = {
        "services": {
            "db": {
                "environment": {
                    "POSTGRES_DB": "drill_db",
                    "POSTGRES_USER": "opssop",
                    "POSTGRES_PASSWORD": "synthetic-password",
                },
                "ports": [],
                "volumes": [
                    {
                        "type": "volume",
                        "source": "pgdata",
                        "target": "/var/lib/postgresql/data",
                    }
                ],
                "networks": {"default": None},
            },
            "backend": {
                "environment": backend_env,
                "ports": [],
                "volumes": [
                    {
                        "type": "bind",
                        "source": str(repo / "backend" / rel),
                        "target": target,
                    }
                    for target, rel in (
                        ("/srv/opssop/app", "app"),
                        ("/srv/opssop/alembic", "alembic"),
                        ("/srv/opssop/scripts", "scripts"),
                        ("/srv/opssop/alembic.ini", "alembic.ini"),
                    )
                ],
                "networks": {"default": None},
            },
            "frontend": {
                "ports": [
                    {
                        "host_ip": "127.0.0.1",
                        "published": "18081",
                        "target": 80,
                    }
                ],
                "volumes": [
                    {
                        "type": "bind",
                        "source": str(repo / "frontend" / rel),
                        "target": target,
                    }
                    for target, rel in (
                        ("/usr/share/nginx/html", "dist"),
                        ("/etc/nginx/conf.d/default.conf", "nginx.conf"),
                    )
                ],
                "networks": {"default": None},
            },
        },
        "networks": {"default": {"internal": False}},
    }
    kwargs = dict(
        repo_root=repo,
        project="opssop-drill-reviewhard1",
        env_file=env,
        expected_db="drill_db",
        expected_user="opssop",
        expected_password="synthetic-password",
        expected_secret="synthetic-secret",
        frontend_port=18081,
        opssop_env="development",
        expected_backend_targets={
            "/srv/opssop/app": repo / "backend" / "app",
            "/srv/opssop/alembic": repo / "backend" / "alembic",
            "/srv/opssop/scripts": repo / "backend" / "scripts",
            "/srv/opssop/alembic.ini": repo / "backend" / "alembic.ini",
        },
        expected_frontend_targets={
            "/usr/share/nginx/html": repo / "frontend" / "dist",
            "/etc/nginx/conf.d/default.conf": repo / "frontend" / "nginx.conf",
        },
    )
    return cfg, kwargs


def test_locked_config_rejects_leaked_model_or_port_settings(tmp_path):
    cfg, kwargs = _locked_config_fixture(tmp_path)
    evidence = drill._assert_locked_config(cfg, **kwargs)
    assert evidence["config_verified"] is True
    assert evidence["env"]["AI_API_KEY_empty"] is True

    leaked = json.loads(json.dumps(cfg))
    leaked["services"]["backend"]["environment"]["AI_MODE"] = "cloud"
    with pytest.raises(OpsError, match="AI_MODE"):
        drill._assert_locked_config(leaked, **kwargs)

    leaked_port = json.loads(json.dumps(cfg))
    leaked_port["services"]["frontend"]["ports"][0]["host_ip"] = "0.0.0.0"
    with pytest.raises(OpsError, match="127.0.0.1"):
        drill._assert_locked_config(leaked_port, **kwargs)


def _load_ops_cli():
    path = Path(__file__).resolve().parents[1] / "scripts" / "ops_cli.py"
    spec = importlib.util.spec_from_file_location("ops_cli_review_fix", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_cli_backup_returns_nonzero_when_cleanup_incomplete(tmp_path, monkeypatch):
    cli = _load_ops_cli()
    monkeypatch.setattr(cli, "_stack", lambda _args: object())
    monkeypatch.setattr(
        cli,
        "run_backup",
        lambda *_a, **_k: {
            "success": False,
            "status": "cleanup_incomplete",
            "core_success": True,
            "cleanup_complete": False,
            "archive_name": "x.dump",
            "archive_bytes": 1,
            "archive_sha256": "0" * 64,
            "container_temp_cleanup": {"removed": False},
        },
    )
    args = SimpleNamespace(
        out_dir=str(tmp_path),
        run_id="reviewhard1",
        name="",
        repo_root=str(Path(__file__).resolve().parents[2]),
        image="",
    )
    assert cli._backup(args) == 4
    saved = json.loads((tmp_path / "backup-result.json").read_text(encoding="utf-8"))
    assert saved["success"] is False
    assert saved["status"] == "cleanup_incomplete"


def test_cli_restore_returns_nonzero_when_cleanup_incomplete(tmp_path, monkeypatch):
    cli = _load_ops_cli()
    monkeypatch.setattr(cli, "_stack", lambda _args: object())
    monkeypatch.setattr(
        cli,
        "run_restore",
        lambda *_a, **_k: {
            "success": False,
            "status": "cleanup_incomplete",
            "core_success": True,
            "cleanup_complete": False,
            "container_temp_cleanup": {"removed": False},
        },
    )
    args = SimpleNamespace(
        archive="x.dump",
        metadata="x.json",
        target_db="opssop_review_restore_cli",
        run_id="reviewhard1",
        out_dir=str(tmp_path),
        cleanup_failed_target=False,
        allow_version_mismatch=False,
        repo_root=str(Path(__file__).resolve().parents[2]),
        image="",
    )
    assert cli._restore(args) == 4


def _drill_config(tmp_path: Path) -> drill.DrillConfig:
    repo = tmp_path / "repo"
    (repo / "backend").mkdir(parents=True)
    (repo / "frontend" / "dist").mkdir(parents=True)
    (repo / "frontend" / "dist" / "index.html").write_text("ok", encoding="utf-8")
    for name in ("docker-compose.yml", "docker-compose.drill.yml"):
        (repo / name).write_text("services: {}\n", encoding="utf-8")
    return drill.DrillConfig(
        repo_root=repo,
        work_dir=tmp_path / "work",
        run_id="reviewhard1",
        skip_frontend_build=True,
    )


def test_prepare_failure_writes_partial_report_and_does_not_start(tmp_path, monkeypatch):
    config = _drill_config(tmp_path)

    def unavailable(*_args):
        raise OpsError("independent unavailable dependency image", stage="docker")

    monkeypatch.setattr(drill, "image_info", unavailable)
    monkeypatch.setattr(
        ComposeStack,
        "up",
        lambda *_a, **_k: pytest.fail("镜像预检失败时不能启动 Compose"),
    )
    with pytest.raises(OpsError):
        drill.run_drill(config)
    result = json.loads((config.work_dir / "result.json").read_text(encoding="utf-8"))
    steps = {step["name"]: step for step in result["steps"]}
    assert result["success"] is False
    assert steps["baseline"]["status"] == "failed"
    assert steps["source_up"]["status"] == "not_run"
    assert (config.work_dir / "summary.md").exists()
    assert result["cleanup"]["status"] == "nothing_to_clean"


def test_preflight_rejection_prevents_compose_up(tmp_path, monkeypatch):
    config = _drill_config(tmp_path)
    monkeypatch.setattr(drill, "image_info", lambda image: {"image": image})
    up_calls: list[str] = []

    def reject(self):
        raise OpsError("existing foreign volume", stage="ownership")

    monkeypatch.setattr(ComposeStack, "preflight_fresh", reject)
    monkeypatch.setattr(
        ComposeStack, "up", lambda self, *services: up_calls.append(self.project)
    )
    with pytest.raises(OpsError):
        drill.run_drill(config)
    assert up_calls == []
    result = json.loads((config.work_dir / "result.json").read_text(encoding="utf-8"))
    steps = {step["name"]: step for step in result["steps"]}
    assert steps["baseline"]["status"] == "failed"
    assert steps["source_up"]["status"] == "not_run"
    assert result["cleanup"]["status"] == "nothing_to_clean"
    # 失败收尾实际执行了清理,报告里的 cleanup 步骤不能停留在 not_run
    assert steps["cleanup"]["status"] == "success"
