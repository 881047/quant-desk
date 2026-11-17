"""Outbound mail, with QQ Mail as the default carrier.

Configuration lives in `.env` — three values, no code change:

    QQMAIL_USER=123456@qq.com
    QQMAIL_AUTH_CODE=xxxxxxxxxxxxxxxx      # 授权码, NOT the account password
    QQMAIL_TO=123456@qq.com

The 授权码 is generated in QQ Mail → 设置 → 账户 → POP3/SMTP 服务 → 生成授权码.
Using the login password here fails with `535 Error: authentication failed`, which
is the single most common way this breaks, so the error message says so instead of
re-raising the SMTP text.

Nothing here retries in a loop: a mail that cannot be sent is reported to the
caller, which decides whether the alert is worth keeping for the next run.
"""

from __future__ import annotations

import os
import smtplib
import ssl
from dataclasses import dataclass
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate

PROVIDERS = {
    "qq": {"host": "smtp.qq.com", "port": 465, "ssl": True},
    "qq-587": {"host": "smtp.qq.com", "port": 587, "ssl": False},
    "163": {"host": "smtp.163.com", "port": 465, "ssl": True},
    "gmail": {"host": "smtp.gmail.com", "port": 465, "ssl": True},
}


class MailNotConfigured(RuntimeError):
    """Raised with the exact missing key, not a generic auth failure."""


class MailDeliveryUnknown(RuntimeError):
    """SMTP accepted the message, but the connection failed afterwards.

    Retrying this case can deliver a duplicate, so callers must not treat it as
    an ordinary pre-delivery failure.
    """


@dataclass
class MailConfig:
    host: str
    port: int
    use_ssl: bool
    user: str
    secret: str
    sender: str
    recipients: list[str]

    @property
    def configured(self) -> bool:
        return bool(self.user and self.secret and self.recipients)


def load_config() -> MailConfig:
    """Env → config. QQ keys win over the generic ones because they carry the
    provider preset (host/port/SSL) that people get wrong by hand."""
    provider = os.getenv("MAIL_PROVIDER", "qq").strip().lower()
    preset = PROVIDERS.get(provider, PROVIDERS["qq"])

    user = (os.getenv("QQMAIL_USER") or os.getenv("SMTP_USER") or "").strip()
    secret = (os.getenv("QQMAIL_AUTH_CODE") or os.getenv("SMTP_PASS") or "").strip()
    sender = (os.getenv("MAIL_FROM") or user).strip()
    raw_to = (os.getenv("QQMAIL_TO") or os.getenv("MAIL_TO") or sender).strip()
    recipients = [item.strip() for item in raw_to.replace(";", ",").split(",") if item.strip()]

    host = (os.getenv("SMTP_HOST") or preset["host"]).strip()
    port = int(os.getenv("SMTP_PORT") or preset["port"])
    use_ssl = (os.getenv("SMTP_SSL") or str(preset["ssl"])).strip().lower() in ("1", "true", "yes")
    return MailConfig(host=host, port=port, use_ssl=use_ssl, user=user,
                      secret=secret, sender=sender, recipients=recipients)


def send_mail(subject: str, text: str, html: str | None = None, *,
              to: list[str] | None = None, config: MailConfig | None = None,
              message_id: str | None = None) -> dict:
    """Send one message. Returns what happened; raises only when unconfigured.

    ``message_id`` is for callers that can name the message before sending it.
    A deterministic id makes a duplicate of the same logical mail recognisable
    to the recipient, which no header-less retry can be.
    """
    cfg = config or load_config()
    if not cfg.configured:
        raise MailNotConfigured(
            "邮件未配置：在 .env 里填 QQMAIL_USER、QQMAIL_AUTH_CODE（授权码，不是登录密码）、"
            "QQMAIL_TO。授权码在 QQ 邮箱 → 设置 → 账户 → POP3/SMTP 服务 里生成。")

    recipients = to or cfg.recipients
    message = MIMEMultipart("alternative")
    message["Subject"] = Header(subject, "utf-8")
    message["From"] = formataddr(("quant-desk", cfg.sender or cfg.user))
    message["To"] = ", ".join(recipients)
    message["Date"] = formatdate(localtime=True)
    if message_id:
        message["Message-ID"] = (message_id if message_id.startswith("<")
                                 else f"<{message_id}>")
    message.attach(MIMEText(text, "plain", "utf-8"))
    if html:
        message.attach(MIMEText(html, "html", "utf-8"))

    accepted = False
    sending = False
    try:
        if cfg.use_ssl:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(cfg.host, cfg.port, context=context, timeout=20) as server:
                server.login(cfg.user, cfg.secret)
                sending = True
                server.sendmail(cfg.sender or cfg.user, recipients, message.as_string())
                accepted = True
        else:
            with smtplib.SMTP(cfg.host, cfg.port, timeout=20) as server:
                server.starttls(context=ssl.create_default_context())
                server.login(cfg.user, cfg.secret)
                sending = True
                server.sendmail(cfg.sender or cfg.user, recipients, message.as_string())
                accepted = True
    except smtplib.SMTPAuthenticationError as exc:
        raise MailNotConfigured(
            f"{cfg.host} 拒绝了登录（{exc.smtp_code}）。QQ 邮箱要用**授权码**，"
            "不是 QQ 登录密码；授权码在 设置 → 账户 → POP3/SMTP 服务 里生成。") from exc
    except (smtplib.SMTPException, OSError) as exc:
        if accepted:
            raise MailDeliveryUnknown(
                f"SMTP 已接受邮件，但随后连接异常（{type(exc).__name__}: {exc}）；"
                "为避免重复投递，不会自动重发") from exc
        if sending and isinstance(exc, (smtplib.SMTPServerDisconnected, OSError)):
            raise MailDeliveryUnknown(
                f"SMTP 投递过程中连接中断，服务器可能已接受邮件"
                f"（{type(exc).__name__}: {exc}）；为避免重复投递，不会自动重发") from exc
        raise RuntimeError(f"发信失败：{type(exc).__name__}: {exc}") from exc

    return {"sent": True, "to": recipients, "subject": subject, "host": cfg.host,
            "message_id": message.get("Message-ID")}
