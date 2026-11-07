"""Deterministic access decisions and reply shaping. No LLM, no network.

Effective access = agent roles ∩ user roles, minus explicit denies (which always win),
minus tools whose taints the agent does not tolerate. Allowed replies are then narrowed
to the caller's data scope (rows), stripped of rows whose taints the agent does not
tolerate, and redacted (fields). A tool whose label depends on an argument (``{scope}``)
is decided once per label the call carries; every one must be allowed.
"""

from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from myco_mcp.catalog import ToolSpec
from myco_mcp.config import (
    PLACEHOLDER_RE,
    AgentPolicy,
    Grant,
    Policy,
    Principal,
    RedactRule,
    ScopeRule,
    UserEntry,
)
from myco_mcp.connector import agent_label_value
from myco_mcp.labels import Label

REDACTED = "[redacted]"


class ScopeError(PermissionError):
    """A single-object reply lies outside the caller's data scope."""


class PinError(PermissionError):
    """A pinned argument needs a user claim the caller does not have."""


@dataclass(frozen=True)
class Caller:
    """Who is calling: the agent (from its Myco key) and the user (from the forwarded token)."""

    agent_key_name: str  # SUPPORT_BOT, from MYCO_AGENT__SUPPORT_BOT__KEY
    agent: AgentPolicy | None
    user: str  # "anonymous", or the token's subject / email
    user_entries: tuple[UserEntry, ...]  # matched users.yaml entries
    claims: Mapping[str, Any] = field(default_factory=dict)
    user_error: str | None = None  # set when a forwarded token was rejected

    @property
    def agent_name(self) -> str:
        return self.agent.name if self.agent else self.agent_key_name.lower().replace("_", "-")

    @property
    def user_entry_names(self) -> str:
        return "+".join(e.name for e in self.user_entries) or "(no match)"


@dataclass(frozen=True)
class Decision:
    allowed: bool
    rule: str
    reason: str = ""


def build_caller(
    policy: Policy,
    agent_key_name: str,
    *,
    claims: Mapping[str, Any] | None,
    user_error: str | None = None,
) -> Caller:
    agent = policy.agent_for_env_name(agent_key_name)
    if user_error:
        return Caller(agent_key_name, agent, "rejected-token", (), {}, user_error)
    if claims is None:
        # No user token: the agent's service user if users.yaml names one, else anonymous.
        service = policy.service_users.get(agent.name) if agent else None
        if service is not None:
            return Caller(agent_key_name, agent, f"service:{service.name}", (service,), {})
        return Caller(agent_key_name, agent, policy.anonymous.name, (policy.anonymous,), {})
    entries = tuple(u for u in policy.users if _claims_match(u.match, claims))
    user = str(claims.get("email") or claims.get("preferred_username") or claims.get("sub") or "unknown")
    return Caller(agent_key_name, agent, user, entries, dict(claims))


def decide(
    caller: Caller, tool: ToolSpec, domains: Mapping[str, frozenset[str]], label: Label | None = None
) -> Decision:
    """The decision for one label: the tool's own, or ``label`` for a templated tool."""
    label = label or tool.label
    if caller.user_error:
        return Decision(False, "user:token-rejected", caller.user_error)
    agent = caller.agent
    if agent is None:
        return Decision(False, "agent:no-file", f"no agents/<name>.yaml for key {caller.agent_key_name}")
    for pattern in agent.deny:
        if pattern.matches(label, domains):
            return Decision(False, f"deny:agent:{pattern}", f"agent {agent.name} denies {pattern}")
    for entry in caller.user_entries:
        for pattern in entry.deny:
            if pattern.matches(label, domains):
                return Decision(False, f"deny:user:{entry.name}:{pattern}", f"user {entry.name} denies {pattern}")
    agent_grant = _first_grant(agent.grants, label, domains)
    if agent_grant is None:
        return Decision(False, "agent:no-role", f"no role of agent {agent.name} grants {label}")
    user_grant = None
    user_entry = None
    for entry in caller.user_entries:
        user_grant = _first_grant(entry.grants, label, domains)
        if user_grant is not None:
            user_entry = entry
            break
    if user_grant is None or user_entry is None:
        return Decision(
            False,
            f"user:{caller.user_entry_names}:no-role",
            f"no role of user {caller.user_entry_names} grants {label}",
        )
    for taint in tool.taints:
        if taint not in agent.tolerations:
            return Decision(False, f"taint:{taint}", f"tool is tainted {taint!r}; agent has no toleration")
    return Decision(True, f"allow:{agent_grant.role}&{user_entry.name}/{user_grant.role}")


def decide_labels(
    caller: Caller, tool: ToolSpec, labels: Iterable[Label], domains: Mapping[str, frozenset[str]]
) -> Decision:
    """Every label of one call must be allowed; the first refusal is the answer."""
    rules = []
    for label in labels:
        decision = decide(caller, tool, domains, label)
        if not decision.allowed:
            return decision
        rules.append(decision.rule)
    if not rules:
        return Decision(False, "label:none", f"{tool.name}: the call names no {tool.label_arg}")
    return Decision(True, ",".join(dict.fromkeys(rules)))


def visible(caller: Caller, tool: ToolSpec, domains: Mapping[str, frozenset[str]]) -> bool:
    """Whether ``tools/list`` shows the tool: some label it can carry is allowed."""
    if tool.label_fill == "agent":
        if caller.agent is None:
            return False
        return decide(caller, tool, domains, tool.label_for(agent_label_value(caller.agent.name))).allowed
    return any(decide(caller, tool, domains, label).allowed for label in tool.labels)


def allowed_values(caller: Caller, tool: ToolSpec, domains: Mapping[str, frozenset[str]]) -> list[str]:
    """The values of a templated tool's label argument this caller may use."""
    return [v for v in tool.label_values if decide(caller, tool, domains, tool.label_for(v)).allowed]


def pinned_args(caller: Caller, tool: ToolSpec, domains: Mapping[str, frozenset[str]]) -> dict[str, Any]:
    """Arguments fixed by the agent's and user's pin rules, rendered for this caller.

    A rule applies when its pattern matches any label the tool can carry, and only to
    arguments the tool has. Later principals (the user entries) win over the agent."""
    params = _parameters(tool)
    pinned: dict[str, Any] = {}
    for principal in _principals(caller):
        for rule in principal.pin:
            if not any(rule.match.matches(label, domains) for label in tool.labels):
                continue
            for name, value in rule.args.items():
                if name in params:
                    pinned[name] = _render_pin(name, value, caller, principal.name)
    return pinned


def shape_reply(
    caller: Caller,
    tool: ToolSpec,
    reply: Any,
    domains: Mapping[str, frozenset[str]],
    labels: Iterable[Label] | None = None,
) -> Any:
    """Filter rows by data scope, then redact fields.

    Returns a new object. ``labels`` are the call's labels (default: the tool's)."""
    labels = tuple(labels) if labels is not None else (tool.label,)
    scopes = _rules_any(caller, "scope", labels, domains)
    redactions = redaction_paths(caller, tool, domains, labels)
    if not scopes and not redactions:
        return reply
    shaped = copy.deepcopy(reply)
    if scopes:
        conditions = [(path, _render(value, caller)) for rule in scopes for path, value in rule.where.items()]
        if isinstance(shaped, list):
            shaped = [row for row in shaped if _row_in_scope(row, conditions)]
        elif not _row_in_scope(shaped, conditions):
            raise ScopeError(f"{tool.label}: this record is outside your data scope")
    rows = shaped if isinstance(shaped, list) else [shaped]
    for path in redactions:
        for row in rows:
            _redact(row, path.split("."))
    return shaped


def redaction_paths(
    caller: Caller,
    tool: ToolSpec,
    domains: Mapping[str, frozenset[str]],
    labels: Iterable[Label] | None = None,
) -> list[str]:
    tolerations = caller.agent.tolerations if caller.agent else frozenset()
    paths = [p for taint, ps in tool.taint_fields.items() if taint not in tolerations for p in ps]
    labels = tuple(labels) if labels is not None else (tool.label,)
    for rule in _rules_any(caller, "redact", labels, domains):
        paths.extend(rule.fields)
    return list(dict.fromkeys(paths))


def describe_limits(caller: Caller, tool: ToolSpec, domains: Mapping[str, frozenset[str]]) -> str:
    """Text appended to a tool description so the agent knows its limits."""
    notes = []
    labels = tool.labels
    if tool.label_arg and not tool.label_fill:
        values = allowed_values(caller, tool, domains)
        notes.append(f"Allowed {tool.label_arg}: {', '.join(values)}.")
    scopes = _rules_any(caller, "scope", labels, domains)
    if scopes:
        conditions = ", ".join(f"{k} = {v}" for rule in scopes for k, v in rule.where.items())
        notes.append(f"Data scope: only records where {conditions}.")
    redacted = redaction_paths(caller, tool, domains, labels)
    if redacted:
        notes.append(f"Redacted fields: {', '.join(redacted)}.")
    try:
        pinned = pinned_args(caller, tool, domains)
    except PinError:
        pinned = {}
    if pinned:
        notes.append("Fixed for you: " + ", ".join(f"{k} = {v}" for k, v in pinned.items()) + ".")
    return " ".join(notes)


# ---------------------------------------------------------------------- helpers
def _first_grant(grants: Iterable[Grant], label: Label, domains: Mapping[str, frozenset[str]]) -> Grant | None:
    return next((g for g in grants if g.pattern.matches(label, domains)), None)


def _principals(caller: Caller) -> list[Principal]:
    return [*([caller.agent] if caller.agent else []), *caller.user_entries]


def _rules(caller: Caller, kind: str, label: Label, domains: Mapping[str, frozenset[str]]) -> list[Any]:
    return _rules_any(caller, kind, (label,), domains)


def _rules_any(caller: Caller, kind: str, labels: Iterable[Label], domains: Mapping[str, frozenset[str]]) -> list[Any]:
    labels = tuple(labels)
    rules: list[ScopeRule | RedactRule] = []
    for principal in _principals(caller):
        rules.extend(r for r in getattr(principal, kind) if any(r.match.matches(label, domains) for label in labels))
    return rules


def _parameters(tool: ToolSpec) -> set[str]:
    import inspect

    try:
        return set(inspect.signature(tool.method).parameters)
    except (TypeError, ValueError):
        return set()


def _render_pin(name: str, value: Any, caller: Caller, owner: str) -> Any:
    if isinstance(value, list):
        return [_render_pin(name, v, caller, owner) for v in value]
    if not isinstance(value, str):
        return value
    rendered = _render(value, caller)
    if rendered is None:
        raise PinError(f"{owner}: pinned argument {name!r} needs a user claim this caller does not have")
    return rendered


def _claims_match(match: Mapping[str, Any], claims: Mapping[str, Any]) -> bool:
    for claim, expected in match.items():
        actual = _get_path(claims, claim)
        wanted = expected if isinstance(expected, list) else [expected]
        values = actual if isinstance(actual, list) else [actual]
        if not any(str(v) == str(w) for v in values if v is not None for w in wanted):
            return False
    return True


def _render(template: str, caller: Caller) -> str | None:
    """Fill ``{user.<claim>}`` and ``{agent.name}``. ``None`` if a placeholder is missing."""
    missing = False

    def sub(match: Any) -> str:
        nonlocal missing
        key = match.group(1)
        if key == "agent.name":
            return caller.agent_name
        value = _get_path(caller.claims, key.removeprefix("user."))
        if value is None or isinstance(value, (dict, list)):
            missing = True
            return ""
        return str(value)

    rendered = PLACEHOLDER_RE.sub(sub, template)
    return None if missing else rendered


def _row_in_scope(row: Any, conditions: list[tuple[str, str | None]]) -> bool:
    if not isinstance(row, dict):
        return False
    for path, wanted in conditions:
        if wanted is None:
            return False
        actual = _get_path(row, path)
        values = actual if isinstance(actual, list) else [actual]
        if not any(v is not None and str(v) == wanted for v in values):
            return False
    return True


def _get_path(obj: Any, path: str) -> Any:
    if isinstance(obj, Mapping) and path in obj:  # claim names may contain dots
        return obj[path]
    current = obj
    for part in path.split("."):
        if isinstance(current, Mapping) and part in current:
            current = current[part]
        else:
            return None
    return current


def _redact(obj: Any, parts: list[str]) -> None:
    if not parts:
        return
    head, rest = parts[0], parts[1:]
    if head == "*":
        children = obj.values() if isinstance(obj, dict) else obj if isinstance(obj, list) else []
        if not rest:
            if isinstance(obj, dict):
                for key in obj:
                    obj[key] = REDACTED
            elif isinstance(obj, list):
                obj[:] = [REDACTED] * len(obj)
            return
        for child in list(children):
            _redact(child, rest)
        return
    if isinstance(obj, list):
        for child in obj:
            _redact(child, parts)
        return
    if not isinstance(obj, dict) or head not in obj:
        return
    if rest:
        _redact(obj[head], rest)
    elif obj[head] not in (None, "", [], {}):
        obj[head] = REDACTED
