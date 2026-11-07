"""Permissions as YAML in Git: the config folder.

    myco-config/
      agents/<agent>.yaml   one file per agent (roles, permissions, deny, may_see, scope, redact, pin)
      users.yaml            SSO claim matching, service users and the built-in ``anonymous`` user
      domains.yaml          optional groups of sources, e.g. support: [zendesk, zendesk]

The whole folder is validated on every load. A broken folder is refused as a whole and
the last good policy keeps running.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

import yaml

from myco_mcp.labels import NAME_RE, LabelError, LabelPattern
from myco_mcp.settings import agent_env_name

AGENT_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")
PLACEHOLDER_RE = re.compile(r"\{([^{}]*)\}")
VALID_PLACEHOLDER_RE = re.compile(r"^(user\.[A-Za-z0-9_.:-]+|agent\.name)$")
ANONYMOUS = "anonymous"


class ConfigError(ValueError):
    """The config folder is invalid; the previous policy stays active."""


@dataclass(frozen=True)
class Grant:
    role: str
    pattern: LabelPattern


@dataclass(frozen=True)
class ScopeRule:
    """Rows whose fields do not equal the rendered values are removed from replies."""

    match: LabelPattern
    where: Mapping[str, str]


@dataclass(frozen=True)
class RedactRule:
    match: LabelPattern
    fields: tuple[str, ...]


@dataclass(frozen=True)
class PinRule:
    """Arguments fixed for every matching call, whatever the agent sends.

    Values may use ``{user.<claim>}`` and ``{agent.name}``. A pinned argument is
    removed from the tool's schema in ``tools/list``, so the agent never sees it."""

    match: LabelPattern
    args: Mapping[str, Any]


@dataclass(frozen=True)
class Principal:
    """Shared shape of an agent file and a users.yaml entry."""

    name: str
    roles: tuple[str, ...] = ()
    grants: tuple[Grant, ...] = ()
    deny: tuple[LabelPattern, ...] = ()
    scope: tuple[ScopeRule, ...] = ()
    redact: tuple[RedactRule, ...] = ()
    pin: tuple[PinRule, ...] = ()


@dataclass(frozen=True)
class AgentPolicy(Principal):
    owner: str = ""
    description: str = ""
    #: Kinds of sensitive data the agent may see (pack `sensitive` / `sensitive_fields`).
    may_see: frozenset[str] = frozenset()


@dataclass(frozen=True)
class UserEntry(Principal):
    match: Mapping[str, Any] = field(default_factory=dict)
    #: Service user: the agents that run as this user when they call without a user token.
    service_for: tuple[str, ...] = ()


@dataclass(frozen=True)
class Policy:
    version: str
    agents: Mapping[str, AgentPolicy]
    users: tuple[UserEntry, ...]  # SSO entries, matched on token claims
    anonymous: UserEntry
    #: agent name -> its service user, used instead of anonymous for calls without a token
    service_users: Mapping[str, UserEntry]
    domains: Mapping[str, frozenset[str]]
    warnings: tuple[str, ...] = ()

    def agent_for_env_name(self, env_name: str) -> AgentPolicy | None:
        for agent in self.agents.values():
            if agent_env_name(agent.name) == env_name:
                return agent
        return None


def config_files(config_dir: Path) -> list[Path]:
    if not config_dir.is_dir():
        return []
    # Skip dot-paths: Kubernetes ConfigMap mounts keep timestamped copies under ..<date>/,
    # and a git checkout has .git/.
    return sorted(
        p
        for p in config_dir.rglob("*")
        if p.is_file()
        and p.suffix in (".yaml", ".yml")
        and not any(part.startswith(".") for part in p.relative_to(config_dir).parts)
    )


def fingerprint(config_dir: Path) -> str:
    digest = hashlib.sha256()
    for path in config_files(config_dir):
        digest.update(str(path.relative_to(config_dir)).encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()[:12]


def load_policy(
    config_dir: Path,
    *,
    roles: Mapping[str, tuple[LabelPattern, ...]],
    sources: Iterable[str],
) -> Policy:
    """Read and validate the whole folder. Raises :class:`ConfigError` on any problem."""
    if not config_dir.is_dir():
        raise ConfigError(f"config folder {config_dir} does not exist")
    resolver = _RoleResolver(roles, set(sources))
    version = fingerprint(config_dir)

    domains = _load_domains(config_dir / "domains.yaml", resolver)
    agents: dict[str, AgentPolicy] = {}
    agents_dir = config_dir / "agents"
    for path in sorted(agents_dir.glob("*.y*ml")) if agents_dir.is_dir() else []:
        agent = _load_agent(path, resolver)
        if agent.name in agents:
            raise ConfigError(f"{path}: agent {agent.name!r} is defined twice")
        agents[agent.name] = agent
    env_names = [agent_env_name(name) for name in agents]
    if len(set(env_names)) != len(env_names):
        raise ConfigError("two agent names map to the same MYCO_AGENT__<NAME>__KEY variable")

    users, anonymous, service_users = _load_users(config_dir / "users.yaml", resolver, agents)
    return Policy(
        version=version,
        agents=agents,
        users=users,
        anonymous=anonymous,
        service_users=service_users,
        domains=domains,
        warnings=tuple(resolver.warnings),
    )


# ---------------------------------------------------------------------- loaders
def _read_yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc


def _load_domains(path: Path, resolver: _RoleResolver) -> dict[str, frozenset[str]]:
    if not path.exists():
        return {}
    raw = _read_yaml(path) or {}
    if isinstance(raw, dict) and set(raw) == {"domains"}:
        raw = raw["domains"] or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: expected a mapping of domain -> [sources]")
    domains: dict[str, frozenset[str]] = {}
    for name, members in raw.items():
        if not isinstance(name, str) or not NAME_RE.match(name):
            raise ConfigError(f"{path}: invalid domain name {name!r}")
        if name in resolver.sources:
            raise ConfigError(f"{path}: domain {name!r} has the same name as a source")
        if not isinstance(members, list) or not all(isinstance(m, str) for m in members):
            raise ConfigError(f"{path}: domain {name!r} must list source names")
        for member in members:
            if member not in resolver.sources:
                resolver.warnings.append(f"{path.name}: domain {name!r} lists unconfigured source {member!r}")
        domains[name] = frozenset(members)
    return domains


def _load_agent(path: Path, resolver: _RoleResolver) -> AgentPolicy:
    raw = _read_yaml(path)
    origin = str(path)
    if not isinstance(raw, dict):
        raise ConfigError(f"{origin}: expected a mapping")
    if "tolerations" in raw:
        raise ConfigError(f"{origin}: 'tolerations' was renamed 'may_see'")
    _only_keys(
        raw,
        {"agent", "owner", "description", "roles", "permissions", "deny", "may_see", "scope", "redact", "pin"},
        origin,
    )
    name = raw.get("agent")
    if not isinstance(name, str) or not AGENT_NAME_RE.match(name):
        raise ConfigError(f"{origin}: 'agent' must be a lowercase name like support-bot")
    if name != path.stem:
        raise ConfigError(f"{origin}: agent {name!r} must live in agents/{name}.yaml")
    principal = _principal(name, raw, resolver, origin)
    may_see = _str_list(raw.get("may_see"), f"{origin}: may_see")
    for kind in may_see:
        if not NAME_RE.match(kind):
            raise ConfigError(f"{origin}: invalid kind of sensitive data {kind!r} in may_see")
    return AgentPolicy(
        **principal,
        owner=str(raw.get("owner") or ""),
        description=str(raw.get("description") or "").strip(),
        may_see=frozenset(may_see),
    )


def _load_users(
    path: Path, resolver: _RoleResolver, agents: Mapping[str, AgentPolicy]
) -> tuple[tuple[UserEntry, ...], UserEntry, dict[str, UserEntry]]:
    anonymous = UserEntry(name=ANONYMOUS)
    service_users: dict[str, UserEntry] = {}
    if not path.exists():
        return (), anonymous, service_users
    raw = _read_yaml(path) or {}
    origin = str(path)
    if not isinstance(raw, dict):
        raise ConfigError(f"{origin}: expected a mapping")
    _only_keys(raw, {"users"}, origin)
    entries = raw.get("users") or {}
    if not isinstance(entries, dict):
        raise ConfigError(f"{origin}: 'users' must map a name to an entry")
    users: list[UserEntry] = []
    for name, spec in entries.items():
        where = f"{origin}: user {name!r}"
        if not isinstance(name, str) or not AGENT_NAME_RE.match(name.replace("_", "-")):
            raise ConfigError(f"{origin}: invalid user entry name {name!r}")
        if not isinstance(spec, dict):
            raise ConfigError(f"{where}: expected a mapping")
        _only_keys(spec, {"match", "service_for", "roles", "permissions", "deny", "scope", "redact", "pin"}, where)
        principal = _principal(name, spec, resolver, where)
        match = spec.get("match")
        service_for = _str_list(spec.get("service_for"), f"{where}: service_for")
        if name == ANONYMOUS:
            if match or service_for:
                raise ConfigError(f"{where}: the built-in anonymous user cannot have 'match' or 'service_for'")
            anonymous = UserEntry(**principal)
            continue
        if service_for:
            if match:
                raise ConfigError(f"{where}: use either 'match' (SSO users) or 'service_for' (service users)")
            entry = UserEntry(**principal, service_for=tuple(service_for))
            for agent in service_for:
                if not AGENT_NAME_RE.match(agent):
                    raise ConfigError(f"{where}: invalid agent name {agent!r} in service_for")
                if agent in service_users:
                    raise ConfigError(
                        f"{where}: agent {agent!r} already runs as service user {service_users[agent].name!r}"
                    )
                if agent not in agents:
                    resolver.warnings.append(f"{where}: service_for lists unknown agent {agent!r}")
                service_users[agent] = entry
            continue
        if not isinstance(match, dict) or not match:
            raise ConfigError(
                f"{where}: needs 'match' (token claims, for SSO users) or 'service_for' (agents, for service users)"
            )
        users.append(UserEntry(**principal, match=dict(match)))
    return tuple(users), anonymous, service_users


def _principal(name: str, raw: Mapping[str, Any], resolver: _RoleResolver, origin: str) -> dict[str, Any]:
    roles = tuple(_str_list(raw.get("roles"), f"{origin}: roles"))
    return {
        "name": name,
        "roles": roles,
        "grants": (
            *resolver.resolve(roles, origin),
            *(
                Grant(role="permissions", pattern=p)
                for p in _patterns(raw.get("permissions"), f"{origin}: permissions")
            ),
        ),
        "deny": tuple(_patterns(raw.get("deny"), f"{origin}: deny")),
        "scope": tuple(_scope_rules(raw.get("scope"), origin)),
        "redact": tuple(_redact_rules(raw.get("redact"), origin)),
        "pin": tuple(_pin_rules(raw.get("pin"), origin)),
    }


def _pin_rules(raw: Any, origin: str) -> Iterable[PinRule]:
    for i, item in enumerate(_list(raw, f"{origin}: pin")):
        where = f"{origin}: pin[{i}]"
        if not isinstance(item, dict):
            raise ConfigError(f"{where}: expected {{match, args}}")
        _only_keys(item, {"match", "args"}, where)
        args = item.get("args")
        if not isinstance(args, dict) or not args:
            raise ConfigError(f"{where}: 'args' must map argument names to values")
        for name, value in args.items():
            if not isinstance(name, str) or not NAME_RE.match(name):
                raise ConfigError(f"{where}: invalid argument name {name!r}")
            items = value if isinstance(value, list) else [value]
            for v in items:
                if isinstance(v, (dict, list)) or v is None:
                    raise ConfigError(f"{where}: {name!r} must be a value or a list of values")
                for placeholder in PLACEHOLDER_RE.findall(str(v)):
                    if not VALID_PLACEHOLDER_RE.match(placeholder):
                        raise ConfigError(
                            f"{where}: unknown placeholder {{{placeholder}}} (use {{user.<claim>}} or {{agent.name}})"
                        )
        yield PinRule(match=_pattern(item.get("match"), where), args=dict(args))


def _scope_rules(raw: Any, origin: str) -> Iterable[ScopeRule]:
    for i, item in enumerate(_list(raw, f"{origin}: scope")):
        where = f"{origin}: scope[{i}]"
        if not isinstance(item, dict):
            raise ConfigError(f"{where}: expected {{match, where}}")
        _only_keys(item, {"match", "where"}, where)
        conditions = item.get("where")
        if not isinstance(conditions, dict) or not conditions:
            raise ConfigError(f"{where}: 'where' must map reply fields to values")
        for value in conditions.values():
            for placeholder in PLACEHOLDER_RE.findall(str(value)):
                if not VALID_PLACEHOLDER_RE.match(placeholder):
                    raise ConfigError(
                        f"{where}: unknown placeholder {{{placeholder}}} (use {{user.<claim>}} or {{agent.name}})"
                    )
        yield ScopeRule(
            match=_pattern(item.get("match"), where),
            where={str(k): str(v) for k, v in conditions.items()},
        )


def _redact_rules(raw: Any, origin: str) -> Iterable[RedactRule]:
    for i, item in enumerate(_list(raw, f"{origin}: redact")):
        where = f"{origin}: redact[{i}]"
        if not isinstance(item, dict):
            raise ConfigError(f"{where}: expected {{match, fields}}")
        _only_keys(item, {"match", "fields"}, where)
        fields = _str_list(item.get("fields"), f"{where}: fields")
        if not fields:
            raise ConfigError(f"{where}: 'fields' must list reply field paths")
        yield RedactRule(match=_pattern(item.get("match"), where), fields=tuple(fields))


def _patterns(raw: Any, origin: str) -> list[LabelPattern]:
    return [_pattern(item, origin) for item in _str_list(raw, origin)]


def _pattern(raw: Any, origin: str) -> LabelPattern:
    try:
        return LabelPattern.parse(str(raw if raw is not None else ""))
    except LabelError as exc:
        raise ConfigError(f"{origin}: {exc}") from exc


def _list(raw: Any, origin: str) -> list[Any]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ConfigError(f"{origin}: expected a list")
    return raw


def _str_list(raw: Any, origin: str) -> list[str]:
    items = _list(raw, origin)
    if not all(isinstance(i, str) for i in items):
        raise ConfigError(f"{origin}: expected a list of strings")
    return items


def _only_keys(raw: Mapping[str, Any], allowed: set[str], origin: str) -> None:
    unknown = set(raw) - allowed
    if unknown:
        raise ConfigError(f"{origin}: unknown keys {sorted(unknown)} (allowed: {sorted(allowed)})")


class _RoleResolver:
    """Turns role references (``zendesk:reader``, ``*:reader``) into label grants."""

    def __init__(self, roles: Mapping[str, tuple[LabelPattern, ...]], sources: set[str]) -> None:
        self.roles = roles
        self.sources = sources
        self.warnings: list[str] = []

    def resolve(self, refs: Iterable[str], origin: str) -> list[Grant]:
        grants: list[Grant] = []
        for ref in refs:
            if any(ch in ref for ch in "*?["):
                names = [r for r in self.roles if fnmatchcase(r, ref)]
                if not names:
                    self.warnings.append(f"{origin}: role pattern {ref!r} matches no configured role")
            else:
                source, sep, role = ref.partition(":")
                if not sep or not NAME_RE.match(source) or not NAME_RE.match(role):
                    raise ConfigError(f"{origin}: role {ref!r} must look like source:role, e.g. zendesk:reader")
                if ref in self.roles:
                    names = [ref]
                elif source in self.sources:
                    available = ", ".join(sorted(r for r in self.roles if r.startswith(f"{source}:")))
                    raise ConfigError(f"{origin}: unknown role {ref!r} (available: {available})")
                else:
                    self.warnings.append(f"{origin}: role {ref!r} ignored, source {source!r} is not configured")
                    names = []
            for name in names:
                grants.extend(Grant(role=name, pattern=p) for p in self.roles[name])
        return grants
