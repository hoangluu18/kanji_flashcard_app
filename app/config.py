from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = Field(default="dev", alias="APP_ENV")
    app_name: str = Field(default="Kanji Telegram SRS Bot", alias="APP_NAME")
    app_log_level: str = Field(default="INFO", alias="APP_LOG_LEVEL")
    app_timezone: str = Field(default="Asia/Ho_Chi_Minh", alias="APP_TIMEZONE")

    telegram_bot_token: str = Field(default="ALIAS_TELEGRAM_BOT_TOKEN", alias="TELEGRAM_BOT_TOKEN")
    telegram_webhook_secret: str = Field(
        default="ALIAS_TELEGRAM_WEBHOOK_SECRET", alias="TELEGRAM_WEBHOOK_SECRET"
    )
    telegram_public_base_url: str = Field(
        default="ALIAS_TELEGRAM_PUBLIC_BASE_URL", alias="TELEGRAM_PUBLIC_BASE_URL"
    )
    telegram_use_webhook: bool = Field(default=True, alias="TELEGRAM_USE_WEBHOOK")

    admin_api_key: str = Field(default="ALIAS_ADMIN_API_KEY", alias="ADMIN_API_KEY")

    sqlite_db_path: str = Field(default="ALIAS_SQLITE_DB_PATH", alias="SQLITE_DB_PATH")
    cards_json_path: str = Field(default="ALIAS_CARDS_JSON_PATH", alias="CARDS_JSON_PATH")
    assets_base_dir: str = Field(default="ALIAS_ASSETS_BASE_DIR", alias="ASSETS_BASE_DIR")

    schedule_morning: str = Field(default="09:00", alias="SCHEDULE_MORNING")
    schedule_noon: str = Field(default="13:00", alias="SCHEDULE_NOON")
    schedule_evening: str = Field(default="20:30", alias="SCHEDULE_EVENING")

    default_new_per_day: int = Field(default=10, alias="DEFAULT_NEW_PER_DAY")
    default_review_limit: int = Field(default=50, alias="DEFAULT_REVIEW_LIMIT")
    leech_threshold: int = Field(default=5, alias="LEECH_THRESHOLD")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @property
    def database_url(self) -> str:
        if self.sqlite_db_path.startswith("ALIAS_"):
            db_path = Path("data/kanji_bot.sqlite3").resolve()
        else:
            db_path = Path(self.sqlite_db_path).expanduser().resolve()
        return f"sqlite:///{db_path}"

    @property
    def cards_json_file(self) -> Path:
        return Path(self.cards_json_path).expanduser().resolve()

    @property
    def assets_root(self) -> Path:
        return Path(self.assets_base_dir).expanduser().resolve()

    @property
    def bot_ready(self) -> bool:
        return not self.telegram_bot_token.startswith("ALIAS_")

    @property
    def webhook_ready(self) -> bool:
        return (
            self.bot_ready
            and not self.telegram_webhook_secret.startswith("ALIAS_")
            and not self.telegram_public_base_url.startswith("ALIAS_")
        )


def parse_hour_minute(value: str) -> tuple[int, int]:
    try:
        hour_str, minute_str = value.split(":", maxsplit=1)
        hour = int(hour_str)
        minute = int(minute_str)
    except ValueError as exc:
        raise ValueError(f"Invalid HH:MM schedule value: {value}") from exc

    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise ValueError(f"Invalid HH:MM schedule value: {value}")
    return hour, minute


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
