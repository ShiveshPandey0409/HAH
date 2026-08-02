from __future__ import annotations

import asyncio
import smtplib
from email.message import EmailMessage
from urllib.parse import urlencode

from app.core.config import Settings


class PasswordResetDeliveryError(Exception):
    pass


class PasswordResetNotifier:
    @property
    def configured(self) -> bool:
        raise NotImplementedError

    async def send(self, *, email: str, token: str) -> None:
        raise NotImplementedError


class UnavailablePasswordResetNotifier(PasswordResetNotifier):
    @property
    def configured(self) -> bool:
        return False

    async def send(self, *, email: str, token: str) -> None:
        del email, token
        raise PasswordResetDeliveryError("Password reset email is unavailable")


class SMTPPasswordResetNotifier(PasswordResetNotifier):
    def __init__(self, settings: Settings) -> None:
        self._host = (settings.smtp_host or "").strip()
        self._port = settings.smtp_port
        self._starttls = settings.smtp_starttls
        self._username = (settings.smtp_username or "").strip()
        self._password = (
            settings.smtp_password.get_secret_value() if settings.smtp_password is not None else ""
        )
        self._from_email = str(settings.smtp_from_email or "")
        self._reset_url = str(settings.password_reset_url)

    @property
    def configured(self) -> bool:
        return bool(self._host and self._from_email)

    async def send(self, *, email: str, token: str) -> None:
        try:
            await asyncio.to_thread(self._send_sync, email, token)
        except (OSError, smtplib.SMTPException) as error:
            raise PasswordResetDeliveryError("Password reset email could not be sent") from error

    def _send_sync(self, email: str, token: str) -> None:
        separator = "&" if "?" in self._reset_url else "?"
        reset_link = f"{self._reset_url}{separator}{urlencode({'token': token})}"
        message = EmailMessage()
        message["Subject"] = "Reset your Hire a Human password"
        message["From"] = self._from_email
        message["To"] = email
        message.set_content(
            "Use this single-use link to reset your password. "
            "If you did not request it, ignore this email.\n\n"
            f"{reset_link}"
        )
        with smtplib.SMTP(self._host, self._port, timeout=10) as smtp:
            smtp.ehlo()
            if self._starttls:
                smtp.starttls()
                smtp.ehlo()
            if self._username:
                smtp.login(self._username, self._password)
            smtp.send_message(message)


def notifier_from_settings(settings: Settings) -> PasswordResetNotifier:
    if settings.smtp_configured:
        return SMTPPasswordResetNotifier(settings)
    return UnavailablePasswordResetNotifier()
