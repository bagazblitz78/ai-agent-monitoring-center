"""Application settings and per-user application path resolution.

All mutable application data lives under a single home directory that defaults to
the platform user data directory and can be overridden with ``AGENTSCOPE_HOME``.
Nothing is ever written inside the repository or a monitored project.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import platformdirs
from pydantic_settings import BaseSettings, SettingsConfigDict

APP_DIR_NAME = "AgentScope"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class Settings(BaseSettings):
    """Runtime settings, read from ``AGENTSCOPE_*`` environment variables."""

    model_config = SettingsConfigDict(env_prefix="AGENTSCOPE_", extra="ignore")

    app_name: str = "AgentScope"
    version: str = "0.1.0"
    host: str = "127.0.0.1"
    port: int = 8787
    home: Path | None = None
    auth_mode: Literal["token", "off"] = "token"
    debug: bool = False
    allow_non_localhost: bool = False
    #: Turn the monitoring engine off entirely: no file/process/git watchers.
    #: Tests set it so a client can never spawn watcher threads behind a test's
    #: back; production leaves it at the default (monitors on).
    disable_monitors: bool = False


@dataclass(frozen=True)
class AppPaths:
    """Every filesystem location the application uses, derived from ``home``."""

    home: Path
    data: Path
    logs: Path
    spool: Path
    backups: Path
    db: Path
    token: Path
    pid: Path


SETTINGS: Settings | None = None


def settings_singleton() -> Settings:
    """Return the process-wide settings, constructing them on first use."""
    global SETTINGS
    if SETTINGS is None:
        SETTINGS = Settings()
    return SETTINGS


def resolve_paths(settings: Settings | None = None) -> AppPaths:
    """Resolve the application path layout for ``settings``."""
    settings = settings if settings is not None else settings_singleton()
    home = settings.home
    if home is None:
        home = Path(platformdirs.user_data_dir(APP_DIR_NAME))
    home = Path(home)
    if not home.is_absolute():
        raise ValueError("AGENTSCOPE_HOME must be an absolute path")
    data = home / "data"
    return AppPaths(
        home=home,
        data=data,
        logs=home / "logs",
        spool=home / "spool",
        backups=home / "backups",
        db=data / "agent_scope.db",
        token=home / "session.token",
        pid=home / "agentscope.pid",
    )


def ensure_dirs(paths: AppPaths) -> None:
    """Create the application directories if they do not exist yet."""
    for directory in (paths.data, paths.logs, paths.spool, paths.backups):
        directory.mkdir(parents=True, exist_ok=True)


def assert_bind_allowed(settings: Settings) -> None:
    """Refuse to bind a non-loopback host unless explicitly allowed."""
    if settings.allow_non_localhost:
        return
    if settings.host.strip().lower() in LOOPBACK_HOSTS:
        return
    raise RuntimeError(
        f"Refusing to bind to {settings.host!r}: only loopback hosts are allowed. "
        "Set AGENTSCOPE_ALLOW_NON_LOCALHOST=1 to override."
    )
