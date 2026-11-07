"""The Myco MCP endpoint: FastMCP + four checks on every call.

1. Identify the agent (Myco key) and its user (forwarded SSO token, else the agent's service
   user, else ``anonymous``).
2. Fix pinned arguments, then map the tool to its action label(s) — one per value of a
   templated argument such as ``{scope}``.
3. Check roles, taints and denies, for every label.
4. Scope the reply by row, withhold tainted rows and redact fields.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import json
import logging
import time
import types
import typing
import weakref
from collections.abc import AsyncIterator, Callable, Mapping
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token, get_http_headers
from fastmcp.server.lifespan import lifespan
from fastmcp.server.middleware import Middleware
from mcp.types import ToolAnnotations
from starlette.requests import Request
from starlette.responses import JSONResponse

from myco_mcp import __version__
from myco_mcp.audit import AuditLog
from myco_mcp.catalog import Catalog, ToolSpec
from myco_mcp.config import ConfigError, Policy, fingerprint, load_policy
from myco_mcp.connector import CallInfo, acting_for, agent_label_value
from myco_mcp.http import UpstreamError
from myco_mcp.identity import AgentKeyVerifier, NoUserTokens, UserTokenError, UserTokenValidator
from myco_mcp.labels import Label
from myco_mcp.policy import (
    Caller,
    PinError,
    ScopeError,
    allowed_values,
    build_caller,
    decide_labels,
    describe_limits,
    pinned_args,
    shape_reply,
    visible,
)
from myco_mcp.settings import Settings, agent_env_name

log = logging.getLogger("myco")
#: Longest witness deposit, in characters; a bigger reply is cut.
WITNESS_MAX_CHARS = 20000

INSTRUCTIONS = (
    "Myco exposes a curated, permission-checked set of SaaS tools. Every tool name starts "
    "with its source (zendesk_, jira_, ...). Calls outside your roles are refused."
)


class PolicyStore:
    """Holds the active policy and swaps it only for a fully valid config folder."""

    def __init__(self, settings: Settings, catalog: Catalog, audit: AuditLog) -> None:
        self.settings = settings
        self.catalog = catalog
        self.audit = audit
        self.current = self._load()  # a broken folder at startup is fatal
        self._report(self.current)

    def _load(self) -> Policy:
        return load_policy(self.settings.config_dir, roles=self.catalog.roles, sources=self.catalog.sources)

    def reload(self) -> bool:
        """Re-read the folder if it changed. Returns True when a new policy is active."""
        try:
            if fingerprint(self.settings.config_dir) == self.current.version:
                return False
            policy = self._load()
        except (ConfigError, OSError) as exc:
            self.audit.record("config.rejected", version=self.current.version, error=str(exc))
            log.error("config rejected, keeping version %s: %s", self.current.version, exc)
            return False
        previous, self.current = self.current, policy
        self.audit.record("config.reloaded", previous=previous.version, version=policy.version)
        self._report(policy)
        return True

    def _report(self, policy: Policy) -> None:
        for warning in policy.warnings:
            log.warning("config: %s", warning)
        keys = set(self.settings.agent_keys)
        for agent in policy.agents.values():
            if agent_env_name(agent.name) not in keys:
                log.warning(
                    "agent %s has no MYCO_AGENT__%s__KEY and cannot connect", agent.name, agent_env_name(agent.name)
                )
        for key_name in keys:
            if policy.agent_for_env_name(key_name) is None:
                log.warning("MYCO_AGENT__%s__KEY has no agents/*.yaml file; it sees no tools", key_name)
        log.info("config version %s: %d agents, %d user entries", policy.version, len(policy.agents), len(policy.users))


class Gate:
    """Runs the four checks for one call and writes the audit record."""

    def __init__(self, settings: Settings, catalog: Catalog, store: PolicyStore, audit: AuditLog) -> None:
        self.settings = settings
        self.catalog = catalog
        self.store = store
        self.audit = audit
        self.users = UserTokenValidator(settings.oidc) if settings.oidc else NoUserTokens()
        #: Witness deposits in flight; kept so they are not garbage-collected mid-way.
        self.witnessing: set[asyncio.Task[None]] = set()

    async def caller(self, policy: Policy | None = None) -> Caller:
        token = get_access_token()
        if token is None:
            raise ToolError("missing agent key: send Authorization: Bearer <MYCO agent key>")
        header = self.settings.user_token_header
        raw = get_http_headers(include={header}).get(header, "").strip()
        if raw.lower().startswith("bearer "):
            raw = raw[7:].strip()
        claims, error = None, None
        if raw:
            try:
                claims = await self.users.claims(raw)
            except UserTokenError as exc:
                error = str(exc)
        return build_caller(policy or self.store.current, token.client_id, claims=claims, user_error=error)

    async def call(self, spec: ToolSpec, arguments: dict[str, Any]) -> Any:
        policy = self.store.current  # one snapshot per call, even if a reload lands meanwhile
        caller = await self.caller(policy)
        record = {
            "agent": caller.agent_name,
            "user": caller.user,
            "tool": spec.name,
            "label": spec.label_text,
            "rule": "",
            "args": sorted(arguments),
            "config": policy.version,
        }
        try:
            arguments = prepare_arguments(caller, spec, arguments, policy.domains)
        except (PinError, ValueError) as exc:
            record["rule"] = "pin:missing-claim" if isinstance(exc, PinError) else "args:invalid"
            self.audit.record("tool.call", decision="deny", reason=str(exc), **record)
            raise ToolError(f"Myco denied {spec.name}: {exc}") from exc
        labels = call_labels(spec, arguments)
        record["label"] = ",".join(str(label) for label in labels) or spec.label_text
        record["args"] = sorted(arguments)
        decision = decide_labels(caller, spec, labels, policy.domains)
        record["rule"] = decision.rule
        if not decision.allowed:
            self.audit.record("tool.call", decision="deny", reason=decision.reason, **record)
            raise ToolError(f"Myco denied {spec.name} ({record['label']}): {decision.reason}")
        started = time.monotonic()
        info = CallInfo(
            agent=caller.agent_name, user=caller.user, tool=spec.name, labels=tuple(str(lb) for lb in labels)
        )
        try:
            with acting_for(info):
                raw = await spec.method(**arguments)
            reply = shape_reply(caller, spec, raw, policy.domains, labels)
        except ScopeError as exc:
            record["rule"] = "scope:outside"
            self.audit.record("tool.call", decision="deny", reason=str(exc), **record)
            raise ToolError(str(exc)) from exc
        except (UpstreamError, ValueError) as exc:
            self.audit.record(
                "tool.call", decision="allow", outcome="error", error=str(exc)[:300], ms=_ms(started), **record
            )
            raise ToolError(str(exc)) from exc
        except Exception as exc:  # timeouts, connection errors: audit, but do not leak internals
            log.exception("tool %s failed", spec.name)
            self.audit.record(
                "tool.call", decision="allow", outcome="error", error=type(exc).__name__, ms=_ms(started), **record
            )
            raise ToolError(f"{spec.source} request failed ({type(exc).__name__})") from exc
        self.audit.record("tool.call", decision="allow", outcome="ok", ms=_ms(started), **record)
        if spec.witness is not None:
            task = asyncio.create_task(self._witness(spec, caller, arguments, reply))
            self.witnessing.add(task)
            task.add_done_callback(self.witnessing.discard)
        return reply

    async def _witness(self, spec: ToolSpec, caller: Caller, arguments: Mapping[str, Any], reply: Any) -> None:
        """Record in memory what this reply showed the agent, after the agent has its answer.

        The reply is the shaped one: exactly what the agent was allowed to see. The writer
        is the calling agent; the record id comes from the pack's `record:`. A failure is
        logged and audited, never raised: the agent's call already succeeded."""
        assert spec.witness is not None and spec.record is not None
        started = time.monotonic()
        record_id, changed = spec.record.render(reply)
        record_id = f"{spec.source}:{record_id}" if record_id else None
        entry = {
            "agent": caller.agent_name,
            "user": caller.user,
            "tool": spec.name,
            "memory": spec.witness.memory,
            "record": record_id,
        }
        try:
            target = self.catalog.sources[spec.witness.memory]
            shown = {k: v for k, v in arguments.items() if v is not None and k != spec.label_arg}
            reply_text = json.dumps(reply, ensure_ascii=False, default=str, indent=1)
            text = (
                f"Shown to {caller.agent_name} by {spec.name}.\n"
                f"Input: {json.dumps(shown, ensure_ascii=False, default=str)}\n"
                f"Result:\n{reply_text}"
            )[:WITNESS_MAX_CHARS]
            result = await target.witness_deposit(
                writer=caller.agent_name or "unknown",
                user=caller.user,
                tool=spec.name,
                text=text,
                scope=spec.witness.scope,
                record_id=record_id,
                occurred_at=changed,
            )
            self.audit.record(
                "tool.witness", outcome="ok", status=(result or {}).get("status"), ms=_ms(started), **entry
            )
        except Exception as exc:  # noqa: BLE001 — witnessing must never hurt the call it follows
            log.warning("witness for %s failed: %s", spec.name, exc)
            self.audit.record("tool.witness", outcome="error", error=str(exc)[:300], ms=_ms(started), **entry)


def prepare_arguments(
    caller: Caller, spec: ToolSpec, arguments: Mapping[str, Any], domains: Mapping[str, frozenset[str]]
) -> dict[str, Any]:
    """Apply pins, then settle the templated label argument.

    An omitted (or empty) label argument becomes every value the caller may use for a
    list argument, or the only one for a single-value argument. A value the connector
    does not know is refused here, before any permission check."""
    args = {**arguments, **pinned_args(caller, spec, domains)}
    arg = spec.label_arg
    if arg is None:
        return args
    if spec.label_fill == "allowed":  # hidden from the agent: always every value it may use
        allowed = allowed_values(caller, spec, domains)
        if not allowed:
            raise ValueError(f"no {arg} you may use")
        args[arg] = allowed if _is_list_param(spec, arg) else allowed[0]
        return args
    if spec.label_fill == "agent":  # hidden from the agent: always the agent itself
        args[arg] = agent_label_value(caller.agent_name or "")
        return args
    value = args.get(arg)
    if value in (None, "", []):
        allowed = allowed_values(caller, spec, domains)
        if _is_list_param(spec, arg):
            args[arg] = allowed
        elif len(allowed) == 1:
            args[arg] = allowed[0]
        else:
            raise ValueError(f"give {arg}: one of {', '.join(allowed) or '(none allowed)'}")
        return args
    values = value if isinstance(value, list) else [value]
    unknown = [v for v in values if v not in spec.label_values]
    if unknown:
        raise ValueError(f"unknown {arg} {', '.join(map(str, unknown))} (one of {', '.join(spec.label_values)})")
    return args


def call_labels(spec: ToolSpec, arguments: Mapping[str, Any]) -> list[Label]:
    if spec.label_arg is None:
        return [spec.label]
    value = arguments.get(spec.label_arg)
    values = value if isinstance(value, list) else [value] if value else []
    return [spec.label_for(v) for v in dict.fromkeys(values)]


def _is_list_param(spec: ToolSpec, name: str) -> bool:
    try:
        annotation = inspect.signature(spec.method, eval_str=True).parameters[name].annotation
    except (KeyError, NameError, TypeError, ValueError):
        return False
    return any(typing.get_origin(c) in (list, tuple) or c in (list, tuple) for c in _flatten_type(annotation))


def _flatten_type(annotation: Any) -> list[Any]:
    """``Annotated[list[str] | None, ...]`` → ``[list[str], NoneType]``."""
    origin = typing.get_origin(annotation)
    if origin is typing.Annotated:
        return _flatten_type(typing.get_args(annotation)[0])
    if origin in (typing.Union, types.UnionType):
        return [t for arg in typing.get_args(annotation) for t in _flatten_type(arg)]
    return [annotation]


def shape_schema(
    parameters: Mapping[str, Any], *, hide: set[str], enum_arg: str | None, enum: list[str]
) -> dict[str, Any]:
    """A caller's view of a tool's input schema: pinned arguments removed, the label
    argument restricted to the values it may use."""
    schema = copy.deepcopy(dict(parameters))
    props = schema.get("properties") or {}
    for name in hide:
        props.pop(name, None)
    if "required" in schema:
        schema["required"] = [r for r in schema["required"] if r not in hide]
    if enum_arg and enum_arg in props:
        _set_string_enum(props[enum_arg], enum)
    return schema


def _set_string_enum(node: Any, enum: list[str]) -> None:
    if not isinstance(node, dict):
        return
    if node.get("type") == "string":
        node["enum"] = list(enum)
    for key in ("anyOf", "oneOf", "allOf"):
        for child in node.get(key, []) or []:
            _set_string_enum(child, enum)
    if "items" in node:
        _set_string_enum(node["items"], enum)


class MycoMiddleware(Middleware):
    """Per-agent ``tools/list``, pinned arguments, and session tracking for ``tools/list_changed``."""

    def __init__(self, gate: Gate) -> None:
        self.gate = gate
        self.sessions: weakref.WeakSet[Any] = weakref.WeakSet()

    async def on_request(self, context: Any, call_next: Any) -> Any:
        if context.fastmcp_context is not None:
            try:
                self.sessions.add(context.fastmcp_context.session)
            except (RuntimeError, TypeError, ValueError):
                pass
        return await call_next(context)

    async def on_list_tools(self, context: Any, call_next: Any) -> Any:
        tools = await call_next(context)
        try:
            caller = await self.gate.caller()
        except ToolError:
            return []
        domains = self.gate.store.current.domains
        shown = []
        for tool in tools:
            spec = self.gate.catalog.tools.get(tool.name)
            if spec is None or not visible(caller, spec, domains):
                continue
            update: dict[str, Any] = {}
            limits = describe_limits(caller, spec, domains)
            if limits:
                update["description"] = f"{tool.description}\n{limits}"
            try:
                pinned = set(pinned_args(caller, spec, domains))
            except PinError:
                pinned = set()
            if spec.label_fill:
                update["parameters"] = shape_schema(
                    tool.parameters, hide={*pinned, spec.label_arg}, enum_arg=None, enum=[]
                )
            elif pinned or spec.label_arg:
                update["parameters"] = shape_schema(
                    tool.parameters,
                    hide=pinned,
                    enum_arg=spec.label_arg,
                    enum=allowed_values(caller, spec, domains) if spec.label_arg else [],
                )
            shown.append(tool.model_copy(update=update) if update else tool)
        return shown

    async def on_call_tool(self, context: Any, call_next: Any) -> Any:
        """Put pinned arguments in before FastMCP validates the call, so a pinned
        required argument the agent never saw does not fail validation. The gate
        applies the same pins again."""
        spec = self.gate.catalog.tools.get(context.message.name)
        if spec is not None:
            try:
                caller = await self.gate.caller()
                pins = pinned_args(caller, spec, self.gate.store.current.domains)
            except (ToolError, PinError):
                pins = {}
            if pins:
                arguments = {**(context.message.arguments or {}), **pins}
                context = context.copy(message=context.message.model_copy(update={"arguments": arguments}))
        return await call_next(context)

    async def notify_tools_changed(self) -> None:
        for session in list(self.sessions):
            try:
                await session.send_tool_list_changed()
            except Exception:  # noqa: BLE001 — a closed session must not stop the others
                self.sessions.discard(session)


def create_server(settings: Settings | None = None, *, audit: AuditLog | None = None) -> tuple[FastMCP, Gate]:
    settings = settings or Settings.from_env()
    audit = audit or AuditLog(settings.audit_file)
    catalog = Catalog.build(settings)
    store = PolicyStore(settings, catalog, audit)
    gate = Gate(settings, catalog, store, audit)
    middleware = MycoMiddleware(gate)
    if not settings.agent_keys:
        log.warning("no MYCO_AGENT__<NAME>__KEY variables set; nobody can connect")

    @lifespan
    async def watch_config(server: FastMCP) -> AsyncIterator[dict[str, Any]]:
        task = asyncio.create_task(_watch(store, middleware, settings.reload_seconds))
        try:
            yield {}
        finally:
            task.cancel()
            await catalog.aclose()

    mcp = FastMCP(
        name="myco",
        version=__version__,
        instructions=INSTRUCTIONS,
        auth=AgentKeyVerifier(settings.agent_keys),
        lifespan=watch_config,
    )
    mcp.add_middleware(middleware)
    for spec in catalog.tools.values():
        mcp.tool(
            _gated(gate, spec),
            name=spec.name,
            description=spec.description,
            annotations=ToolAnnotations(
                readOnlyHint=spec.label.read_only,
                destructiveHint=spec.label.destructive,
                openWorldHint=True,
            ),
            tags={spec.source, str(spec.label)},
        )

    @mcp.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "version": __version__, "config": store.current.version})

    return mcp, gate


def create_app(settings: Settings | None = None) -> Any:
    """ASGI app serving the MCP endpoint (default ``/mcp``) and ``/health``."""
    settings = settings or Settings.from_env()
    mcp, _ = create_server(settings)
    return mcp.http_app(path=settings.mcp_path, stateless_http=settings.stateless_http)


def _gated(gate: Gate, spec: ToolSpec) -> Callable[..., Any]:
    """A function with the connector method's signature that runs through the gate."""
    signature = inspect.signature(spec.method, eval_str=True)

    async def gated(**arguments: Any) -> Any:
        return await gate.call(spec, arguments)

    gated.__name__ = spec.name
    gated.__doc__ = spec.description
    gated.__signature__ = signature  # type: ignore[attr-defined]
    gated.__annotations__ = {
        **{name: p.annotation for name, p in signature.parameters.items()},
        "return": signature.return_annotation,
    }
    return gated


async def _watch(store: PolicyStore, middleware: MycoMiddleware, seconds: float) -> None:
    if seconds <= 0:
        return
    while True:
        await asyncio.sleep(seconds)
        if await asyncio.to_thread(store.reload):
            await middleware.notify_tools_changed()


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
