"""异地备份副本每日任务(宿主执行入口)。

用法(宿主,WSL/Linux):
  python3 backend/scripts/offsite_daily.py --env-file .env.production

配置优先级:命令行 > 进程环境变量 > `--env-file` 中的 `OFFSITE_*` 键。
失败时按 `ALERT_EMAIL_TO` 发邮件告警,并返回非零退出码(4),便于监控发现。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from app.ops.common import OpsError  # noqa: E402
from app.ops.offsite_daily import (  # noqa: E402
    OffsiteDailyConfig,
    execute,
    failure_body,
    load_env_file,
    resolve,
    send_failure_alert,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="异地备份副本每日任务")
    parser.add_argument("--env-file", default="", help="环境文件(读取 OFFSITE_*/EMAIL_* 配置)")
    parser.add_argument("--compose-project", default="", help="Compose 项目名,用于定位后端容器")
    parser.add_argument("--container", default="", help="后端容器名(直接指定)")
    parser.add_argument("--container-backup-dir", default="", help="容器内备份目录")
    parser.add_argument("--host", default="", help="异地主机")
    parser.add_argument("--user", default="", help="异地 SSH 用户")
    parser.add_argument("--remote-dir", default="", help="异地绝对路径目录")
    parser.add_argument("--port", default="", help="SSH 端口")
    parser.add_argument("--identity", default="", help="SSH 私钥路径")
    parser.add_argument("--keep", default="", help="异地保留份数")
    parser.add_argument("--max-age-hours", default="", help="备份超过该小时数则跳过")
    parser.add_argument("--state-file", default="", help="幂等状态文件路径")
    parser.add_argument("--report-dir", default="", help="操作报告输出目录")
    parser.add_argument("--force", action="store_true", help="忽略幂等状态,强制再复制一次")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    env = load_env_file(Path(args.env_file) if args.env_file else None)
    state_default = str(Path.home() / ".opssop" / "offsite-state.json")
    report_default = str(Path.home() / ".opssop" / "reports")
    config = OffsiteDailyConfig(
        project=resolve("OFFSITE_COMPOSE_PROJECT", args.compose_project, env, "opssop-prod-rehearsal"),
        backup_dir=resolve("OFFSITE_BACKUP_DIR", args.container_backup_dir, env, "/data/OpsSop/BackUp"),
        container=resolve("OFFSITE_CONTAINER", args.container, env),
        host=resolve("OFFSITE_HOST", args.host, env),
        user=resolve("OFFSITE_USER", args.user, env),
        remote_dir=resolve("OFFSITE_REMOTE_DIR", args.remote_dir, env),
        port=int(resolve("OFFSITE_PORT", args.port, env, "22")),
        identity=resolve("OFFSITE_IDENTITY", args.identity, env),
        keep=int(resolve("OFFSITE_KEEP", args.keep, env, "14")),
        max_age_hours=float(resolve("OFFSITE_MAX_AGE_HOURS", args.max_age_hours, env, "30")),
        state_file=resolve("OFFSITE_STATE_FILE", args.state_file, env, state_default),
        report_dir=resolve("OFFSITE_REPORT_DIR", args.report_dir, env, report_default),
        force=args.force,
        env=env,
    )
    try:
        result = execute(config)
    except OpsError as exc:
        print(f"异地副本失败: {exc}", flush=True)
        send_failure_alert(env, "OpsSOP 异地副本失败", failure_body(exc))
        return 4
    except Exception as exc:  # noqa: BLE001
        print(f"异地副本异常: {type(exc).__name__}: {exc}", flush=True)
        send_failure_alert(env, "OpsSOP 异地副本失败", failure_body(exc))
        return 4

    status = result.get("status")
    if status == "copied":
        report = result["report"]
        print(
            f"异地副本完成: {result['backup']} -> {config.user}@{config.host}:{config.remote_dir}"
            f"(sha256_verified={report['sha256_verified']}, 清理={report['remote_removed']})",
            flush=True,
        )
    elif status == "skipped":
        print(f"{result.get('backup')}: {result.get('reason')},跳过", flush=True)
    elif status == "no_backup":
        print(f"{result.get('container')}: 没有找到备份文件,跳过", flush=True)
    else:
        print(f"跳过: {result.get('reason')}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
