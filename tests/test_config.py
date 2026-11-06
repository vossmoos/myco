import io

import pytest

from myco_mcp.audit import AuditLog
from myco_mcp.catalog import Catalog
from myco_mcp.config import ConfigError, load_policy
from myco_mcp.server import PolicyStore
from myco_mcp.settings import Settings


@pytest.fixture
def catalog(env):
    return Catalog.build(Settings.from_env(env))


def load(config_dir, catalog):
    return load_policy(config_dir, roles=catalog.roles, sources=catalog.sources)


def test_example_config_is_valid(config_dir, catalog):
    policy = load(config_dir, catalog)
    assert set(policy.agents) == {"default", "support-bot"}
    default = policy.agents["default"]
    assert {g.role for g in default.grants} == {"zendesk:responder", "jira:contributor", "wrike:contributor"}
    assert "pii" in default.tolerations
    assert policy.anonymous.grants == ()  # unattended calls are denied unless a service user exists
    assert policy.service_users["default"].name == "default-service"
    assert {g.role for g in policy.service_users["default"].grants} == {g.role for g in default.grants}
    assert policy.domains["work"] == frozenset({"jira", "wrike"})


@pytest.mark.parametrize(
    ("filename", "content", "message"),
    [
        ("agents/x.yaml", "agent: y\nroles: []\n", "must live in agents/y.yaml"),
        ("agents/x.yaml", "agent: x\nroles: [jira:admin]\n", "unknown role 'jira:admin'"),
        ("agents/x.yaml", "agent: x\nroles: [reader]\n", "must look like source:role"),
        ("agents/x.yaml", "agent: x\ndeny: ['*:*:nuke']\n", "unknown operation"),
        ("agents/x.yaml", "agent: x\nperms: []\n", "unknown keys"),
        ("agents/x.yaml", "agent: x\nscope: [{match: '*:*:*', where: {org: '{user}'}}]\n", "unknown placeholder"),
        ("users.yaml", "users:\n  anonymous: {match: {a: b}}\n", "cannot have 'match'"),
        ("users.yaml", "users:\n  vp: {roles: [jira:reader]}\n", "needs 'match'"),
        ("users.yaml", "users:\n  anonymous: {service_for: [default]}\n", "cannot have 'match' or 'service_for'"),
        ("users.yaml", "users:\n  s: {service_for: [default], match: {a: b}}\n", "either 'match'"),
        (
            "users.yaml",
            "users:\n  a: {service_for: [default]}\n  b: {service_for: [default]}\n",
            "already runs as service user 'a'",
        ),
        ("domains.yaml", "jira: [wrike]\n", "same name as a source"),
        ("agents/x.yaml", "agent: x\nroles: [\n", "x.yaml"),
    ],
)
def test_invalid_config_is_refused(config_dir, catalog, filename, content, message):
    (config_dir / filename).write_text(content)
    with pytest.raises(ConfigError, match=message.replace("(", r"\(").replace("[", r"\[").replace("*", r"\*")):
        load(config_dir, catalog)


def test_roles_of_unconfigured_sources_are_warnings(config_dir, catalog):
    (config_dir / "agents" / "x.yaml").write_text("agent: x\nroles: [servicenow:reader, '*:reader']\n")
    policy = load(config_dir, catalog)
    assert any("servicenow" in w for w in policy.warnings)
    assert {g.role for g in policy.agents["x"].grants} == {"zendesk:reader", "jira:reader", "wrike:reader"}


def test_reload_keeps_last_good_config(env, config_dir, catalog):
    store = PolicyStore(Settings.from_env(env), catalog, AuditLog(stream=io.StringIO()))
    good = store.current.version
    assert store.reload() is False  # unchanged

    (config_dir / "agents" / "default.yaml").write_text("agent: default\nroles: [jira:nope]\n")
    assert store.reload() is False
    assert store.current.version == good

    (config_dir / "agents" / "default.yaml").write_text("agent: default\nroles: [jira:reader]\n")
    assert store.reload() is True
    assert store.current.version != good
    assert {g.role for g in store.current.agents["default"].grants} == {"jira:reader"}


def test_fingerprint_ignores_dot_paths(config_dir):
    from myco_mcp.config import config_files

    hidden = config_dir / "..2026_10_11_00_00_00" / "agents"
    hidden.mkdir(parents=True)
    (hidden / "default.yaml").write_text("agent: default\n")
    (config_dir / ".git").mkdir()
    (config_dir / ".git" / "x.yaml").write_text("x: 1\n")
    assert all(".." not in str(p) and ".git" not in str(p) for p in config_files(config_dir))
