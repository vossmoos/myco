"""The tool catalog: every configured source, its curated tools and its roles.

Built once at startup from the environment. Sources, tools and roles never change
while Myco runs; only the permissions in the config folder are hot-reloaded.
"""

from __future__ import annotations

import importlib
import inspect
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from myco_mcp.connector import TEMPLATE_RE, Connector, PackError, RecordDef, ToolDef
from myco_mcp.labels import NAME_RE, Label, LabelPattern
from myco_mcp.settings import Settings

log = logging.getLogger("myco")

BUILTIN_MODULES = (
    "myco_mcp.packs.zendesk",
    "myco_mcp.packs.jira",
    "myco_mcp.packs.wrike",
    "myco_mcp.packs.etchmem",
)


WITNESS_SETTINGS = ("witness", "witness_memory", "witness_scope")


@dataclass(frozen=True)
class Witness:
    """Record what a tool's reply showed the calling agent, in a memory source.

    Set per source instance: ``MYCO_SOURCE__<NAME>__WITNESS`` (tool names, without the
    source prefix), ``__WITNESS_MEMORY`` (a source whose connector can take deposits, e.g.
    an etchmem source) and ``__WITNESS_SCOPE`` (the topic, default ``general``)."""

    memory: str
    scope: str


@dataclass(frozen=True)
class ToolSpec:
    name: str  # published MCP name, e.g. zendesk_get_ticket
    source: str
    #: The tool's label; for a templated tool, the label of its first value.
    label: Label
    taints: tuple[str, ...]
    taint_fields: Mapping[str, tuple[str, ...]]
    method: Callable[..., Any]  # bound connector method
    description: str
    #: The argument whose value is the label's entity (templated tools only).
    label_arg: str | None = None
    #: The values `label_arg` may take, in the connector's order.
    label_values: tuple[str, ...] = ()
    #: "allowed" / "agent": Myco sets `label_arg` itself and hides it from the agent.
    label_fill: str | None = None
    #: How to name the record a reply is about (from the pack).
    record: RecordDef | None = None
    #: Set when the source's WITNESS setting lists this tool.
    witness: Witness | None = None

    @property
    def labels(self) -> tuple[Label, ...]:
        """Every label a call to this tool can carry."""
        if self.label_arg is None or self.label_fill == "agent":
            # "fill: agent": the entity is the caller, unknown here; the base label still
            # matches wildcard rules such as memory:*:create (pins, redaction, deny).
            return (self.label,)
        return tuple(self.label_for(v) for v in self.label_values)

    def label_for(self, value: str) -> Label:
        return Label(self.source, value, self.label.operation)

    @property
    def label_text(self) -> str:
        if self.label_arg is None:
            return str(self.label)
        return f"{self.source}:{{{self.label_arg}}}:{self.label.operation}"


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
        witnessing: dict[str, dict[str, str]] = {}
        for source in settings.sources:
            connector_cls = Connector.registry.get(source.type)
            if connector_cls is None:
                known = ", ".join(sorted(Connector.registry)) or "none"
                raise PackError(f"source {source.name!r}: unknown TYPE {source.type!r} (known: {known})")
            pack = connector_cls.load_pack()
            values = dict(source.values)
            # WITNESS settings belong to Myco, not to the connector: every instance has them.
            witnessing[source.name] = {k: values.pop(k, "") for k in WITNESS_SETTINGS}
            config = connector_cls.resolve_settings(source.name, values)
            connector = connector_cls(source.name, config)
            sources[source.name] = connector
            values: dict[str, tuple[str, ...]] = {}
            for tool_def in pack.tools.values():
                arg = tool_def.label_from
                # "fill: agent" takes its value from the caller, so it has no list of values.
                if arg is not None and arg not in values and tool_def.label_fill != "agent":
                    values[arg] = _label_values(connector, source.name, arg)
                spec = _tool_spec(source.name, connector, tool_def, values.get(arg, ()) if arg else ())
                tools[spec.name] = spec
            for role, perms in pack.roles.items():
                roles.update(_expand_role(source.name, role, perms, values))
            log.info("source %s (%s pack v%s): %d tools", source.name, source.type, pack.version, len(pack.tools))
        if not sources:
            log.warning("no sources configured; set MYCO_SOURCE__<NAME>__<SETTING> variables")
        tools.update(_witness_tools(witnessing, sources, tools))
        return cls(sources=sources, tools=tools, roles=roles)

    async def aclose(self) -> None:
        for connector in self.sources.values():
            await connector.aclose()


def load_connector_modules(plugins: tuple[str, ...] = ()) -> None:
    """Import the example packs and any ``MYCO_PLUGINS`` modules so their connectors register."""
    for module in (*BUILTIN_MODULES, *plugins):
        importlib.import_module(module)


def _tool_spec(source: str, connector: Connector, tool_def: ToolDef, values: tuple[str, ...]) -> ToolSpec:
    method = getattr(connector, tool_def.name)
    first = values[0] if values else None
    label = Label.parse(f"{source}:{tool_def.entity_for(first)}:{tool_def.operation}")
    spec = ToolSpec(
        name=f"{source}_{tool_def.name}",
        source=source,
        label=label,
        taints=tool_def.taints,
        taint_fields=tool_def.taint_fields,
        method=method,
        description="",
        label_arg=tool_def.label_from,
        label_values=values,
        label_fill=tool_def.label_fill,
        record=tool_def.record,
    )
    return replace(spec, description=_describe(method, spec))


def _label_values(connector: Connector, source: str, arg: str) -> tuple[str, ...]:
    values = tuple(dict.fromkeys(connector.label_values(arg)))
    if not values:
        raise PackError(f"source {source!r}: templated argument {arg!r} has no values")
    for value in values:
        if not NAME_RE.match(value):
            raise PackError(
                f"source {source!r}: {arg} value {value!r} cannot be a label entity "
                "(lowercase letters, digits and underscores, starting with a letter)"
            )
    return values


def _expand_role(
    source: str, role: str, perms: tuple[str, ...], values: Mapping[str, tuple[str, ...]]
) -> dict[str, tuple[LabelPattern, ...]]:
    """One role, or one role per value for a templated role like ``{scope}_reader``."""
    placeholders = TEMPLATE_RE.findall(role)
    if not placeholders:
        return {f"{source}:{role}": tuple(LabelPattern.parse(f"{source}:{perm}") for perm in perms)}
    arg = placeholders[0]
    out = {}
    for value in values.get(arg, ()):
        name = role.replace(f"{{{arg}}}", value)
        out[f"{source}:{name}"] = tuple(
            LabelPattern.parse(f"{source}:{perm.replace(f'{{{arg}}}', value)}") for perm in perms
        )
    return out


def _describe(method: Callable[..., Any], spec: ToolSpec) -> str:
    doc = inspect.getdoc(method) or ""
    footer = f"Label: {spec.label_text}."
    if spec.label_arg and not spec.label_fill:
        footer += f" {spec.label_arg}: one of {', '.join(spec.label_values)}."
    if spec.taints:
        footer += f" Taints: {', '.join(spec.taints)}."
    return f"{doc}\n\n{footer}".strip()


def _witness_tools(
    witnessing: Mapping[str, Mapping[str, str]], sources: Mapping[str, Connector], tools: Mapping[str, ToolSpec]
) -> dict[str, ToolSpec]:
    """The tool specs that WITNESS settings turn on, checked so a mistake fails at startup."""
    out: dict[str, ToolSpec] = {}
    for source, cfg in witnessing.items():
        names = [t.strip() for t in (cfg.get("witness") or "").split(",") if t.strip()]
        env = f"MYCO_SOURCE__{source.upper()}__"
        if not names:
            if cfg.get("witness_memory") or cfg.get("witness_scope"):
                log.warning("%sWITNESS_MEMORY/SCOPE set but %sWITNESS lists no tool", env, env)
            continue
        memory = (cfg.get("witness_memory") or "").strip().lower()
        target = sources.get(memory)
        if target is None:
            raise PackError(f"{env}WITNESS_MEMORY: {memory or '(empty)'} is not a configured source")
        if not callable(getattr(target, "witness_deposit", None)):
            raise PackError(f"{env}WITNESS_MEMORY: source {memory!r} ({target.type}) cannot take deposits")
        scope = (cfg.get("witness_scope") or "").strip() or "general"
        for name in names:
            spec = tools.get(f"{source}_{name}")
            if spec is None:
                raise PackError(f"{env}WITNESS: {source} has no tool {name!r}")
            if spec.record is None:
                raise PackError(
                    f"{env}WITNESS: tool {name!r} declares no record in its pack, so its replies cannot be "
                    "witnessed (add record: {id: '<entity>:{id}'} to the pack)"
                )
            out[spec.name] = replace(spec, witness=Witness(memory=memory, scope=scope))
    return out
