"""Configuration for the local Free Claude Code gateway."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from ccproxy.models.provider import ModelCard


def _default_models() -> list[ModelCard]:
    return [
        ModelCard(id="fcc-default", owned_by="free-claude-code", root="fcc-default"),
        ModelCard(
            id="open_router/stealth/union-alpha",
            owned_by="open-router",
            root="open_router/stealth/union-alpha",
        ),
        ModelCard(
            id="open_router/x-ai/grok-4.3",
            owned_by="open-router",
            root="open_router/x-ai/grok-4.3",
        ),
        ModelCard(
            id="open_router/deepseek/deepseek-v4-flash",
            owned_by="open-router",
            root="open_router/deepseek/deepseek-v4-flash",
        ),
    ]


class FccSettings(BaseSettings):
    """Local-only FCC connection settings.

    FCC remains the credential owner.  The adapter reads its bearer token into
    memory and never copies it into generated config, logs, or request errors.
    """

    model_config = SettingsConfigDict(env_prefix="FCC_", extra="ignore")

    base_url: str = "http://127.0.0.1:8082"
    auth_token: str = ""
    env_file: Path = Field(default_factory=lambda: Path.home() / ".fcc" / ".env")
    default_alias: str = "claude-sonnet-4-5"
    connect_timeout_seconds: float = 5.0
    timeout_seconds: float = 600.0
    models_endpoint: list[ModelCard] = Field(default_factory=_default_models)
