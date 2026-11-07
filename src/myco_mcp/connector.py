"""Connector base class and pack format.

A connector is two files with the same name:

* ``acme.py``   — a :class:`Connector` subclass whose ``@tool`` methods call the vendor API.
* ``acme.yaml`` — the pack: one label per tool, optional sensitive data, and the roles.

Myco refuses to start when the two disagree: every ``@tool`` method needs a label in the
pack and every pack tool needs a method, so no tool can ever be published unlabelled.

A label's entity may come from an argument: ``action: "{scope}:list"`` labels a call
with ``scope="sales"`` as ``<source>:sales:list``. With ``fill``, Myco sets that argument
itself and hides it from the agent: ``fill: allowed`` passes every value the caller may
use, ``fill: agent`` passes the calling agent's own name. The connector names the values the
argument may take (:meth:`Connector.label_values`), and roles can be templated the same
way (``"{scope}_reader": ["{scope}:list"]`` gives one role per value).
"""

from __future__ import annotations

import inspect
import re
import sys
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

import yaml

from myco_mcp.labels import NAME_RE, Label, LabelError, LabelPattern

_TOOL_MARK = "__myco_tool__"
#: ``{argument}`` in the entity position of an action, or in a role name or permission.
TEMPLATE_RE = re.compile(r"\{([a-z][a-z0-9_]*)\}")
#: Stands in for a templated entity while a pack is validated, before values are known.
_STAND_IN = "x_value"
#: How Myco fills a hidden label argument: every allowed value, or the calling agent.
FILL_MODES = ("allowed", "agent")


def agent_label_value(agent_name: str) -> str:
    """An agent's name as a label entity: ``support-bot`` -> ``support_bot``."""
    return agent_name.lower().replace("-", "_").replace(".", "_")


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


#: ``{field}`` or ``{nested.field}`` in a record id template.
RECORD_FIELD_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\}")


@dataclass(frozen=True)
class RecordDef:
    """Which record a tool's reply is about, so Myco can witness it (see WITNESS).

    ``id`` is a template over reply fields, e.g. ``ticket:{id}`` or ``issue:{fields.key}``;
    Myco prefixes it with the source name (``zendesk:ticket:123``). ``time`` is the dot path
    of the record's last change, e.g. ``updated_at``."""

    id: str
    time: str | None = None

    def render(self, reply: Any) -> tuple[str | None, Any]:
        """(record id, time) for one reply object; the id is None when a field is missing."""
        if not isinstance(reply, Mapping):
            return None, None
        missing = False

        def sub(match: re.Match[str]) -> str:
            nonlocal missing
            value = _dig(reply, match.group(1))
            if value is None or value == "":
                missing = True
                return ""
            return str(value)

        rendered = RECORD_FIELD_RE.sub(sub, self.id)
        return (None if missing else rendered), (_dig(reply, self.time) if self.time else None)


def _dig(obj: Any, path: str) -> Any:
    for part in path.split("."):
        if not isinstance(obj, Mapping):
            return None
        obj = obj.get(part)
    return obj


@dataclass(frozen=True)
class ToolDef:
    name: str
    action: str  # "entity:operation", or "{argument}:operation"
    #: Kinds of sensitive data the whole tool is about, e.g. ("pii",): hidden from agents
    #: whose file does not list them under `may_see`.
    sensitive: tuple[str, ...] = ()
    #: kind -> dot paths in the reply holding it, e.g. {"pii": ("requester.email",)}:
    #: redacted for agents that may not see that kind.
    sensitive_fields: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    #: The argument whose value is the label's entity, for ``{argument}:operation`` actions.
    label_from: str | None = None
    #: How to name the record a reply is about; only tools with one can be witnessed.
    record: RecordDef | None = None
    #: "allowed" or "agent": Myco fills the label argument and hides it (see module doc).
    label_fill: str | None = None

    @property
    def operation(self) -> str:
        return self.action.rpartition(":")[2]

    def entity_for(self, value: str | None = None) -> str:
        """The label's entity: fixed, or the argument value (a stand-in when unknown)."""
        if self.label_from is None:
            return self.action.partition(":")[0]
        return value if value is not None else _STAND_IN


@dataclass(frozen=True)
class CallInfo:
    """Who a tool call acts for. Myco sets it for the duration of every allowed call."""

    agent: str
    user: str
    tool: str
    labels: tuple[str, ...]


_CURRENT_CALL: ContextVar[CallInfo | None] = ContextVar("myco_call", default=None)


def current_call() -> CallInfo | None:
    """The agent and user the running tool call acts for (``None`` outside a call).

    A connector reads this instead of taking "who am I" as an argument, so an agent
    can never claim to be someone else."""
    return _CURRENT_CALL.get()


@contextmanager
def acting_for(info: CallInfo) -> Iterator[None]:
    token = _CURRENT_CALL.set(info)
    try:
        yield
    finally:
        _CURRENT_CALL.reset(token)


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
        templated = {t.label_from for t in tools.values() if t.label_from}

        labels = [Label.parse(f"x:{t.entity_for()}:{t.operation}") for t in tools.values()]
        roles: dict[str, tuple[str, ...]] = {}
        for role, perms in (raw.get("roles") or {}).items():
            role = str(role)
            placeholders = set(TEMPLATE_RE.findall(role))
            if len(placeholders) > 1 or not (placeholders <= templated):
                raise PackError(
                    f"{origin}: role {role!r}: a role name may use one placeholder, "
                    f"from a templated action ({', '.join(sorted(templated)) or 'none here'})"
                )
            if not NAME_RE.match(TEMPLATE_RE.sub(_STAND_IN, role)):
                raise PackError(f"{origin}: invalid role name {role!r}")
            if not isinstance(perms, list) or not perms:
                raise PackError(f"{origin}: role {role!r} must list entity:operation patterns")
            for perm in perms:
                used = set(TEMPLATE_RE.findall(str(perm)))
                if used - placeholders:
                    raise PackError(f"{origin}: role {role!r}: {perm!r} uses a placeholder the role name does not")
                try:
                    pattern = LabelPattern.parse(f"x:{TEMPLATE_RE.sub(_STAND_IN, str(perm))}")
                except LabelError as exc:
                    raise PackError(f"{origin}: role {role!r}: {exc}") from exc
                if not any(pattern.matches(label) for label in labels):
                    raise PackError(f"{origin}: role {role!r}: {perm!r} matches no tool in this pack")
            roles[role] = tuple(str(p) for p in perms)
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
    for old, new in (("taints", "sensitive"), ("taint_fields", "sensitive_fields")):
        if old in spec:
            raise PackError(f"{origin}: tool {name!r}: {old!r} was renamed {new!r}")
    _only_keys(spec, {"action", "sensitive", "sensitive_fields", "fill", "record"}, f"{origin}: tool {name!r}")
    action = str(spec.get("action", ""))
    entity, _, _ = action.partition(":")
    template = TEMPLATE_RE.fullmatch(entity)
    label_from = template.group(1) if template else None
    try:
        Label.parse(f"x:{_STAND_IN if label_from else entity}:{action.rpartition(':')[2]}")
        if action.count(":") != 1:
            raise LabelError(f"action {action!r} must have the form entity:operation")
    except LabelError as exc:
        raise PackError(f"{origin}: tool {name!r}: {exc}") from exc
    sensitive = tuple(str(t) for t in spec.get("sensitive") or ())
    fields_raw = spec.get("sensitive_fields") or {}
    if not isinstance(fields_raw, dict):
        raise PackError(f"{origin}: tool {name!r}: sensitive_fields must map kind -> [paths]")
    sensitive_fields = {str(t): tuple(str(p) for p in paths) for t, paths in fields_raw.items()}
    for kind in (*sensitive, *sensitive_fields):
        if not NAME_RE.match(kind):
            raise PackError(f"{origin}: tool {name!r}: invalid kind of sensitive data {kind!r}")
    fill = spec.get("fill")
    if fill is not None and (fill not in FILL_MODES or label_from is None):
        raise PackError(f"{origin}: tool {name!r}: fill must be one of {', '.join(FILL_MODES)}, on a templated action")
    return ToolDef(
        name=name,
        action=action,
        sensitive=sensitive,
        sensitive_fields=sensitive_fields,
        label_from=label_from,
        label_fill=fill,
        record=_parse_record(spec.get("record"), f"{origin}: tool {name!r}"),
    )


def _parse_record(raw: Any, origin: str) -> RecordDef | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        raw = {"id": raw}
    if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not raw["id"].strip():
        raise PackError(f"{origin}: record needs an id template, e.g. {{id: 'ticket:{{id}}'}}")
    _only_keys(raw, {"id", "time"}, f"{origin}: record")
    template = raw["id"].strip()
    if not RECORD_FIELD_RE.search(template):
        raise PackError(f"{origin}: record id {template!r} names no reply field, e.g. 'ticket:{{id}}'")
    time_path = raw.get("time")
    if time_path is not None and (not isinstance(time_path, str) or not time_path.strip()):
        raise PackError(f"{origin}: record time must be a dot path, e.g. updated_at")
    return RecordDef(id=template, time=time_path.strip() if time_path else None)


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

    def label_values(self, argument: str) -> tuple[str, ...]:
        """The values an argument used in a templated label may take, e.g. the scopes.

        Each value becomes a label entity, so it must be a lowercase name. Override in
        connectors whose pack has ``{argument}:operation`` actions."""
        raise PackError(f"source {self.source!r} ({self.type}): no values for templated argument {argument!r}")

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
        tool_methods = cls.tool_methods()
        for tool_def in pack.tools.values():
            if tool_def.label_from is None:
                continue
            params = inspect.signature(tool_methods[tool_def.name]).parameters
            if tool_def.label_from not in params:
                raise PackError(
                    f"pack {cls.type!r}: tool {tool_def.name!r} takes its label from "
                    f"{tool_def.label_from!r}, which is not one of its arguments"
                )
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
