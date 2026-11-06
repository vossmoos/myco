"""Zendesk (API v2) connector.

Settings (``MYCO_SOURCE__<NAME>__<SETTING>``):

    DOMAIN    acme.zendesk.com
    API_KEY   the agent's Zendesk API key (plain key, Myco adds the Basic auth encoding)
    TIMEOUT   seconds, default 30
"""

from __future__ import annotations

import base64
import re
from datetime import date
from typing import Annotated, Any, Literal

from pydantic import Field

from myco_mcp.connector import Connector, Setting, tool
from myco_mcp.http import HttpClient

Status = Literal["open", "pending", "resolved", "closed"]
Priority = Literal["low", "medium", "high", "urgent"]
STATUS_IDS: dict[str, int] = {"open": 2, "pending": 3, "resolved": 4, "closed": 5}
PRIORITY_IDS: dict[str, int] = {"low": 1, "medium": 2, "high": 3, "urgent": 4}
STATUS_NAMES = {v: k for k, v in STATUS_IDS.items()}
PRIORITY_NAMES = {v: k for k, v in PRIORITY_IDS.items()}
TAG_RE = re.compile(r"^[\w .:/-]{1,64}$")

Limit = Annotated[int, Field(ge=1, le=100, description="Maximum number of results")]


class ZendeskConnector(Connector):
    type = "zendesk"
    settings = {
        "domain": Setting(help="e.g. acme.zendesk.com"),
        "api_key": Setting(secret=True, help="Zendesk API key"),
        "timeout": Setting(required=False, default="30"),
    }

    def __init__(self, source: str, config: dict[str, str], *, transport: Any = None) -> None:
        super().__init__(source, config)
        domain = config["domain"].removeprefix("https://").removeprefix("http://").strip("/")
        token = base64.b64encode(f"{config['api_key']}:X".encode()).decode("ascii")
        self.http = HttpClient(
            f"https://{domain}/api/v2",
            headers={"Authorization": f"Basic {token}"},
            timeout=float(config.get("timeout", 30)),
            transport=transport,
        )

    async def aclose(self) -> None:
        await self.http.aclose()

    # ------------------------------------------------------------------ tickets
    @tool
    async def list_tickets(
        self,
        requester_email: str | None = None,
        company_id: int | None = None,
        updated_since: date | None = None,
        order_by: Literal["created_at", "updated_at", "due_by", "status"] = "created_at",
        limit: Limit = 30,
    ) -> list[dict[str, Any]]:
        """List tickets, newest first. Filter by requester e-mail, company or last update date."""
        params: dict[str, Any] = {
            "email": requester_email,
            "company_id": company_id,
            "updated_since": updated_since.isoformat() if updated_since else None,
            "order_by": order_by,
            "order_type": "desc",
            "per_page": limit,
        }
        rows = await self.http.get("/tickets", params=params) or []
        return [_ticket_summary(row) for row in rows][:limit]

    @tool
    async def search_tickets(
        self,
        status: list[Status] | None = None,
        priority: list[Priority] | None = None,
        tag: str | None = None,
        created_after: date | None = None,
        limit: Limit = 30,
    ) -> list[dict[str, Any]]:
        """Search tickets by status, priority, tag and creation date (all conditions must hold)."""
        clauses: list[str] = []
        if status:
            clauses.append("(" + " OR ".join(f"status:{STATUS_IDS[s]}" for s in status) + ")")
        if priority:
            clauses.append("(" + " OR ".join(f"priority:{PRIORITY_IDS[p]}" for p in priority) + ")")
        if tag:
            if not TAG_RE.match(tag):
                raise ValueError("tag may contain letters, digits, spaces and . : / - only")
            clauses.append(f"tag:'{tag}'")
        if created_after:
            clauses.append(f"created_at:>'{created_after.isoformat()}'")
        if not clauses:
            raise ValueError("give at least one of status, priority, tag or created_after")
        out: list[dict[str, Any]] = []
        page = 1
        while len(out) < limit and page <= 10:  # Zendesk search: 30 per page, 10 pages max
            data = await self.http.get("/search/tickets", params={"query": f'"{" AND ".join(clauses)}"', "page": page})
            results = (data or {}).get("results") or []
            out.extend(_ticket_summary(row) for row in results)
            if len(results) < 30:
                break
            page += 1
        return out[:limit]

    @tool
    async def get_ticket(self, ticket_id: int) -> dict[str, Any]:
        """Get one ticket with its description and requester."""
        data = await self.http.get(f"/tickets/{ticket_id}", params={"include": "requester"})
        return _ticket_detail(data)

    @tool
    async def list_conversations(self, ticket_id: int, limit: Limit = 100) -> list[dict[str, Any]]:
        """List a ticket's replies and notes, oldest first."""
        rows = await self.http.get(f"/tickets/{ticket_id}/conversations", params={"per_page": limit}) or []
        return [
            {
                "id": row.get("id"),
                "private": row.get("private"),
                "incoming": row.get("incoming"),
                "source": row.get("source"),
                "user_id": row.get("user_id"),
                "from_email": row.get("from_email"),
                "to_emails": row.get("to_emails"),
                "cc_emails": row.get("cc_emails"),
                "body_text": row.get("body_text"),
                "created_at": row.get("created_at"),
            }
            for row in rows
        ][:limit]

    @tool
    async def create_ticket(
        self,
        subject: Annotated[str, Field(min_length=1, max_length=255)],
        description: Annotated[str, Field(min_length=1, description="HTML body")],
        requester_email: str,
        priority: Priority = "low",
        status: Status = "open",
        tags: list[str] | None = None,
        group_id: int | None = None,
        ticket_type: str | None = None,
    ) -> dict[str, Any]:
        """Create a ticket on behalf of a requester."""
        body: dict[str, Any] = {
            "subject": subject,
            "description": description,
            "email": requester_email,
            "priority": PRIORITY_IDS[priority],
            "status": STATUS_IDS[status],
        }
        if tags:
            body["tags"] = _clean_tags(tags)
        if group_id is not None:
            body["group_id"] = group_id
        if ticket_type:
            body["type"] = ticket_type
        data = await self.http.post("/tickets", json_body=body)
        return _ticket_summary(data)

    @tool
    async def update_ticket(
        self,
        ticket_id: int,
        status: Status | None = None,
        priority: Priority | None = None,
        ticket_type: str | None = None,
        group_id: int | None = None,
        responder_id: int | None = None,
        add_tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Change a ticket's status, priority, type, group or assignee, or add tags (existing tags are kept)."""
        body: dict[str, Any] = {}
        if status:
            body["status"] = STATUS_IDS[status]
        if priority:
            body["priority"] = PRIORITY_IDS[priority]
        if ticket_type:
            body["type"] = ticket_type
        if group_id is not None:
            body["group_id"] = group_id
        if responder_id is not None:
            body["responder_id"] = responder_id
        if add_tags:
            current = await self.http.get(f"/tickets/{ticket_id}")
            existing = list((current or {}).get("tags") or [])
            body["tags"] = existing + [t for t in _clean_tags(add_tags) if t not in existing]
        if not body:
            raise ValueError("nothing to update")
        data = await self.http.put(f"/tickets/{ticket_id}", json_body=body)
        return _ticket_summary(data)

    @tool
    async def add_note(
        self, ticket_id: int, body: Annotated[str, Field(min_length=1, description="HTML body")]
    ) -> dict[str, Any]:
        """Add a private (internal) note to a ticket. Customers do not see it."""
        data = await self.http.post(f"/tickets/{ticket_id}/notes", json_body={"body": body, "private": True})
        return {"id": (data or {}).get("id"), "ticket_id": ticket_id, "created_at": (data or {}).get("created_at")}

    @tool
    async def reply_to_ticket(
        self, ticket_id: int, body: Annotated[str, Field(min_length=1, description="HTML body")]
    ) -> dict[str, Any]:
        """Send a public reply to the requester. This e-mails the customer."""
        data = await self.http.post(f"/tickets/{ticket_id}/reply", json_body={"body": body})
        return {"id": (data or {}).get("id"), "ticket_id": ticket_id, "created_at": (data or {}).get("created_at")}

    # ------------------------------------------------------------------ people & knowledge
    @tool
    async def get_contact(self, contact_id: int) -> dict[str, Any]:
        """Get a contact (customer) record, including e-mail and phone numbers."""
        data = await self.http.get(f"/contacts/{contact_id}")
        keys = (
            "id",
            "name",
            "email",
            "phone",
            "mobile",
            "company_id",
            "job_title",
            "language",
            "time_zone",
            "created_at",
        )
        return {k: (data or {}).get(k) for k in keys}

    @tool
    async def get_company(self, company_id: int) -> dict[str, Any]:
        """Get a company record."""
        data = await self.http.get(f"/companies/{company_id}")
        keys = ("id", "name", "description", "domains", "industry", "account_tier", "created_at", "updated_at")
        return {k: (data or {}).get(k) for k in keys}

    @tool
    async def get_article(
        self,
        article_id: int,
        language: Annotated[str | None, Field(pattern=r"^[a-z]{2}(-[a-z]{2})?$")] = None,
    ) -> dict[str, Any]:
        """Get a knowledge-base (solution) article, optionally in a specific language such as 'de'."""
        path = f"/solutions/articles/{article_id}" + (f"/{language}" if language else "")
        data = await self.http.get(path)
        keys = ("id", "title", "description_text", "status", "folder_id", "category_id", "tags", "updated_at")
        return {k: (data or {}).get(k) for k in keys}


def _clean_tags(tags: list[str]) -> list[str]:
    cleaned = [t.strip() for t in tags if t and t.strip()]
    for tag in cleaned:
        if not TAG_RE.match(tag):
            raise ValueError(f"invalid tag {tag!r}")
    return cleaned


def _ticket_summary(row: dict[str, Any] | None) -> dict[str, Any]:
    row = row or {}
    return {
        "id": row.get("id"),
        "subject": row.get("subject"),
        "status": STATUS_NAMES.get(row.get("status"), row.get("status")),
        "priority": PRIORITY_NAMES.get(row.get("priority"), row.get("priority")),
        "type": row.get("type"),
        "tags": row.get("tags"),
        "requester_id": row.get("requester_id"),
        "company_id": row.get("company_id"),
        "group_id": row.get("group_id"),
        "responder_id": row.get("responder_id"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def _ticket_detail(row: dict[str, Any] | None) -> dict[str, Any]:
    row = row or {}
    requester = row.get("requester") or {}
    return {
        **_ticket_summary(row),
        "description_text": row.get("description_text"),
        "cc_emails": row.get("cc_emails"),
        "requester": {
            "id": requester.get("id"),
            "name": requester.get("name"),
            "email": requester.get("email"),
            "phone": requester.get("phone"),
            "mobile": requester.get("mobile"),
        },
        "due_by": row.get("due_by"),
        "custom_fields": row.get("custom_fields"),
    }
