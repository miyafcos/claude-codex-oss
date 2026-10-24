"""Dispatcher system plugin factory.

A router-only (is_provider=False) plugin that mounts the dispatcher router.
The router itself delegates to the codex / claude_api / gemini_cli provider
plugins via request.app.state.plugin_registry, so this plugin holds no adapter,
auth, or detection of its own. Pattern mirrors plugins/dashboard/plugin.py.
"""

from __future__ import annotations

from ccproxy.core.logging import get_plugin_logger
from ccproxy.core.plugins import (
    PluginManifest,
    RouteSpec,
    SystemPluginFactory,
    SystemPluginRuntime,
)

from .config import DispatcherSettings


logger = get_plugin_logger()


class DispatcherRuntime(SystemPluginRuntime):
    async def _on_initialize(self) -> None:
        if not self.context:
            raise RuntimeError("Context not set")
        logger.debug("dispatcher_initialized")


class DispatcherFactory(SystemPluginFactory):
    def __init__(self) -> None:
        from .routes import router as dispatcher_router

        manifest = PluginManifest(
            name="dispatcher",
            version="0.1.0",
            description=(
                "Route /v1/messages to a backend (codex/claude_api/gemini_cli) "
                "by model name for mixed-provider ultracode fan-out"
            ),
            is_provider=False,
            config_class=DispatcherSettings,
            routes=[RouteSpec(router=dispatcher_router, prefix="/router", tags=["dispatcher"])],
        )
        super().__init__(manifest)

    def create_runtime(self) -> DispatcherRuntime:
        return DispatcherRuntime(self.manifest)


factory = DispatcherFactory()
