import base64
import json
from datetime import date
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from myco_mcp.http import UpstreamError
from myco_mcp.packs.jira import JiraConnector, adf_to_text, text_to_adf
from myco_mcp.packs.wrike import WrikeConnector
from myco_mcp.packs.zendesk import ZendeskConnector

ZD = "https://acme.zendesk.com/api/v2"
JIRA = "https://acme.atlassian.net/rest/api/3"
WRIKE = "https://www.wrike.com/api/v4"


@pytest.fixture
def zendesk():
    return ZendeskConnector("zendesk", {"domain": "acme.zendesk.com", "api_key": "abc", "timeout": "5"})


@pytest.fixture
def jira():
    return JiraConnector(
        "jira", {"site": "https://acme.atlassian.net/", "email": "bot@acme.test", "api_token": "t", "projects": "OPS"}
    )


@pytest.fixture
def wrike():
    return WrikeConnector("wrike", {"token": "w", "base_url": WRIKE})


@respx.mock
async def test_zendesk_auth_and_ticket_shape(zendesk):
    route = respx.get(f"{ZD}/tickets/7").respond(
        json={"id": 7, "subject": "Help", "status": 2, "priority": 3, "requester": {"id": 1, "email": "a@b.c"}}
    )
    ticket = await zendesk.get_ticket(7)
    assert ticket["status"] == "open" and ticket["priority"] == "high"
    assert ticket["requester"]["email"] == "a@b.c"
    auth = route.calls.last.request.headers["Authorization"]
    assert auth == "Basic " + base64.b64encode(b"abc:X").decode()


@respx.mock
async def test_zendesk_search_builds_constrained_query(zendesk):
    route = respx.get(f"{ZD}/search/tickets").respond(json={"results": [{"id": 1, "status": 3}], "total": 1})
    rows = await zendesk.search_tickets(status=["open", "pending"], tag="vip", created_after=date(2026, 1, 2))
    assert rows[0]["status"] == "pending"
    query = route.calls.last.request.url.params["query"]
    assert query == "\"(status:2 OR status:3) AND tag:'vip' AND created_at:>'2026-01-02'\""
    with pytest.raises(ValueError):
        await zendesk.search_tickets(tag="x' OR status:5")


@respx.mock
async def test_zendesk_update_merges_tags(zendesk):
    respx.get(f"{ZD}/tickets/7").respond(json={"id": 7, "tags": ["a"]})
    put = respx.put(f"{ZD}/tickets/7").respond(json={"id": 7, "status": 4, "tags": ["a", "b"]})
    result = await zendesk.update_ticket(7, status="resolved", add_tags=["b", "a"])
    assert json.loads(put.calls.last.request.content) == {"status": 4, "tags": ["a", "b"]}
    assert result["status"] == "resolved"


@respx.mock
async def test_zendesk_upstream_error_is_readable(zendesk):
    respx.get(f"{ZD}/tickets/9").respond(404, json={"description": "Not found", "errors": []})
    with pytest.raises(UpstreamError, match="HTTP 404"):
        await zendesk.get_ticket(9)


@respx.mock
async def test_jira_search_is_confined_to_projects(jira):
    route = respx.post(f"{JIRA}/search/jql").respond(
        json={"issues": [{"key": "OPS-1", "fields": {"summary": "s", "status": {"name": "To Do"}}}]}
    )
    issues = await jira.search_issues("status = Open order by created DESC")
    body = json.loads(route.calls.last.request.content)
    assert body["jql"] == "project in (OPS) AND (status = Open) ORDER BY created DESC"
    assert issues[0]["url"] == "https://acme.atlassian.net/browse/OPS-1"


async def test_jira_rejects_other_projects(jira):
    with pytest.raises(ValueError, match="not available"):
        await jira.get_issue("SEC-1")
    with pytest.raises(ValueError, match="invalid issue key"):
        await jira.get_issue("OPS-1/../../myself")


@respx.mock
async def test_jira_create_issue_unassigned_on_ambiguous_user(jira):
    respx.get(f"{JIRA}/user/assignable/search").respond(
        json=[{"accountId": "a1", "displayName": "Ann A"}, {"accountId": "a2", "displayName": "Ann B"}]
    )
    create = respx.post(f"{JIRA}/issue").respond(201, json={"key": "OPS-5", "id": "100"})
    result = await jira.create_issue("Broken", description="line 1\nline 2", assignee="Ann")
    fields = json.loads(create.calls.last.request.content)["fields"]
    assert fields["project"] == {"key": "OPS"} and "assignee" not in fields
    assert result["key"] == "OPS-5" and "ambiguous" in result["warnings"][0]


@respx.mock
async def test_jira_transition_by_status_name(jira):
    respx.get(f"{JIRA}/issue/OPS-1/transitions").respond(
        json={"transitions": [{"id": "31", "name": "Finish", "to": {"name": "Done"}}]}
    )
    post = respx.post(f"{JIRA}/issue/OPS-1/transitions").respond(204)
    result = await jira.transition_issue("ops-1", "done")
    assert json.loads(post.calls.last.request.content) == {"transition": {"id": "31"}}
    assert result["status"] == "Done"


def test_adf_round_trip():
    assert adf_to_text(text_to_adf("hello\nworld")) == "hello\nworld"


@respx.mock
async def test_wrike_empty_is_not_visible(wrike):
    respx.get(f"{WRIKE}/tasks/MAAAAAELaG4z").respond(json={"kind": "tasks", "data": []})
    with pytest.raises(ValueError, match="does not prove"):
        await wrike.get_task("MAAAAAELaG4z")


@respx.mock
async def test_wrike_create_task_sends_form(wrike):
    route = respx.post(f"{WRIKE}/folders/IEAAAAAQ/tasks").respond(
        json={"data": [{"id": "MAAAAAEL", "title": "T", "status": "Active", "dates": {"due": "2026-10-20"}}]}
    )
    task = await wrike.create_task("IEAAAAAQ", "T", due_date=date(2026, 10, 20))
    form = parse_qs(route.calls.last.request.content.decode())
    assert form["title"] == ["T"] and json.loads(form["dates"][0]) == {"due": "2026-10-20"}
    assert route.calls.last.request.headers["Authorization"] == "bearer w"
    assert task["due"] == "2026-10-20"


@respx.mock
async def test_retry_once_on_rate_limit(wrike, monkeypatch):
    monkeypatch.setattr("myco_mcp.http.asyncio.sleep", _no_sleep)
    respx.get(f"{WRIKE}/folders").mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "1"}), httpx.Response(200, json={"data": []})]
    )
    assert await wrike.list_folders() == []


async def _no_sleep(_seconds):
    return None
