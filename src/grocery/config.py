from functools import lru_cache
from ipaddress import IPv4Network, IPv6Network, ip_network
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import SecretStr, field_validator, model_validator
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

    # --- Optional sign-in through Authentik (grocery.security.oidc, the identity-platform repo) ---
    # Empty issuer = off: only the username/password login. With it set, the login page gets an
    # "Sign in with Authentik" button; the password login stays as the emergency way in.
    # Issuer = what the browser sees: https://<authentik host>/application/o/grocery-agent/
    oidc_issuer: str = ""
    oidc_client_id: str = "grocery-agent"
    oidc_client_secret: SecretStr | None = None
    # Every address the app is opened on, ending in /login/oidc/callback (comma or space separated).
    oidc_redirect_uris: str = ""
    # How the container reaches Authentik over the shared docker network, e.g. http://authentik:9000.
    oidc_internal_url: str = ""
    oidc_admin_group: str = "grocery-agent-admin"
    oidc_session_days: int = 7

    @property
    def oidc_redirect_uri_list(self) -> list[str]:
        return [u for u in self.oidc_redirect_uris.replace(",", " ").split() if u]

    @model_validator(mode="after")
    def _check_oidc(self) -> "Settings":
        """A half-filled OIDC block stops the start, not the first sign-in."""
        if not self.oidc_issuer:
            return self

        def secure(url: str) -> bool:
            parsed = urlsplit(url)
            return parsed.scheme == "https" or (parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1"))

        missing = [
            name
            for name, value in (
                ("OIDC_CLIENT_ID", self.oidc_client_id),
                ("OIDC_CLIENT_SECRET", self.oidc_client_secret),
                ("OIDC_REDIRECT_URIS", self.oidc_redirect_uri_list),
            )
            if not value
        ]
        if missing:
            raise ValueError(f"OIDC_ISSUER is set, so these are required too: {', '.join(missing)}")
        if not secure(self.oidc_issuer):
            raise ValueError("OIDC_ISSUER must be an https URL (http only for localhost)")
        for uri in self.oidc_redirect_uri_list:
            if not secure(uri) or not uri.endswith("/login/oidc/callback"):
                raise ValueError(f"OIDC_REDIRECT_URIS entry {uri!r} must be an https URL ending in /login/oidc/callback")
        if self.oidc_internal_url and urlsplit(self.oidc_internal_url).scheme not in ("http", "https"):
            raise ValueError("OIDC_INTERNAL_URL must be an http(s) URL")
        if not 1 <= self.oidc_session_days <= 90:
            raise ValueError("OIDC_SESSION_DAYS must be between 1 and 90")
        return self

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
