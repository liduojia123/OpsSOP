"""备份、恢复、持久化与生产配置的隔离演练编排。

一次完整演练:
- 新建 Compose 项目/命名卷/源库 A,正式 Alembic 迁移;
- 全部业务夹具通过真实 HTTP API 创建;
- pg_dump -Fc 备份 A,恢复到全新库 B;
- 启动指向 B 的后端/nginx,真实 HTTP 登录、检索、权限、续办;
- 命名卷正常重启 + 删除 DB 容器后同卷重建,验证数据仍在;
- 生产配置正反例与 nginx 同源代理 smoke;
- 记录资源归属和清理/保留状态。
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from .backup import run_backup
from .common import (
    OpsCancelled,
    OpsError,
    atomic_write_json,
    atomic_write_text,
    code_info,
    docker as docker_cmd,
    free_tcp_port,
    now_iso,
    redact_text,
    safe_secret,
    validate_run_id,
)
from .dockerops import (
    ComposeStack,
    cleanup_failed,
    compose_version,
    image_info,
)
from .fixtures import DrillFixtures, seed_drill_data
from .httpclient import HttpClient
from .restore import run_restore
from .snapshot import compare_snapshots, take_snapshot

DEFAULT_BACKEND_IMAGE = "opssop-backend:1.1.2"
DB_IMAGE = "pgvector/pgvector:pg16"
NGINX_IMAGE = "nginx:1.27-alpine"

STEP_NAMES = [
    "baseline",
    "frontend_build",
    "source_up",
    "fixtures",
    "preflight_checks",
    "backup",
    "restore",
    "restored_app_http",
    "restored_app_write",
    "source_unchanged",
    "volume_restart",
    "volume_recreate",
    "production_config",
    "production_positive",
    "production_negative_weak_password",
    "production_negative_demo_flag",
    "production_negative_legacy_account",
    "production_negative_migration_failure",
    "cleanup",
    "report",
]


@dataclass
class DrillConfig:
    repo_root: Path
    work_dir: Path
    run_id: str
    backend_image: str = DEFAULT_BACKEND_IMAGE
    timeout: int = 180
    keep_resources: bool = False
    cleanup_on_failure: bool = False
    skip_frontend_build: bool = False


@dataclass
class DrillState:
    result: dict
    config: DrillConfig
    source_stack: ComposeStack | None = None
    prod_stack: ComposeStack | None = None
    fixtures: DrillFixtures | None = None
    b_backend: str = ""
    b_backend_name: str = ""
    b_frontend: str = ""
    b_frontend_name: str = ""
    source_snapshot: dict | None = None
    target_db: str = ""
    volume_name: str = ""
    network_name: str = ""
    b_port: int = 0
    prod_port: int = 0
    negative_databases: list[str] = field(default_factory=list)
    negative_role: str = ""


def _step(result: dict, name: str, status: str, **detail) -> dict:
    for entry in result["steps"]:
        if entry["name"] == name:
            entry["status"] = status
            entry["updated_at"] = now_iso()
            entry.update(detail)
            return entry
    entry = {"name": name, "status": status, "updated_at": now_iso(), **detail}
    result["steps"].append(entry)
    return entry


def _write_env(path: Path, values: dict[str, str]) -> Path:
    lines = ["# generated synthetic drill environment; do not commit"]
    for key, value in values.items():
        lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def _port_url(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def _wait_health(url: str, timeout: int) -> None:
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        try:
            client = HttpClient(url, timeout=5)
            resp = client.get("/api/system/health")
            if resp.status == 200 and (resp.body or {}).get("status") == "ok":
                return
            last = f"status={resp.status}"
        except Exception as exc:  # noqa: BLE001
            last = type(exc).__name__
        time.sleep(1.5)
    raise OpsError(f"等待 {url} 健康检查超时({last})", stage="http")


def _http_json(client: HttpClient, path: str) -> dict:
    return client.get(path).body or {}


def _assert_fixture_permissions(base_url: str, fx: DrillFixtures) -> dict:
    """真实 HTTP:登录、查询、权限过滤、正式检索范围。"""
    op1 = HttpClient(base_url)
    op1.login(fx.users["operator1"]["username"], fx.passwords["operator1"])
    op2 = HttpClient(base_url)
    op2.login(fx.users["operator2"]["username"], fx.passwords["operator2"])
    reviewer = HttpClient(base_url)
    reviewer.login(fx.users["reviewer"]["username"], fx.passwords["reviewer"])

    faults = _http_json(op1, "/api/faults")
    if fx.fault_id not in {row["id"] for row in faults}:
        raise OpsError("恢复库 HTTP 未看到演练故障", stage="http")
    cases = _http_json(op1, "/api/cases")
    case_ids = {row["id"] for row in cases}
    if fx.case_id not in case_ids:
        raise OpsError("恢复库 HTTP 未看到已通过案例", stage="http")
    sops = _http_json(op1, "/api/sops")
    sop_ids = {row["id"] for row in sops}
    if not {fx.sop_v2_id, fx.draft_sop_id} <= sop_ids:
        raise OpsError("恢复库 HTTP 未看到已发布 SOP/本人草稿", stage="http")

    forbidden = [
        ("case", fx.other_case_id),
        ("sop", fx.other_draft_sop_id),
        ("fault", fx.other_fault_id),
    ]
    for kind, ident in forbidden:
        path = {"case": f"/api/cases/{ident}", "sop": f"/api/sops/{ident}", "fault": f"/api/faults/{ident}"}[kind]
        resp = op1.raw("GET", path)
        if resp.status != 403:
            raise OpsError(
                f"越权对照未返回 403: {path} -> {resp.status}", stage="http"
            )
    for path in (f"/api/sops/{fx.draft_sop_id}", f"/api/faults/{fx.fault_id}"):
        resp = op2.raw("GET", path)
        if resp.status != 403:
            raise OpsError(f"其他运维越权未返回 403: {path} -> {resp.status}", stage="http")
    # 其他运维自己的待审案例/草稿应可见,证明权限不是“一律拒绝”
    for path in (f"/api/cases/{fx.other_case_id}", f"/api/sops/{fx.other_draft_sop_id}"):
        resp = op2.raw("GET", path)
        if resp.status != 200:
            raise OpsError(f"其他运维无法查看自己的材料: {path} -> {resp.status}", stage="http")

    audit = _http_json(reviewer, "/api/audit?limit=500")
    actions = {row["action"] for row in audit}
    for expected in ("fault.resolve", "case.approve", "sop.publish"):
        if expected not in actions:
            raise OpsError(f"恢复库审计缺少 {expected}", stage="http")

    search = _http_json(
        op1, f"/api/search?q={fx.marker.replace(' ', '%20')}"
    )
    hits = search.get("hits") or []
    hit_ids = {(h.get("source_type"), h.get("source_id")) for h in hits}
    if ("case", fx.case_id) not in hit_ids or ("sop", fx.sop_v2_id) not in hit_ids:
        raise OpsError("恢复库正式检索未返回已审核案例/有效发布 SOP", stage="http")
    for forbidden_id in (fx.draft_sop_id, fx.other_draft_sop_id):
        if ("sop", forbidden_id) in hit_ids:
            raise OpsError("恢复库正式检索返回了未发布 SOP", stage="http")
    if ("case", fx.other_case_id) in hit_ids:
        raise OpsError("恢复库正式检索返回了他人待审案例", stage="http")

    workbench = _http_json(op1, "/api/workbench")
    if int(workbench.get("published_sop_count") or 0) < 1:
        raise OpsError("恢复库工作台未统计到有效 SOP", stage="http")

    return {
        "login": True,
        "faults": len(faults),
        "cases": len(cases),
        "sops": len(sops),
        "audit_events": len(audit),
        "search_hits": len(hits),
        "forbidden_checks": 7,
        "published_sop_count": workbench.get("published_sop_count"),
    }


def _http_write_business(base_url: str, fx: DrillFixtures, run_id: str, *, prefix: str) -> dict:
    op1 = HttpClient(base_url)
    op1.login(fx.users["operator1"]["username"], fx.passwords["operator1"])
    reviewer = HttpClient(base_url)
    reviewer.login(fx.users["reviewer"]["username"], fx.passwords["reviewer"])
    title = f"[{fx.marker}] {prefix}-{run_id}"
    fault = op1.post(
        "/api/faults",
        {
            "title": title,
            "description": f"{prefix} 续办写入",
            "environment": "drill",
            "service": "checkout",
            "component_version": "1.0",
            "error_logs": "演练续办",
            "category_id": fx.category_id,
            "tags": [fx.marker, prefix],
            "status": "open",
        },
        expected=(201,),
    ).body
    record = op1.post(
        f"/api/faults/{fault['id']}/records",
        {"kind": "check_result", "content": f"{prefix} 续办检查记录"},
        expected=(201,),
    ).body
    op1.post(
        f"/api/faults/{fault['id']}/resolve",
        {
            "actual_root_cause": f"{prefix} 演练续办根因",
            "handling_steps": f"{prefix} 演练续办处理",
            "recovery_verification": f"{prefix} 演练续办恢复验证",
        },
        expected=(200,),
    )
    case = op1.get(f"/api/cases?q={prefix}-{run_id}&status=pending_review").body or []
    if len(case) != 1:
        raise OpsError(f"{prefix}: 续办案例定位失败", stage="http")
    reviewer.post(
        f"/api/cases/{case[0]['id']}/approve",
        {"comment": f"{prefix} 续办审核"},
        expected=(200,),
    )
    return {
        "fault_id": fault["id"],
        "record_id": record["id"],
        "case_id": case[0]["id"],
        "title": title,
    }


def _build_frontend(repo_root: Path) -> dict:
    frontend = repo_root / "frontend"
    npm = "npm.cmd" if os.name == "nt" else "npm"
    proc = subprocess.run(
        [npm, "run", "build"],
        cwd=str(frontend),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=600,
    )
    if proc.returncode != 0:
        raise OpsError(
            f"前端生产构建失败: {redact_text(proc.stderr or proc.stdout)[:500]}",
            stage="frontend",
        )
    index = frontend / "dist" / "index.html"
    if not index.exists():
        raise OpsError("前端构建后缺少 dist/index.html", stage="frontend")
    return {"exit_code": proc.returncode, "dist_index": "frontend/dist/index.html"}


def _container_ports(stack: ComposeStack, service: str) -> dict:
    cid = stack.ps_q(service)
    return stack.container_ports(cid)


def _assert_no_host_port(stack: ComposeStack, service: str) -> dict:
    ports = _container_ports(stack, service)
    published = {
        key: value for key, value in ports.items() if value
    }
    if published:
        raise OpsError(f"{service} 不应发布宿主端口: {published}", stage="ports")
    return {"service": service, "ports": published}


def _assert_frontend_loopback(stack: ComposeStack) -> dict:
    ports = _container_ports(stack, "frontend")
    for key, mappings in ports.items():
        for mapping in mappings or []:
            if mapping.get("HostIp") != "127.0.0.1":
                raise OpsError(
                    f"前端端口未绑定回环: {key} {mapping}", stage="ports"
                )
    return {"service": "frontend", "ports": ports}


def _start_backend_instance(
    *,
    name: str,
    image: str,
    network: str,
    env_file: Path,
    repo_root: Path,
    run_id: str,
) -> str:
    cmd = [
        "run",
        "-d",
        "--name",
        name,
        "--network",
        network,
        "--network-alias",
        "backend",
        "--label",
        "com.opssop.purpose=drill",
        "--label",
        f"com.opssop.run_id={run_id}",
        "--env-file",
        str(env_file),
        "-e",
        "POSTGRES_HOST=db",
        "-e",
        "POSTGRES_PORT=5432",
        "-v",
        f"{repo_root / 'backend' / 'app'}:/srv/opssop/app:ro",
        "-v",
        f"{repo_root / 'backend' / 'alembic'}:/srv/opssop/alembic:ro",
        "-v",
        f"{repo_root / 'backend' / 'scripts'}:/srv/opssop/scripts:ro",
        "-v",
        f"{repo_root / 'backend' / 'alembic.ini'}:/srv/opssop/alembic.ini:ro",
        image,
        "sh",
        "scripts/start.sh",
    ]
    docker_cmd(cmd, timeout=120)
    return docker_cmd(["inspect", "--format", "{{.Id}}", name]).stdout.strip()


def _start_nginx_instance(
    *,
    name: str,
    network: str,
    port: int,
    repo_root: Path,
    run_id: str,
) -> str:
    cmd = [
        "run",
        "-d",
        "--name",
        name,
        "--network",
        network,
        "--label",
        "com.opssop.purpose=drill",
        "--label",
        f"com.opssop.run_id={run_id}",
        "-p",
        f"127.0.0.1:{port}:80",
        "-v",
        f"{repo_root / 'frontend' / 'dist'}:/usr/share/nginx/html:ro",
        "-v",
        f"{repo_root / 'frontend' / 'nginx.conf'}:/etc/nginx/conf.d/default.conf:ro",
        NGINX_IMAGE,
    ]
    docker_cmd(cmd, timeout=120)
    return docker_cmd(["inspect", "--format", "{{.Id}}", name]).stdout.strip()


def _remove_container_if_owned(
    name: str, run_id: str, *, expected_id: str = ""
) -> dict:
    """只清理本轮创建清单中的独立容器;按 ID 和标签双重核对。"""
    proc = docker_cmd(
        ["inspect", "--format", "{{json .}}", name], timeout=60, check=False
    )
    if proc.returncode != 0:
        text = (proc.stderr or proc.stdout or "").lower()
        if "no such" in text or "not found" in text:
            return {
                "container": name,
                "removed": False,
                "status": "not_found",
                "idempotent": True,
            }
        return {
            "container": name,
            "removed": False,
            "status": "failed",
            "error": redact_text(proc.stderr or proc.stdout)[:300],
        }
    try:
        info = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {
            "container": name,
            "removed": False,
            "status": "failed",
            "error": "无法解析容器信息",
        }
    actual_id = info.get("Id")
    labels = (info.get("Config") or {}).get("Labels") or {}
    if expected_id and actual_id != expected_id:
        return {
            "container": name,
            "removed": False,
            "status": "refused",
            "reason": "id_mismatch",
            "expected_id": expected_id,
            "actual_id": actual_id,
        }
    if labels.get("com.opssop.run_id") != run_id:
        return {
            "container": name,
            "removed": False,
            "status": "refused",
            "reason": "run_id_mismatch",
        }
    if labels.get("com.opssop.purpose") != "drill":
        return {
            "container": name,
            "removed": False,
            "status": "refused",
            "reason": "purpose_mismatch",
        }
    remove = docker_cmd(["rm", "-f", actual_id or name], timeout=120, check=False)
    if remove.returncode != 0:
        return {
            "container": name,
            "container_id": actual_id,
            "removed": False,
            "status": "failed",
            "exit_code": remove.returncode,
            "error": redact_text(remove.stderr or remove.stdout)[:300],
        }
    return {
        "container": name,
        "container_id": actual_id,
        "removed": True,
        "status": "removed",
    }


def _oneoff_backend(
    *,
    name: str,
    image: str,
    network: str,
    env_file: Path,
    repo_root: Path,
    run_id: str,
    command: list[str],
) -> dict:
    cmd = [
        "run",
        "--rm",
        "--name",
        name,
        "--network",
        network,
        "--label",
        "com.opssop.purpose=drill",
        "--label",
        f"com.opssop.run_id={run_id}",
        "--env-file",
        str(env_file),
        "-e",
        "POSTGRES_HOST=db",
        "-e",
        "POSTGRES_PORT=5432",
        "-v",
        f"{repo_root / 'backend' / 'app'}:/srv/opssop/app:ro",
        "-v",
        f"{repo_root / 'backend' / 'alembic'}:/srv/opssop/alembic:ro",
        "-v",
        f"{repo_root / 'backend' / 'scripts'}:/srv/opssop/scripts:ro",
        "-v",
        f"{repo_root / 'backend' / 'alembic.ini'}:/srv/opssop/alembic.ini:ro",
        image,
        *command,
    ]
    proc = docker_cmd(cmd, timeout=180, check=False)
    return {
        "exit_code": proc.returncode,
        "stderr": redact_text(proc.stderr or "")[:800],
        "stdout": redact_text(proc.stdout or "")[:800],
    }


def _env_copy(base: dict[str, str], **overrides) -> dict[str, str]:
    out = copy.deepcopy(base)
    out.update({k: str(v) for k, v in overrides.items()})
    return out


def _create_role(stack: ComposeStack, role: str, password: str) -> None:
    # 角色名/口令均来自受控生成值;只在本轮临时集群中使用
    stack.psql(
        f'CREATE ROLE "{role}" LOGIN PASSWORD \'{password}\'',
        database="postgres",
    )


def _drop_role(stack: ComposeStack, role: str) -> dict:
    try:
        stack.psql(f'DROP ROLE IF EXISTS "{role}"', database="postgres")
        return {"role": role, "dropped": True}
    except OpsError as exc:
        return {"role": role, "dropped": False, "error": str(exc)[:300]}


def _db_has_alembic(stack: ComposeStack, database: str) -> bool:
    raw = stack.psql(
        "SELECT to_regclass('public.alembic_version') IS NOT NULL",
        database=database,
    ).strip()
    return raw == "t"


def _finalize_report(result: dict, work_dir: Path) -> None:
    """先定稿 report 步骤,再写 JSON/摘要,保证两个文件看到同一终态。"""
    result["finished_at"] = now_iso()
    _step(result, "report", "success")
    result["finished_at"] = now_iso()
    result["report_files"] = {
        "result": "result.json",
        "summary": "summary.md",
    }
    atomic_write_json(work_dir / "result.json", result)
    atomic_write_text(work_dir / "summary.md", _summary(result))


def _mark_failure(result: dict, message: str) -> None:
    """把当前 running 步骤终结为 failed,并把未执行步骤标为 not_run。"""
    steps = result.get("steps") or []
    current = next((step for step in steps if step.get("status") == "running"), None)
    if current is None:
        current = next(
            (
                step
                for step in steps
                if step.get("status") == "planned" and step.get("name") != "report"
            ),
            None,
        )
    if current is not None and current.get("status") != "failed":
        current["status"] = "failed"
        current["error"] = message[:600]
        current["updated_at"] = now_iso()
    for step in steps:
        if step.get("name") == "report":
            continue
        if step.get("status") == "planned":
            step["status"] = "not_run"
            step["reason"] = "前置步骤失败或取消,未执行"
            step["updated_at"] = now_iso()


def run_drill(config: DrillConfig) -> dict:
    run_id = validate_run_id(config.run_id)
    repo_root = config.repo_root.resolve()
    work_dir = config.work_dir.resolve()
    try:
        rel = work_dir.relative_to(repo_root)
        allowed = rel.parts and rel.parts[0] in ("ops-artifacts", "backups")
        if not allowed:
            raise OpsError(
                "演练产物必须放在仓库外的 work-dir,或仓库内已忽略的 ops-artifacts/ backups/ 下",
                stage="prepare",
            )
    except ValueError:
        pass
    if not work_dir.exists():
        work_dir.mkdir(parents=True)
    if (work_dir / "result.json").exists():
        raise OpsError("工作目录已有 result.json,拒绝覆盖", stage="prepare")

    result: dict = {
        "schema_version": "opssop-drill/1",
        "run_id": run_id,
        "mode": "local-linux-container-source-mount",
        "started_at": now_iso(),
        "finished_at": None,
        "success": False,
        "cancelled": False,
        "error": None,
        "code": None,
        "compose_version": None,
        "images": {},
        "resources": {},
        "preflight": {},
        "steps": [{"name": name, "status": "planned"} for name in STEP_NAMES],
        "fixtures": {},
        "backup": {},
        "restore": {},
        "http": {},
        "persistence": {},
        "production": {},
        "cleanup": {},
        "limitations": [
            "演练使用本地 Linux 容器、已有镜像和当前源码只读挂载;报告不覆盖镜像重建、目标云环境或公网 HTTPS。",
            "本演练不评估真实聊天模型、embedding 效果或人工标签;运行时 AI/embedding 均为 disabled。",
            "本演练不覆盖强制终止进程、断电恢复、自动异地容灾或自动调度。",
            "演练未启用 Docker internal 网络;外发约束来自受控子进程环境、禁用模型和空端点,严格隔离需部署环境限制出口。",
        ],
    }
    state = DrillState(result=result, config=config)

    def fail(exc: Exception) -> None:
        result["success"] = False
        result["error"] = str(exc)[:1200]
        if isinstance(exc, OpsCancelled):
            result["cancelled"] = True
        _mark_failure(result, result["error"])
        try:
            _cleanup_after_run(state, success=False)
        except Exception as cleanup_exc:  # noqa: BLE001
            result["cleanup"] = {
                "status": "failed",
                "failed": True,
                "error": f"{type(cleanup_exc).__name__}: {cleanup_exc}"[:500],
            }
        try:
            _finalize_report(result, work_dir)
        except Exception as write_exc:  # noqa: BLE001
            result["report_write_failed"] = True
            _step(result, "report", "failed", error=str(write_exc)[:300])
            result["error"] = (
                f"{result['error']}; 报告写入失败: {type(write_exc).__name__}"
            )[:2000]
            try:
                atomic_write_json(work_dir / "result.json", result)
            except Exception:  # noqa: BLE001
                pass

    try:
        # 子目录创建也在报告边界内,失败仍写 result.json/summary.md
        (work_dir / "env").mkdir(exist_ok=True)
        (work_dir / "backup").mkdir(exist_ok=True)
        (work_dir / "restore").mkdir(exist_ok=True)
        _step(result, "baseline", "running")
        result["code"] = code_info(repo_root)
        result["compose_version"] = compose_version()

        source_project = f"opssop-drill-{run_id}"
        prod_project = f"opssop-prod-{run_id}"
        source_port = free_tcp_port()
        b_port = free_tcp_port()
        prod_port = free_tcp_port()
        state.b_port = b_port
        state.prod_port = prod_port
        source_db = f"opssop_drill_{run_id}_src"
        target_db = f"opssop_restore_{run_id}"
        state.target_db = target_db
        source_env_values = {
            "POSTGRES_USER": "opssop",
            "POSTGRES_PASSWORD": safe_secret(32),
            "POSTGRES_DB": source_db,
            "POSTGRES_TEST_DB": "opssop_test",
            "SECRET_KEY": safe_secret(40),
            "SESSION_EXPIRE_HOURS": "8",
            "OPSSOP_ENV": "development",
            "OPSSOP_ADMIN_USERNAME": "admin",
            "OPSSOP_ADMIN_PASSWORD": safe_secret(32),
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
            "FRONTEND_PORT": str(source_port),
            "OPS_RUN_ID": run_id,
        }
        source_env = _write_env(work_dir / "env" / "source.env", source_env_values)
        source_env_values["OPS_ENV_FILE"] = str(source_env).replace("\\", "/")
        source_env = _write_env(source_env, source_env_values)
        prod_env_values = _env_copy(
            source_env_values,
            POSTGRES_DB=f"opssop_prod_{run_id}",
            POSTGRES_PASSWORD=safe_secret(32),
            SECRET_KEY=safe_secret(40),
            OPSSOP_ENV="production",
            OPSSOP_ADMIN_PASSWORD=safe_secret(32),
            OPSSOP_SEED_DEMO="0",
            FRONTEND_PORT=str(prod_port),
        )
        prod_env = _write_env(work_dir / "env" / "production.env", prod_env_values)
        prod_env_values["OPS_ENV_FILE"] = str(prod_env).replace("\\", "/")
        prod_env = _write_env(prod_env, prod_env_values)

        source_files = [repo_root / "docker-compose.yml", repo_root / "docker-compose.drill.yml"]
        prod_files = [repo_root / "docker-compose.yml", repo_root / "docker-compose.drill.yml"]
        state.source_stack = ComposeStack(
            repo_root,
            source_project,
            source_env,
            source_files,
            run_id=run_id,
            controlled_env=True,
        )
        state.prod_stack = ComposeStack(
            repo_root,
            prod_project,
            prod_env,
            prod_files,
            run_id=run_id,
            controlled_env=True,
        )
        result["images"]["backend"] = image_info(config.backend_image)
        result["images"]["db"] = image_info(DB_IMAGE)
        result["images"]["nginx"] = image_info(NGINX_IMAGE)
        result["resources"] = {
            "source_project": source_project,
            "source_database": source_db,
            "target_database": target_db,
            "source_frontend_port": source_port,
            "restored_frontend_port": b_port,
            "production_project": prod_project,
            "production_frontend_port": prod_port,
        }
        # 首次 up 前:项目/卷/网络必须全新;最终渲染配置必须与生成值一致。
        result["preflight"]["source_fresh"] = state.source_stack.preflight_fresh()
        result["preflight"]["production_fresh"] = state.prod_stack.preflight_fresh()
        result["preflight"]["source_config"] = _assert_locked_config(
            state.source_stack.config(),
            repo_root=repo_root,
            project=source_project,
            env_file=source_env,
            expected_db=source_db,
            expected_user=source_env_values["POSTGRES_USER"],
            expected_password=source_env_values["POSTGRES_PASSWORD"],
            expected_secret=source_env_values["SECRET_KEY"],
            frontend_port=source_port,
            opssop_env="development",
            expected_backend_targets={
                "/srv/opssop/app": repo_root / "backend" / "app",
                "/srv/opssop/alembic": repo_root / "backend" / "alembic",
                "/srv/opssop/scripts": repo_root / "backend" / "scripts",
                "/srv/opssop/alembic.ini": repo_root / "backend" / "alembic.ini",
            },
            expected_frontend_targets={
                "/usr/share/nginx/html": repo_root / "frontend" / "dist",
                "/etc/nginx/conf.d/default.conf": repo_root / "frontend" / "nginx.conf",
            },
        )
        _step(result, "baseline", "success", code=result["code"], preflight=result["preflight"])

        _step(result, "frontend_build", "running")
        if config.skip_frontend_build:
            if not (repo_root / "frontend" / "dist" / "index.html").exists():
                raise OpsError("跳过构建但 dist/index.html 不存在", stage="frontend")
            result["production"]["frontend_build"] = {"skipped": True}
        else:
            result["production"]["frontend_build"] = _build_frontend(repo_root)
        _step(result, "frontend_build", "success")

        _step(result, "source_up", "running")
        try:
            state.source_stack.up()
        finally:
            # up 部分失败时也记录已创建对象,供失败收尾核对
            try:
                state.source_stack.record_resources()
            except Exception as record_exc:  # noqa: BLE001
                result.setdefault("resource_record_errors", []).append(
                    f"source: {type(record_exc).__name__}"
                )
        _wait_health(_port_url(source_port), config.timeout)
        state.source_stack.ensure_run_labels()
        state.volume_name = state.source_stack.volume_name()
        state.network_name = state.source_stack.network_name()
        result["resources"].update(
            {
                "source_volume": state.volume_name,
                "source_network": state.network_name,
                "source_db_container": state.source_stack.db_container(),
                "source_frontend_url": _port_url(source_port),
                "source_manifest": state.source_stack.manifest,
            }
        )
        result["preflight"].update(
            {
                "db_ports": _assert_no_host_port(state.source_stack, "db"),
                "backend_ports": _assert_no_host_port(state.source_stack, "backend"),
                "frontend_ports": _assert_frontend_loopback(state.source_stack),
            }
        )
        _step(result, "source_up", "success", **result["preflight"])

        _step(result, "fixtures", "running")
        admin_client = HttpClient(_port_url(source_port))
        state.fixtures = seed_drill_data(
            admin_client,
            run_id=run_id,
            admin_username=source_env_values["OPSSOP_ADMIN_USERNAME"],
            admin_password=source_env_values["OPSSOP_ADMIN_PASSWORD"],
        )
        result["fixtures"] = {"api": state.fixtures.public(), "direct_db_writes": "only startup admin/categories seed"}
        _step(result, "fixtures", "success")

        _step(result, "preflight_checks", "running")
        result["http"]["source"] = _assert_fixture_permissions(_port_url(source_port), state.fixtures)
        # 权限/检索 HTTP 检查本身会产生登录审计;必须在这些写入之后再采集备份时点快照
        source_snapshot = take_snapshot(state.source_stack)
        state.source_snapshot = source_snapshot
        result["source_snapshot"] = source_snapshot
        _step(result, "preflight_checks", "success")

        _step(result, "backup", "running")
        backup = run_backup(
            state.source_stack,
            out_dir=work_dir / "backup",
            run_id=run_id,
            archive_name=f"opssop-drill-{run_id}.dump",
            repo_root=repo_root,
            image=config.backend_image,
        )
        result["backup"] = {
            key: backup.get(key)
            for key in (
                "success",
                "status",
                "core_success",
                "cleanup_complete",
                "archive_name",
                "archive_bytes",
                "archive_sha256",
                "alembic_version",
                "vector_version",
                "container_temp_cleanup",
                "warning",
            )
        }
        if not backup.get("success"):
            raise OpsError(
                f"备份未完整成功: {backup.get('status') or backup.get('warning') or '未知原因'}",
                stage="backup",
            )
        _step(result, "backup", "success", archive_sha256=backup.get("archive_sha256"))

        _step(result, "restore", "running")
        restore = run_restore(
            state.source_stack,
            archive_path=work_dir / "backup" / backup["archive_name"],
            metadata_path=work_dir / "backup" / backup["metadata_name"],
            target_db=target_db,
            run_id=run_id,
            out_dir=work_dir / "restore",
            cleanup_failed_target=False,
            repo_root=repo_root,
            image=config.backend_image,
        )
        result["restore"] = {
            key: restore.get(key)
            for key in (
                "success",
                "status",
                "core_success",
                "cleanup_complete",
                "target_database",
                "target_created",
                "target_preserved",
                "alembic_version",
                "vector_version",
                "comparison",
                "container_temp_cleanup",
            )
        }
        if not restore.get("success"):
            raise OpsError(
                f"恢复未完整成功: {restore.get('status') or '未知原因'}",
                stage="restore",
            )
        _step(result, "restore", "success")

        _step(result, "restored_app_http", "running")
        state.source_stack.stop("backend", "frontend")
        b_env_values = _env_copy(
            source_env_values,
            POSTGRES_DB=target_db,
            FRONTEND_PORT=str(b_port),
            OPS_ENV_FILE=str(work_dir / "env" / "b.env").replace("\\", "/"),
        )
        b_env = _write_env(work_dir / "env" / "b.env", b_env_values)
        b_backend_name = f"{source_project}-b-backend"
        b_frontend_name = f"{source_project}-b-frontend"
        # 名称只用于报告与定位;创建返回 ID 前不视为可清理资源
        state.b_backend_name = b_backend_name
        state.b_frontend_name = b_frontend_name
        state.b_backend = _start_backend_instance(
            name=b_backend_name,
            image=config.backend_image,
            network=state.network_name,
            env_file=b_env,
            repo_root=repo_root,
            run_id=run_id,
        )
        state.b_frontend = _start_nginx_instance(
            name=b_frontend_name,
            network=state.network_name,
            port=b_port,
            repo_root=repo_root,
            run_id=run_id,
        )
        _wait_health(_port_url(b_port), config.timeout)
        result["http"]["restored"] = _assert_fixture_permissions(_port_url(b_port), state.fixtures)
        # 真实前端静态与 SPA 路由,经 nginx 同源代理
        landing = HttpClient(_port_url(b_port), timeout=10).get("/")
        if landing.status != 200 or "<div id=\"app\">" not in (landing.text or ""):
            raise OpsError("恢复库前端静态首页不可用", stage="http")
        spa = HttpClient(_port_url(b_port), timeout=10).get("/faults")
        if spa.status != 200 or "<div id=\"app\">" not in (spa.text or ""):
            raise OpsError("恢复库前端 SPA 路由不可用", stage="http")
        result["http"]["restored"]["static_index"] = True
        result["http"]["restored"]["spa_route"] = True
        _step(result, "restored_app_http", "success")

        _step(result, "restored_app_write", "running")
        audit_before_write = int(result["http"]["restored"].get("audit_events") or 0)
        write1 = _http_write_business(
            _port_url(b_port), state.fixtures, run_id, prefix="B-WRITE"
        )
        audit_after_write = _audit_count(state.source_stack, target_db)
        write1["audit_events_before"] = audit_before_write
        write1["audit_events_after"] = audit_after_write
        write1["audit_increased"] = audit_after_write > audit_before_write
        if not write1["audit_increased"]:
            raise OpsError("恢复库续办后审计未增加", stage="http")
        result["http"]["restored_write"] = write1
        _step(result, "restored_app_write", "success")

        _step(result, "source_unchanged", "running")
        source_after = take_snapshot(state.source_stack)
        diff = compare_snapshots(source_snapshot, source_after)
        result["http"]["source_unchanged"] = diff
        if not diff["equal"]:
            raise OpsError("恢复库写入后来源库发生变化,可能误连", stage="source")
        _step(result, "source_unchanged", "success")

        _step(result, "volume_restart", "running")
        before_cid = state.source_stack.db_container()
        before_volume = state.source_stack.volume_name()
        audit_persist_before = _audit_count(state.source_stack, target_db)
        persist1 = _http_write_business(
            _port_url(b_port), state.fixtures, run_id, prefix="PERSIST"
        )
        audit_persist_after = _audit_count(state.source_stack, target_db)
        persist1["audit_events_before"] = audit_persist_before
        persist1["audit_events_after"] = audit_persist_after
        persist1["audit_increased"] = audit_persist_after > audit_persist_before
        if not persist1["audit_increased"]:
            raise OpsError("持久化写入后审计未增加", stage="persistence")
        state.source_stack.run("restart", "db", timeout=120)
        _wait_db(state.source_stack, config.timeout)
        docker_cmd(["restart", state.b_backend], timeout=120)
        _wait_health(_port_url(b_port), config.timeout)
        restart_marker = _http_marker_check(_port_url(b_port), state.fixtures, run_id, "PERSIST")
        result["persistence"]["restart"] = {
            "db_container_before": before_cid,
            "db_container_after": state.source_stack.db_container(),
            "volume_before": before_volume,
            "volume_after": state.source_stack.volume_name(),
            "marker": restart_marker,
            "write": persist1,
        }
        if not restart_marker["found"]:
            raise OpsError("数据库正常重启后演练标记丢失", stage="persistence")
        _step(result, "volume_restart", "success")

        _step(result, "volume_recreate", "running")
        old_cid = state.source_stack.db_container()
        state.source_stack.ensure_run_labels()
        docker_cmd(["rm", "-f", old_cid], timeout=120)
        state.source_stack.up("db")
        _wait_db(state.source_stack, config.timeout)
        new_cid = state.source_stack.db_container()
        new_volume = state.source_stack.volume_name()
        if new_cid == old_cid:
            raise OpsError("数据库容器 ID 未变化,不能证明容器重建", stage="persistence")
        if new_volume != before_volume:
            raise OpsError("重建后命名卷发生变化", stage="persistence")
        try:
            state.source_stack.record_resources()
        except Exception as record_exc:  # noqa: BLE001
            result.setdefault("resource_record_errors", []).append(
                f"source-recreate: {type(record_exc).__name__}"
            )
        docker_cmd(["restart", state.b_backend], timeout=120)
        _wait_health(_port_url(b_port), config.timeout)
        recreate_marker = _http_marker_check(_port_url(b_port), state.fixtures, run_id, "PERSIST")
        audit_recreate_before = _audit_count(state.source_stack, target_db)
        write2 = _http_write_business(
            _port_url(b_port), state.fixtures, run_id, prefix="PERSIST2"
        )
        audit_recreate_after = _audit_count(state.source_stack, target_db)
        write2["audit_events_before"] = audit_recreate_before
        write2["audit_events_after"] = audit_recreate_after
        write2["audit_increased"] = audit_recreate_after > audit_recreate_before
        if not write2["audit_increased"]:
            raise OpsError("同卷重建后续写审计未增加", stage="persistence")
        result["persistence"]["recreate"] = {
            "db_container_old": old_cid,
            "db_container_new": new_cid,
            "volume": new_volume,
            "marker": recreate_marker,
            "write": write2,
            "migration_version": take_snapshot(state.source_stack, database=target_db)[
                "alembic_version"
            ],
        }
        if not recreate_marker["found"]:
            raise OpsError("同卷重建后演练标记丢失", stage="persistence")
        _step(result, "volume_recreate", "success")

        _step(result, "production_config", "running")
        result["production"]["config"] = _validate_production_config(
            repo_root,
            prod_env,
            run_id,
            prod_project,
            prod_port,
            database=prod_env_values["POSTGRES_DB"],
            user=prod_env_values["POSTGRES_USER"],
            password=prod_env_values["POSTGRES_PASSWORD"],
            secret=prod_env_values["SECRET_KEY"],
        )
        _step(result, "production_config", "success")

        _step(result, "production_positive", "running")
        try:
            state.prod_stack.up()
        finally:
            try:
                state.prod_stack.record_resources()
            except Exception as record_exc:  # noqa: BLE001
                result.setdefault("resource_record_errors", []).append(
                    f"production: {type(record_exc).__name__}"
                )
        _wait_health(_port_url(prod_port), config.timeout)
        state.prod_stack.ensure_run_labels()
        result["resources"]["production_manifest"] = state.prod_stack.manifest
        result["production"]["positive"] = _production_positive(
            _port_url(prod_port),
            prod_env_values["OPSSOP_ADMIN_USERNAME"],
            prod_env_values["OPSSOP_ADMIN_PASSWORD"],
            state.prod_stack,
        )
        _step(result, "production_positive", "success")

        for step_name, kind in (
            ("production_negative_weak_password", "weak_password"),
            ("production_negative_demo_flag", "demo_flag"),
            ("production_negative_legacy_account", "legacy_account"),
            ("production_negative_migration_failure", "migration_failure"),
        ):
            _step(result, step_name, "running")
            result["production"][kind] = _production_negative(
                state, kind, config, prod_env_values, prod_env
            )
            _step(result, step_name, "success")

        _step(result, "cleanup", "running")
        _cleanup_after_run(state, success=True)
        cleanup = result["cleanup"]
        record_errors = result.get("resource_record_errors") or []
        if record_errors:
            cleanup["failed"] = True
            cleanup["status"] = "failed"
            cleanup["record_errors"] = record_errors
        cleanup_failed_flag = bool(cleanup.get("failed"))
        cleanup_status = cleanup.get("status")
        _step(
            result,
            "cleanup",
            "failed" if cleanup_failed_flag else "success",
            cleanup_status=cleanup_status,
        )
        if cleanup_failed_flag:
            raise OpsError("清理未全部成功,不能把整体标为成功", stage="cleanup")

        result["success"] = True
        _step(result, "report", "running")
        _finalize_report(result, work_dir)
    except KeyboardInterrupt as exc:
        fail(OpsCancelled("演练被用户取消", stage="drill"))
        raise OpsCancelled("演练被用户取消", stage="drill") from exc
    except OpsError as exc:
        fail(exc)
        if result.get("report_write_failed"):
            raise OpsError(
                f"{exc}; 演练报告写入失败", stage="report"
            ) from exc
        raise
    except Exception as exc:  # noqa: BLE001
        failure = OpsError(f"演练异常: {type(exc).__name__}: {exc}", stage="drill")
        fail(failure)
        if result.get("report_write_failed"):
            raise OpsError(
                f"{failure}; 演练报告写入失败", stage="report"
            ) from exc
        raise failure from exc
    return result


def _wait_db(stack: ComposeStack, timeout: int) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if stack.psql("SELECT 1", database="postgres").strip() == "1":
                return
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1.5)
    raise OpsError("等待数据库恢复超时", stage="persistence")


def _audit_count(stack: ComposeStack, database: str) -> int:
    data = stack.psql_json(
        "SELECT json_build_object('audit',(SELECT count(*) FROM audit_events))::text",
        database=database,
    )
    return int(data.get("audit") or 0)


def _http_marker_check(base_url: str, fx: DrillFixtures, run_id: str, prefix: str) -> dict:
    client = HttpClient(base_url)
    client.login(fx.users["operator1"]["username"], fx.passwords["operator1"])
    rows = client.get("/api/faults").body or []
    matches = [row for row in rows if f"{prefix}-{run_id}" in row.get("title", "")]
    return {"found": bool(matches), "count": len(matches), "ids": [r["id"] for r in matches]}


def _env_map(service: dict) -> dict[str, str]:
    env = service.get("environment") or {}
    if isinstance(env, list):
        env = dict(item.split("=", 1) for item in env if "=" in item)
    return {str(key): "" if value is None else str(value) for key, value in env.items()}


def _mount_sources(service: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for item in service.get("volumes") or []:
        out[str(item.get("target"))] = item
    return out


def _assert_locked_config(
    cfg: dict,
    *,
    repo_root: Path,
    project: str,
    env_file: Path,
    expected_db: str,
    expected_user: str,
    expected_password: str,
    expected_secret: str,
    frontend_port: int,
    opssop_env: str,
    expected_backend_targets: dict[str, Path],
    expected_frontend_targets: dict[str, Path],
) -> dict:
    """启动前核对最终渲染配置,确保宿主变量/环境文件没有替换演练设置。"""
    services = cfg.get("services") or {}
    problems: list[str] = []
    if set(services) != {"db", "backend", "frontend"}:
        problems.append(f"服务集合异常: {sorted(services)}")
    db = services.get("db") or {}
    backend = services.get("backend") or {}
    frontend = services.get("frontend") or {}
    db_env = _env_map(db)
    backend_env = _env_map(backend)

    if db_env.get("POSTGRES_DB") != expected_db:
        problems.append("db POSTGRES_DB 被覆盖")
    if db_env.get("POSTGRES_USER") != expected_user:
        problems.append("db POSTGRES_USER 被覆盖")
    if db_env.get("POSTGRES_PASSWORD") != expected_password:
        problems.append("db POSTGRES_PASSWORD 被覆盖")
    for key, expected in (
        ("POSTGRES_DB", expected_db),
        ("POSTGRES_USER", expected_user),
        ("POSTGRES_PASSWORD", expected_password),
        ("SECRET_KEY", expected_secret),
        ("POSTGRES_HOST", "db"),
        ("OPSSOP_ENV", opssop_env),
        ("AI_MODE", "disabled"),
        ("AI_BASE_URL", ""),
        ("AI_API_KEY", ""),
        ("AI_MODEL", ""),
        ("EMBEDDING_MODE", "disabled"),
        ("EMBEDDING_BASE_URL", ""),
        ("EMBEDDING_API_KEY", ""),
        ("EMBEDDING_MODEL", ""),
        ("LOCAL_ONLY", "1"),
    ):
        if backend_env.get(key) != expected:
            problems.append(f"backend {key} 与演练生成值不一致")
    if backend_env.get("OPSSOP_SEED_DEMO") not in ("0", "false", "False"):
        problems.append("backend OPSSOP_SEED_DEMO 未关闭")
    # Compose 在某些覆盖组合下不会把 env_file 中的纯插值变量回显到 environment;
    # 关键值已逐项核对,这里只在变量存在时额外核对文件路径。
    if "OPS_ENV_FILE" in backend_env:
        try:
            configured_env_file = Path(backend_env.get("OPS_ENV_FILE", "")).resolve()
        except (OSError, ValueError):
            configured_env_file = Path("")
        if configured_env_file != env_file.resolve():
            problems.append("backend OPS_ENV_FILE 指向了其他环境文件")

    db_ports = db.get("ports") or []
    backend_ports = backend.get("ports") or []
    frontend_ports = frontend.get("ports") or []
    if db_ports:
        problems.append("db 仍发布宿主端口")
    if backend_ports:
        problems.append("backend 仍发布宿主端口")
    if len(frontend_ports) != 1:
        problems.append("frontend 端口映射数量不是 1")
    else:
        mapping = frontend_ports[0]
        if mapping.get("host_ip") != "127.0.0.1":
            problems.append("frontend 未绑定 127.0.0.1")
        if str(mapping.get("published")) != str(frontend_port):
            problems.append("frontend 端口与演练分配端口不一致")

    db_mounts = _mount_sources(db)
    data = db_mounts.get("/var/lib/postgresql/data") or {}
    if data.get("type") != "volume":
        problems.append("db 数据目录不是命名卷")
    elif str(data.get("source")) not in ("pgdata", f"{project}_pgdata"):
        problems.append("db 数据卷来源不是本项目 pgdata")

    backend_mounts = _mount_sources(backend)
    if set(backend_mounts) != set(expected_backend_targets):
        problems.append(
            "backend 挂载目标集合异常: "
            + ",".join(sorted(backend_mounts))
        )
    for target, source in expected_backend_targets.items():
        item = backend_mounts.get(target) or {}
        if item.get("type") != "bind":
            problems.append(f"backend {target} 不是 bind 挂载")
            continue
        if Path(str(item.get("source"))).resolve() != source.resolve():
            problems.append(f"backend {target} 来源路径异常")

    frontend_mounts = _mount_sources(frontend)
    if set(frontend_mounts) != set(expected_frontend_targets):
        problems.append("frontend 挂载目标集合异常")
    for target, source in expected_frontend_targets.items():
        item = frontend_mounts.get(target) or {}
        if Path(str(item.get("source"))).resolve() != source.resolve():
            problems.append(f"frontend {target} 来源路径异常")

    for name, service in (("db", db), ("backend", backend), ("frontend", frontend)):
        networks = service.get("networks") or {}
        if set(networks) != {"default"}:
            problems.append(f"{name} 网络归属异常: {sorted(networks)}")
    network_spec = (cfg.get("networks") or {}).get("default") or {}
    if network_spec.get("external"):
        problems.append("演练网络不能使用 external")

    if problems:
        raise OpsError("演练合并配置不合格: " + "; ".join(problems), stage="config")
    return {
        "services": sorted(services),
        "db_ports": db_ports,
        "backend_ports": backend_ports,
        "frontend_ports": frontend_ports,
        "backend_volume_targets": sorted(backend_mounts),
        "frontend_volume_targets": sorted(frontend_mounts),
        "network_internal": bool(network_spec.get("internal")),
        "env": {
            "OPSSOP_ENV": backend_env.get("OPSSOP_ENV"),
            "OPSSOP_SEED_DEMO": backend_env.get("OPSSOP_SEED_DEMO"),
            "AI_MODE": backend_env.get("AI_MODE"),
            "AI_BASE_URL_empty": backend_env.get("AI_BASE_URL") == "",
            "AI_API_KEY_empty": backend_env.get("AI_API_KEY") == "",
            "EMBEDDING_MODE": backend_env.get("EMBEDDING_MODE"),
            "LOCAL_ONLY": backend_env.get("LOCAL_ONLY"),
        },
        "config_verified": True,
    }


def _validate_production_config(
    repo_root: Path,
    env_file: Path,
    run_id: str,
    project: str,
    port: int,
    *,
    database: str,
    user: str,
    password: str,
    secret: str,
) -> dict:
    stack = ComposeStack(
        repo_root,
        project,
        env_file,
        [repo_root / "docker-compose.yml", repo_root / "docker-compose.prod.yml"],
        run_id=run_id,
        controlled_env=True,
    )
    cfg = stack.config()
    evidence = _assert_locked_config(
        cfg,
        repo_root=repo_root,
        project=project,
        env_file=env_file,
        expected_db=database,
        expected_user=user,
        expected_password=password,
        expected_secret=secret,
        frontend_port=port,
        opssop_env="production",
        expected_backend_targets={},
        expected_frontend_targets={},
    )
    services = cfg.get("services") or {}
    backend_env = _env_map(services.get("backend") or {})
    if backend_env.get("OPSSOP_ENV") != "production":
        raise OpsError("生产模式 OPSSOP_ENV 不是 production", stage="production")
    if backend_env.get("OPSSOP_SEED_DEMO") not in ("0", "false", "False"):
        raise OpsError("生产模式 OPSSOP_SEED_DEMO 未关闭", stage="production")
    evidence["production"] = True
    return evidence


def _production_positive(base_url: str, admin_username: str, admin_password: str, stack: ComposeStack) -> dict:
    client = HttpClient(base_url)
    client.login(admin_username, admin_password)
    users = client.get("/api/users").body or []
    if [u.get("username") for u in users] != [admin_username]:
        raise OpsError("生产模式活动账号不是仅管理员", stage="production")
    if (client.get("/api/faults").body or []):
        raise OpsError("生产空库出现业务数据", stage="production")
    cats = client.get("/api/categories").body or []
    if len(cats) < 8:
        raise OpsError("生产空库缺少系统分类", stage="production")
    health = client.get("/api/system/health").body or {}
    if health.get("status") != "ok":
        raise OpsError("生产健康接口异常", stage="production")
    landing = HttpClient(base_url, timeout=10).get("/")
    if landing.status != 200 or "<div id=\"app\">" not in (landing.text or ""):
        raise OpsError("生产前端静态首页不可用", stage="production")
    spa = HttpClient(base_url, timeout=10).get("/sops")
    if spa.status != 200 or "<div id=\"app\">" not in (spa.text or ""):
        raise OpsError("生产前端 SPA 路由不可用", stage="production")
    counts = stack.psql_json(
        "SELECT json_build_object('users',(SELECT count(*) FROM users),"
        "'demo',(SELECT count(*) FROM demo_seed_records),"
        "'faults',(SELECT count(*) FROM faults))::text"
    )
    if counts.get("users") != 1 or counts.get("demo") != 0 or counts.get("faults") != 0:
        raise OpsError("生产空库计数不符合预期", stage="production")
    return {
        "users": [u.get("username") for u in users],
        "categories": len(cats),
        "counts": counts,
        "health": health,
        "static_index": True,
        "spa_route": True,
    }


def _production_negative(
    state: DrillState,
    kind: str,
    config: DrillConfig,
    prod_env_values: dict[str, str],
    prod_env: Path,
) -> dict:
    stack = state.prod_stack
    assert stack is not None
    run_id = config.run_id
    network = stack.network_name()
    suffix = kind.replace("_", "")
    db_name = f"opssop_prod_neg_{run_id}_{suffix}"[:63]
    stack.create_database(db_name)
    state.negative_databases.append(db_name)
    env_values = _env_copy(
        prod_env_values,
        POSTGRES_DB=db_name,
        FRONTEND_PORT=str(state.prod_port),
        OPS_ENV_FILE=str(prod_env).replace("\\", "/"),
    )
    image = config.backend_image
    if kind == "weak_password":
        env_values["OPSSOP_ADMIN_PASSWORD"] = "change-me-admin-password"
        env = _write_env(state.config.work_dir / "env" / f"neg-{kind}.env", env_values)
        attempt = _oneoff_backend(
            name=f"opssop-neg-{kind}-{run_id}",
            image=image,
            network=network,
            env_file=env,
            repo_root=state.config.repo_root,
            run_id=run_id,
            command=["sh", "scripts/start.sh"],
        )
        if attempt["exit_code"] == 0:
            raise OpsError("弱口令生产启动未被拒绝", stage="production")
        if _db_has_alembic(stack, db_name):
            raise OpsError("弱口令在迁移前未失败", stage="production")
        return {"exit_code": attempt["exit_code"], "alembic_created": False, "stderr": attempt["stderr"]}
    if kind == "demo_flag":
        env_values["OPSSOP_SEED_DEMO"] = "1"
        env = _write_env(state.config.work_dir / "env" / f"neg-{kind}.env", env_values)
        attempt = _oneoff_backend(
            name=f"opssop-neg-{kind}-{run_id}",
            image=image,
            network=network,
            env_file=env,
            repo_root=state.config.repo_root,
            run_id=run_id,
            command=["sh", "scripts/start.sh"],
        )
        if attempt["exit_code"] == 0:
            raise OpsError("生产演示开关未被拒绝", stage="production")
        if _db_has_alembic(stack, db_name):
            raise OpsError("演示开关在迁移前未失败", stage="production")
        return {"exit_code": attempt["exit_code"], "alembic_created": False, "stderr": attempt["stderr"]}
    if kind == "legacy_account":
        env = _write_env(state.config.work_dir / "env" / f"neg-{kind}.env", env_values)
        dev_env_values = _env_copy(
            env_values,
            OPSSOP_ENV="development",
            OPSSOP_SEED_DEMO="1",
        )
        dev_env = _write_env(state.config.work_dir / "env" / "neg-legacy-dev.env", dev_env_values)
        seed_attempt = _oneoff_backend(
            name=f"opssop-neg-legacy-seed-{run_id}",
            image=image,
            network=network,
            env_file=dev_env,
            repo_root=state.config.repo_root,
            run_id=run_id,
            command=["sh", "-c", "alembic upgrade head && python -c 'from app.seed import ensure_seed; ensure_seed()'"],
        )
        if seed_attempt["exit_code"] != 0:
            raise OpsError("遗留账号夹具准备失败", stage="production")
        prod_attempt = _oneoff_backend(
            name=f"opssop-neg-legacy-prod-{run_id}",
            image=image,
            network=network,
            env_file=env,
            repo_root=state.config.repo_root,
            run_id=run_id,
            command=["sh", "scripts/start.sh"],
        )
        if prod_attempt["exit_code"] == 0:
            raise OpsError("遗留演示账号未被生产启动拒绝", stage="production")
        return {
            "dev_seed_exit": seed_attempt["exit_code"],
            "prod_exit": prod_attempt["exit_code"],
            "stderr": prod_attempt["stderr"],
        }
    if kind == "migration_failure":
        role = f"opssop_ro_{run_id}"[:63]
        password = safe_secret(24)
        _create_role(stack, role, password)
        state.negative_role = role
        restricted = _env_copy(
            env_values,
            POSTGRES_USER=role,
            POSTGRES_PASSWORD=password,
        )
        env = _write_env(state.config.work_dir / "env" / f"neg-{kind}.env", restricted)
        attempt = _oneoff_backend(
            name=f"opssop-neg-{kind}-{run_id}",
            image=image,
            network=network,
            env_file=env,
            repo_root=state.config.repo_root,
            run_id=run_id,
            command=["sh", "scripts/start.sh"],
        )
        if attempt["exit_code"] == 0:
            raise OpsError("受限角色迁移失败未被发现", stage="production")
        if _db_has_alembic(stack, db_name):
            raise OpsError("迁移失败仍创建了 alembic_version", stage="production")
        return {"exit_code": attempt["exit_code"], "alembic_created": False, "stderr": attempt["stderr"]}
    raise OpsError(f"未知生产负例: {kind}", stage="production")


def _state_has_resources(state: DrillState) -> bool:
    if state.b_backend or state.b_frontend:
        return True
    if state.negative_databases or state.negative_role:
        return True
    for stack in (state.source_stack, state.prod_stack):
        manifest = getattr(stack, "manifest", None) if stack is not None else None
        if manifest and (
            manifest.get("containers")
            or manifest.get("volumes")
            or manifest.get("networks")
        ):
            return True
    return False


def _sync_cleanup_step(result: dict) -> None:
    """失败收尾也会真的执行清理:把报告里 planned/not_run 的 cleanup 步骤改成真实结果。"""
    step = next(
        (item for item in result.get("steps") or [] if item.get("name") == "cleanup"),
        None,
    )
    if step is not None and step.get("status") in ("planned", "not_run"):
        failed = bool((result.get("cleanup") or {}).get("failed"))
        step["status"] = "failed" if failed else "success"
        step["updated_at"] = now_iso()


def _cleanup_after_run(state: DrillState, *, success: bool) -> None:
    result = state.result
    if (result.get("cleanup") or {}).get("status"):
        return
    cleanup: dict = {
        "containers": [],
        "stacks": {},
        "negative_databases": [],
        "role": None,
        "failed": False,
    }
    has_resources = _state_has_resources(state)
    # 默认成功即清理;失败默认保留现场供诊断,除非显式 cleanup_on_failure
    should_cleanup = state.config.keep_resources is False and (
        success or state.config.cleanup_on_failure
    )
    if not should_cleanup:
        cleanup["retained_requested"] = state.config.keep_resources
        unknown = bool(result.get("resource_record_errors"))
        if not has_resources and not unknown:
            cleanup["status"] = "nothing_to_clean"
            cleanup["reason"] = "准备阶段失败,尚未创建资源"
        else:
            cleanup["status"] = "retained"
            cleanup["reason"] = (
                "resource_state_unknown"
                if unknown
                else "keep_resources"
                if state.config.keep_resources
                else "failure_diagnosis"
            )
        result["cleanup"] = cleanup
        _sync_cleanup_step(result)
        return
    for name, expected_id in (
        (state.b_frontend_name, state.b_frontend),
        (state.b_backend_name, state.b_backend),
    ):
        # 只有确实返回过容器 ID 的名称才算本轮创建的;预先登记的名称不足以授权 rm
        if name and expected_id:
            try:
                cleanup["containers"].append(
                    _remove_container_if_owned(
                        name, state.config.run_id, expected_id=expected_id
                    )
                )
            except Exception as exc:  # noqa: BLE001
                cleanup["containers"].append(
                    {
                        "container": name,
                        "removed": False,
                        "status": "failed",
                        "error": f"{type(exc).__name__}: {exc}"[:300],
                    }
                )
    if state.prod_stack is not None:
        for db_name in state.negative_databases:
            try:
                cleanup["negative_databases"].append(
                    state.prod_stack.drop_database_exact(db_name)
                )
            except Exception as exc:  # noqa: BLE001
                cleanup["negative_databases"].append(
                    {"database": db_name, "dropped": False, "error": str(exc)[:300]}
                )
        if state.negative_role:
            cleanup["role"] = _drop_role(state.prod_stack, state.negative_role)
        try:
            cleanup["stacks"]["production"] = state.prod_stack.cleanup(remove_volume=True)
        except Exception as exc:  # noqa: BLE001
            cleanup["stacks"]["production"] = {
                "failed": True,
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}"[:500],
            }
    if state.source_stack is not None:
        try:
            cleanup["stacks"]["source"] = state.source_stack.cleanup(remove_volume=True)
        except Exception as exc:  # noqa: BLE001
            cleanup["stacks"]["source"] = {
                "failed": True,
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}"[:500],
            }
    failed = cleanup_failed(cleanup)
    cleanup["failed"] = failed
    removed = sum(
        1 for item in cleanup["containers"] if item.get("status") == "removed"
    )
    for stack_cleanup in cleanup["stacks"].values():
        removed += int(stack_cleanup.get("removed_count") or 0)
    removed += sum(
        1
        for item in cleanup["negative_databases"]
        if isinstance(item, dict) and item.get("dropped") is True
    )
    cleanup["removed_count"] = removed
    if failed:
        cleanup["status"] = "failed"
    elif removed == 0 and not has_resources:
        # 栈因缺少本轮清单而保留时,不能说成"没有资源"
        retained = any(
            isinstance(item, dict) and item.get("status") == "retained"
            for item in cleanup["stacks"].values()
        )
        cleanup["status"] = "retained" if retained else "nothing_to_clean"
    elif removed == 0:
        cleanup["status"] = "not_found"
    else:
        cleanup["status"] = "cleaned"
    result["cleanup"] = cleanup
    _sync_cleanup_step(result)


def _summary(result: dict) -> str:
    lines = [
        "# OpsSOP 运维演练报告",
        "",
        f"- run_id: `{result.get('run_id')}`",
        f"- 模式: `{result.get('mode')}`",
        f"- 成功: `{result.get('success')}`;取消: `{result.get('cancelled')}`",
        f"- 开始/结束: {result.get('started_at')} / {result.get('finished_at')}",
        "",
        "## 步骤",
        "",
        "| 步骤 | 状态 |",
        "|---|---|",
    ]
    for step in result.get("steps", []):
        lines.append(f"| {step['name']} | {step['status']} |")
    lines += ["", "## 关键结果", ""]
    if result.get("backup"):
        b = result["backup"]
        lines += [
            f"- 备份归档: `{b.get('archive_name')}` / {b.get('archive_bytes')} bytes / "
            f"sha256 `{str(b.get('archive_sha256'))[:16]}…`",
        ]
    if result.get("restore"):
        r = result["restore"]
        lines += [
            f"- 恢复到 `{r.get('target_database')}`: success={r.get('success')}, "
            f"comparison_equal={(r.get('comparison') or {}).get('equal')}",
        ]
    if result.get("http"):
        lines += [f"- HTTP 检查: {json.dumps(result['http'], ensure_ascii=False)[:1200]}"]
    if result.get("persistence"):
        lines += [f"- 持久化: {json.dumps(result['persistence'], ensure_ascii=False)[:1200]}"]
    if result.get("production"):
        lines += [f"- 生产 smoke: {json.dumps(result['production'], ensure_ascii=False)[:1600]}"]
    lines += [
        "",
        "## 资源与清理",
        "",
        f"- 资源: {json.dumps(result.get('resources', {}), ensure_ascii=False)}",
        f"- 清理: {json.dumps(result.get('cleanup', {}), ensure_ascii=False)}",
        "",
        "## 限制",
        "",
    ]
    lines += [f"- {item}" for item in result.get("limitations", [])]
    if result.get("error"):
        lines += ["", f"## 错误", "", f"- {result['error']}"]
    return "\n".join(lines) + "\n"
