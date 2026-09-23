from functools import lru_cache
from ipaddress import IPv4Network, IPv6Network, ip_network
from pathlib import Path
from typing import Annotated, Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def _split(value: object) -> object:
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./grocery.db"

    # Host header allowlist (no scheme/port). Empty disables the check (dev only).
    allowed_hosts: Annotated[list[str], NoDecode] = []

    ts_identity_mode: Literal["off", "require", "sso"] = "off"
    ts_allowed_logins: Annotated[list[str], NoDecode] = []
    ts_trusted_proxy_ips: Annotated[list[str], NoDecode] = ["127.0.0.1/32"]

    cookie_secure: bool = True
    session_idle_days: int = 30
    session_absolute_days: int = 90

    log_level: str = "INFO"

    # --- Claude API (receipts, later shelf labels) ---
    anthropic_api_key: SecretStr | None = None
    llm_model: str = "claude-sonnet-5"
    llm_effort: Literal["low", "medium", "high"] = "medium"
    # Hard stop for new extractions; estimates come from logged token usage.
    llm_monthly_budget_eur: float = 5.0
    usd_to_eur: float = 0.92
    # Shelf labels are short and simple: a cheaper effort setting is enough, model is tunable per task.
    llm_model_shelf: str = "claude-sonnet-5"
    llm_effort_shelf: Literal["low", "medium", "high"] = "low"
    label_max_edge: int = 1400  # shelf label photos are downscaled to this many pixels on the long edge

    # --- Open Food Facts (barcode lookups, cached) ---
    off_user_agent: str = "GroceryAgent/0.1 (self-hosted)"
    off_found_ttl_days: int = 30
    off_missing_ttl_days: int = 7

    # What food should cost per month; the overview compares food spend against this.
    monthly_reference_eur: float = 400.0

    timezone: str = "Europe/Amsterdam"  # for "today" when a capture has no explicit date

    # --- Deals radar: weekly offers from PrijsProfeet, alerts through Home Assistant ---
    # The user agent must not contain "bot", "crawler" or "spider": PrijsProfeet blocks keyless requests with those.
    deals_user_agent: str = "GroceryAgent/1.0 (self-hosted, personal)"
    prijsprofeet_api_key: SecretStr | None = None  # optional free key: 150 requests/min on the key instead of per IP
    ha_url: str = ""  # e.g. http://10.0.20.20:8123
    ha_token: SecretStr | None = None  # long-lived access token
    ha_notify_service: str = ""  # e.g. notify.mobile_app_joren_iphone

    # --- Uploads and transient image storage ---
    files_dir: Path = Path("./data/files")
    max_upload_bytes: int = 15 * 1024 * 1024  # per file
    max_request_bytes: int = 40 * 1024 * 1024  # whole request
    max_photos: int = 6
    confirmed_retention_days: int = 7
    unconfirmed_retention_days: int = 30

    @field_validator("allowed_hosts", "ts_allowed_logins", "ts_trusted_proxy_ips", mode="before")
    @classmethod
    def _comma_list(cls, value: object) -> object:
        return _split(value)

    @field_validator("allowed_hosts", "ts_allowed_logins", mode="after")
    @classmethod
    def _lower(cls, value: list[str]) -> list[str]:
        return [v.lower() for v in value]

    @property
    def trusted_networks(self) -> list[IPv4Network | IPv6Network]:
        return [ip_network(n, strict=False) for n in self.ts_trusted_proxy_ips]

    @property
    def session_cookie_name(self) -> str:
        # The __Host- prefix needs Secure, so it is only usable over HTTPS.
        return "__Host-session" if self.cookie_secure else "session"


@lru_cache
def get_settings() -> Settings:
    return Settings()
