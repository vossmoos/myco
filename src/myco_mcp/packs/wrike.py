"""Wrike (API v4) connector.

Settings (``MYCO_SOURCE__<NAME>__<SETTING>``):

    TOKEN     Wrike permanent access token
    BASE_URL  default https://www.wrike.com/api/v4 (EU accounts: https://app-eu.wrike.com/api/v4)
    TIMEOUT   seconds, default 30

Ids are Wrike API ids (``MAAAAAELaG4z``), not the number in a permalink. Wrike answers a
read the token may not see with ``200`` and an empty list, so an empty answer is reported
as "not visible", never as "does not exist".
"""

from __future__ import annotations

import json
from datetime import date
from typing import Annotated, Any, Literal

from pydantic import Field

from myco_mcp.connector import Connector, Setting, tool
from myco_mcp.http import HttpClient

WrikeId = Annotated[str, Field(pattern=r"^[A-Z0-9]{8,32}$", description="Wrike API id, e.g. MAAAAAELaG4z")]
Status = Literal["Active", "Completed", "Deferred", "Cancelled"]
Importance = Literal["High", "Normal", "Low"]
Limit = Annotated[int, Field(ge=1, le=500, description="Maximum number of results")]

NOT_VISIBLE = (
    "Wrike returned nothing. A token without access gets the same empty answer, "
    "so this does not prove the item is absent."
)


class WrikeConnector(Connector):
    type = "wrike"
    settings = {
        "token": Setting(secret=True, help="Wrike permanent access token"),
        "base_url": Setting(required=False, default="https://www.wrike.com/api/v4"),
        "timeout": Setting(required=False, default="30"),
    }

    def __init__(self, source: str, config: dict[str, str], *, transport: Any = None) -> None:
        super().__init__(source, config)
        self.http = HttpClient(
            config.get("base_url", "https://www.wrike.com/api/v4"),
            headers={"Authorization": f"bearer {config['token']}"},
            timeout=float(config.get("timeout", 30)),
            transport=transport,
        )

    async def aclose(self) -> None:
        await self.http.aclose()

    # ------------------------------------------------------------------ reads
    @tool
    async def get_task(self, task_id: WrikeId) -> dict[str, Any]:
        """Get one task by its API id, including its description."""
        rows = _rows(await self.http.get(f"/tasks/{task_id}"))
        if not rows:
            raise ValueError(f"task {task_id}: {NOT_VISIBLE}")
        return _task(rows[0], detail=True)

    @tool
    async def list_tasks(
        self,
        folder_id: WrikeId | None = None,
        title_contains: str | None = None,
        status: Status | None = None,
        updated_since: date | None = None,
        limit: Limit = 50,
    ) -> list[dict[str, Any]]:
        """List tasks, optionally inside one folder or project (including sub-folders)."""
        params: dict[str, Any] = {
            "title": title_contains,
            "status": status,
            "pageSize": limit,
            "sortField": "UpdatedDate",
            "sortOrder": "Desc",
        }
        if updated_since:
            params["updatedDate"] = json.dumps({"start": f"{updated_since.isoformat()}T00:00:00Z"})
        path = f"/folders/{folder_id}/tasks" if folder_id else "/tasks"
        if folder_id:
            params["descendants"] = "true"
        return [_task(row) for row in _rows(await self.http.get(path, params=params))][:limit]

    @tool
    async def get_task_comments(self, task_id: WrikeId) -> list[dict[str, Any]]:
        """List a task's comments (text with HTML), oldest first."""
        return [
            {
                "id": row.get("id"),
                "author_id": row.get("authorId"),
                "text": row.get("text"),
                "created": row.get("createdDate"),
            }
            for row in _rows(await self.http.get(f"/tasks/{task_id}/comments"))
        ]

    @tool
    async def list_folders(self, parent_folder_id: WrikeId | None = None) -> list[dict[str, Any]]:
        """List folders and projects, either all of them or the children of one folder."""
        path = f"/folders/{parent_folder_id}/folders" if parent_folder_id else "/folders"
        return [
            {
                "id": row.get("id"),
                "title": row.get("title"),
                "is_project": bool(row.get("project")),
                "child_ids": row.get("childIds"),
                "scope": row.get("scope"),
            }
            for row in _rows(await self.http.get(path))
        ]

    # ------------------------------------------------------------------ writes
    @tool
    async def create_task(
        self,
        folder_id: WrikeId,
        title: Annotated[str, Field(min_length=1, max_length=250)],
        description: str = "",
        importance: Importance = "Normal",
        due_date: date | None = None,
    ) -> dict[str, Any]:
        """Create a task in a folder or project. description may contain simple HTML."""
        form: dict[str, Any] = {"title": title, "importance": importance}
        if description:
            form["description"] = description
        if due_date:
            form["dates"] = json.dumps({"due": due_date.isoformat()})
        rows = _rows(await self.http.post(f"/folders/{folder_id}/tasks", data=form))
        if not rows:
            raise ValueError("Wrike accepted the request but returned no task")
        return _task(rows[0])

    @tool
    async def update_task(
        self,
        task_id: WrikeId,
        title: Annotated[str | None, Field(max_length=250)] = None,
        description: str | None = None,
        status: Status | None = None,
        importance: Importance | None = None,
        due_date: date | None = None,
    ) -> dict[str, Any]:
        """Change a task's title, description, status, importance or due date."""
        form: dict[str, Any] = {}
        if title:
            form["title"] = title
        if description is not None:
            form["description"] = description
        if status:
            form["status"] = status
        if importance:
            form["importance"] = importance
        if due_date:
            form["dates"] = json.dumps({"due": due_date.isoformat()})
        if not form:
            raise ValueError("nothing to update")
        rows = _rows(await self.http.put(f"/tasks/{task_id}", data=form))
        if not rows:
            raise ValueError(f"task {task_id}: {NOT_VISIBLE}")
        return _task(rows[0])

    @tool
    async def add_comment(
        self, task_id: WrikeId, text: Annotated[str, Field(min_length=1, description="Text, simple HTML allowed")]
    ) -> dict[str, Any]:
        """Add a comment to a task."""
        rows = _rows(await self.http.post(f"/tasks/{task_id}/comments", data={"text": text}))
        row = rows[0] if rows else {}
        return {"id": row.get("id"), "task_id": task_id, "created": row.get("createdDate")}


def _rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        return [row for row in payload["data"] if isinstance(row, dict)]
    return []


def _task(row: dict[str, Any], *, detail: bool = False) -> dict[str, Any]:
    task = {
        "id": row.get("id"),
        "title": row.get("title"),
        "status": row.get("status"),
        "importance": row.get("importance"),
        "permalink": row.get("permalink"),
        "parent_ids": row.get("parentIds"),
        "responsible_ids": row.get("responsibleIds"),
        "due": (row.get("dates") or {}).get("due"),
        "created": row.get("createdDate"),
        "updated": row.get("updatedDate"),
    }
    if detail:
        task["description"] = row.get("description")
    return task
