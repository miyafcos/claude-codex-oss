"""Provider factory for the local Free Claude Code gateway."""

from __future__ import annotations

from ccproxy.core.logging import get_plugin_logger
from ccproxy.core.plugins import BaseProviderPluginFactory, ProviderPluginRuntime

from .adapter import FccAdapter
from .config import FccSettings


logger = get_plugin_logger()


class FccRuntime(ProviderPluginRuntime):
    def __init__(self, manifest):  # type: ignore[no-untyped-def]
        super().__init__(manifest)
        self.config: FccSettings | None = None

    async def _on_initialize(self) -> None:
        if not self.context:
            raise RuntimeError("Context not set")
        await super()._on_initialize()
        try:
            self.config = self.context.get(FccSettings)
        except ValueError:
            self.config = FccSettings()
        logger.debug(
            "fcc_initialized",
            has_adapter=self.adapter is not None,
            base_url=self.config.base_url if self.config else "",
        )


class FccFactory(BaseProviderPluginFactory):
    cli_safe = False
    plugin_name = "fcc"
    plugin_description = "Local Free Claude Code Anthropic gateway"
    runtime_class = FccRuntime
    adapter_class = FccAdapter
    config_class = FccSettings
    detection_service_class = None
    credentials_manager_class = None
    auth_manager_name = None
    routers = []
    dependencies = []
    optional_requires = []


factory = FccFactory()

__all__ = ["FccFactory", "FccRuntime", "factory"]
