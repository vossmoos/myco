import pytest

from myco_mcp.catalog import Catalog, load_connector_modules
from myco_mcp.connector import Connector, Pack, PackError, tool
from myco_mcp.settings import Settings


def test_example_packs_match_their_connectors():
    load_connector_modules(("acme_crm",))
    for connector_type in ("zendesk", "jira", "wrike", "acme_crm"):
        pack = Connector.registry[connector_type].load_pack()
        assert pack.tools and pack.roles


def test_catalog_prefixes_tools_labels_and_roles(env):
    catalog = Catalog.build(Settings.from_env(env))
    spec = catalog.tools["zendesk_get_ticket"]
    assert str(spec.label) == "zendesk:ticket:read"
    assert "Label: zendesk:ticket:read." in spec.description
    assert "zendesk:reader" in catalog.roles and "jira:contributor" in catalog.roles
    assert catalog.tools["zendesk_get_contact"].taints == ("pii",)


def test_second_source_of_same_type_is_independent(env):
    env = {
        **env,
        "MYCO_SOURCE__SUPPORT_EU__TYPE": "zendesk",
        "MYCO_SOURCE__SUPPORT_EU__DOMAIN": "eu.zendesk.com",
        "MYCO_SOURCE__SUPPORT_EU__API_KEY": "k",
    }
    catalog = Catalog.build(Settings.from_env(env))
    assert str(catalog.tools["support_eu_get_ticket"].label) == "support_eu:ticket:read"
    assert "support_eu:reader" in catalog.roles


def test_missing_and_unknown_settings_fail_fast(env):
    broken = {k: v for k, v in env.items() if k != "MYCO_SOURCE__WRIKE__TOKEN"}
    broken["MYCO_SOURCE__WRIKE__BASE_URL"] = "https://app-eu.wrike.com/api/v4"
    with pytest.raises(PackError, match="MYCO_SOURCE__WRIKE__TOKEN is required"):
        Catalog.build(Settings.from_env(broken))
    with pytest.raises(PackError, match="unknown settings"):
        Catalog.build(Settings.from_env({**env, "MYCO_SOURCE__JIRA__SPACE": "x"}))
    with pytest.raises(PackError, match="unknown TYPE"):
        Catalog.build(Settings.from_env({**env, "MYCO_SOURCE__NOSUCH__SUBDOMAIN": "x"}))


def test_pack_validation():
    with pytest.raises(PackError, match="matches no tool"):
        Pack.parse({"tools": {"get": "thing:read"}, "roles": {"writer": ["thing:create"]}}, "x")
    with pytest.raises(PackError, match="unknown keys"):
        Pack.parse({"tools": {"get": {"action": "thing:read", "scope": 1}}}, "x")
    with pytest.raises(PackError):
        Pack.parse({"tools": {"get": "thing:write"}}, "x")  # write is a pattern shorthand, not an operation


def test_unlabelled_tool_is_refused(tmp_path):
    (tmp_path / "pack.yaml").write_text("tools:\n  ping: thing:read\n")

    class Unlabelled(Connector):
        type = "unlabelled_test"
        pack_file = tmp_path / "pack.yaml"

        @tool
        async def ping(self) -> str:
            return "pong"

        @tool
        async def nuke(self) -> str:
            return "boom"

    with pytest.raises(PackError, match="without a label"):
        Unlabelled.load_pack()
