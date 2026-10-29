"""邮件发送接口预留。

当前默认 `EMAIL_MODE=disabled`，只记录日志、不对外发信；注册/找回密码等
流程可在后续接入 SMTP 时复用同一接口。`EMAIL_MODE=smtp` 使用标准库
smtplib，不引入额外依赖；发送失败返回 False，由调用方决定是否重试。
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Protocol

from app.config import Settings

logger = logging.getLogger("opssop.mail")


@dataclass(frozen=True)
class MailMessage:
    to: str
    subject: str
    body: str


class Mailer(Protocol):
    def send(self, message: MailMessage) -> bool: ...


class DisabledMailer:
    """未启用邮件时的占位实现：只记录元信息，不发送。"""

    def send(self, message: MailMessage) -> bool:
        logger.info(
            "邮件未启用(EMAIL_MODE=disabled)，跳过发送: to=%s subject=%s",
            message.to,
            message.subject,
        )
        return False


class SmtpMailer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def send(self, message: MailMessage) -> bool:
        email = EmailMessage()
        email["From"] = self.settings.email_from
        email["To"] = message.to
        email["Subject"] = message.subject
        email.set_content(message.body)
        try:
            context = ssl.create_default_context()
            if self.settings.email_use_ssl:
                # 465 隐式 SSL(网易/QQ 等常见要求);此时不再 STARTTLS
                client_cm = smtplib.SMTP_SSL(
                    self.settings.email_host,
                    self.settings.email_port,
                    timeout=15,
                    context=context,
                )
            else:
                client_cm = smtplib.SMTP(
                    self.settings.email_host,
                    self.settings.email_port,
                    timeout=15,
                )
            with client_cm as client:
                if not self.settings.email_use_ssl and self.settings.email_use_tls:
                    client.starttls(context=context)
                if self.settings.email_username:
                    client.login(self.settings.email_username, self.settings.email_password)
                client.send_message(email)
            return True
        except Exception as exc:  # noqa: BLE001 - 邮件失败不应泄露口令或中断业务
            logger.warning("发送邮件失败: %s", type(exc).__name__)
            return False


def build_mailer(settings: Settings) -> Mailer:
    if settings.email_mode == "smtp":
        return SmtpMailer(settings)
    return DisabledMailer()


def send_registration_code(settings: Settings, to: str, code: str) -> bool:
    """预留的注册验证码发送入口，当前仅走邮件接口。"""
    return build_mailer(settings).send(
        MailMessage(to=to, subject="OpsSOP 注册验证码", body=f"验证码：{code}\n如非本人操作请忽略。")
    )
