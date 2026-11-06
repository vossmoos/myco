"""Example connector: a tiny CRM with deals. Load it with MYCO_PLUGINS=acme_crm.

MYCO_SOURCE__CRM__TYPE=acme_crm
MYCO_SOURCE__CRM__BASE_URL=https://crm.example.com/api
MYCO_SOURCE__CRM__API_KEY=...
"""

from __future__ import annotations

from typing import Any, Literal

from myco_mcp.connector import Connector, Setting, tool
from myco_mcp.http import HttpClient

Stage = Literal["lead", "qualified", "proposal", "won", "lost"]


class AcmeCrm(Connector):
    type = "acme_crm"  # the value of MYCO_SOURCE__<NAME>__TYPE
    settings = {
        "base_url": Setting(help="CRM API root"),
        "api_key": Setting(secret=True),
    }

    def __init__(self, source: str, config: dict[str, str]) -> None:
        super().__init__(source, config)
        self.http = HttpClient(config["base_url"], headers={"X-Api-Key": config["api_key"]})

    async def aclose(self) -> None:
        await self.http.aclose()

    @tool
    async def list_deals(self, stage: Stage | None = None, limit: int = 20) -> list[dict[str, Any]]:
        """List deals, optionally only one pipeline stage."""
        return (await self.http.get("/deals", params={"stage": stage, "limit": limit}))[:limit]

    @tool
    async def get_deal(self, deal_id: int) -> dict[str, Any]:
        """Get one deal, including its value and margin."""
        return await self.http.get(f"/deals/{deal_id}")

    @tool
    async def move_deal(self, deal_id: int, stage: Stage) -> dict[str, Any]:
        """Move a deal to another pipeline stage."""
        return await self.http.put(f"/deals/{deal_id}", json_body={"stage": stage})
