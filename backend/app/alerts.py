"""备份告警：失败或长期无成功备份时通过邮件接口通知管理员。

只在 `EMAIL_MODE=smtp` 且配置了 `ALERT_EMAIL_TO` 时发送；发送失败只记日志，
不影响备份、维护或业务请求。邮件内容只包含备份名、时间、触发方式和已截断
的失败原因，不含口令、连接串或堆栈。
"""

from __future__ import annotations

import logging

from app.config import Settings
from app.mailer import MailMessage, build_mailer

logger = logging.getLogger("opssop.alerts")

# 超过该时长没有成功备份即视为过期(每日任务在 02:00 前后执行,留出余量)
STALE_AFTER_HOURS = 26


def alert_recipients(settings: Settings) -> list[str]:
    raw = (settings.alert_email_to or "").replace(";", ",")
    return [item.strip() for item in raw.split(",") if item.strip()]


def send_alert(settings: Settings, subject: str, body: str) -> bool:
    """按收件人逐个发送;全部成功才返回 True。未启用/未配置时返回 False。"""
    recipients = alert_recipients(settings)
    if settings.email_mode != "smtp" or not recipients:
        logger.info("备份告警未发送(EMAIL_MODE=%s, 收件人数=%s)", settings.email_mode, len(recipients))
        return False
    mailer = build_mailer(settings)
    results = [mailer.send(MailMessage(to=to, subject=subject, body=body)) for to in recipients]
    return all(results)


def notify_backup_failure(settings: Settings, reason: str, trigger: str) -> bool:
    trigger_label = {"scheduled": "每日定时", "manual": "管理员手动"}.get(trigger, trigger or "未知")
    return send_alert(
        settings,
        "OpsSOP 备份失败",
        "\n".join(
            [
                "OpsSOP 平台备份执行失败。",
                "",
                f"触发方式：{trigger_label}",
                f"失败原因：{(reason or '未知')[:300]}",
                "",
                "服务仍在运行，请检查备份目录、数据库连接与磁盘空间，",
                "并在「系统管理 → 数据备份」手动重试。",
            ]
        ),
    )


def notify_backup_stale(settings: Settings, last_success_at: str | None, age_hours: float | None) -> bool:
    if not last_success_at:
        detail = "尚未记录到任何成功备份。"
    else:
        detail = f"最近成功备份：{last_success_at}（约 {age_hours} 小时前）。"
    return send_alert(
        settings,
        "OpsSOP 备份已超过 26 小时未成功",
        "\n".join(
            [
                "OpsSOP 平台备份超过预期时间没有成功记录。",
                "",
                detail,
                "",
                "请检查备份任务、数据库连接与磁盘空间；",
                "在「系统管理 → 数据备份」可查看最近失败原因并手动重试。",
            ]
        ),
    )