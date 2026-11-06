"""Process settings, read once from environment variables at startup.

Sources:     MYCO_SOURCE__<NAME>__<SETTING>=value   (TYPE picks the pack, defaults to NAME)
Agent keys:  MYCO_AGENT__<NAME>__KEY=value          (agent ``support-bot`` -> SUPPORT_BOT)
User tokens: MYCO_OIDC__ISSUER, MYCO_OIDC__AUDIENCE, optional MYCO_OIDC__JWKS_URL
Everything else is a plain ``MYCO_*`` variable, listed on :class:`Settings`.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from myco_mcp.labels import NAME_RE

SOURCE_PREFIX = "MYCO_SOURCE__"
AGENT_PREFIX = "MYCO_AGENT__"
OIDC_PREFIX = "MYCO_OIDC__"


class SettingsError(ValueError):
    """The environment is incomplete or contradictory."""


@dataclass(frozen=True)
class SourceSettings:
    name: str
    type: str
    values: Mapping[str, str]


@dataclass(frozen=True)
class OidcSettings:
    issuer: str
    audience: str
    jwks_url: str | None = None
    algorithms: tuple[str, ...] = ("RS256", "ES256", "PS256")


@dataclass(frozen=True)
class Settings:
    config_dir: Path
    host: str = "0.0.0.0"
    port: int = 8080
    mcp_path: str = "/mcp"
    reload_seconds: float = 10.0
    audit_file: Path | None = None
    log_level: str = "INFO"
    plugins: tuple[str, ...] = ()
    user_token_header: str = "x-myco-user-token"
    stateless_http: bool = False
    sources: tuple[SourceSettings, ...] = ()
    #: env agent name (``SUPPORT_BOT``) -> key
    agent_keys: Mapping[str, str] = field(default_factory=dict)
    oidc: OidcSettings | None = None

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        env = dict(os.environ if environ is None else environ)
        oidc_values = _prefixed(env, OIDC_PREFIX)
        oidc = None
        if oidc_values:
            issuer = oidc_values.get("ISSUER", "").strip()
            audience = oidc_values.get("AUDIENCE", "").strip()
            if not issuer or not audience:
                raise SettingsError("MYCO_OIDC__ISSUER and MYCO_OIDC__AUDIENCE must be set together")
            algorithms = tuple(a.strip() for a in oidc_values.get("ALGORITHMS", "").split(",") if a.strip())
            oidc = OidcSettings(
                issuer=issuer,
                audience=audience,
                jwks_url=oidc_values.get("JWKS_URL", "").strip() or None,
                **({"algorithms": algorithms} if algorithms else {}),
            )
        audit_file = env.get("MYCO_AUDIT_FILE", "").strip()
        return cls(
            config_dir=Path(env.get("MYCO_CONFIG_DIR", "config")).expanduser(),
            host=env.get("MYCO_HOST", "0.0.0.0"),
            port=_int(env, "MYCO_PORT", 8080),
            mcp_path="/" + env.get("MYCO_MCP_PATH", "/mcp").strip("/"),
            reload_seconds=float(env.get("MYCO_RELOAD_SECONDS", "10") or 0),
            audit_file=Path(audit_file) if audit_file else None,
            log_level=env.get("MYCO_LOG_LEVEL", "INFO").upper(),
            plugins=tuple(p.strip() for p in env.get("MYCO_PLUGINS", "").split(",") if p.strip()),
            user_token_header=env.get("MYCO_USER_TOKEN_HEADER", "X-Myco-User-Token").lower(),
            stateless_http=env.get("MYCO_STATELESS_HTTP", "false").strip().lower() == "true",
            sources=_sources(env),
            agent_keys=_agent_keys(env),
            oidc=oidc,
        )


def agent_env_name(agent: str) -> str:
    """``support-bot`` -> ``SUPPORT_BOT``, the form used in ``MYCO_AGENT__<NAME>__KEY``."""
    return agent.strip().upper().replace("-", "_")


def _prefixed(env: Mapping[str, str], prefix: str) -> dict[str, str]:
    return {k[len(prefix) :]: v for k, v in env.items() if k.startswith(prefix)}


def _sources(env: Mapping[str, str]) -> tuple[SourceSettings, ...]:
    grouped: dict[str, dict[str, str]] = {}
    for key, value in _prefixed(env, SOURCE_PREFIX).items():
        name, sep, setting = key.partition("__")
        if not sep or not name or not setting:
            raise SettingsError(f"{SOURCE_PREFIX}{key}: expected {SOURCE_PREFIX}<NAME>__<SETTING>")
        grouped.setdefault(name.lower(), {})[setting.lower()] = value
    out = []
    for name, values in sorted(grouped.items()):
        if not NAME_RE.match(name):
            raise SettingsError(
                f"source name {name!r} must start with a letter and contain only letters, digits and underscores"
            )
        source_type = values.pop("type", name).strip().lower()
        out.append(SourceSettings(name=name, type=source_type, values=values))
    return tuple(out)


def _agent_keys(env: Mapping[str, str]) -> dict[str, str]:
    keys: dict[str, str] = {}
    for key, value in _prefixed(env, AGENT_PREFIX).items():
        name, sep, setting = key.partition("__")
        if not sep or setting != "KEY":
            raise SettingsError(f"{AGENT_PREFIX}{key}: expected {AGENT_PREFIX}<NAME>__KEY")
        value = value.strip()
        if len(value) < 16:
            raise SettingsError(f"{AGENT_PREFIX}{key}: keys must be at least 16 characters")
        keys[name.upper()] = value
    if len(set(keys.values())) != len(keys):
        raise SettingsError("two agents share the same key; every agent needs its own key")
    return keys


def _int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = env.get(key, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise SettingsError(f"{key} must be an integer, got {raw!r}") from exc
