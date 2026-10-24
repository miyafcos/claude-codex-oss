"""Configuration for the fugu (Sakana AI) backend plugin."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from ccproxy.models.provider import ModelCard


def _default_models() -> list[ModelCard]:
    return [
        ModelCard(id="fugu-ultra", owned_by="sakana-ai", root="fugu-ultra"),
        ModelCard(id="fugu", owned_by="sakana-ai", root="fugu"),
        ModelCard(
            id="fugu-ultra-20260615",
            owned_by="sakana-ai",
            root="fugu-ultra-20260615",
        ),
    ]


class FuguSettings(BaseSettings):
    """Settings for the Sakana Fugu HTTP backend.

    Fugu is OpenAI chat-completions compatible, authenticated via Bearer token
    from the FUGU_API_KEY env var. Internally multi-agent (multiple frontier
    LLMs orchestrated behind a single API), so per-call token consumption is
    higher than a plain single-model provider.
    """

    model_config = SettingsConfigDict(env_prefix="FUGU_", extra="ignore")

    # FUGU_API_KEY (Bearer token).
    api_key: str = ""
    base_url: str = "https://api.sakana.ai/v1"
    default_model: str = "fugu-ultra"
    # Fugu spends completion tokens on hidden internal reasoning before the
    # visible answer. Very small Anthropic max_tokens values can therefore
    # produce an empty response; keep a conservative provider-side floor.
    minimum_max_tokens: int = 512
    # Fugu Ultra runs internal multi-agent council; long-running prompts can
    # take minutes. Generous timeout by default.
    timeout_seconds: int = 300
    models_endpoint: list[ModelCard] = Field(default_factory=_default_models)
