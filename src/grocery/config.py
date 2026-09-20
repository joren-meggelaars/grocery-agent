from functools import lru_cache
from ipaddress import IPv4Network, IPv6Network, ip_network
from typing import Annotated, Literal

from pydantic import field_validator
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
