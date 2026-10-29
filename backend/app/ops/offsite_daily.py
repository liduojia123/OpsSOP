"""异地备份副本每日任务的核心逻辑。

平台备份在容器内完成(`pg_dump` → `BACKUP_DIR`),本模块在宿主侧取件并复制到
另一台主机:通过 `docker exec`/`docker cp` 读取最新成对备份(不依赖宿主目录
权限),再复用 `app.ops.offsite` 做 SSH 传输、远端 SHA-256 校验与保留清理。

设计要点:
- 幂等:状态文件记录已复制的备份名,同一备份只复制一次,可安全按小时调度;
- 过期保护:备份超过 `max_age_hours` 不复制,避免把陈旧备份当新备份上传;
- 失败可观测:抛出 `OpsError`,由调用方发邮件告警并返回非零退出码;
- 只依赖标准库与系统 `docker`/`ssh`/`scp`,宿主无需安装 Python 第三方包。
"""

from __future__ import annotations

import json
import os
import shlex
import smtplib
import ssl
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

from app.ops.common import OpsError, atomic_write_json
from app.ops.offsite import OffsiteTarget, run_offsite

DEFAULT_PROJECT = "opssop-prod-rehearsal"
DEFAULT_BACKUP_DIR = "/data/OpsSop/BackUp"


@dataclass(frozen=True)
class OffsiteDailyConfig:
    """每日任务配置;`env` 保存环境文件里的键值(告警用)。"""

    project: str = DEFAULT_PROJECT
    backup_dir: str = DEFAULT_BACKUP_DIR
    container: str = ""
    host: str = ""
    user: str = ""
    remote_dir: str = ""
    port: int = 22
    identity: str = ""
    keep: int = 14
    max_age_hours: float = 30.0
    state_file: str = ""
    report_dir: str = ""
    force: bool = False
    env: dict[str, str] = field(default_factory=dict)

    @property
    def target(self) -> OffsiteTarget:
        return OffsiteTarget(
            host=self.host,
            user=self.user,
            remote_dir=self.remote_dir,
            port=self.port,
            identity=self.identity,
        )


def load_env_file(path: Path | None) -> dict[str, str]:
    """读取 KEY=VALUE 环境文件(忽略注释与空行),不展开变量。"""
    values: dict[str, str] = {}
    if not path:
        return values
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return values
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def resolve(name: str, cli_value: str | None, env: dict[str, str], fallback: str = "") -> str:
    """配置优先级:命令行 > 进程环境变量 > 环境文件。"""
    if cli_value:
        return cli_value
    if name in os.environ:
        return os.environ[name]
    return env.get(name, fallback)


def run(cmd: list[str], *, timeout: int = 300) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise OpsError(f"未找到命令 {cmd[0]}", stage="offsite") from exc
    except subprocess.TimeoutExpired as exc:
        raise OpsError(f"命令超时: {cmd[0]}", stage="offsite") from exc


def find_container(project: str, explicit: str = "") -> str:
    """定位运行平台备份的后端容器。"""
    if explicit:
        return explicit
    proc = run(
        [
            "docker",
            "ps",
            "--filter",
            f"label=com.docker.compose.project={project}",
            "--format",
            "{{.Names}}",
        ]
    )
    names = [line.strip() for line in proc.stdout.splitlines() if "backend" in line]
    if not names:
        raise OpsError(f"未找到 {project} 的后端容器(请用 --container 指定)", stage="offsite")
    return names[0]


def find_newest_pair(container: str, backup_dir: str) -> tuple[str, str] | None:
    """容器内最新的 (.dump, .json) 文件名对;没有备份时返回 None。"""
    pattern = f"{shlex.quote(backup_dir)}/opssop-*.dump"
    proc = run(["docker", "exec", container, "sh", "-c", f"ls -1t {pattern} 2>/dev/null | head -1"])
    lines = proc.stdout.strip().splitlines()
    if not lines:
        return None
    name = Path(lines[0]).name[: -len(".dump")]
    if not name:
        return None
    return f"{name}.dump", f"{name}.json"


def backup_age_hours(container: str, backup_dir: str, dump_name: str) -> float | None:
    """备份文件在容器内的年龄(小时);取不到时返回 None。"""
    proc = run(["docker", "exec", container, "stat", "-c", "%Y", f"{backup_dir}/{dump_name}"])
    try:
        mtime = int(proc.stdout.strip())
    except ValueError:
        return None
    return round((time.time() - mtime) / 3600, 2)


def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def send_mail(env: dict[str, str], subject: str, body: str) -> bool:
    """纯标准库发信(宿主 python3 不装第三方依赖)。

    与应用内 `app/mailer.py` 行为一致:`EMAIL_MODE=smtp` 时按
    `EMAIL_USE_SSL`(465 隐式 SSL)/`EMAIL_USE_TLS`(587 STARTTLS)选择连接方式。
    """
    if env.get("EMAIL_MODE", "disabled").strip() != "smtp":
        return False
    host = env.get("EMAIL_HOST", "").strip()
    sender = env.get("EMAIL_FROM", "").strip()
    recipients = [
        item.strip()
        for item in env.get("ALERT_EMAIL_TO", "").replace(";", ",").split(",")
        if item.strip()
    ]
    if not (host and sender and recipients):
        return False
    port = int(env.get("EMAIL_PORT", "587") or 587)
    use_ssl = env.get("EMAIL_USE_SSL", "false").strip().lower() in ("1", "true", "yes")
    use_tls = env.get("EMAIL_USE_TLS", "true").strip().lower() in ("1", "true", "yes")
    username = env.get("EMAIL_USERNAME", "").strip()
    password = env.get("EMAIL_PASSWORD", "")
    for to in recipients:
        message = EmailMessage()
        message["From"] = sender
        message["To"] = to
        message["Subject"] = subject
        message.set_content(body)
        try:
            if use_ssl:
                with smtplib.SMTP_SSL(host, port, timeout=15, context=ssl.create_default_context()) as client:
                    if username:
                        client.login(username, password)
                    client.send_message(message)
            else:
                with smtplib.SMTP(host, port, timeout=15) as client:
                    if use_tls:
                        client.starttls(context=ssl.create_default_context())
                    if username:
                        client.login(username, password)
                    client.send_message(message)
        except Exception as exc:  # noqa: BLE001 - 告警失败不影响主流程退出码
            print(f"告警发送失败({to}): {type(exc).__name__}", flush=True)
            return False
    return True


def send_failure_alert(env: dict[str, str], subject: str, body: str) -> bool:
    """失败告警;未配置收件人时只记日志。"""
    if env.get("ALERT_EMAIL_TO", "").strip() in ("", "0", "false"):
        return False
    return send_mail(env, subject, body)


def execute(config: OffsiteDailyConfig) -> dict:
    """执行一轮:取件 → 异地复制 → 记录状态。返回结果字典。

    状态取值:`noop`(未配置目标)、`no_backup`(容器内无备份)、
    `skipped`(已复制或备份过旧)、`copied`(本次完成复制)。
    """
    if not (config.host and config.user and config.remote_dir):
        return {"status": "noop", "reason": "未配置异地目标"}
    target = config.target
    target.validate()

    state_path = Path(config.state_file)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    report_dir = Path(config.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    state = load_state(state_path)

    container = find_container(config.project, config.container)
    pair = find_newest_pair(container, config.backup_dir)
    if not pair:
        return {"status": "no_backup", "container": container}
    dump_name, meta_name = pair
    if not config.force and state.get("last_copied") == dump_name:
        return {"status": "skipped", "reason": "已复制过", "backup": dump_name, "container": container}

    age = backup_age_hours(container, config.backup_dir, dump_name)
    if age is not None and age > config.max_age_hours:
        return {"status": "skipped", "reason": "备份过旧", "backup": dump_name, "age_hours": age}

    staging = state_path.parent / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    archive = staging / dump_name
    metadata = staging / meta_name
    for source, destination in ((dump_name, archive), (meta_name, metadata)):
        proc = run(
            ["docker", "cp", f"{container}:{config.backup_dir}/{source}", str(destination)],
            timeout=600,
        )
        if proc.returncode != 0:
            raise OpsError(f"docker cp {source} 失败: {(proc.stderr or '').strip()[:200]}", stage="offsite")

    report = run_offsite(archive, metadata, target, keep=config.keep, out_dir=report_dir)
    report["container"] = container
    report["backup_age_hours"] = age
    atomic_write_json(
        state_path,
        {"last_copied": dump_name, "last_copied_at": report.get("completed_at")},
    )
    for path in (archive, metadata):
        path.unlink(missing_ok=True)
    return {"status": "copied", "report": report, "backup": dump_name, "container": container}


def failure_body(exc: Exception) -> str:
    return "\n".join(
        [
            "每日异地备份副本任务执行失败。",
            "",
            f"原因：{str(exc)[:300]}",
            f"时间：{datetime.now(timezone.utc).isoformat(timespec='seconds')}",
            "",
            "请检查宿主机备份目录、SSH 密钥与异地主机连通性。",
        ]
    )
