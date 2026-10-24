"""Configuration for the gemini_cli backend plugin."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from ccproxy.models.provider import ModelCard


def _default_models() -> list[ModelCard]:
    return [
        ModelCard(
            id="gemini-3.1-pro-preview",
            owned_by="google",
            root="gemini-3.1-pro-preview",
        ),
        ModelCard(
            id="gemini-3.5-flash",
            owned_by="google",
            root="gemini-3.5-flash",
        ),
        ModelCard(
            id="gemini-3.1-flash-lite",
            owned_by="google",
            root="gemini-3.1-flash-lite",
        ),
    ]


class GeminiCliSettings(BaseSettings):
    """Settings for the gemini CLI wrapper backend.

    The gemini CLI authenticates via its own cached OAuth (no API key), so there
    is no auth manager here. Prompts are passed on stdin to avoid command-line
    escaping/length limits; only cli_command/default_model (trusted config) reach
    the shell.
    """

    model_config = SettingsConfigDict(env_prefix="GEMINI_CLI_", extra="ignore")

    # On Windows the npm shim is gemini.cmd; the adapter runs it via `cmd /c`
    # with an argv list, so an absolute path (even one containing spaces) is
    # safe. Override with a full path if `gemini` is not on PATH.
    cli_command: str = "gemini"
    default_model: str = "gemini-3.5-flash"
    timeout_seconds: int = 180
    # Lines the gemini CLI prints to stdout that are not model output.
    noise_prefixes: list[str] = Field(
        default_factory=lambda: [
            "Loaded cached credentials.",
            "Data collection is",
        ]
    )
    models_endpoint: list[ModelCard] = Field(default_factory=_default_models)
