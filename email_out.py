"""Outbound e-mail — the "Телефон" bridge for iPhone email automations.

On the "Телефон" SOS option the bot e-mails the owner's inbox with subject
``SOS: <contact>`` and the message in the body. An iOS Shortcuts *email*
automation ("Выполнять сразу") then calls / texts that contact hands-free.

Uses the standard library SMTP client (no extra dependency), run off the
event loop. Enabled only when SMTP_HOST + SMTP_FROM are configured.
"""

import asyncio
import logging
import smtplib
import ssl
from email.message import EmailMessage

from config import settings

logger = logging.getLogger(__name__)


def enabled() -> bool:
    return bool(settings.smtp_host and settings.smtp_from)


def _send_sync(to: str, subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["From"] = settings.smtp_from
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)

    ctx = ssl.create_default_context()
    if settings.smtp_ssl:
        with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, context=ctx, timeout=15) as s:
            if settings.smtp_user:
                s.login(settings.smtp_user, settings.smtp_password or "")
            s.send_message(msg)
    else:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as s:
            try:
                s.starttls(context=ctx)
            except smtplib.SMTPException:
                pass  # server without STARTTLS
            if settings.smtp_user:
                s.login(settings.smtp_user, settings.smtp_password or "")
            s.send_message(msg)


async def send(to: str, subject: str, body: str) -> bool:
    """Send an e-mail. Never raises; returns True on success."""
    if not enabled() or not to:
        return False
    try:
        await asyncio.to_thread(_send_sync, to, subject, body)
        logger.info("SOS e-mail sent to %s", to)
        return True
    except Exception:
        logger.exception("SOS e-mail failed to %s", to)
        return False
