from __future__ import annotations

import json
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from ccproxy.models.provider import ModelCard


def _model_profile() -> dict:
    paths = (
        Path(__file__).resolve().parents[2] / "models.json",
        Path.home() / ".claude-codex" / "config" / "models.json",
    )
    for path in paths:
        try:
            profile = json.loads(path.read_text(encoding="utf-8"))["backends"]["nanogpt"]
            if isinstance(profile, dict):
                return profile
        except (OSError, UnicodeError, ValueError, KeyError, TypeError):
            continue
    return {}


def _default_models() -> list[ModelCard]:
    models = _model_profile().get("availableModels", [])
    if not isinstance(models, list):
        return []
    return [
        ModelCard(id=model.removeprefix("nanogpt/"), owned_by="nanogpt", root=model.removeprefix("nanogpt/"))
        for model in models if isinstance(model, str) and model.startswith("nanogpt/")
    ]


def _upstream_aliases() -> dict[str, str]:
    aliases = _model_profile().get("upstreamAliases", {})
    if not isinstance(aliases, dict):
        return {}
    return {name: target for name, target in aliases.items() if isinstance(name, str) and isinstance(target, str)}


class NanoGptSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NANOGPT_", extra="ignore")

    key_file: Path = Field(default_factory=lambda: Path.home() / ".claude-codex" / "keys" / "nanogpt.dpapi")
    timeout_seconds: float = Field(default=600.0, gt=0)
    models_endpoint: list[ModelCard] = Field(default_factory=_default_models)
    upstream_aliases: dict[str, str] = Field(default_factory=_upstream_aliases)
