"""Docker Compose / PostgreSQL 操作封装(标准库 + docker CLI)。

安全原则:
- 只接受 `opssop-` 前缀的 Compose 项目,避免误触受保护的既有资源;
- 数据库/归档操作使用参数列表,不拼接 shell;
- 数据库备份/恢复在 DB 容器内通过本地 socket 执行,不把密码放到命令行;
- 清理前核对容器/卷/网络的名称、Compose 项目标签和本轮 run_id。
"""

from __future__ import annotations

import json
from pathlib import Path

from .common import (
    OpsError,
    compose_interpolation_keys,
    controlled_compose_env,
    docker,
    redact_text,
    run,
    validate_db_identifier,
    validate_resource_name,
    validate_run_id,
    validate_source_database,
    validate_target_database,
)


class ComposeStack:
    def __init__(
        self,
        repo_root: Path,
        project: str,
        env_file: Path,
        compose_files: list[Path],
        *,
        run_id: str,
        controlled_env: bool = False,
    ):
        self.repo_root = repo_root
        self.project = validate_project_name(project)
        self.env_file = env_file
        self.compose_files = compose_files
        self.run_id = validate_run_id(run_id)
        self.controlled_env = controlled_env
        # 记录本轮实际创建/确认过的资源;清理只处理这份清单
        self.manifest: dict | None = None
        # 只有本进程调用过 up 才算发起过创建;预检拒绝后不得回退认领旧资源
        self.creation_attempted = False
        self._interpolation_keys: set[str] | None = None
        if not env_file.exists():
            raise OpsError(f"环境文件不存在: {env_file}", stage="compose")
        for path in compose_files:
            if not path.exists():
                raise OpsError(f"Compose 文件不存在: {path}", stage="compose")

    def _cmd(self, *args: str) -> list[str]:
        cmd = ["docker", "compose", "-p", self.project, "--env-file", str(self.env_file)]
        for path in self.compose_files:
            cmd += ["-f", str(path)]
        cmd += list(args)
        return cmd

    def _subprocess_env(self) -> dict[str, str] | None:
        if not self.controlled_env:
            return None
        if self._interpolation_keys is None:
            self._interpolation_keys = compose_interpolation_keys(
                self.compose_files, self.env_file
            )
        return controlled_compose_env(self._interpolation_keys)

    def run(self, *args: str, timeout: int = 300) -> str:
        proc = run(
            self._cmd(*args),
            cwd=self.repo_root,
            env=self._subprocess_env(),
            timeout=timeout,
        )
        return proc.stdout

    def config(self) -> dict:
        raw = self.run("config", "--format", "json")
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise OpsError("docker compose config 未返回合法 JSON", stage="compose") from exc

    def up(self, *services: str) -> None:
        self.creation_attempted = True
        self.run("up", "-d", "--no-build", *services, timeout=600)

    def stop(self, *services: str) -> None:
        self.run("stop", *services, timeout=120)

    def ps_q(self, service: str) -> str:
        raw = self.run("ps", "-q", service).strip()
        ids = [line.strip() for line in raw.splitlines() if line.strip()]
        if not ids:
            raise OpsError(f"服务 {service} 没有运行中的容器", stage="docker")
        if len(ids) != 1:
            raise OpsError(
                f"服务 {service} 应恰有一个容器,实际 {len(ids)} 个", stage="docker"
            )
        return ids[0]

    def db_service_config(self) -> dict:
        cfg = self.config()
        service = (cfg.get("services") or {}).get("db")
        if not isinstance(service, dict):
            raise OpsError("Compose 配置缺少 db 服务", stage="compose")
        env = service.get("environment") or {}
        if isinstance(env, list):
            env = dict(item.split("=", 1) for item in env if "=" in item)
        user = env.get("POSTGRES_USER")
        database = env.get("POSTGRES_DB")
        if not user or not database:
            raise OpsError("db 服务缺少 POSTGRES_USER/POSTGRES_DB", stage="compose")
        return {
            "user": str(user),
            "database": str(database),
        }

    def db_container(self) -> str:
        return self.ps_q("db")

    def container_name(self, container_id: str) -> str:
        return docker(
            ["inspect", "--format", "{{.Name}}", container_id]
        ).stdout.strip().lstrip("/")

    def container_ports(self, container_id: str) -> dict:
        raw = docker(
            ["inspect", "--format", "{{json .NetworkSettings.Ports}}", container_id]
        ).stdout.strip()
        try:
            return json.loads(raw) if raw and raw != "null" else {}
        except json.JSONDecodeError:
            return {}

    def container_labels(self, container_id: str) -> dict:
        raw = docker(
            ["inspect", "--format", "{{json .Config.Labels}}", container_id]
        ).stdout.strip()
        try:
            return json.loads(raw) if raw and raw != "null" else {}
        except json.JSONDecodeError:
            return {}

    def volume_name(self) -> str:
        cid = self.db_container()
        name = docker(
            [
                "inspect",
                "--format",
                '{{range .Mounts}}{{if eq .Destination "/var/lib/postgresql/data"}}{{.Name}}{{end}}{{end}}',
                cid,
            ]
        ).stdout.strip()
        if not name:
            raise OpsError("数据库容器没有命名卷挂载", stage="docker")
        return name

    def volume_labels(self, volume: str) -> dict:
        raw = docker(
            ["volume", "inspect", volume, "--format", "{{json .Labels}}"]
        ).stdout.strip()
        try:
            return json.loads(raw) if raw and raw != "null" else {}
        except json.JSONDecodeError:
            return {}

    def network_name(self) -> str:
        cid = self.db_container()
        raw = docker(
            [
                "inspect",
                "--format",
                "{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{end}}",
                cid,
            ]
        ).stdout.strip()
        names = [line for line in raw.splitlines() if line]
        if len(names) != 1:
            raise OpsError("数据库容器网络归属不唯一", stage="docker")
        return names[0]

    def network_labels(self, network: str) -> dict:
        raw = docker(
            ["network", "inspect", network, "--format", "{{json .Labels}}"]
        ).stdout.strip()
        try:
            return json.loads(raw) if raw and raw != "null" else {}
        except json.JSONDecodeError:
            return {}

    # ---- 启动前资源存在性/归属校验 ----

    def config_resource_names(self, cfg: dict | None = None) -> dict:
        """从合并后的 Compose 配置推导实际卷/网络名称(含默认前缀)。"""
        cfg = cfg or self.config()
        volumes: dict[str, str] = {}
        for key, spec in (cfg.get("volumes") or {}).items():
            spec = spec or {}
            volumes[str(key)] = str(spec.get("name") or f"{self.project}_{key}")
        networks: dict[str, str] = {}
        for key, spec in (cfg.get("networks") or {}).items():
            spec = spec or {}
            networks[str(key)] = str(spec.get("name") or f"{self.project}_{key}")
        return {"volumes": volumes, "networks": networks}

    def preflight_fresh(self) -> dict:
        """首次 up 前硬校验:本轮项目/卷/网络必须均不存在。

        已有同名对象(无论标签如何)直接拒绝;避免先启动并写库、再发现归属不符。
        """
        cfg = self.config()
        names = self.config_resource_names(cfg)
        existing_containers = [
            line.strip()
            for line in docker(
                [
                    "ps",
                    "-aq",
                    "--filter",
                    f"label=com.docker.compose.project={self.project}",
                ],
                timeout=60,
            ).stdout.splitlines()
            if line.strip()
        ]
        if existing_containers:
            raise OpsError(
                f"Compose 项目已存在容器,拒绝在非全新项目上启动演练: {self.project}",
                stage="ownership",
            )
        for kind, mapping in (("volume", names["volumes"]), ("network", names["networks"])):
            for key, name in mapping.items():
                proc = docker([kind, "inspect", name], timeout=60, check=False)
                if proc.returncode == 0:
                    raise OpsError(
                        f"发现已存在的{kind}({key}={name}),拒绝在旧资源上启动演练",
                        stage="ownership",
                    )
                if not _resource_not_found(proc.stderr or proc.stdout):
                    raise OpsError(
                        f"无法确认{kind} {name} 不存在: "
                        f"{redact_text(proc.stderr or proc.stdout)[:300]}",
                        stage="ownership",
                    )
        for kind in ("volume", "network"):
            proc = docker(
                [
                    kind,
                    "ls",
                    "-q",
                    "--filter",
                    f"label=com.docker.compose.project={self.project}",
                ],
                timeout=60,
                check=False,
            )
            if proc.returncode != 0:
                raise OpsError(
                    f"无法检查项目 {kind} 标签归属: "
                    f"{redact_text(proc.stderr or proc.stdout)[:300]}",
                    stage="ownership",
                )
            if proc.stdout.strip():
                raise OpsError(
                    f"项目已有带标签的{kind},拒绝在非全新项目上启动演练: {self.project}",
                    stage="ownership",
                )
        return {
            "project": self.project,
            "containers": 0,
            "volumes": sorted(names["volumes"].values()),
            "networks": sorted(names["networks"].values()),
            "fresh": True,
        }

    def record_resources(self) -> dict:
        """记录本轮实际存在的项目容器/卷/网络及 ID;后续清理只认这份清单。"""
        cfg = self.config()
        names = self.config_resource_names(cfg)
        containers: list[dict] = []
        raw = self.run("ps", "-aq", timeout=60)
        for cid in [line.strip() for line in raw.splitlines() if line.strip()]:
            labels = self.container_labels(cid)
            if labels.get("com.docker.compose.project") != self.project:
                continue
            containers.append(
                {
                    "id": cid,
                    "name": self.container_name(cid),
                    "service": labels.get("com.docker.compose.service"),
                    "labels": labels,
                }
            )
        volumes: list[dict] = []
        for key, name in names["volumes"].items():
            proc = docker(["volume", "inspect", name], timeout=60, check=False)
            if proc.returncode != 0:
                continue
            volumes.append(
                {
                    "key": key,
                    "name": name,
                    "labels": self.volume_labels(name),
                }
            )
        networks: list[dict] = []
        for key, name in names["networks"].items():
            proc = docker(
                ["network", "inspect", name, "--format", "{{.Id}}"],
                timeout=60,
                check=False,
            )
            if proc.returncode != 0:
                continue
            networks.append(
                {
                    "key": key,
                    "name": name,
                    "id": proc.stdout.strip(),
                    "labels": self.network_labels(name),
                }
            )
        self.manifest = {
            "project": self.project,
            "run_id": self.run_id,
            "containers": containers,
            "volumes": volumes,
            "networks": networks,
        }
        return self.manifest

    def verify_manifest(self) -> dict:
        """启动后核对清单对象仍带项目/用途/run_id 标签;不匹配即拒绝继续。"""
        manifest = self.manifest or self.record_resources()
        for item in manifest.get("containers", []):
            labels = self.container_labels(item["id"])
            _require_ownership(
                labels,
                project=self.project,
                run_id=self.run_id,
                kind="容器",
                name=item.get("name") or item["id"],
            )
        for item in manifest.get("volumes", []):
            labels = self.volume_labels(item["name"])
            _require_ownership(
                labels,
                project=self.project,
                run_id=self.run_id,
                kind="命名卷",
                name=item["name"],
            )
        for item in manifest.get("networks", []):
            labels = self.network_labels(item["name"])
            _require_ownership(
                labels,
                project=self.project,
                run_id=self.run_id,
                kind="网络",
                name=item["name"],
            )
        return {
            "containers": [item["id"] for item in manifest.get("containers", [])],
            "volumes": [item["name"] for item in manifest.get("volumes", [])],
            "networks": [item["name"] for item in manifest.get("networks", [])],
        }

    def ensure_run_labels(self) -> None:
        """确认数据库容器/卷/网络均带本轮 run_id、用途和 Compose 项目标签。"""
        if self.manifest:
            self.verify_manifest()
            return
        cid = self.db_container()
        _require_ownership(
            self.container_labels(cid),
            project=self.project,
            run_id=self.run_id,
            kind="数据库容器",
            name=cid,
        )
        volume = self.volume_name()
        _require_ownership(
            self.volume_labels(volume),
            project=self.project,
            run_id=self.run_id,
            kind="命名卷",
            name=volume,
        )
        network = self.network_name()
        _require_ownership(
            self.network_labels(network),
            project=self.project,
            run_id=self.run_id,
            kind="网络",
            name=network,
        )

    # ---- PostgreSQL(local socket,不把密码放命令行) ----

    def psql(self, sql: str, *, database: str | None = None, user: str | None = None) -> str:
        cfg = self.db_service_config()
        database = database or cfg["database"]
        user = user or cfg["user"]
        validate_db_identifier(database, role="psql")
        container = self.db_container()
        proc = docker(
            [
                "exec",
                container,
                "psql",
                "-X",
                "-v",
                "ON_ERROR_STOP=1",
                "-U",
                user,
                "-d",
                database,
                "-At",
                "-c",
                sql,
            ],
            timeout=120,
        )
        return proc.stdout

    def psql_json(self, sql: str, *, database: str | None = None) -> dict:
        raw = self.psql(sql, database=database).strip()
        if not raw:
            raise OpsError("psql 查询没有返回结果", stage="psql")
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise OpsError("psql 查询结果不是 JSON", stage="psql") from exc

    def database_exists(self, name: str) -> bool:
        name = validate_db_identifier(name)
        raw = self.psql(
            f"SELECT 1 FROM pg_database WHERE datname = '{name}'", database="postgres"
        ).strip()
        return raw == "1"

    def create_database(self, name: str) -> None:
        name = validate_target_database(name)
        if self.database_exists(name):
            raise OpsError(f"目标数据库已存在,拒绝覆盖: {name}", stage="restore")
        self.psql(f'CREATE DATABASE "{name}"', database="postgres")

    def drop_database_exact(self, name: str) -> dict:
        """只用于本轮创建且已核实归属的目标库;先终止连接再删除。"""
        name = validate_target_database(name)
        if not self.database_exists(name):
            return {"dropped": False, "reason": "database_not_found", "database": name}
        self.psql(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            f"WHERE datname = '{name}' AND pid <> pg_backend_pid()",
            database="postgres",
        )
        self.psql(f'DROP DATABASE "{name}"', database="postgres")
        return {"dropped": True, "database": name}

    def pg_dump_to_container(self, container_path: str, *, database: str | None = None) -> str:
        cfg = self.db_service_config()
        database = database or cfg["database"]
        validate_source_database(database, role="source")
        path = validate_container_path(container_path)
        container = self.db_container()
        docker(
            [
                "exec",
                container,
                "pg_dump",
                "-U",
                cfg["user"],
                "-d",
                database,
                "-Fc",
                "-f",
                path,
            ],
            timeout=600,
        )
        return path

    def pg_restore_list(self, container_path: str) -> str:
        path = validate_container_path(container_path)
        container = self.db_container()
        return docker(
            ["exec", container, "pg_restore", "-l", path], timeout=120
        ).stdout

    def pg_restore_to_database(self, container_path: str, target_db: str) -> None:
        cfg = self.db_service_config()
        target_db = validate_target_database(target_db)
        path = validate_container_path(container_path)
        container = self.db_container()
        docker(
            [
                "exec",
                container,
                "pg_restore",
                "-U",
                cfg["user"],
                "-d",
                target_db,
                "--no-owner",
                "--no-privileges",
                "--exit-on-error",
                path,
            ],
            timeout=900,
        )

    def pg_tool_version(self, tool: str) -> str:
        if tool not in ("pg_dump", "pg_restore"):
            raise OpsError("不支持的 PostgreSQL 工具", stage="validate")
        container = self.db_container()
        return docker(["exec", container, tool, "--version"]).stdout.strip()

    def remove_container_file(self, container_path: str) -> dict:
        path = validate_container_path(container_path)
        try:
            container = self.db_container()
            docker(["exec", container, "rm", "-f", path], timeout=60)
            return {"removed": True, "path": path}
        except OpsError as exc:
            return {"removed": False, "path": path, "error": str(exc)[:300]}

    def copy_from_container(self, container_path: str, host_path: Path) -> None:
        path = validate_container_path(container_path)
        container = self.db_container()
        docker(["cp", f"{container}:{path}", str(host_path)], timeout=300)

    def copy_to_container(self, host_path: Path, container_path: str) -> None:
        path = validate_container_path(container_path)
        container = self.db_container()
        docker(["cp", str(host_path), f"{container}:{path}"], timeout=300)

    def cleanup(self, *, remove_volume: bool = True) -> dict:
        """清理本轮项目容器/网络/卷;先核对清单、标签,不使用 compose down -v。

        没有本轮清单(未记录或记录失败)时不删除任何对象,返回 retained 与原因。
        每个资源返回独立状态(removed/not_found/retained/refused/failed);
        任何拒绝或命令非零都会把栈级 failed 置为 True,不再只写 warning。
        """
        result: dict = {
            "project": self.project,
            "run_id": self.run_id,
            "containers": [],
            "network": None,
            "volume": None,
            "failed": False,
        }
        manifest = self.manifest
        if manifest is None:
            # 没有本轮记录的对象清单:同名项目/相同标签不足以证明是本进程创建,
            # 不回退到 ps/挂载发现,保留现场交人工核对
            result["status"] = "retained"
            result["reason"] = (
                "creation_unknown" if self.creation_attempted else "never_created"
            )
            return result
        container_ids = [item["id"] for item in manifest.get("containers", [])]
        volume_names = [item["name"] for item in manifest.get("volumes", [])]
        network_names = [item["name"] for item in manifest.get("networks", [])]
        if not volume_names or not network_names:
            # 清单缺失时按实际挂载补充,但仍要核对三组标签
            try:
                if not volume_names:
                    volume_names = [self.volume_name()]
                if not network_names:
                    network_names = [self.network_name()]
            except OpsError:
                pass

        for cid in sorted(set(container_ids)):
            result["containers"].append(
                self._remove_container(
                    cid, require_project=True, expected_id=cid
                )
            )
        if network_names:
            result["network"] = self._remove_network(network_names[0])
        if volume_names:
            if remove_volume:
                result["volume"] = self._remove_volume(volume_names[0])
            else:
                result["volume"] = {
                    "volume": volume_names[0],
                    "removed": False,
                    "status": "retained",
                }
        result["failed"] = _cleanup_has_failure(result)
        removed = sum(
            1
            for item in result["containers"]
            if item.get("status") == "removed"
        )
        removed += sum(
            1
            for key in ("network", "volume")
            if (result.get(key) or {}).get("status") == "removed"
        )
        retained = any(
            (result.get(key) or {}).get("status") == "retained"
            for key in ("network", "volume")
        )
        if result["failed"]:
            result["status"] = "failed"
        elif retained:
            result["status"] = "retained"
        elif removed == 0:
            result["status"] = "nothing_to_clean"
        else:
            result["status"] = "cleaned"
        result["removed_count"] = removed
        return result

    def _remove_container(
        self, name_or_id: str, *, require_project: bool, expected_id: str | None = None
    ) -> dict:
        proc = docker(
            ["inspect", "--format", "{{json .}}", name_or_id],
            timeout=60,
            check=False,
        )
        if proc.returncode != 0:
            if _resource_not_found(proc.stderr or proc.stdout):
                return {
                    "container": name_or_id,
                    "removed": False,
                    "status": "not_found",
                    "idempotent": True,
                }
            return {
                "container": name_or_id,
                "removed": False,
                "status": "failed",
                "error": redact_text(proc.stderr or proc.stdout)[:300],
            }
        try:
            info = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return {
                "container": name_or_id,
                "removed": False,
                "status": "failed",
                "error": "无法解析容器信息",
            }
        labels = (info.get("Config") or {}).get("Labels") or {}
        actual_id = info.get("Id")
        if expected_id and actual_id != expected_id:
            return {
                "container": name_or_id,
                "removed": False,
                "status": "refused",
                "reason": "id_mismatch",
                "expected_id": expected_id,
                "actual_id": actual_id,
            }
        if not _labels_owned(
            labels, project=self.project, run_id=self.run_id, require_project=require_project
        ):
            return {
                "container": name_or_id,
                "removed": False,
                "status": "refused",
                "reason": "ownership_mismatch",
                "container_id": actual_id,
            }
        remove = docker(["rm", "-f", actual_id or name_or_id], timeout=120, check=False)
        if remove.returncode == 0:
            return {
                "container": name_or_id,
                "container_id": actual_id,
                "removed": True,
                "status": "removed",
            }
        return {
            "container": name_or_id,
            "container_id": actual_id,
            "removed": False,
            "status": "failed",
            "exit_code": remove.returncode,
            "error": redact_text(remove.stderr or remove.stdout)[:300],
        }

    def _remove_network(self, name: str) -> dict:
        try:
            labels = self.network_labels(name)
        except OpsError as exc:
            # 对象已被外部删除时按幂等 not_found 处理,不中断后续卷清理
            if _resource_not_found(str(exc)):
                return {
                    "network": name,
                    "removed": False,
                    "status": "not_found",
                    "idempotent": True,
                }
            return {
                "network": name,
                "removed": False,
                "status": "failed",
                "error": redact_text(str(exc))[:300],
            }
        if not _labels_owned(
            labels, project=self.project, run_id=self.run_id, require_project=True
        ):
            return {
                "network": name,
                "removed": False,
                "status": "refused",
                "reason": "ownership_mismatch",
            }
        proc = docker(["network", "rm", name], timeout=60, check=False)
        if proc.returncode == 0:
            return {"network": name, "removed": True, "status": "removed"}
        if _resource_not_found(proc.stderr or proc.stdout):
            return {
                "network": name,
                "removed": False,
                "status": "not_found",
                "idempotent": True,
            }
        return {
            "network": name,
            "removed": False,
            "status": "failed",
            "exit_code": proc.returncode,
            "error": redact_text(proc.stderr or proc.stdout)[:300],
        }

    def _remove_volume(self, name: str) -> dict:
        try:
            labels = self.volume_labels(name)
        except OpsError as exc:
            if _resource_not_found(str(exc)):
                return {
                    "volume": name,
                    "removed": False,
                    "status": "not_found",
                    "idempotent": True,
                }
            return {
                "volume": name,
                "removed": False,
                "status": "failed",
                "error": redact_text(str(exc))[:300],
            }
        if not _labels_owned(
            labels, project=self.project, run_id=self.run_id, require_project=True
        ):
            return {
                "volume": name,
                "removed": False,
                "status": "refused",
                "reason": "ownership_mismatch",
            }
        proc = docker(["volume", "rm", name], timeout=60, check=False)
        if proc.returncode == 0:
            return {"volume": name, "removed": True, "status": "removed"}
        if _resource_not_found(proc.stderr or proc.stdout):
            return {
                "volume": name,
                "removed": False,
                "status": "not_found",
                "idempotent": True,
            }
        return {
            "volume": name,
            "removed": False,
            "status": "failed",
            "exit_code": proc.returncode,
            "error": redact_text(proc.stderr or proc.stdout)[:300],
        }


def _resource_not_found(text: str) -> bool:
    lowered = (text or "").lower()
    return "no such" in lowered or "not found" in lowered


def _labels_owned(
    labels: dict, *, project: str, run_id: str, require_project: bool
) -> bool:
    if labels.get("com.opssop.run_id") != run_id:
        return False
    if labels.get("com.opssop.purpose") != "drill":
        return False
    if require_project and labels.get("com.docker.compose.project") != project:
        return False
    return True


def _require_ownership(
    labels: dict, *, project: str, run_id: str, kind: str, name: str
) -> None:
    if not _labels_owned(labels, project=project, run_id=run_id, require_project=True):
        raise OpsError(
            f"{kind} {name} 的项目/用途/run_id 标签不匹配,拒绝继续",
            stage="ownership",
        )


def _cleanup_has_failure(cleanup: dict) -> bool:
    """递归判断清理结果;拒绝、命令失败、未知状态都算失败。"""
    if cleanup.get("failed") is True or cleanup.get("status") == "failed":
        return True
    for item in cleanup.get("containers") or []:
        if isinstance(item, dict) and item.get("status") in ("failed", "refused"):
            return True
        if isinstance(item, dict) and item.get("removed") is False and not item.get(
            "idempotent"
        ) and item.get("status") not in ("retained", "not_found"):
            return True
    for key in ("network", "volume"):
        item = cleanup.get(key)
        if isinstance(item, dict) and item.get("status") in ("failed", "refused"):
            return True
        if (
            isinstance(item, dict)
            and item.get("removed") is False
            and item.get("status") not in ("retained", "not_found")
        ):
            return True
    return False


def cleanup_failed(cleanup: dict) -> bool:
    """公开给编排层:遍历栈/数据库/角色等嵌套清理结果。"""
    if not isinstance(cleanup, dict):
        return True
    if _cleanup_has_failure(cleanup):
        return True
    for item in cleanup.get("negative_databases") or []:
        if not isinstance(item, dict):
            return True
        if item.get("dropped") is True:
            continue
        if item.get("reason") == "database_not_found":
            continue
        return True
    role = cleanup.get("role")
    if isinstance(role, dict):
        if role.get("dropped") is True or role.get("reason") == "not_found":
            pass
        else:
            return True
    for stack in (cleanup.get("stacks") or {}).values():
        if not isinstance(stack, dict):
            return True
        if stack.get("failed") is True:
            return True
        if _cleanup_has_failure(stack):
            return True
    return False


def validate_project_name(value: str) -> str:
    value = validate_resource_name(value, role="Compose 项目")
    if not value.startswith("opssop-"):
        raise OpsError(
            "Compose 项目必须以 opssop- 开头,避免误触受保护的既有资源", stage="validate"
        )
    return value


def validate_container_path(value: str) -> str:
    path = (value or "").strip()
    if not path.startswith("/tmp/opssop-") or ".." in path or "\x00" in path:
        raise OpsError("容器临时路径必须是 /tmp/opssop-* 下的受控路径", stage="validate")
    return path


def image_info(image: str) -> dict:
    raw = docker(
        ["image", "inspect", image, "--format", "{{json .}}"]
    ).stdout.strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise OpsError(f"无法读取镜像信息: {image}", stage="docker") from exc
    return {
        "image": image,
        "image_id": data.get("Id"),
        "repo_digests": data.get("RepoDigests") or [],
        "created": data.get("Created"),
    }


def compose_version() -> str:
    return docker(["compose", "version"]).stdout.strip()
