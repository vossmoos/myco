"""WITNESS: Myco records in memory what a connector's tool showed the calling agent.

Set per source instance (MYCO_SOURCE__<NAME>__WITNESS, __WITNESS_MEMORY, __WITNESS_SCOPE);
the pack's `record:` names the record a reply is about. A real Myco server and MCP client;
Zendesk and etchmem are mocked with respx.
"""

import asyncio
import io
import json
import socket
import threading
import time

import pytest
import respx
import uvicorn
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from myco_mcp.audit import AuditLog
from myco_mcp.catalog import Catalog
from myco_mcp.connector import Pack, PackError
from myco_mcp.server import create_server
from myco_mcp.settings import Settings

ZD = "https://acme.zendesk.com/api/v2"
ETCH = "https://etchmem.test/v1"
BOT_KEY = "test-support-bot-key-0123456789"

WITNESS_ENV = {
    "MYCO_SOURCE__ZENDESK__WITNESS": "get_ticket, add_note",
    "MYCO_SOURCE__ZENDESK__WITNESS_MEMORY": "memory",
    "MYCO_SOURCE__ZENDESK__WITNESS_SCOPE": "support",
    "MYCO_SOURCE__MEMORY__TYPE": "etchmem",
    "MYCO_SOURCE__MEMORY__BASE_URL": "https://etchmem.test",
    "MYCO_SOURCE__MEMORY__WRITERS": "support_bot=support-bot",
    "MYCO_AGENT__SUPPORT_BOT__KEY": BOT_KEY,
}

TICKET = {
    "id": 7,
    "subject": "Invoice wrong",
    "status": 2,
    "priority": 3,
    "updated_at": "2026-11-03T09:00:00Z",
    "description_text": "The October invoice was charged twice.",
    "requester": {"id": 1, "name": "Maria Muster", "email": "maria@acme.test"},
}


@pytest.fixture
def server(env, config_dir):
    (config_dir / "agents" / "support-bot.yaml").write_text(
        "agent: support-bot\nroles: [zendesk:contributor]\npermissions: ['memory:support_bot:*']\n"
    )
    (config_dir / "users.yaml").write_text(
        "users:\n  anonymous: { roles: [] }\n"
        "  bots: { service_for: [support-bot], roles: ['zendesk:*'], permissions: ['memory:*:*'] }\n"
    )
    stream = io.StringIO()
    mcp, gate = create_server(Settings.from_env({**env, **WITNESS_ENV}), audit=AuditLog(stream=stream))
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
        routes = {
            "ticket": mock.get(f"{ZD}/tickets/7").respond(json=TICKET),
            "search": mock.get(f"{ZD}/search/tickets").respond(json={"results": [TICKET]}),
            "note": mock.post(f"{ZD}/tickets/7/notes").respond(json={"id": 99, "created_at": "2026-11-07T10:00:00Z"}),
            "remember": mock.post(f"{ETCH}/remember").respond(
                202, json={"id": "s1", "stored": True, "status": "new", "message": "Accepted."}
            ),
        }
        yield {"url": f"http://127.0.0.1:{port}", "audit": stream, "routes": routes, "gate": gate}
    uv.should_exit = True
    thread.join(timeout=5)


def client(server):
    return Client(StreamableHttpTransport(f"{server['url']}/mcp", headers={"Authorization": f"Bearer {BOT_KEY}"}))


async def deposits(server, expected, timeout=3.0):
    route = server["routes"]["remember"]
    deadline = time.monotonic() + timeout
    while route.call_count < expected and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    return [json.loads(call.request.content) for call in route.calls]


def audit(server, event):
    return [json.loads(line) for line in server["audit"].getvalue().splitlines() if f'"{event}"' in line]


async def test_a_witnessed_read_lands_in_the_agents_memory(server):
    async with client(server) as c:
        result = await c.call_tool("zendesk_get_ticket", {"ticket_id": 7})
    assert result.data["subject"] == "Invoice wrong"  # the agent's answer is unaffected
    [body] = await deposits(server, 1)
    assert body["source"] == "support-bot"  # the writer is the agent that was shown it
    assert body["scope"] == "support"
    assert body["source_record_id"] == "zendesk:ticket:7"
    assert body["occurred_at"] == "2026-11-03T09:00:00Z"
    assert body["actor"] == {"agent": "support-bot", "user": "service:bots", "via": "witness:zendesk_get_ticket"}
    assert '"ticket_id": 7' in body["data"] and "charged twice" in body["data"]
    # exactly what the agent saw: the requester's e-mail was redacted for it (no pii toleration)
    assert "maria@acme.test" not in body["data"] and "[redacted]" in body["data"]
    [entry] = audit(server, "tool.witness")
    assert entry["outcome"] == "ok" and entry["record"] == "zendesk:ticket:7" and entry["agent"] == "support-bot"


async def test_write_outcomes_are_witnessed_and_unlisted_tools_are_not(server):
    async with client(server) as c:
        await c.call_tool("zendesk_search_tickets", {"status": ["open"]})
        await c.call_tool("zendesk_add_note", {"ticket_id": 7, "body": "Credit note CN-2291 issued."})
    [body] = await deposits(server, 1)
    assert body["source_record_id"] == "zendesk:ticket:7" and "CN-2291" in body["data"]
    assert body["actor"]["via"] == "witness:zendesk_add_note"
    await asyncio.sleep(0.2)
    assert server["routes"]["remember"].call_count == 1  # search_tickets is not listed


async def test_a_failing_memory_never_hurts_the_call(server):
    server["routes"]["remember"].respond(500, json={"detail": "down"})
    async with client(server) as c:
        result = await c.call_tool("zendesk_get_ticket", {"ticket_id": 7})
    assert result.data["id"] == 7
    deadline = time.monotonic() + 3
    while not audit(server, "tool.witness") and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    [entry] = audit(server, "tool.witness")
    assert entry["outcome"] == "error" and "500" in entry["error"]


def _build(env, **extra):
    return Catalog.build(Settings.from_env({**env, **WITNESS_ENV, **extra}))


def test_witness_settings_are_checked_at_startup(env):
    catalog = _build(env)
    assert catalog.tools["zendesk_get_ticket"].witness.scope == "support"
    assert catalog.tools["zendesk_search_tickets"].witness is None
    with pytest.raises(PackError, match="has no tool 'get_tikcet'"):
        _build(env, MYCO_SOURCE__ZENDESK__WITNESS="get_tikcet")
    with pytest.raises(PackError, match="declares no record"):
        _build(env, MYCO_SOURCE__ZENDESK__WITNESS="search_tickets")
    with pytest.raises(PackError, match="nowhere is not a configured source"):
        _build(env, MYCO_SOURCE__ZENDESK__WITNESS_MEMORY="nowhere")
    with pytest.raises(PackError, match="cannot take deposits"):
        _build(env, MYCO_SOURCE__ZENDESK__WITNESS_MEMORY="jira")


def test_witness_is_per_instance(env):
    catalog = _build(
        env,
        MYCO_SOURCE__SUPPORT_EU__TYPE="zendesk",
        MYCO_SOURCE__SUPPORT_EU__DOMAIN="eu.zendesk.com",
        MYCO_SOURCE__SUPPORT_EU__API_KEY="k",
    )
    assert catalog.tools["zendesk_get_ticket"].witness is not None
    assert catalog.tools["support_eu_get_ticket"].witness is None  # same type, not witnessed


def test_record_definitions():
    pack = Pack.parse({"tools": {"get": {"action": "issue:read", "record": {"id": "issue:{fields.key}"}}}}, "x")
    record = pack.tools["get"].record
    assert record.render({"fields": {"key": "OPS-7"}}) == ("issue:OPS-7", None)
    assert record.render({"fields": {}}) == (None, None)  # a missing field: no record id
    with pytest.raises(PackError, match="names no reply field"):
        Pack.parse({"tools": {"get": {"action": "issue:read", "record": "issue"}}}, "x")
