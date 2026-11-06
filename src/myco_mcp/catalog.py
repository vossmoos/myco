"""The tool catalog: every configured source, its curated tools and its roles.

Built once at startup from the environment. Sources, tools and roles never change
while Myco runs; only the permissions in the config folder are hot-reloaded.
"""

from __future__ import annotations

import importlib
import inspect
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from myco_mcp.connector import Connector, PackError
from myco_mcp.labels import Label, LabelPattern
from myco_mcp.settings import Settings

log = logging.getLogger("myco")

BUILTIN_MODULES = ("myco_mcp.packs.zendesk", "myco_mcp.packs.jira", "myco_mcp.packs.wrike")


@dataclass(frozen=True)
class ToolSpec:
    name: str  # published MCP name, e.g. zendesk_get_ticket
    source: str
    label: Label
    taints: tuple[str, ...]
    taint_fields: Mapping[str, tuple[str, ...]]
    method: Callable[..., Any]  # bound connector method
    description: str


@dataclass(frozen=True)
class Catalog:
    sources: Mapping[str, Connector]
    tools: Mapping[str, ToolSpec]
    #: "zendesk:reader" -> fully-qualified label patterns
    roles: Mapping[str, tuple[LabelPattern, ...]]

    @classmethod
    def build(cls, settings: Settings) -> Catalog:
        load_connector_modules(settings.plugins)
        sources: dict[str, Connector] = {}
        tools: dict[str, ToolSpec] = {}
        roles: dict[str, tuple[LabelPattern, ...]] = {}
        for source in settings.sources:
            connector_cls = Connector.registry.get(source.type)
            if connector_cls is None:
                known = ", ".join(sorted(Connector.registry)) or "none"
                raise PackError(f"source {source.name!r}: unknown TYPE {source.type!r} (known: {known})")
            pack = connector_cls.load_pack()
            config = connector_cls.resolve_settings(source.name, source.values)
            connector = connector_cls(source.name, config)
            sources[source.name] = connector
            for tool_def in pack.tools.values():
                label = Label.parse(f"{source.name}:{tool_def.action}")
                method = getattr(connector, tool_def.name)
                spec = ToolSpec(
                    name=f"{source.name}_{tool_def.name}",
                    source=source.name,
                    label=label,
                    taints=tool_def.taints,
                    taint_fields=tool_def.taint_fields,
                    method=method,
                    description=_describe(method, label, tool_def.taints),
                )
                tools[spec.name] = spec
            for role, perms in pack.roles.items():
                roles[f"{source.name}:{role}"] = tuple(LabelPattern.parse(f"{source.name}:{perm}") for perm in perms)
            log.info("source %s (%s pack v%s): %d tools", source.name, source.type, pack.version, len(pack.tools))
        if not sources:
            log.warning("no sources configured; set MYCO_SOURCE__<NAME>__<SETTING> variables")
        return cls(sources=sources, tools=tools, roles=roles)

    async def aclose(self) -> None:
        for connector in self.sources.values():
            await connector.aclose()


def load_connector_modules(plugins: tuple[str, ...] = ()) -> None:
    """Import the example packs and any ``MYCO_PLUGINS`` modules so their connectors register."""
    for module in (*BUILTIN_MODULES, *plugins):
        importlib.import_module(module)


def _describe(method: Callable[..., Any], label: Label, taints: tuple[str, ...]) -> str:
    doc = inspect.getdoc(method) or ""
    footer = f"Label: {label}."
    if taints:
        footer += f" Taints: {', '.join(taints)}."
    return f"{doc}\n\n{footer}".strip()
