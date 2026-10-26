"""运维工具公共能力:安全命名、命令执行、哈希与原子写入。

约束:
- 不打印连接串密码、Token 或完整环境;错误信息做最小脱敏;
- 归档只走二进制安全的 docker cp,不经过文本管道;
- 资源名/数据库名必须先通过白名单校验再拼接;
- 所有“完成”状态由调用方在归档、元信息、校验和、恢复复核均成功后写入。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SAFE_RUN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{5,40}$")
SAFE_DB_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
SAFE_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,80}$")
COMPOSE_VAR_RE = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)")

PROTECTED_DATABASES = {
    "postgres",
    "template0",
    "template1",
    "opssop",
    "opssop_test",
    "opssop_demo",
    "opssop_prod",
}

REDACT_KEYS = ("password", "secret", "token", "api_key", "authorization")


class OpsError(Exception):
    """可预期的运维错误:调用方应以非零状态退出,不打印敏感信息。"""

    def __init__(self, message: str, *, stage: str = "", detail: dict | None = None):
        super().__init__(message)
        self.stage = stage
        self.detail = detail or {}


class OpsCancelled(OpsError):
    """可捕获的用户取消(KeyboardInterrupt 的可控包装)。"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_run_id(prefix: str = "ops") -> str:
    return f"{prefix}{datetime.now().strftime('%m%d')}{secrets.token_hex(5)}"


def validate_run_id(value: str) -> str:
    value = (value or "").strip()
    if not SAFE_RUN_ID_RE.fullmatch(value):
        raise OpsError(
            "run_id 必须是 6-41 位小写字母/数字/下划线/连字符,且以字母或数字开头",
            stage="validate",
        )
    return value


def validate_db_identifier(value: str, *, role: str = "database") -> str:
    """只校验 SQL 标识符安全性;允许 postgres 等管理连接库。"""
    value = (value or "").strip().lower()
    if not SAFE_DB_RE.fullmatch(value):
        raise OpsError(
            f"{role} 数据库名必须匹配 ^[a-z][a-z0-9_]{{0,62}}$",
            stage="validate",
        )
    return value


def validate_target_database(value: str, *, role: str = "target") -> str:
    """恢复目标:必须是可安全创建的新库,拒绝业务/测试/系统库。"""
    value = validate_db_identifier(value, role=role)
    if value in PROTECTED_DATABASES:
        raise OpsError(f"拒绝使用受保护数据库名: {value}", stage="validate")
    if value.startswith(("opssop_eval_", "opssop_migrate_", "opssop_test")):
        raise OpsError(f"拒绝使用测试/评测命名空间: {value}", stage="validate")
    return value


def validate_source_database(value: str, *, role: str = "source") -> str:
    """备份来源:允许业务库名,但拒绝系统库/模板库和测试/评测命名空间。"""
    value = validate_db_identifier(value, role=role)
    if value in {"postgres", "template0", "template1"}:
        raise OpsError(f"拒绝从系统数据库备份: {value}", stage="validate")
    if value.startswith(("opssop_eval_", "opssop_migrate_", "opssop_test")):
        raise OpsError(f"拒绝从测试/评测命名空间备份: {value}", stage="validate")
    return value


def validate_resource_name(value: str, *, role: str = "resource") -> str:
    value = (value or "").strip()
    if not SAFE_NAME_RE.fullmatch(value):
        raise OpsError(f"{role} 名称含非法字符", stage="validate")
    return value


def safe_secret(length: int = 32) -> str:
    """生成合成随机口令/密钥(仅演练环境使用,不写入公开报告)。"""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
    return "".join(secrets.choice(alphabet) for _ in range(max(12, length)))


def redact_text(text: str) -> str:
    if not text:
        return ""
    cleaned = text
    for key in REDACT_KEYS:
        cleaned = re.sub(
            rf"(?i)({key}\s*[=:]\s*)([^\s,;]+)", rf"\1<redacted>", cleaned
        )
    # 连接串中的密码
    cleaned = re.sub(r"(://[^:/@\s]+:)([^@\s]+)(@)", r"\1<redacted>\3", cleaned)
    return cleaned[:2000]


def run(
    cmd: list[str | os.PathLike[str]],
    *,
    cwd: Path | str | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 300,
    check: bool = True,
    input_text: str | None = None,
) -> subprocess.CompletedProcess:
    """执行命令;失败时抛出脱敏后的 OpsError,保留退出码。"""
    printable = " ".join(str(x) for x in cmd[:4])
    try:
        proc = subprocess.run(
            [str(x) for x in cmd],
            cwd=str(cwd) if cwd else None,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            input=input_text,
            capture_output=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise OpsError(f"找不到命令: {cmd[0]}", stage="command") from exc
    except subprocess.TimeoutExpired as exc:
        raise OpsError(f"命令超时: {printable}", stage="command") from exc
    if check and proc.returncode != 0:
        stderr = redact_text(proc.stderr or proc.stdout or "")
        raise OpsError(
            f"命令失败(退出码 {proc.returncode}): {printable}; {stderr}",
            stage="command",
            detail={"returncode": proc.returncode},
        )
    return proc


def docker(args: Iterable[str | os.PathLike[str]], **kwargs) -> subprocess.CompletedProcess:
    return run(["docker", *args], **kwargs)


def env_file_keys(path: Path) -> set[str]:
    """读取 env 文件的变量名(只看键,不读取/记录值)。"""
    keys: set[str] = set()
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return keys
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key = line.split("=", 1)[0].strip()
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            keys.add(key)
    return keys


def compose_interpolation_keys(compose_files: Iterable[Path], env_file: Path) -> set[str]:
    """Compose 插值可能读取的变量名:env 文件键 + compose 文件中的 ${VAR}。

    只收集变量名,不读取/回显变量值;用于从子进程环境中剔除宿主同名变量。
    """
    keys = env_file_keys(env_file)
    for path in compose_files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        keys.update(COMPOSE_VAR_RE.findall(text))
    return keys


def controlled_compose_env(keys: Iterable[str]) -> dict[str, str]:
    """构造 Compose 子进程环境:剔除宿主同名变量及 COMPOSE_* 覆盖项。

    宿主的 shell 变量优先级高于 `--env-file`;不剔除会让演练配置被宿主覆盖。
    保留 PATH/HOME/DOCKER_* 等 Docker CLI 自身需要的变量。
    """
    blocked = {key.upper() for key in keys}
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if upper in blocked or upper.startswith("COMPOSE_"):
            continue
        env[key] = value
    return env


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def atomic_write_json(path: Path, data: Any) -> None:
    """先写同目录临时文件,再原子替换;不把半截 JSON 当结果。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}-{secrets.token_hex(4)}")
    try:
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}-{secrets.token_hex(4)}")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise OpsError(f"缺少文件: {path.name}", stage="read") from exc
    except json.JSONDecodeError as exc:
        raise OpsError(f"文件不是合法 JSON: {path.name}", stage="read") from exc


def ensure_within(path: Path, parent: Path) -> Path:
    resolved = path.resolve()
    base = parent.resolve()
    try:
        resolved.relative_to(base)
    except ValueError as exc:
        raise OpsError(f"路径不在允许目录内: {path}", stage="validate") from exc
    return resolved


def workspace_fingerprint(repo_root: Path) -> dict:
    """对实际运行/评测相关源码做内容指纹;脏工作区不能只用 HEAD 标识。"""
    roots = [
        "backend/app",
        "backend/scripts",
        "backend/alembic",
        "backend/alembic.ini",
        "backend/Dockerfile",
        "backend/tests",
        "frontend/src",
        "frontend/package.json",
        "frontend/nginx.conf",
        "frontend/Dockerfile",
        "docker-compose.yml",
        "docker-compose.prod.yml",
        "docker-compose.drill.yml",
        ".env.production.example",
        "README.md",
        "docs",
    ]
    skip_parts = {
        "__pycache__",
        "node_modules",
        "dist",
        ".git",
        ".pytest_cache",
        "eval-artifacts",
        "e2e-artifacts",
    }
    digest = hashlib.sha256()
    count = 0
    for rel in roots:
        base = repo_root / rel
        if not base.exists():
            continue
        paths = [base] if base.is_file() else sorted(base.rglob("*"))
        for path in paths:
            if not path.is_file():
                continue
            if any(part in skip_parts for part in path.parts):
                continue
            if path.suffix in (".pyc", ".pyo"):
                continue
            try:
                data = path.read_bytes()
            except OSError:
                continue
            rel_path = path.relative_to(repo_root).as_posix()
            digest.update(rel_path.encode("utf-8"))
            digest.update(b"\0")
            digest.update(data)
            digest.update(b"\0")
            count += 1
    return {
        "workspace_fingerprint": digest.hexdigest(),
        "workspace_file_count": count,
        "fingerprint_roots": roots,
    }


def git_info(repo_root: Path) -> dict:
    info: dict = {"head": "unknown", "branch": "unknown", "status_sha256": None}
    try:
        info["head"] = run(
            ["git", "-C", repo_root, "rev-parse", "HEAD"], check=True
        ).stdout.strip()
        info["branch"] = run(
            ["git", "-C", repo_root, "rev-parse", "--abbrev-ref", "HEAD"], check=True
        ).stdout.strip()
        status = run(
            ["git", "-C", repo_root, "status", "--porcelain=v1", "-uall"], check=True
        ).stdout
        info["status_sha256"] = hashlib.sha256(status.encode("utf-8")).hexdigest()
        info["status_lines"] = len([line for line in status.splitlines() if line.strip()])
    except OpsError:
        pass
    return info


def code_info(repo_root: Path) -> dict:
    return {**git_info(repo_root), **workspace_fingerprint(repo_root)}


def wait_until(predicate, *, timeout: float, interval: float = 1.0, description: str = "条件"):
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except Exception as exc:  # noqa: BLE001
            last_error = exc
        time.sleep(interval)
    suffix = f";最后错误: {type(last_error).__name__}" if last_error else ""
    raise OpsError(f"等待 {description} 超时{suffix}", stage="wait")


def free_tcp_port() -> int:
    """由操作系统分配一个本机空闲端口,避免固定端口跨项目串用。"""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def remove_file_exact(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def copy_tree_limited(src: Path, dst: Path) -> None:
    if dst.exists():
        raise OpsError(f"目标已存在,拒绝覆盖: {dst}", stage="copy")
    shutil.copytree(src, dst)
