"""Jira Cloud (REST API v3) connector.

Settings (``MYCO_SOURCE__<NAME>__<SETTING>``):

    SITE       acme.atlassian.net
    EMAIL      Atlassian account e-mail used with the API token
    API_TOKEN  Atlassian API token
    PROJECTS   optional comma-separated project keys; when set, every tool is confined to them
    TIMEOUT    seconds, default 30
"""

from __future__ import annotations

import re
from typing import Annotated, Any

from pydantic import Field

from myco_mcp.connector import Connector, Setting, tool
from myco_mcp.http import HttpClient

ISSUE_KEY_RE = re.compile(r"^([A-Z][A-Z0-9_]+)-\d+$")
PROJECT_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]+$")
ORDER_BY_RE = re.compile(r"\border\s+by\b", re.IGNORECASE)
TIME_SPENT_RE = re.compile(r"^\s*(\d+w\s*)?(\d+d\s*)?(\d+h\s*)?(\d+m\s*)?$")
SEARCH_FIELDS = ["summary", "status", "assignee", "reporter", "priority", "issuetype", "created", "updated"]

IssueKey = Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_]+-\d+$", description="Issue key, e.g. OPS-123")]
Limit = Annotated[int, Field(ge=1, le=100, description="Maximum number of results")]


class JiraConnector(Connector):
    type = "jira"
    settings = {
        "site": Setting(help="e.g. acme.atlassian.net"),
        "email": Setting(help="Atlassian account e-mail for the API token"),
        "api_token": Setting(secret=True, help="Atlassian API token"),
        "projects": Setting(required=False, help="comma-separated project keys to confine tools to"),
        "timeout": Setting(required=False, default="30"),
    }

    def __init__(self, source: str, config: dict[str, str], *, transport: Any = None) -> None:
        super().__init__(source, config)
        site = config["site"].removeprefix("https://").removeprefix("http://").strip("/")
        self.site = site
        self.projects = tuple(p.strip().upper() for p in config.get("projects", "").split(",") if p.strip())
        for project in self.projects:
            if not PROJECT_KEY_RE.match(project):
                raise ValueError(f"invalid Jira project key {project!r} in PROJECTS")
        self.http = HttpClient(
            f"https://{site}/rest/api/3",
            auth=(config["email"], config["api_token"]),
            timeout=float(config.get("timeout", 30)),
            transport=transport,
        )

    async def aclose(self) -> None:
        await self.http.aclose()

    # ------------------------------------------------------------------ reads
    @tool
    async def get_issue(self, issue_key: IssueKey) -> dict[str, Any]:
        """Get one issue with its description (as plain text), status, people and dates."""
        key = self._key(issue_key)
        data = await self.http.get(
            f"/issue/{key}",
            params={"fields": ",".join([*SEARCH_FIELDS, "description", "labels", "comment"])},
        )
        issue = _issue(data, self.site)
        fields = (data or {}).get("fields") or {}
        issue["description"] = adf_to_text(fields.get("description"))
        issue["labels"] = fields.get("labels")
        issue["comment_count"] = (fields.get("comment") or {}).get("total")
        return issue

    @tool
    async def search_issues(
        self,
        jql: Annotated[str, Field(min_length=1, max_length=2000, description="JQL, e.g. status = 'In Progress'")],
        limit: Limit = 20,
    ) -> list[dict[str, Any]]:
        """Search issues with JQL. Results are confined to the configured projects, if any."""
        data = await self.http.post(
            "/search/jql",
            json_body={"jql": self._confine(jql), "maxResults": limit, "fields": SEARCH_FIELDS},
        )
        return [_issue(row, self.site) for row in (data or {}).get("issues") or []][:limit]

    @tool
    async def list_comments(self, issue_key: IssueKey, limit: Limit = 50) -> list[dict[str, Any]]:
        """List an issue's comments, oldest first, as plain text."""
        key = self._key(issue_key)
        data = await self.http.get(f"/issue/{key}/comment", params={"maxResults": limit, "orderBy": "created"})
        return [
            {
                "id": c.get("id"),
                "author": _person(c.get("author")),
                "body": adf_to_text(c.get("body")),
                "created": c.get("created"),
                "updated": c.get("updated"),
            }
            for c in (data or {}).get("comments") or []
        ]

    @tool
    async def list_transitions(self, issue_key: IssueKey) -> list[dict[str, Any]]:
        """List the workflow transitions currently available for an issue."""
        key = self._key(issue_key)
        data = await self.http.get(f"/issue/{key}/transitions")
        return [
            {"id": t.get("id"), "name": t.get("name"), "to_status": (t.get("to") or {}).get("name")}
            for t in (data or {}).get("transitions") or []
        ]

    # ------------------------------------------------------------------ writes
    @tool
    async def create_issue(
        self,
        summary: Annotated[str, Field(min_length=1, max_length=255)],
        project_key: str | None = None,
        description: str = "",
        issue_type: str = "Task",
        assignee: str | None = None,
        labels: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create an issue. project_key defaults to the first configured project.
        assignee (name, e-mail or account id) is best effort: if it does not resolve to exactly
        one user the issue is created unassigned and a warning is returned."""
        project = (project_key or (self.projects[0] if self.projects else "")).strip().upper()
        if not PROJECT_KEY_RE.match(project):
            raise ValueError("project_key is required (no PROJECTS configured for this source)")
        self._check_project(project)
        fields: dict[str, Any] = {
            "project": {"key": project},
            "summary": summary.strip(),
            "issuetype": {"name": issue_type.strip() or "Task"},
        }
        if description.strip():
            fields["description"] = text_to_adf(description)
        if labels:
            fields["labels"] = [label.strip().replace(" ", "-") for label in labels if label.strip()]
        warnings: list[str] = []
        if assignee and assignee.strip():
            account_id, warning = await self._find_user(project, assignee)
            if account_id:
                fields["assignee"] = {"accountId": account_id}
            else:
                warnings.append(f"created unassigned: {warning}")
        data = await self.http.post("/issue", json_body={"fields": fields})
        key = (data or {}).get("key")
        result: dict[str, Any] = {"key": key, "id": (data or {}).get("id"), "url": f"https://{self.site}/browse/{key}"}
        if warnings:
            result["warnings"] = warnings
        return result

    @tool
    async def add_comment(
        self, issue_key: IssueKey, body: Annotated[str, Field(min_length=1, description="Plain text")]
    ) -> dict[str, Any]:
        """Add a plain-text comment to an issue."""
        key = self._key(issue_key)
        data = await self.http.post(f"/issue/{key}/comment", json_body={"body": text_to_adf(body)})
        return {"issue_key": key, "comment_id": (data or {}).get("id"), "created": (data or {}).get("created")}

    @tool
    async def transition_issue(
        self, issue_key: IssueKey, transition: Annotated[str, Field(description="Transition name, target status or id")]
    ) -> dict[str, Any]:
        """Move an issue through its workflow, by transition name, target status name or id."""
        key = self._key(issue_key)
        available = await self.list_transitions(key)
        match = _match_transition(available, transition.strip())
        if match is None:
            names = ", ".join(f"{t['name']} (-> {t['to_status']})" for t in available) or "none"
            raise ValueError(f"no transition matching {transition!r} on {key}; available: {names}")
        await self.http.post(f"/issue/{key}/transitions", json_body={"transition": {"id": str(match["id"])}})
        return {"issue_key": key, "transition": match["name"], "status": match["to_status"]}

    @tool
    async def assign_issue(
        self, issue_key: IssueKey, assignee: Annotated[str, Field(description="Display name, e-mail or account id")]
    ) -> dict[str, Any]:
        """Assign an issue to exactly one user, found by display name, e-mail or account id."""
        key = self._key(issue_key)
        account_id, warning = await self._find_user(key.split("-")[0], assignee)
        if account_id is None:
            raise ValueError(f"cannot assign {key}: {warning}")
        await self.http.put(f"/issue/{key}/assignee", json_body={"accountId": account_id})
        return {"issue_key": key, "assignee_account_id": account_id}

    @tool
    async def log_work(
        self,
        issue_key: IssueKey,
        time_spent: Annotated[str, Field(description="Jira duration, e.g. 30m, 1h 30m, 2d")],
        comment: str | None = None,
    ) -> dict[str, Any]:
        """Log time spent on an issue."""
        key = self._key(issue_key)
        if not time_spent.strip() or not TIME_SPENT_RE.match(time_spent):
            raise ValueError("time_spent must look like 30m, 1h 30m or 2d")
        body: dict[str, Any] = {"timeSpent": time_spent.strip()}
        if comment and comment.strip():
            body["comment"] = text_to_adf(comment)
        data = await self.http.post(f"/issue/{key}/worklog", json_body=body)
        return {"issue_key": key, "worklog_id": (data or {}).get("id"), "time_spent": (data or {}).get("timeSpent")}

    # ------------------------------------------------------------------ helpers
    def _key(self, issue_key: str) -> str:
        key = issue_key.strip().upper()
        match = ISSUE_KEY_RE.match(key)
        if not match:
            raise ValueError(f"invalid issue key {issue_key!r}; expected PROJECT-123")
        self._check_project(match.group(1))
        return key

    def _check_project(self, project: str) -> None:
        if self.projects and project not in self.projects:
            raise ValueError(f"project {project} is not available through this source")

    def _confine(self, jql: str) -> str:
        if not self.projects:
            return jql
        parts = ORDER_BY_RE.split(jql, maxsplit=1)
        query = parts[0].strip()
        order = f" ORDER BY {parts[1].strip()}" if len(parts) > 1 else ""
        scope = f"project in ({', '.join(self.projects)})"
        return f"{scope} AND ({query}){order}" if query else f"{scope}{order}"

    async def _find_user(self, project: str, query: str) -> tuple[str | None, str | None]:
        value = query.strip()
        if re.fullmatch(r"[0-9a-f]{24}|\d+:[0-9a-f-]{36}", value):
            return value, None
        users = await self.http.get(
            "/user/assignable/search", params={"project": project, "query": value, "maxResults": 20}
        )
        users = [u for u in users or [] if isinstance(u, dict) and u.get("accountId")]
        lowered = value.casefold()
        for user in users:
            if lowered in (str(user.get("displayName", "")).casefold(), str(user.get("emailAddress", "")).casefold()):
                return user["accountId"], None
        if len(users) == 1:
            return users[0]["accountId"], None
        if not users:
            return None, f"no assignable user matches {value!r} in {project}"
        names = ", ".join(str(u.get("displayName")) for u in users)
        return None, f"{value!r} is ambiguous ({names})"


def _person(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {"account_id": value.get("accountId"), "name": value.get("displayName"), "email": value.get("emailAddress")}


def _issue(data: Any, site: str) -> dict[str, Any]:
    data = data or {}
    fields = data.get("fields") or {}
    key = data.get("key")
    return {
        "key": key,
        "url": f"https://{site}/browse/{key}" if key else None,
        "summary": fields.get("summary"),
        "status": (fields.get("status") or {}).get("name"),
        "type": (fields.get("issuetype") or {}).get("name"),
        "priority": (fields.get("priority") or {}).get("name"),
        "assignee": _person(fields.get("assignee")),
        "reporter": _person(fields.get("reporter")),
        "created": fields.get("created"),
        "updated": fields.get("updated"),
    }


def _match_transition(transitions: list[dict[str, Any]], needle: str) -> dict[str, Any] | None:
    lowered = needle.casefold()
    for t in transitions:
        if str(t.get("id")) == needle:
            return t
    for t in transitions:
        if lowered in (str(t.get("name", "")).casefold(), str(t.get("to_status", "")).casefold()):
            return t
    return None


def text_to_adf(text: str) -> dict[str, Any]:
    """Plain text -> Atlassian Document Format, one paragraph per line."""
    paragraphs = []
    for line in text.strip().splitlines():
        paragraphs.append(
            {"type": "paragraph", "content": [{"type": "text", "text": line}]}
            if line.strip()
            else {"type": "paragraph"}
        )
    return {"type": "doc", "version": 1, "content": paragraphs}


def adf_to_text(node: Any) -> str:
    """Atlassian Document Format -> plain text (best effort, structure flattened)."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(adf_to_text(n) for n in node)
    if not isinstance(node, dict):
        return ""
    kind = node.get("type")
    if kind == "text":
        return str(node.get("text", ""))
    if kind == "hardBreak":
        return "\n"
    if kind in ("mention", "emoji"):
        return str((node.get("attrs") or {}).get("text", ""))
    inner = adf_to_text(node.get("content"))
    if kind in ("paragraph", "heading", "codeBlock", "blockquote"):
        return inner + "\n"
    if kind == "listItem":
        return "- " + inner
    if kind == "doc":
        return inner.strip()
    return inner
