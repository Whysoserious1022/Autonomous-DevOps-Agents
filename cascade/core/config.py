"""
cascade/core/config.py
──────────────────────
Pydantic-Settings configuration. Reads from environment variables and .env file.
All Cascade components pull their config from settings().
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class CascadeSettings(BaseSettings):
    """
    Central configuration for Cascade.
    Values are loaded from environment variables or a .env file.
    Override any value by setting the corresponding env var.
    """

    model_config = SettingsConfigDict(
        env_prefix="CASCADE_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── LLM ───────────────────────────────────────────────────────────────────
    llm_model: str = Field(
        default="openai/gpt-4o",
        description="LiteLLM model string. E.g. 'anthropic/claude-3-5-sonnet-20241022'",
    )
    llm_temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    llm_max_tokens: int = Field(default=4096, ge=1)

    # ── Database ───────────────────────────────────────────────────────────────
    database_url: str = Field(
        default="sqlite+aiosqlite:///{home}/.cascade/cascade.db",
        description="SQLAlchemy async database URL.",
    )

    # ── Artifact Store ─────────────────────────────────────────────────────────
    artifact_backend: str = Field(default="local", pattern="^(local|s3)$")
    artifact_local_root: str = Field(default="{home}/.cascade/artifacts")
    artifact_s3_bucket: str = Field(default="cascade-artifacts")
    artifact_s3_endpoint_url: str | None = Field(default=None)

    # ── Sandbox ────────────────────────────────────────────────────────────────
    sandbox_image: str = Field(default="python:3.12-slim")
    sandbox_mem_limit: str = Field(default="1g")
    sandbox_cpu_quota: int = Field(default=100_000)
    sandbox_timeout_seconds: int = Field(default=300, ge=30)

    # ── Behaviour ──────────────────────────────────────────────────────────────
    max_retries: int = Field(default=3, ge=1)
    log_level: str = Field(default="INFO")
    home: Path = Field(default_factory=lambda: Path.home())

    # ── API ────────────────────────────────────────────────────────────────────
    api_host: str = Field(default="0.0.0.0")
    api_port: int = Field(default=8000, ge=1, le=65535)
    api_reload: bool = Field(default=True)

    @field_validator("home", mode="before")
    @classmethod
    def expand_home(cls, v: str | Path) -> Path:
        return Path(str(v).replace("~", str(Path.home()))).expanduser()

    @property
    def resolved_database_url(self) -> str:
        db_path = Path(self.database_url.replace("{home}", str(self.home))
                       .replace("sqlite+aiosqlite:///", ""))
        db_path.parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite+aiosqlite:///{db_path}"

    @property
    def resolved_artifact_local_root(self) -> Path:
        return Path(self.artifact_local_root.replace("{home}", str(self.home))).expanduser()


@lru_cache(maxsize=1)
def settings() -> CascadeSettings:
    """
    Return the singleton CascadeSettings instance.
    Uses lru_cache so settings are parsed once and reused.
    Call settings.cache_clear() in tests to reload.
    """
    return CascadeSettings()
