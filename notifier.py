"""Notification handlers for email and Telegram."""
import html
import logging
import smtplib

import requests
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from config import Config

logger = logging.getLogger(__name__)


class Notifier:
    def __init__(self, config: Config):
        self.config = config

    def send_email(self, subject: str, body: str) -> bool:
        if not self.config.email.enabled:
            return False
        try:
            msg = MIMEMultipart()
            msg["From"] = self.config.email.username
            msg["To"] = self.config.email.to_email
            msg["Subject"] = subject
            msg.attach(MIMEText(body, "html"))

            with smtplib.SMTP(self.config.email.smtp_server, self.config.email.smtp_port) as server:
                server.starttls()
                server.login(self.config.email.username, self.config.email.password)
                server.send_message(msg)
            logger.info("Email notification sent to %s", self.config.email.to_email)
            return True
        except Exception as e:
            logger.error("Email notification failed: %s", e)
            return False

    def send_telegram(self, message: str) -> bool:
        if not self.config.telegram.enabled:
            return False
        url = f"https://api.telegram.org/bot{self.config.telegram.bot_token}/sendMessage"
        payload = {
            "chat_id": self.config.telegram.chat_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        try:
            response = requests.post(url, json=payload, timeout=10)
            if response.status_code == 200:
                logger.info("Telegram notification sent to chat %s", self.config.telegram.chat_id)
                return True
            logger.error(
                "Telegram API error %s: %s", response.status_code, response.text[:300]
            )
            return False
        except Exception as e:
            logger.error("Telegram notification failed: %s", e)
            return False

    def notify_availability(
        self,
        doctor_name: str,
        clinic: str,
        date: str,
        time: str,
        booking_url: str = "",
        service: str = "",
    ) -> None:
        hospital = "\U0001F3E5"
        doctor_emoji = "\U0001F468\u200D\u2695\uFE0F"
        calendar = "\U0001F4C5"
        clock = "\U0001F550"
        stethoscope = "\U0001FA7A"

        esc = html.escape
        doctor_safe = esc(doctor_name)
        clinic_safe = esc(clinic)
        date_safe = esc(date)
        time_safe = esc(time)
        service_safe = esc(service)

        subject = f"{hospital} RVSQ: Spot Available with {doctor_name}!"
        body = f"""
        <h2>Appointment Available!</h2>
        <p><strong>Doctor:</strong> {doctor_safe}</p>
        <p><strong>Clinic:</strong> {clinic_safe}</p>
        <p><strong>Date:</strong> {date_safe}</p>
        <p><strong>Time:</strong> {time_safe}</p>
        {f'<p><strong>Service:</strong> {service_safe}</p>' if service else ''}
        {f'<p><a href="{esc(booking_url)}">Open in RVSQ</a></p>' if booking_url else ''}
        <hr>
        <p>This is an automated notification from your RVSQ checker.</p>
        """

        service_line = f"\n{stethoscope} <b>Service:</b> {service_safe}" if service else ""
        message = (
            f"{hospital} <b>RVSQ: Spot Available!</b>\n\n"
            f"{doctor_emoji} <b>Doctor:</b> {doctor_safe}\n"
            f"{hospital} <b>Clinic:</b> {clinic_safe}\n"
            f"{calendar} <b>Date:</b> {date_safe}\n"
            f"{clock} <b>Time:</b> {time_safe}"
            f"{service_line}"
        )

        self.send_email(subject, body)
        self.send_telegram(message)

    def notify_error(self, error: str) -> None:
        warning = "\u26A0\uFE0F"
        error_safe = html.escape(error[:1500])
        subject = f"{warning} RVSQ Checker Error"
        body = f"<p>The RVSQ checker encountered an error:</p><pre>{error_safe}</pre>"
        message = f"{warning} <b>RVSQ Checker Error</b>\n\n<pre>{error_safe}</pre>"

        self.send_email(subject, body)
        self.send_telegram(message)
