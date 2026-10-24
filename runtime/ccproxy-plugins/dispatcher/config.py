"""Configuration for the dispatcher plugin.

Routing rules are currently prefix-based and live in routes.py
(_select_backend). This settings object exists so the plugin manifest has a
config_class and so a future config.toml [plugins.dispatcher] section can
override the default backend without code changes.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class DispatcherSettings(BaseModel):
    """Settings for the dispatcher plugin."""

    default_backend: str = Field(
        default="codex",
        description="Backend plugin used when no routing rule matches the model name.",
    )
