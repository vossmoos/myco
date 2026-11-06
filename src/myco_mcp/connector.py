"""Connector base class and pack format.

A connector is two files with the same name:

* ``acme.py``   — a :class:`Connector` subclass whose ``@tool`` methods call the vendor API.
* ``acme.yaml`` — the pack: one label per tool, optional taints, and the roles.

Myco refuses to start when the two disagree: every ``@tool`` method needs a label in the
pack and every pack tool needs a method, so no tool can ever be published unlabelled.
"""

from __future__ import annotations

import inspect
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

import yaml

from myco_mcp.labels import NAME_RE, Label, LabelError, LabelPattern

_TOOL_MARK = "__myco_tool__"


class PackError(ValueError):
    """A pack or connector definition is invalid."""


def tool(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Mark a connector method as a curated tool. Its label comes from the pack."""
    if not inspect.iscoroutinefunction(fn):
        raise PackError(f"@tool {fn.__qualname__} must be an async method")
    setattr(fn, _TOOL_MARK, True)
    return fn


@dataclass(frozen=True)
class Setting:
    """One ``MYCO_SOURCE__<NAME>__<SETTING>`` variable a connector reads."""

    required: bool = True
    secret: bool = False
    default: str | None = None
    help: str = ""


@dataclass(frozen=True)
class ToolDef:
    name: str
    action: str  # "entity:operation"
    taints: tuple[str, ...] = ()
    #: taint -> dot paths in the reply that carry it, e.g. {"pii": ("requester.email",)}
    taint_fields: Mapping[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class Pack:
    type: str
    version: str
    description: str
    tools: Mapping[str, ToolDef]
    roles: Mapping[str, tuple[str, ...]]  # role -> "entity:operation" patterns

    @classmethod
    def load(cls, path: Path, connector_type: str) -> Pack:
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise PackError(f"pack {path}: {exc}") from exc
        return cls.parse(raw, connector_type, origin=str(path))

    @classmethod
    def parse(cls, raw: Any, connector_type: str, *, origin: str = "pack") -> Pack:
        if not isinstance(raw, dict):
            raise PackError(f"{origin}: expected a mapping")
        _only_keys(raw, {"pack", "version", "description", "tools", "roles"}, origin)
        declared = raw.get("pack", connector_type)
        if declared != connector_type:
            raise PackError(f"{origin}: pack {declared!r} does not match connector {connector_type!r}")
        tools_raw = raw.get("tools")
        if not isinstance(tools_raw, dict) or not tools_raw:
            raise PackError(f"{origin}: 'tools' must be a non-empty mapping")
        tools = {name: _parse_tool(name, spec, origin) for name, spec in tools_raw.items()}

        labels = [Label.parse(f"x:{t.action}") for t in tools.values()]
        roles: dict[str, tuple[str, ...]] = {}
        for role, perms in (raw.get("roles") or {}).items():
            if not NAME_RE.match(str(role)):
                raise PackError(f"{origin}: invalid role name {role!r}")
            if not isinstance(perms, list) or not perms:
                raise PackError(f"{origin}: role {role!r} must list entity:operation patterns")
            for perm in perms:
                try:
                    pattern = LabelPattern.parse(f"x:{perm}")
                except LabelError as exc:
                    raise PackError(f"{origin}: role {role!r}: {exc}") from exc
                if not any(pattern.matches(label) for label in labels):
                    raise PackError(f"{origin}: role {role!r}: {perm!r} matches no tool in this pack")
            roles[str(role)] = tuple(str(p) for p in perms)
        return cls(
            type=connector_type,
            version=str(raw.get("version", "0")),
            description=str(raw.get("description", "")).strip(),
            tools=tools,
            roles=roles,
        )


def _parse_tool(name: Any, spec: Any, origin: str) -> ToolDef:
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise PackError(f"{origin}: invalid tool name {name!r}")
    if isinstance(spec, str):
        spec = {"action": spec}
    if not isinstance(spec, dict):
        raise PackError(f"{origin}: tool {name!r} must be 'entity:operation' or a mapping")
    _only_keys(spec, {"action", "taints", "taint_fields"}, f"{origin}: tool {name!r}")
    action = str(spec.get("action", ""))
    try:
        Label.parse(f"x:{action}")
    except LabelError as exc:
        raise PackError(f"{origin}: tool {name!r}: {exc}") from exc
    taints = tuple(str(t) for t in spec.get("taints") or ())
    taint_fields_raw = spec.get("taint_fields") or {}
    if not isinstance(taint_fields_raw, dict):
        raise PackError(f"{origin}: tool {name!r}: taint_fields must map taint -> [paths]")
    taint_fields = {str(t): tuple(str(p) for p in paths) for t, paths in taint_fields_raw.items()}
    for taint in (*taints, *taint_fields):
        if not NAME_RE.match(taint):
            raise PackError(f"{origin}: tool {name!r}: invalid taint {taint!r}")
    return ToolDef(name=name, action=action, taints=taints, taint_fields=taint_fields)


def _only_keys(raw: Mapping[str, Any], allowed: set[str], origin: str) -> None:
    unknown = set(raw) - allowed
    if unknown:
        raise PackError(f"{origin}: unknown keys {sorted(unknown)} (allowed: {sorted(allowed)})")


class Connector:
    """Base class for a source type. Subclasses set ``type`` and ``settings``.

    One instance is created per configured source, so ``MYCO_SOURCE__SUPPORT_EU__TYPE=zendesk``
    gives a second, independent Zendesk connector named ``support_eu``.
    """

    type: ClassVar[str]
    settings: ClassVar[Mapping[str, Setting]] = {}
    #: Defaults to the ``.yaml`` file next to the module that defines the class.
    pack_file: ClassVar[Path | None] = None

    registry: ClassVar[dict[str, type[Connector]]] = {}

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        connector_type = cls.__dict__.get("type")
        if not connector_type:
            return
        if not NAME_RE.match(connector_type):
            raise PackError(f"{cls.__qualname__}: invalid connector type {connector_type!r}")
        existing = Connector.registry.get(connector_type)
        if existing is not None and existing.__qualname__ != cls.__qualname__:
            raise PackError(
                f"connector type {connector_type!r} is defined twice "
                f"({existing.__module__}.{existing.__qualname__} and {cls.__module__}.{cls.__qualname__})"
            )
        Connector.registry[connector_type] = cls

    def __init__(self, source: str, config: Mapping[str, str]) -> None:
        self.source = source
        self.config = dict(config)

    async def aclose(self) -> None:
        """Release network clients. Called once on shutdown."""

    # ------------------------------------------------------------------ class helpers
    @classmethod
    def tool_methods(cls) -> dict[str, Callable[..., Any]]:
        return {
            name: member
            for name, member in inspect.getmembers(cls, inspect.isfunction)
            if getattr(member, _TOOL_MARK, False)
        }

    @classmethod
    def resolve_pack_file(cls) -> Path:
        if cls.pack_file is not None:
            return Path(cls.pack_file)
        module = sys.modules[cls.__module__]
        return Path(module.__file__ or "").with_suffix(".yaml")

    @classmethod
    def load_pack(cls) -> Pack:
        pack = Pack.load(cls.resolve_pack_file(), cls.type)
        methods = set(cls.tool_methods())
        declared = set(pack.tools)
        if methods - declared:
            raise PackError(f"pack {cls.type!r}: @tool methods without a label: {sorted(methods - declared)}")
        if declared - methods:
            raise PackError(f"pack {cls.type!r}: labelled tools without a method: {sorted(declared - methods)}")
        return pack

    @classmethod
    def resolve_settings(cls, source: str, values: Mapping[str, str]) -> dict[str, str]:
        unknown = set(values) - set(cls.settings)
        if unknown:
            names = ", ".join(f"MYCO_SOURCE__{source.upper()}__{u.upper()}" for u in sorted(unknown))
            raise PackError(f"source {source!r} ({cls.type}): unknown settings {names}")
        resolved: dict[str, str] = {}
        for key, setting in cls.settings.items():
            value = (values.get(key) or "").strip() or setting.default
            if value is None or value == "":
                if setting.required:
                    raise PackError(
                        f"source {source!r} ({cls.type}): MYCO_SOURCE__{source.upper()}__{key.upper()} "
                        f"is required{f' ({setting.help})' if setting.help else ''}"
                    )
                continue
            resolved[key] = value
        return resolved
