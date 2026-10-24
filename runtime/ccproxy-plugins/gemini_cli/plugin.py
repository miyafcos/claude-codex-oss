"""Gemini CLI backend plugin factory.

A provider plugin whose adapter shells out to the gemini CLI. No auth/detection
(the CLI holds its own cached OAuth). dispatcher reaches it via
registry.get_runtime("gemini_cli").adapter; list_models reads runtime.config.
"""

from __future__ import annotations

from ccproxy.core.logging import get_plugin_logger
from ccproxy.core.plugins import (
    BaseProviderPluginFactory,
    ProviderPluginRuntime,
)

from .adapter import GeminiCliAdapter
from .config import GeminiCliSettings


logger = get_plugin_logger()


class GeminiCliRuntime(ProviderPluginRuntime):
    def __init__(self, manifest):  # type: ignore[no-untyped-def]
        super().__init__(manifest)
        self.config: GeminiCliSettings | None = None

    async def _on_initialize(self) -> None:
        if not self.context:
            raise RuntimeError("Context not set")
        await super()._on_initialize()
        try:
            self.config = self.context.get(GeminiCliSettings)
        except ValueError:
            self.config = GeminiCliSettings()
        logger.debug(
            "gemini_cli_initialized",
            has_adapter=self.adapter is not None,
            models=len(self.config.models_endpoint) if self.config else 0,
        )


class GeminiCliFactory(BaseProviderPluginFactory):
    cli_safe = False

    plugin_name = "gemini_cli"
    plugin_description = (
        "Gemini CLI wrapper backend (one-shot text via the CLI's cached OAuth, "
        "no API key; tool-use not supported)"
    )
    runtime_class = GeminiCliRuntime
    adapter_class = GeminiCliAdapter
    config_class = GeminiCliSettings

    # No auth / detection / credentials — the gemini CLI authenticates itself.
    detection_service_class = None
    credentials_manager_class = None
    auth_manager_name = None
    routers = []
    dependencies = []
    optional_requires = []


factory = GeminiCliFactory()

__all__ = ["GeminiCliFactory", "GeminiCliRuntime", "factory"]
