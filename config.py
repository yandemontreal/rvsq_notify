"""Configuration management for RVSQ checker."""
import os
import re
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


DEFAULT_SERVICE_REASONS = (
    "Suivi",
    "Consultation semi-urgente",
    "Consultation urgente",
)


def _parse_reasons(value: str) -> tuple[str, ...]:
    items = tuple(r.strip() for r in value.split(",") if r.strip())
    return items or DEFAULT_SERVICE_REASONS



@dataclass
class RVSQConfig:
    ramq_number: str
    special_number: str
    first_name: str
    last_name: str
    date_of_birth: str
    headless: bool = True
    doctor_name: str = ""
    clinic_name: str = ""
    postal_code: str = ""
    perimeter_km: int = 50
    save_traces: bool = False
    debug: bool = False
    service_reasons: tuple[str, ...] = DEFAULT_SERVICE_REASONS

@dataclass
class EmailConfig:
    enabled: bool = False
    smtp_server: str = "smtp.gmail.com"
    smtp_port: int = 587
    username: str = ""
    password: str = ""
    to_email: str = ""

@dataclass
class TelegramConfig:
    enabled: bool = False
    bot_token: str = ""
    chat_id: str = ""

@dataclass
class Config:
    rvsq: RVSQConfig
    email: EmailConfig
    telegram: TelegramConfig
    check_interval_min_minutes: int = 45
    check_interval_max_minutes: int = 90

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            rvsq=RVSQConfig(
                ramq_number=os.getenv("RAMQ_NUMBER", ""),
                special_number=os.getenv("SPECIAL_NUMBER", ""),
                first_name=os.getenv("FIRST_NAME", ""),
                last_name=os.getenv("LAST_NAME", ""),
                date_of_birth=os.getenv("DATE_OF_BIRTH", ""),
                headless=True,
                doctor_name=os.getenv("DOCTOR_NAME", ""),
                clinic_name=os.getenv("CLINIC_NAME", ""),
                postal_code=os.getenv("POSTAL_CODE", ""),
                perimeter_km=int(os.getenv("PERIMETER_KM", "50")),
                save_traces=False,
                service_reasons=_parse_reasons(os.getenv("SERVICE_REASONS", "")),
            ),
            email=EmailConfig(
                enabled=os.getenv("EMAIL_ENABLED", "false").lower() == "true",
                smtp_server=os.getenv("EMAIL_SMTP_SERVER", "smtp.gmail.com"),
                smtp_port=int(os.getenv("EMAIL_SMTP_PORT", "587")),
                username=os.getenv("EMAIL_USERNAME", ""),
                password=os.getenv("EMAIL_PASSWORD", ""),
                to_email=os.getenv("EMAIL_TO", ""),
            ),
            telegram=TelegramConfig(
                enabled=os.getenv("TELEGRAM_ENABLED", "false").lower() == "true",
                bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
                chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
            ),
            check_interval_min_minutes=int(os.getenv("CHECK_INTERVAL_MIN_MINUTES", "45")),
            check_interval_max_minutes=int(os.getenv("CHECK_INTERVAL_MAX_MINUTES", "90")),
        )

    def validate(self) -> list[str]:
        errors = []
        if not self.rvsq.ramq_number:
            errors.append("RAMQ_NUMBER is required")
        if not self.rvsq.special_number:
            errors.append("SPECIAL_NUMBER is required")
        if not self.rvsq.first_name:
            errors.append("FIRST_NAME is required")
        if not self.rvsq.last_name:
            errors.append("LAST_NAME is required")
        if not self.rvsq.date_of_birth:
            errors.append("DATE_OF_BIRTH is required")
        if self.check_interval_min_minutes <= 0 or self.check_interval_max_minutes <= 0:
            errors.append("Check interval minutes must be positive")
        if self.check_interval_max_minutes < self.check_interval_min_minutes:
            errors.append("CHECK_INTERVAL_MAX_MINUTES must be >= CHECK_INTERVAL_MIN_MINUTES")
        if self.email.enabled and not all([self.email.username, self.email.password, self.email.to_email]):
            errors.append("Email config incomplete")
        if self.telegram.enabled:
            if not self.telegram.bot_token or not self.telegram.chat_id:
                errors.append("Telegram config incomplete")
            elif not re.match(r"^\d{6,}:[A-Za-z0-9_-]{30,}$", self.telegram.bot_token.strip()):
                errors.append(
                    "TELEGRAM_BOT_TOKEN looks invalid (expected like 123456789:AAxx... from @BotFather)"
                )
        return errors