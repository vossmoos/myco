"""End to end: a real HTTP server, a real MCP client, the example CRM connector."""

import io
import json
import socket
import threading
import time

import httpx
import pytest
import respx
import uvicorn
from conftest import AGENT_KEY, ORPHAN_KEY, READER_KEY
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError

from myco_mcp.audit import AuditLog
from myco_mcp.identity import UserTokenError
from myco_mcp.policy import REDACTED
from myco_mcp.server import create_server
from myco_mcp.settings import Settings

CRM = "https://crm.test/api"


class FakeUsers:
    async def claims(self, token):
        if token == "good-jwt":
            return {"sub": "u1", "email": "ann@acme.test", "groups": ["sales"]}
        raise UserTokenError("user token rejected: bad signature")


@pytest.fixture
def server(env, config_dir):
    default = config_dir / "agents" / "default.yaml"
    default.write_text(default.read_text().replace("  - wrike:contributor", "  - wrike:contributor\n  - crm:sales"))
    (config_dir / "agents" / "reader.yaml").write_text("agent: reader\nroles: [zendesk:reader, crm:reader]\n")
    (config_dir / "users.yaml").write_text(
        "users:\n"
        "  anonymous: { roles: [] }\n"
        "  bots: { service_for: [default, reader], roles: ['*:responder', '*:contributor', crm:sales] }\n"
        "  sales: { match: { groups: sales }, roles: [crm:reader] }\n"
    )
    env = {
        **env,
        "MYCO_PLUGINS": "acme_crm",
        "MYCO_SOURCE__CRM__TYPE": "acme_crm",
        "MYCO_SOURCE__CRM__BASE_URL": CRM,
        "MYCO_SOURCE__CRM__API_KEY": "crm-key",
        "MYCO_AGENT__READER__KEY": READER_KEY,
        "MYCO_AGENT__ORPHAN__KEY": ORPHAN_KEY,
    }
    stream = io.StringIO()
    mcp, gate = create_server(Settings.from_env(env), audit=AuditLog(stream=stream))
    gate.users = FakeUsers()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    uv = uvicorn.Server(uvicorn.Config(mcp.http_app(path="/mcp"), port=port, log_level="warning"))
    thread = threading.Thread(target=uv.run, daemon=True)
    thread.start()
    for _ in range(100):
        if uv.started:
            break
        time.sleep(0.05)
    with respx.mock(assert_all_called=False) as mock:
        mock.route(host="127.0.0.1").pass_through()
        mock.get(f"{CRM}/deals/1").respond(json={"id": 1, "name": "Big", "stage": "proposal", "margin": 0.31})
        mock.put(f"{CRM}/deals/1").respond(json={"id": 1, "stage": "won", "margin": 0.31})
        yield {"url": f"http://127.0.0.1:{port}", "audit": stream, "mock": mock}
    uv.should_exit = True
    thread.join(timeout=5)


def client(server, key, user_token=None):
    headers = {"Authorization": f"Bearer {key}"}
    if user_token:
        headers["X-Myco-User-Token"] = user_token
    return Client(StreamableHttpTransport(f"{server['url']}/mcp", headers=headers))


def audit_records(server):
    return [json.loads(line) for line in server["audit"].getvalue().splitlines()]


def test_health_and_unknown_key(server):
    assert httpx.get(f"{server['url']}/health").json()["status"] == "ok"
    response = httpx.post(f"{server['url']}/mcp", json={}, headers={"Authorization": "Bearer nope-nope-nope-nope"})
    assert response.status_code == 401


async def test_each_key_sees_its_own_tools(server):
    async with client(server, AGENT_KEY) as c:
        default_tools = {t.name for t in await c.list_tools()}
    async with client(server, READER_KEY) as c:
        reader_tools = {t.name: t for t in await c.list_tools()}
    async with client(server, ORPHAN_KEY) as c:
        assert await c.list_tools() == []

    assert len(default_tools) == 30  # 11 zendesk + 9 jira + 7 wrike + 3 crm
    assert set(reader_tools) == {
        "zendesk_list_tickets",
        "zendesk_search_tickets",
        "zendesk_get_ticket",
        "zendesk_list_conversations",
        "zendesk_get_company",
        "zendesk_get_article",
        "crm_list_deals",
        "crm_get_deal",
    }  # zendesk_get_contact is hidden: sensitive pii, no may_see
    deal = reader_tools["crm_get_deal"]
    assert deal.annotations.read_only_hint is True
    assert "Redacted fields: margin." in deal.description


async def test_allowed_call_is_shaped_and_audited(server):
    async with client(server, READER_KEY) as c:
        result = await c.call_tool("crm_get_deal", {"deal_id": 1})
    assert result.data == {"id": 1, "name": "Big", "stage": "proposal", "margin": REDACTED}
    record = audit_records(server)[-1]
    assert record["audit"] == "tool.call" and record["decision"] == "allow"
    assert record["agent"] == "reader" and record["user"] == "service:bots"
    assert record["label"] == "crm:deal:read" and record["rule"] == "allow:crm:reader&bots/crm:sales"


async def test_hidden_tool_call_is_denied_and_audited(server):
    async with client(server, READER_KEY) as c:
        with pytest.raises(ToolError):
            await c.call_tool("crm_move_deal", {"deal_id": 1, "stage": "won"})
    assert not server["mock"].routes[2].called  # the CRM was never touched
    denials = [r for r in audit_records(server) if r.get("decision") == "deny"]
    assert denials and denials[-1]["tool"] == "crm_move_deal" and denials[-1]["rule"] == "agent:no-role"


async def test_forwarded_user_token_narrows_access(server):
    async with client(server, AGENT_KEY, user_token="good-jwt") as c:
        names = {t.name for t in await c.list_tools()}
        assert names == {"crm_list_deals", "crm_get_deal"}  # default agent ∩ user "sales" (crm:reader)
        await c.call_tool("crm_get_deal", {"deal_id": 1})
    assert audit_records(server)[-1]["user"] == "ann@acme.test"

    async with client(server, AGENT_KEY, user_token="forged") as c:
        assert await c.list_tools() == []
        with pytest.raises(ToolError, match="bad signature"):
            await c.call_tool("crm_get_deal", {"deal_id": 1})


async def test_network_failure_is_audited(server):
    server["mock"].get(f"{CRM}/deals/2").mock(side_effect=httpx.ConnectError("refused"))
    async with client(server, READER_KEY) as c:
        with pytest.raises(ToolError, match="crm request failed"):
            await c.call_tool("crm_get_deal", {"deal_id": 2})
    record = audit_records(server)[-1]
    assert record["outcome"] == "error" and record["error"] == "ConnectError"
