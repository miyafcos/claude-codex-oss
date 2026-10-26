from __future__ import annotations

from ccproxy.core.plugins import BaseProviderPluginFactory, ProviderPluginRuntime

from .adapter import NanoGptAdapter
from .config import NanoGptSettings


class NanoGptRuntime(ProviderPluginRuntime):
    async def _on_initialize(self) -> None:
        if not self.context:
            raise RuntimeError("Context not set")
        await super()._on_initialize()
        try:
            self.config = self.context.get(NanoGptSettings)
        except ValueError:
            self.config = NanoGptSettings()


class NanoGptFactory(BaseProviderPluginFactory):
    cli_safe = False
    plugin_name = "nanogpt"
    plugin_description = "NanoGPT subscription-only Anthropic gateway"
    runtime_class = NanoGptRuntime
    adapter_class = NanoGptAdapter
    config_class = NanoGptSettings
    detection_service_class = None
    credentials_manager_class = None
    auth_manager_name = None
    routers = []
    dependencies = []
    optional_requires = []


factory = NanoGptFactory()
