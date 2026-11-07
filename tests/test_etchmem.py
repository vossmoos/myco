"""The etchmem pack end to end: memory permissioned by WHO WROTE IT.

Labels are memory:<writer>:<operation>. Reads are labelled with every writer the caller
may read (Myco fills that in and hides it); remember is labelled with the calling agent.
Scopes are a free topic filter, not a permission. Also covers the core features this
relies on: `fill`, direct `permissions:` in agent and user files, and pins.

A real HTTP server and MCP client; etchmem itself is mocked with respx.
"""

import io
import json
import socket
import threading
import time

import pytest
import respx
import uvicorn
from conftest import AGENT_KEY
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError

from myco_mcp.audit import AuditLog
from myco_mcp.catalog import Catalog
from myco_mcp.config import ConfigError, load_policy
from myco_mcp.connector import Pack, PackError
from myco_mcp.policy import PinError, build_caller, pinned_args
from myco_mcp.server import create_server
from myco_mcp.settings import Settings

ETCH = "https://etchmem.test/v1"
SALES_KEY = "test-sales-bot-key-0123456789"
PUBLIC_KEY = "test-public-bot-key-0123456789"
DPO_KEY = "test-dpo-bot-key-0123456789"
TRUSTED_KEY = "test-trusted-key-0123456789"

MEMORY_ENV = {
    "MYCO_SOURCE__MEMORY__TYPE": "etchmem",
    "MYCO_SOURCE__MEMORY__BASE_URL": "https://etchmem.test",
    "MYCO_SOURCE__MEMORY__API_KEY": "etm-secret",
    "MYCO_SOURCE__MEMORY__WRITERS": "sales_bot=sales-bot,public_bot=public-bot,hubspot,email",
    "MYCO_AGENT__SALES_BOT__KEY": SALES_KEY,
    "MYCO_AGENT__PUBLIC_BOT__KEY": PUBLIC_KEY,
    "MYCO_AGENT__DPO_BOT__KEY": DPO_KEY,
    "MYCO_AGENT__TRUSTED__KEY": TRUSTED_KEY,
}

ROWS = [{"id": "company_acme::tier", "scope": "sales", "value": "gold", "source": "hubspot"}]
READ_TOOLS = {"memory_recall", "memory_know", "memory_list_beliefs", "memory_history", "memory_dossier"}


class FakeUsers:
    async def claims(self, token):
        return {"sub": "u1", "email": "ann@acme.test", "groups": ["sales"]}


def write_config(config_dir):
    agents = config_dir / "agents"
    (agents / "sales-bot.yaml").write_text(
        "agent: sales-bot\npermissions: ['memory:sales_bot:*', 'memory:hubspot:read']\n"
    )
    (agents / "public-bot.yaml").write_text("agent: public-bot\npermissions: ['memory:public_bot:*']\n")
    (agents / "dpo-bot.yaml").write_text("agent: dpo-bot\npermissions: ['memory:subject:delete']\n")
    (agents / "trusted.yaml").write_text("agent: trusted\npermissions: ['memory:*:read']\n")
    (config_dir / "users.yaml").write_text(
        "users:\n"
        "  anonymous: { roles: [] }\n"
        "  bots: { service_for: [sales-bot, public-bot, dpo-bot, trusted], permissions: ['memory:*:*'] }\n"
        "  sales: { match: { groups: sales }, permissions: ['memory:hubspot:read'] }\n"
    )


@pytest.fixture
def server(env, config_dir):
    write_config(config_dir)
    stream = io.StringIO()
    mcp, gate = create_server(Settings.from_env({**env, **MEMORY_ENV}), audit=AuditLog(stream=stream))
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
        routes = {
            "recall": mock.post(f"{ETCH}/recall").respond(json={"results": ROWS}),
            "know": mock.get(f"{ETCH}/entity/Acme/etches").respond(
                json={"entity": {"id": "company_acme"}, "etches": ROWS, "count": 1}
            ),
            "remember": mock.post(f"{ETCH}/remember").respond(
                202, json={"id": "s1", "stored": True, "status": "new", "message": "Accepted."}
            ),
            "forget": mock.post(f"{ETCH}/forget").respond(json={"erasure_id": "e1", "signals_deleted": 3}),
            "dossier": mock.get(f"{ETCH}/etch/company_acme::tier/dossier").respond(
                json={"etch": {"id": "company_acme::tier", "scope": "sales"}, "versions": [{"version": 1}]}
            ),
            "dossier_none": mock.get(f"{ETCH}/etch/company_acme::margin/dossier").respond(
                404, json={"detail": "Unknown etch: company_acme::margin"}
            ),
        }
        yield {"url": f"http://127.0.0.1:{port}", "audit": stream, "routes": routes}
    uv.should_exit = True
    thread.join(timeout=5)


def client(server, key, user_token=None):
    headers = {"Authorization": f"Bearer {key}"}
    if user_token:
        headers["X-Myco-User-Token"] = user_token
    return Client(StreamableHttpTransport(f"{server['url']}/mcp", headers=headers))


def last_audit(server):
    return json.loads(server["audit"].getvalue().splitlines()[-1])


def sent(route):
    return json.loads(route.calls.last.request.content)


# ── what the agent sees ──────────────────────────────────────────────────────


async def test_agents_never_see_writers_or_labels(server):
    async with client(server, SALES_KEY) as c:
        tools = {t.name: t for t in await c.list_tools()}
    assert set(tools) == READ_TOOLS | {"memory_remember"}
    for tool in tools.values():
        assert "writers" not in tool.inputSchema["properties"]
        assert "writer" not in tool.inputSchema["properties"]
        assert "taints" not in tool.inputSchema["properties"]
        assert "hubspot" not in tool.description  # which writers exist is not the agent's business
    assert "scope" in tools["memory_recall"].inputSchema["properties"]  # topics stay free

    async with client(server, DPO_KEY) as c:
        assert {t.name for t in await c.list_tools()} == {"memory_forget"}


# ── reads: every writer the caller may read, filled in by Myco ──────────────


async def test_reads_carry_exactly_the_writers_the_agent_may_read(server):
    async with client(server, SALES_KEY) as c:
        await c.call_tool("memory_recall", {"query": "Acme", "scope": ["triage"]})
        await c.call_tool("memory_know", {"entity": "Acme"})
    body = sent(server["routes"]["recall"])
    assert body["writers"] == ["sales-bot", "hubspot"]  # its own notes and HubSpot; not public-bot, not email
    assert body["scope"] == ["triage"]
    assert server["routes"]["recall"].calls.last.request.headers["authorization"] == "Bearer etm-secret"
    query = server["routes"]["know"].calls.last.request.url.params
    assert query.get_list("writers") == ["sales-bot", "hubspot"] and "scope" not in query
    record = last_audit(server)
    assert record["decision"] == "allow" and record["label"] == "memory:sales_bot:read,memory:hubspot:read"


async def test_an_agent_cannot_widen_its_writers(server):
    async with client(server, PUBLIC_KEY) as c:
        await c.call_tool("memory_recall", {"query": "Acme", "writers": ["email", "sales_bot"]})
    assert sent(server["routes"]["recall"])["writers"] == ["public-bot"]


async def test_a_wildcard_reader_reads_every_writer(server):
    async with client(server, TRUSTED_KEY) as c:
        await c.call_tool("memory_recall", {"query": "Acme"})
    assert sent(server["routes"]["recall"])["writers"] == ["sales-bot", "public-bot", "hubspot", "email"]


async def test_the_user_narrows_what_the_agent_reads(server):
    async with client(server, SALES_KEY, user_token="jwt") as c:  # user "sales": memory:hubspot:read only
        tools = {t.name for t in await c.list_tools()}
        await c.call_tool("memory_recall", {"query": "Acme"})
    assert "memory_remember" not in tools
    assert sent(server["routes"]["recall"])["writers"] == ["hubspot"]


async def test_dossier_of_an_unreadable_belief_is_unknown(server):
    async with client(server, PUBLIC_KEY) as c:
        result = await c.call_tool("memory_dossier", {"etch_id": "company_acme::tier"})
        assert result.data["etch"]["id"] == "company_acme::tier"
        with pytest.raises(ToolError, match="no belief 'company_acme::margin'"):
            await c.call_tool("memory_dossier", {"etch_id": "company_acme::margin"})
    assert server["routes"]["dossier"].calls.last.request.url.params.get_list("writers") == ["public-bot"]


# ── writes: always as the calling agent ──────────────────────────────────────


async def test_remember_writes_as_the_calling_agent(server):
    async with client(server, SALES_KEY) as c:
        await c.call_tool("memory_remember", {"data": "Acme renewed at 40k", "scope": "spare-parts"})
        await c.call_tool("memory_remember", {"data": "Acme likes quarterly reviews"})
    first, second = (json.loads(call.request.content) for call in server["routes"]["remember"].calls)
    assert first["source"] == "sales-bot" and second["source"] == "sales-bot"
    assert first["scope"] == "spare-parts" and second["scope"] == "general"  # topic: free, default general
    assert first["actor"] == {"agent": "sales-bot", "user": "service:bots", "via": "myco:memory"}
    assert "taints" not in first
    assert last_audit(server)["label"] == "memory:sales_bot:create"


async def test_an_agent_cannot_write_as_another(server):
    async with client(server, PUBLIC_KEY) as c:
        await c.call_tool("memory_remember", {"data": "x", "writer": "sales_bot"})
    assert sent(server["routes"]["remember"])["source"] == "public-bot"
    assert last_audit(server)["label"] == "memory:public_bot:create"


async def test_forget_has_its_own_label(server):
    async with client(server, DPO_KEY) as c:
        result = await c.call_tool("memory_forget", {"entity": "Maria Muster", "reason": "DSR-9"})
    assert result.data["erasure_id"] == "e1"
    assert sent(server["routes"]["forget"])["entity"] == "Maria Muster"
    assert last_audit(server)["label"] == "memory:subject:delete"
    async with client(server, SALES_KEY) as c:
        with pytest.raises(ToolError):
            await c.call_tool("memory_forget", {"entity": "Maria Muster"})


# ── unit: catalog, config, pins ──────────────────────────────────────────────


def test_one_label_per_writer(env):
    catalog = Catalog.build(Settings.from_env({**env, **MEMORY_ENV}))
    recall = catalog.tools["memory_recall"]
    assert recall.label_fill == "allowed" and recall.label_values == ("sales_bot", "public_bot", "hubspot", "email")
    assert [str(label) for label in recall.labels][:2] == ["memory:sales_bot:read", "memory:public_bot:read"]
    assert catalog.tools["memory_remember"].label_fill == "agent"
    assert str(catalog.tools["memory_forget"].label) == "memory:subject:delete"
    assert {"memory:reader", "memory:eraser"} <= set(catalog.roles)


def test_writers_must_be_label_names(env):
    with pytest.raises(PackError, match="support_bot=support-bot"):
        Catalog.build(Settings.from_env({**env, **MEMORY_ENV, "MYCO_SOURCE__MEMORY__WRITERS": "support-bot"}))


def test_permissions_are_label_patterns(config_dir):
    (config_dir / "agents" / "bad.yaml").write_text("agent: bad\npermissions: ['memory:Bad Name:read']\n")
    with pytest.raises(ConfigError, match="permissions"):
        load_policy(config_dir, roles={}, sources=[])


def test_pack_template_validation():
    with pytest.raises(PackError, match="placeholder"):
        Pack.parse({"tools": {"get": "thing:read"}, "roles": {"{scope}_reader": ["thing:read"]}}, "x")
    with pytest.raises(PackError, match="placeholder the role name does not"):
        Pack.parse({"tools": {"get": "{scope}:read"}, "roles": {"reader": ["{scope}:read"]}}, "x")
    with pytest.raises(PackError, match="fill must be one of"):
        Pack.parse({"tools": {"get": {"action": "thing:read", "fill": "allowed"}}}, "x")
    pack = Pack.parse({"tools": {"get": {"action": "{who}:read", "fill": "agent"}}}, "x")
    assert pack.tools["get"].label_from == "who" and pack.tools["get"].label_fill == "agent"


def test_pins_still_apply_to_remember(env, config_dir):
    write_config(config_dir)
    (config_dir / "agents" / "sales-bot.yaml").write_text(
        "agent: sales-bot\n"
        "permissions: ['memory:sales_bot:*']\n"
        "pin:\n"
        "  - match: 'memory:*:create'\n"
        "    args: { scope: triage, context: 'by {user.email}' }\n"
    )
    catalog = Catalog.build(Settings.from_env({**env, **MEMORY_ENV}))
    policy = load_policy(config_dir, roles=catalog.roles, sources=catalog.sources)
    spec = catalog.tools["memory_remember"]
    person = build_caller(policy, "SALES_BOT", claims={"sub": "u1", "email": "ann@acme.test", "groups": ["x"]})
    assert pinned_args(person, spec, policy.domains) == {"scope": "triage", "context": "by ann@acme.test"}
    unattended = build_caller(policy, "SALES_BOT", claims=None)
    with pytest.raises(PinError):
        pinned_args(unattended, spec, policy.domains)


def test_pin_rule_validation(config_dir):
    (config_dir / "agents" / "bad.yaml").write_text("agent: bad\npin:\n  - match: '*:*:*'\n    args: { x: '{nope}' }\n")
    with pytest.raises(ConfigError, match="unknown placeholder"):
        load_policy(config_dir, roles={}, sources=[])


def test_default_agent_key_still_works(env):
    catalog = Catalog.build(Settings.from_env(env))
    assert AGENT_KEY and "memory_recall" not in catalog.tools
