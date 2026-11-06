"""The Myco MCP endpoint: FastMCP + four checks on every call.

1. Identify the agent (Myco key) and its user (forwarded SSO token, else the agent's service
   user, else ``anonymous``).
2. Map the tool to its action label.
3. Check roles, taints and denies.
4. Scope the reply by row and redact fields.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
import weakref
from collections.abc import AsyncIterator, Callable
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
from myco_mcp.http import UpstreamError
from myco_mcp.identity import AgentKeyVerifier, NoUserTokens, UserTokenError, UserTokenValidator
from myco_mcp.policy import Caller, ScopeError, build_caller, decide, describe_limits, shape_reply
from myco_mcp.settings import Settings, agent_env_name

log = logging.getLogger("myco")

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
        decision = decide(caller, spec, policy.domains)
        record = {
            "agent": caller.agent_name,
            "user": caller.user,
            "tool": spec.name,
            "label": str(spec.label),
            "rule": decision.rule,
            "args": sorted(arguments),
            "config": policy.version,
        }
        if not decision.allowed:
            self.audit.record("tool.call", decision="deny", reason=decision.reason, **record)
            raise ToolError(f"Myco denied {spec.name} ({spec.label}): {decision.reason}")
        started = time.monotonic()
        try:
            reply = shape_reply(caller, spec, await spec.method(**arguments), policy.domains)
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
        return reply


class MycoMiddleware(Middleware):
    """Per-agent ``tools/list`` and session tracking for ``tools/list_changed``."""

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
        visible = []
        for tool in tools:
            spec = self.gate.catalog.tools.get(tool.name)
            if spec is None or not decide(caller, spec, domains).allowed:
                continue
            limits = describe_limits(caller, spec, domains)
            visible.append(tool.model_copy(update={"description": f"{tool.description}\n{limits}"}) if limits else tool)
        return visible

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
