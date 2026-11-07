import pytest

from myco_mcp.catalog import Catalog
from myco_mcp.config import load_policy
from myco_mcp.policy import REDACTED, ScopeError, build_caller, decide, describe_limits, shape_reply
from myco_mcp.settings import Settings


@pytest.fixture
def setup(env, config_dir):
    catalog = Catalog.build(Settings.from_env(env))
    (config_dir / "agents" / "portal.yaml").write_text(
        """
agent: portal
roles: [zendesk:reader, jira:reader]
deny: ["jira:*:*"]
scope:
  - match: "zendesk:ticket:*"
    where: { company_id: "{user.org}" }
redact:
  - match: "zendesk:*:*"
    fields: [custom_fields]
"""
    )
    (config_dir / "users.yaml").write_text(
        """
users:
  anonymous: { roles: [jira:reader] }
  customer: { match: { groups: customers }, roles: [zendesk:reader] }
  staff: { match: { groups: staff }, roles: [zendesk:responder, jira:contributor], deny: ["*:*:send"] }
"""
    )

    def caller(agent_key, claims=None, error=None):
        policy = load_policy(config_dir, roles=catalog.roles, sources=catalog.sources)
        return build_caller(policy, agent_key, claims=claims, user_error=error), policy

    return catalog, caller


def check(catalog, caller_policy, tool):
    caller, policy = caller_policy
    return decide(caller, catalog.tools[tool], policy.domains)


def test_intersection_of_agent_and_user_roles(setup):
    catalog, caller = setup
    staff = caller("DEFAULT", {"sub": "1", "groups": ["staff"]})
    assert check(catalog, staff, "zendesk_update_ticket").allowed
    assert check(catalog, staff, "jira_create_issue").allowed
    # user deny wins over agent role
    assert check(catalog, staff, "zendesk_reply_to_ticket").rule == "deny:user:staff:*:*:send"
    # anonymous only has jira:reader
    anon = caller("DEFAULT")
    assert check(catalog, anon, "jira_get_issue").allowed
    assert check(catalog, anon, "jira_create_issue").rule == "user:anonymous:no-role"
    assert check(catalog, anon, "zendesk_get_ticket").rule == "user:anonymous:no-role"


def test_agent_deny_wins_and_unmatched_user_gets_nothing(setup):
    catalog, caller = setup
    staff = caller("PORTAL", {"sub": "1", "groups": ["staff"]})
    assert check(catalog, staff, "jira_get_issue").rule == "deny:agent:jira:*:*"
    stranger = caller("DEFAULT", {"sub": "2", "groups": ["visitors"]})
    assert check(catalog, stranger, "jira_get_issue").rule == "user:(no match):no-role"


def test_sensitive_tools_need_may_see(setup):
    catalog, caller = setup
    customer = caller("PORTAL", {"sub": "3", "groups": "customers", "org": "42"})
    assert check(catalog, customer, "zendesk_get_ticket").allowed
    assert check(catalog, customer, "zendesk_get_contact").rule == "sensitive:pii"
    staff = caller("DEFAULT", {"sub": "1", "groups": ["staff"]})
    assert check(catalog, staff, "zendesk_get_contact").allowed  # default may see pii


def test_unknown_agent_and_rejected_token(setup):
    catalog, caller = setup
    assert check(catalog, caller("ORPHAN"), "jira_get_issue").rule == "agent:no-file"
    assert check(catalog, caller("DEFAULT", error="bad token"), "jira_get_issue").rule == "user:token-rejected"


def test_scope_and_redaction(setup):
    catalog, caller = setup
    customer, policy = caller("PORTAL", {"sub": "3", "groups": ["customers"], "org": 42})
    rows = [
        {"id": 1, "company_id": 42, "custom_fields": {"x": 1}},
        {"id": 2, "company_id": 7, "custom_fields": {"x": 2}},
    ]
    shaped = shape_reply(customer, catalog.tools["zendesk_list_tickets"], rows, policy.domains)
    assert shaped == [{"id": 1, "company_id": 42, "custom_fields": REDACTED}]
    assert rows[1]["company_id"] == 7  # input untouched

    ticket = {
        "id": 1,
        "company_id": 42,
        "requester": {"id": 5, "email": "a@b.c", "name": "Ann"},
        "cc_emails": ["x@y.z"],
    }
    shaped = shape_reply(customer, catalog.tools["zendesk_get_ticket"], ticket, policy.domains)
    assert shaped["requester"] == {"id": 5, "email": REDACTED, "name": REDACTED}
    assert shaped["cc_emails"] == REDACTED
    with pytest.raises(ScopeError):
        shape_reply(customer, catalog.tools["zendesk_get_ticket"], {**ticket, "company_id": 7}, policy.domains)

    limits = describe_limits(customer, catalog.tools["zendesk_get_ticket"], policy.domains)
    assert "company_id = {user.org}" in limits and "requester.email" in limits


def test_missing_claim_means_out_of_scope(setup):
    catalog, caller = setup
    customer, policy = caller("PORTAL", {"sub": "3", "groups": ["customers"]})  # no org claim
    shaped = shape_reply(customer, catalog.tools["zendesk_list_tickets"], [{"id": 1, "company_id": 42}], policy.domains)
    assert shaped == []


def test_tolerated_fields_are_not_redacted(setup):
    catalog, caller = setup
    staff, policy = caller("DEFAULT", {"sub": "1", "groups": ["staff"]})
    ticket = {"id": 1, "requester": {"email": "a@b.c"}}
    assert shape_reply(staff, catalog.tools["zendesk_get_ticket"], ticket, policy.domains) == ticket


def test_claim_names_with_dots(setup, config_dir):
    catalog, caller = setup
    (config_dir / "users.yaml").write_text(
        'users:\n  staff: { match: { "https://acme.com/groups": staff }, roles: [jira:reader] }\n'
    )
    staff = caller("DEFAULT", {"sub": "1", "https://acme.com/groups": ["staff"]})
    assert check(catalog, staff, "jira_get_issue").allowed


def test_service_user_replaces_anonymous_only_without_token(setup, config_dir):
    catalog, caller = setup
    (config_dir / "users.yaml").write_text(
        """
users:
  anonymous: { roles: [] }
  default-service: { service_for: [default], roles: [jira:contributor] }
  staff: { match: { groups: staff }, roles: [jira:reader] }
"""
    )
    unattended, _ = caller("DEFAULT")
    assert unattended.user == "service:default-service"
    assert (
        check(catalog, (unattended, _), "jira_create_issue").rule
        == "allow:jira:contributor&default-service/jira:contributor"
    )
    # Another agent without a service user falls back to anonymous, which has no roles.
    assert check(catalog, caller("PORTAL"), "zendesk_get_ticket").rule == "user:anonymous:no-role"
    # A signed-in person never gets the service user's roles.
    person = caller("DEFAULT", {"sub": "1", "groups": ["staff"]})
    assert check(catalog, person, "jira_create_issue").rule == "user:staff:no-role"
    assert check(catalog, person, "jira_get_issue").allowed
