"""Sakana Fugu backend plugin factory.

A provider plugin whose adapter speaks Anthropic /v1/messages on the inside
and translates to Fugu's OpenAI chat-completions wire on the outside. Auth is
a simple Bearer header populated from FUGU_API_KEY; no detection / credentials
manager is needed (the proxy itself does not own the credential — the user's
env var is). dispatcher reaches it via registry.get_runtime("fugu").adapter;
list_models reads runtime.config.
"""

from __future__ import annotations

from ccproxy.core.logging import get_plugin_logger
from ccproxy.core.plugins import (
    BaseProviderPluginFactory,
    ProviderPluginRuntime,
)

from .adapter import FuguAdapter
from .config import FuguSettings


logger = get_plugin_logger()


class FuguRuntime(ProviderPluginRuntime):
    def __init__(self, manifest):  # type: ignore[no-untyped-def]
        super().__init__(manifest)
        self.config: FuguSettings | None = None

    async def _on_initialize(self) -> None:
        if not self.context:
            raise RuntimeError("Context not set")
        await super()._on_initialize()
        try:
            self.config = self.context.get(FuguSettings)
        except ValueError:
            self.config = FuguSettings()
        logger.debug(
            "fugu_initialized",
            has_adapter=self.adapter is not None,
            has_api_key=bool(self.config and self.config.api_key),
            models=len(self.config.models_endpoint) if self.config else 0,
        )


class FuguFactory(BaseProviderPluginFactory):
    cli_safe = False

    plugin_name = "fugu"
    plugin_description = (
        "Sakana Fugu HTTP backend — OpenAI chat-completions wire, Bearer auth "
        "via FUGU_API_KEY, internal multi-agent orchestration. Heavy tokens; "
        "good for reasoning / council / final-gate."
    )
    runtime_class = FuguRuntime
    adapter_class = FuguAdapter
    config_class = FuguSettings

    # Bearer token is read from config (env-loaded). No detection/credentials.
    detection_service_class = None
    credentials_manager_class = None
    auth_manager_name = None
    routers = []
    dependencies = []
    optional_requires = []


factory = FuguFactory()

__all__ = ["FuguFactory", "FuguRuntime", "factory"]
