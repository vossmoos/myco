"""Who is calling: agent keys (Myco's own) and forwarded user SSO tokens (OIDC)."""

from __future__ import annotations

import asyncio
import hmac
import logging
from collections.abc import Mapping
from typing import Any

import httpx
import jwt
from fastmcp.server.auth import AccessToken, TokenVerifier

from myco_mcp.settings import OidcSettings

log = logging.getLogger("myco")


class AgentKeyVerifier(TokenVerifier):
    """Accepts ``Authorization: Bearer <agent key>``. Unknown keys get HTTP 401.

    The returned token's ``client_id`` is the env form of the agent name (``SUPPORT_BOT``).
    A valid key without a matching agent file connects but sees no tools.
    """

    def __init__(self, agent_keys: Mapping[str, str]) -> None:
        super().__init__()
        self._keys = [(name, key.encode()) for name, key in agent_keys.items()]

    async def verify_token(self, token: str) -> AccessToken | None:
        presented = token.strip().encode()
        match = None
        for name, key in self._keys:  # compare against every key: constant time per key
            if hmac.compare_digest(presented, key):
                match = name
        if match is None:
            return None
        return AccessToken(token="agent-key", client_id=match, scopes=[])


class UserTokenError(Exception):
    pass


class UserTokenValidator:
    """Validates a forwarded OIDC ID/access token against the issuer's JWKS."""

    def __init__(self, settings: OidcSettings) -> None:
        self.settings = settings
        self._jwks: jwt.PyJWKClient | None = None
        self._lock = asyncio.Lock()

    async def claims(self, token: str) -> dict[str, Any]:
        client = await self._jwks_client()
        try:
            signing_key = await asyncio.to_thread(client.get_signing_key_from_jwt, token)
            return jwt.decode(
                token,
                signing_key.key,
                algorithms=list(self.settings.algorithms),
                audience=self.settings.audience,
                issuer=self.settings.issuer,
                options={"require": ["exp", "iss", "aud"]},
                leeway=30,
            )
        except jwt.PyJWTError as exc:
            raise UserTokenError(f"user token rejected: {exc}") from exc

    async def _jwks_client(self) -> jwt.PyJWKClient:
        async with self._lock:
            if self._jwks is None:
                url = self.settings.jwks_url or await self._discover_jwks_url()
                self._jwks = jwt.PyJWKClient(url, cache_keys=True, lifespan=3600)
            return self._jwks

    async def _discover_jwks_url(self) -> str:
        url = self.settings.issuer.rstrip("/") + "/.well-known/openid-configuration"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(url)
                response.raise_for_status()
                return str(response.json()["jwks_uri"])
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            raise UserTokenError(f"cannot read OIDC discovery document {url}: {exc}") from exc


class NoUserTokens:
    """Used when MYCO_OIDC__* is not set: any forwarded token is refused, never ignored."""

    async def claims(self, token: str) -> dict[str, Any]:
        raise UserTokenError("user token sent but MYCO_OIDC__ISSUER / MYCO_OIDC__AUDIENCE are not configured")
