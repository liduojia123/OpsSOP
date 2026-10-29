"""异地备份副本:把成对的 .dump/.json 复制到另一台主机并校验 SHA-256。

用于补齐"备份只落在数据库同机同盘"的风险:每日备份完成后把加密或原始归档
复制到独立主机/挂载盘,并从副本做过一次恢复验证。

依赖系统 `ssh` / `scp` 与免交互公钥认证;应用和本工具都不保存远端口令。
远端目录只允许绝对路径和安全字符,所有远端路径经 shell 引号转义;
远端保留最近 `--keep` 份,更早的成对文件按修改时间删除。
"""

from __future__ import annotations

import re
import shlex
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.ops.common import OpsError, atomic_write_json, sha256_file

_HOST_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")
_USER_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_REMOTE_DIR_RE = re.compile(r"^/[A-Za-z0-9._/-]+$")
_NAME_RE = re.compile(r"^opssop-\d{8}T\d{6}Z$")


@dataclass(frozen=True)
class OffsiteTarget:
    host: str
    user: str
    remote_dir: str
    port: int = 22
    identity: str = ""

    def validate(self) -> None:
        if not _HOST_RE.fullmatch(self.host or ""):
            raise OpsError(f"远端主机名不合法: {self.host!r}", stage="validate")
        if not _USER_RE.fullmatch(self.user or ""):
            raise OpsError(f"远端用户名不合法: {self.user!r}", stage="validate")
        if ".." in (self.remote_dir or "") or not _REMOTE_DIR_RE.fullmatch(self.remote_dir or ""):
            raise OpsError(
                f"远端目录必须是绝对路径且只含安全字符: {self.remote_dir!r}", stage="validate"
            )
        if not 1 <= int(self.port) <= 65535:
            raise OpsError(f"SSH 端口不合法: {self.port}", stage="validate")
        if self.identity and not Path(self.identity).expanduser().is_file():
            raise OpsError(f"SSH 私钥不存在: {self.identity}", stage="validate")


def _remote(target: OffsiteTarget) -> str:
    return f"{target.user}@{target.host}"


def _run(cmd: list[str], *, timeout: int = 300) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise OpsError(
            f"未找到 {cmd[0]};异地备份需要系统 ssh/scp 客户端", stage="validate"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise OpsError("远端操作超时", stage="offsite") from exc


def _require_ok(proc: subprocess.CompletedProcess, action: str) -> None:
    if proc.returncode == 0:
        return
    lines = (proc.stderr or proc.stdout or "").strip().splitlines()
    detail = lines[-1][:200] if lines else f"exit {proc.returncode}"
    raise OpsError(f"{action}失败: {detail}", stage="offsite")


def _ssh(target: OffsiteTarget, command: str, *, timeout: int = 120) -> subprocess.CompletedProcess:
    cmd = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-p",
        str(target.port),
    ]
    if target.identity:
        cmd += ["-i", str(Path(target.identity).expanduser())]
    cmd += [_remote(target), command]
    return _run(cmd, timeout=timeout)


def _scp(target: OffsiteTarget, local: Path, remote: str, *, timeout: int = 600) -> subprocess.CompletedProcess:
    cmd = [
        "scp",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-P",
        str(target.port),
    ]
    if target.identity:
        cmd += ["-i", str(Path(target.identity).expanduser())]
    cmd += [str(local), f"{_remote(target)}:{remote}"]
    return _run(cmd, timeout=timeout)


def parse_sha256sum(output: str) -> dict[str, str]:
    """解析 `sha256sum` 输出为 {文件名: 摘要}。"""
    result: dict[str, str] = {}
    for line in output.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2 or not re.fullmatch(r"[0-9a-f]{64}", parts[0]):
            continue
        result[Path(parts[1].lstrip("*")).name] = parts[0]
    return result


def expired_names(dump_names: list[str], keep: int) -> list[str]:
    """输入按修改时间倒序的 dump 名,返回超出保留份数、应删除的名字。"""
    if keep <= 0:
        return []
    return [name for name in dump_names[keep:] if _NAME_RE.fullmatch(name)]


def _validate_pair(archive: Path, metadata: Path) -> str:
    if not archive.is_file():
        raise OpsError(f"归档不存在: {archive}", stage="validate")
    if not metadata.is_file():
        raise OpsError(f"元信息不存在: {metadata}", stage="validate")
    name = archive.name[: -len(".dump")] if archive.name.endswith(".dump") else ""
    if not _NAME_RE.fullmatch(name) or metadata.name != f"{name}.json":
        raise OpsError("归档与元信息必须按 opssop-<时间>.dump/.json 成对提供", stage="validate")
    try:
        import json

        expected = json.loads(metadata.read_text(encoding="utf-8")).get("sha256", "")
    except (OSError, ValueError) as exc:
        raise OpsError("元信息不是合法 JSON", stage="validate") from exc
    actual = sha256_file(archive)
    if expected and expected != actual:
        raise OpsError("归档 SHA-256 与元信息不一致,拒绝复制", stage="validate")
    return actual


def run_offsite(
    archive: Path,
    metadata: Path,
    target: OffsiteTarget,
    *,
    keep: int = 14,
    out_dir: Path | None = None,
) -> dict:
    target.validate()
    digest = _validate_pair(archive, metadata)
    remote_dir = shlex.quote(target.remote_dir)

    proc = _ssh(target, f"mkdir -p {remote_dir} && chmod 700 {remote_dir}")
    _require_ok(proc, "创建远端目录")

    uploaded: list[str] = []
    for local in (archive, metadata):
        final = f"{target.remote_dir}/{local.name}"
        temp = f"{target.remote_dir}/.{local.name}.tmp"
        _require_ok(_scp(target, local, temp), f"复制 {local.name}")
        _require_ok(
            _ssh(
                target,
                f"mv -f {shlex.quote(temp)} {shlex.quote(final)} && chmod 600 {shlex.quote(final)}",
            ),
            f"落位 {local.name}",
        )
        uploaded.append(final)

    proc = _ssh(
        target,
        "sha256sum "
        + " ".join(shlex.quote(f"{target.remote_dir}/{f.name}") for f in (archive, metadata)),
    )
    _require_ok(proc, "远端校验")
    remote_hashes = parse_sha256sum(proc.stdout)
    verified = (
        remote_hashes.get(archive.name) == digest
        and remote_hashes.get(metadata.name, "") == sha256_file(metadata)
    )
    if not verified:
        raise OpsError("远端 SHA-256 校验不一致", stage="offsite")

    removed: list[str] = []
    if keep > 0:
        listing = _ssh(
            target,
            f"cd {remote_dir} && ls -1t opssop-*.dump 2>/dev/null || true",
        )
        _require_ok(listing, "读取远端备份列表")
        names = [Path(line.strip()).name[: -len(".dump")] for line in listing.stdout.splitlines() if line.strip()]
        stale = expired_names(names, keep)
        if stale:
            files = []
            for name in stale:
                files.extend([f"{target.remote_dir}/{name}.dump", f"{target.remote_dir}/{name}.json"])
            _require_ok(
                _ssh(target, "rm -f " + " ".join(shlex.quote(f) for f in files)),
                "清理远端过期备份",
            )
            removed = stale

    report = {
        "success": True,
        "target": {
            "host": target.host,
            "user": target.user,
            "remote_dir": target.remote_dir,
            "port": target.port,
        },
        "files": [f.name for f in (archive, metadata)],
        "sha256": digest,
        "sha256_verified": verified,
        "remote_removed": removed,
        "keep": keep,
        "completed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if out_dir is not None:
        atomic_write_json(out_dir / f"offsite-{archive.name[: -len('.dump')]}.json", report)
    return report