"""Small async HTTP helper shared by connectors."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any

import httpx

MAX_RETRY_AFTER_SECONDS = 30.0


class UpstreamError(RuntimeError):
    """The vendor API answered with an error. The message is safe to show to the agent."""

    def __init__(self, message: str, *, status: int | None = None, body: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body

    def __str__(self) -> str:
        text = super().__str__()
        if self.status is not None:
            text += f" (HTTP {self.status})"
        if self.body:
            detail = self.body if isinstance(self.body, str) else json.dumps(self.body, ensure_ascii=False)
            text += f": {detail[:300]}"
        return text


class HttpClient:
    """``httpx.AsyncClient`` with JSON decoding, one retry on 429 and readable errors."""

    def __init__(
        self,
        base_url: str,
        *,
        headers: Mapping[str, str] | None = None,
        auth: httpx.Auth | tuple[str, str] | None = None,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Accept": "application/json", "User-Agent": "myco-mcp", **(headers or {})},
            auth=auth,
            timeout=timeout,
            transport=transport,
        )

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
        data: Mapping[str, Any] | None = None,
        expect: tuple[int, ...] = (200, 201, 204),
    ) -> Any:
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}
        for attempt in range(2):
            response = await self.client.request(method, path, params=clean_params or None, json=json_body, data=data)
            if response.status_code == 429 and attempt == 0:
                await asyncio.sleep(_retry_after(response))
                continue
            break
        body = _decode(response)
        if response.status_code not in expect:
            raise UpstreamError(f"{method} {path} failed", status=response.status_code, body=_error_text(body))
        return body

    async def get(self, path: str, **kwargs: Any) -> Any:
        return await self.request("GET", path, **kwargs)

    async def post(self, path: str, **kwargs: Any) -> Any:
        return await self.request("POST", path, **kwargs)

    async def put(self, path: str, **kwargs: Any) -> Any:
        return await self.request("PUT", path, **kwargs)

    async def aclose(self) -> None:
        await self.client.aclose()


def _retry_after(response: httpx.Response) -> float:
    try:
        return min(float(response.headers.get("Retry-After", "5")), MAX_RETRY_AFTER_SECONDS)
    except ValueError:
        return 5.0


def _decode(response: httpx.Response) -> Any:
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        return response.text


def _error_text(body: Any) -> Any:
    if isinstance(body, dict):
        for key in ("errorMessages", "errors", "errorDescription", "description", "message", "error"):
            if body.get(key):
                return body[key]
    return body
