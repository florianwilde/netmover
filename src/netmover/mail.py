"""E-Mail-Benachrichtigung per SMTP.

* Verschlüsselung: ``starttls`` (Port 587, Standard), ``ssl`` (Port 465) oder ``none``
  (nur für interne Relays ohne TLS). Zertifikate werden immer geprüft.
* Das Passwort liegt Fernet-verschlüsselt in den Einstellungen.
"""

from __future__ import annotations

import re
import smtplib
import socket
import ssl
from datetime import datetime
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from netmover.settings_store import AppSettings

SECURITY_MODES = ("starttls", "ssl", "none")
_ADDRESS = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")


class MailConfigError(ValueError):
    pass


def parse_recipients(raw: str) -> list[str]:
    recipients = [r.strip() for r in re.split(r"[,;\n]", raw) if r.strip()]
    bad = [r for r in recipients if not _ADDRESS.fullmatch(r)]
    if bad:
        raise MailConfigError(f"Ungültige Adresse: {', '.join(bad)}")
    return recipients


def valid_address(value: str) -> bool:
    return bool(_ADDRESS.fullmatch(value.strip()))


def mail_configured(cfg: AppSettings) -> bool:
    return bool(cfg.email_enabled and cfg.smtp_host and cfg.smtp_from and cfg.smtp_to)


def build_message(cfg: AppSettings, subject: str, body: str) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg.smtp_from
    msg["To"] = ", ".join(parse_recipients(cfg.smtp_to))
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=cfg.smtp_from.rsplit("@", 1)[-1])
    msg["X-Mailer"] = "NetMover"
    footer = (
        f"\n\n-- \nNetMover auf {socket.gethostname()}, "
        f"{datetime.now().astimezone():%d.%m.%Y %H:%M:%S %Z}"
    )
    msg.set_content(body + footer)
    return msg


def tls_context() -> ssl.SSLContext:
    """Zertifikatsprüfung gegen die System-CAs (eigene Funktion, damit Tests ihr
    Test-Zertifikat einsetzen können, ohne die Prüfung abzuschalten)."""
    return ssl.create_default_context()


def smtp_send(
    cfg: AppSettings, password: str | None, subject: str, body: str, timeout: float = 20
) -> None:
    """Verschickt eine Nachricht; Fehler werden als Ausnahme weitergereicht."""
    if cfg.smtp_security not in SECURITY_MODES:
        raise MailConfigError(f"Unbekannte Verschlüsselung: {cfg.smtp_security}")
    msg = build_message(cfg, subject, body)
    context = tls_context()
    smtp: smtplib.SMTP
    if cfg.smtp_security == "ssl":
        smtp = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=timeout, context=context)
    else:
        smtp = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=timeout)
    try:
        smtp.ehlo()
        if cfg.smtp_security == "starttls":
            smtp.starttls(context=context)
            smtp.ehlo()
        if cfg.smtp_user:
            smtp.login(cfg.smtp_user, password or "")
        smtp.send_message(msg)
    finally:
        try:
            smtp.quit()
        except smtplib.SMTPException:
            smtp.close()
