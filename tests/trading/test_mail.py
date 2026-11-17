"""Mail: unconfigured must fail with the fix, not with an SMTP code."""

from __future__ import annotations

import pytest

from service.notify import mail


def test_missing_configuration_names_the_three_keys(monkeypatch):
    for key in ("QQMAIL_USER", "QQMAIL_AUTH_CODE", "QQMAIL_TO",
                "SMTP_USER", "SMTP_PASS", "MAIL_FROM", "MAIL_TO"):
        monkeypatch.delenv(key, raising=False)
    cfg = mail.load_config()
    assert cfg.configured is False
    with pytest.raises(mail.MailNotConfigured) as excinfo:
        mail.send_mail("x", "y")
    message = str(excinfo.value)
    assert "QQMAIL_USER" in message and "授权码" in message


def test_qq_preset_fills_host_and_port(monkeypatch):
    monkeypatch.setenv("QQMAIL_USER", "someone@qq.com")
    monkeypatch.setenv("QQMAIL_AUTH_CODE", "abcd1234")
    monkeypatch.delenv("QQMAIL_TO", raising=False)
    monkeypatch.delenv("MAIL_PROVIDER", raising=False)
    cfg = mail.load_config()
    assert (cfg.host, cfg.port, cfg.use_ssl) == ("smtp.qq.com", 465, True)
    assert cfg.recipients == ["someone@qq.com"], "QQMAIL_TO 缺省时发给自己"


def test_multiple_recipients_accept_commas_and_semicolons(monkeypatch):
    monkeypatch.setenv("QQMAIL_USER", "a@qq.com")
    monkeypatch.setenv("QQMAIL_AUTH_CODE", "x")
    monkeypatch.setenv("QQMAIL_TO", "b@qq.com; c@qq.com, d@qq.com")
    cfg = mail.load_config()
    assert cfg.recipients == ["b@qq.com", "c@qq.com", "d@qq.com"]


def test_authentication_failure_explains_the_auth_code(monkeypatch):
    """QQ rejects the login password with 535; the message must say why."""
    monkeypatch.setenv("QQMAIL_USER", "a@qq.com")
    monkeypatch.setenv("QQMAIL_AUTH_CODE", "wrong")
    monkeypatch.setenv("QQMAIL_TO", "a@qq.com")

    import smtplib

    class _Failing:
        def __init__(self, *a, **k):
            raise smtplib.SMTPAuthenticationError(535, b"Error: authentication failed")

    monkeypatch.setattr(mail.smtplib, "SMTP_SSL", _Failing)
    with pytest.raises(mail.MailNotConfigured) as excinfo:
        mail.send_mail("x", "y")
    assert "授权码" in str(excinfo.value)


def test_send_builds_a_message_with_both_parts(monkeypatch):
    monkeypatch.setenv("QQMAIL_USER", "a@qq.com")
    monkeypatch.setenv("QQMAIL_AUTH_CODE", "ok")
    monkeypatch.setenv("QQMAIL_TO", "b@qq.com")
    captured = {}

    class _Fake:
        def __init__(self, host, port, context=None, timeout=None):
            captured["host"] = host

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def login(self, user, secret):
            captured["login"] = (user, secret)

        def sendmail(self, sender, to, body):
            captured["body"] = body

    monkeypatch.setattr(mail.smtplib, "SMTP_SSL", _Fake)
    result = mail.send_mail("主题", "纯文本", "<b>html</b>")
    assert result["sent"] is True
    assert captured["host"] == "smtp.qq.com"
    assert "multipart/alternative" in captured["body"]
    assert "text/plain" in captured["body"] and "text/html" in captured["body"]


def test_connection_failure_after_acceptance_is_not_a_retryable_failure(monkeypatch):
    monkeypatch.setenv("QQMAIL_USER", "a@qq.com")
    monkeypatch.setenv("QQMAIL_AUTH_CODE", "ok")
    monkeypatch.setenv("QQMAIL_TO", "b@qq.com")

    class _AcceptedThenDisconnected:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            raise OSError("connection lost while closing")

        def login(self, user, secret):
            pass

        def sendmail(self, sender, to, body):
            return {}

    monkeypatch.setattr(mail.smtplib, "SMTP_SSL", _AcceptedThenDisconnected)
    with pytest.raises(mail.MailDeliveryUnknown, match="已接受"):
        mail.send_mail("x", "y")


def test_connection_failure_before_acceptance_remains_retryable(monkeypatch):
    monkeypatch.setenv("QQMAIL_USER", "a@qq.com")
    monkeypatch.setenv("QQMAIL_AUTH_CODE", "ok")
    monkeypatch.setenv("QQMAIL_TO", "b@qq.com")

    class _DisconnectedBeforeAcceptance:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def login(self, user, secret):
            raise OSError("connection lost before acceptance")

        def sendmail(self, sender, to, body):
            raise AssertionError("login failed; sendmail must not run")

    monkeypatch.setattr(mail.smtplib, "SMTP_SSL", _DisconnectedBeforeAcceptance)
    with pytest.raises(RuntimeError, match="发信失败") as excinfo:
        mail.send_mail("x", "y")
    assert not isinstance(excinfo.value, mail.MailDeliveryUnknown)


def test_lost_response_during_sendmail_is_unknown_and_not_blindly_retried(monkeypatch):
    monkeypatch.setenv("QQMAIL_USER", "a@qq.com")
    monkeypatch.setenv("QQMAIL_AUTH_CODE", "ok")
    monkeypatch.setenv("QQMAIL_TO", "b@qq.com")

    class _ResponseLost:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def login(self, user, secret):
            pass

        def sendmail(self, sender, to, body):
            raise mail.smtplib.SMTPServerDisconnected("response lost after DATA")

    monkeypatch.setattr(mail.smtplib, "SMTP_SSL", _ResponseLost)
    with pytest.raises(mail.MailDeliveryUnknown, match="可能已接受"):
        mail.send_mail("x", "y")


def test_a_named_message_carries_a_deterministic_message_id(monkeypatch):
    monkeypatch.setenv("QQMAIL_USER", "a@qq.com")
    monkeypatch.setenv("QQMAIL_AUTH_CODE", "ok")
    monkeypatch.setenv("QQMAIL_TO", "b@qq.com")
    captured = {}

    class _Fake:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def login(self, user, secret):
            pass

        def sendmail(self, sender, to, body):
            captured["body"] = body

    monkeypatch.setattr(mail.smtplib, "SMTP_SSL", _Fake)
    result = mail.send_mail("主题", "纯文本",
                            message_id="<20261029-093012@quant-desk.local>")

    assert result["message_id"] == "<20261029-093012@quant-desk.local>"
    assert "Message-ID: <20261029-093012@quant-desk.local>" in captured["body"], (
        "收件端要靠这个头把重复投递折叠成同一封")
