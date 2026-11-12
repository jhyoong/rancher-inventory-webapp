import os
import re
from datetime import date
from pathlib import Path

import yaml
from pydantic import BaseModel, field_validator
from pydantic_settings import BaseSettings


class ConfigError(Exception):
    pass


class Settings(BaseSettings):
    listen_port: int = 8443
    tls_cert_file: str = ""
    tls_key_file: str = ""
    session_secret: str
    session_hours: int = 12
    cookie_secure: bool = True
    admin_user: str = "admin"
    admin_password: str = ""
    collector_enabled: bool = True
    collect_daily_at: str = "02:00"
    stale_after_hours: int = 36
    concurrency_per_site: int = 4
    request_timeout_s: float = 30
    metrics_retention_days: int = 365
    data_dir: Path = Path("/data")
    sites_file: Path = Path("/config/sites.yaml")
    defaults_dir: Path = Path("/app/defaults")

    @field_validator("collect_daily_at")
    @classmethod
    def _check_daily_at(cls, v: str) -> str:
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v):
            raise ValueError(f"must be HH:MM in 24-hour time (00:00 to 23:59), got {v!r}")
        return v

    @property
    def db_path(self) -> Path:
        return self.data_dir / "inventory.db"

    @property
    def config_dir(self) -> Path:
        return self.sites_file.parent


class Site(BaseModel):
    name: str
    url: str
    token_env: str
    ca_file: str | None = None
    tls_verify: bool = True
    token_expires: date | None = None

    @field_validator("url")
    @classmethod
    def _strip_slash(cls, v: str) -> str:
        return v.rstrip("/")


def load_sites(settings: Settings) -> list[Site]:
    path = settings.sites_file
    if not path.is_file():
        raise ConfigError(f"sites file not found: {path}")
    data = yaml.safe_load(path.read_text()) or {}
    raw = data.get("sites") if isinstance(data, dict) else None
    if not raw:
        raise ConfigError(f"no sites defined in {path}")
    sites = [Site(**item) for item in raw]
    seen: set[str] = set()
    for site in sites:
        if site.name in seen:
            raise ConfigError(f"duplicate site name: {site.name}")
        seen.add(site.name)
    return sites


def site_token(site: Site) -> str:
    try:
        return os.environ[site.token_env]
    except KeyError:
        raise ConfigError(f"{site.token_env} is not set") from None


def mask_token(token: str) -> str:
    head, sep, _ = token.partition(":")
    return f"{head}:****" if sep else "****"


def load_env_file(path: Path) -> None:
    """For tests and local runs only. Existing environment values win."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())
