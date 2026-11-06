"""Keep the README's examples true."""

from myco_mcp.catalog import Catalog
from myco_mcp.config import load_policy
from myco_mcp.policy import build_caller, decide
from myco_mcp.settings import Settings


def visible_tools(env, config_dir, agent_key_name):
    catalog = Catalog.build(Settings.from_env(env))
    policy = load_policy(config_dir, roles=catalog.roles, sources=catalog.sources)
    caller = build_caller(policy, agent_key_name, claims=None)
    return {name for name, spec in catalog.tools.items() if decide(caller, spec, policy.domains).allowed}


def test_default_agent_has_every_example_tool(env, config_dir):
    catalog = Catalog.build(Settings.from_env(env))
    assert visible_tools(env, config_dir, "DEFAULT") == set(catalog.tools)


def test_anonymous_has_no_access_in_example_config(env, config_dir):
    (config_dir / "agents" / "other.yaml").write_text("agent: other\nroles: [jira:reader]\n")
    assert visible_tools(env, config_dir, "OTHER") == set()


def test_jira_triage_example(env, config_dir):
    (config_dir / "agents" / "jira-triage.yaml").write_text(
        """
agent: jira-triage
owner: platform@acme.com
roles: [jira:contributor]
deny:
  - "jira:issue:create"
  - "*:*:delete"
"""
    )
    (config_dir / "users.yaml").write_text(
        """
users:
  anonymous:
    roles: []
  jira-triage-service:
    service_for: [jira-triage]
    roles: [jira:contributor]
"""
    )
    assert visible_tools(env, config_dir, "JIRA_TRIAGE") == {
        "jira_get_issue",
        "jira_search_issues",
        "jira_list_comments",
        "jira_list_transitions",
        "jira_add_comment",
        "jira_transition_issue",
        "jira_assign_issue",
        "jira_log_work",
    }
