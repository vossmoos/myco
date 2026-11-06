import pytest

from myco_mcp.labels import Label, LabelError, LabelPattern


def test_label_parse_and_hints():
    label = Label.parse("zendesk:ticket:read")
    assert (label.source, label.entity, label.operation) == ("zendesk", "ticket", "read")
    assert label.read_only and not label.destructive
    assert Label.parse("zendesk:reply:send").destructive


@pytest.mark.parametrize("text", ["zendesk:ticket", "zendesk:ticket:write", "Zendesk:ticket:read", "a:b:c:d"])
def test_label_rejects_malformed(text):
    with pytest.raises(LabelError):
        Label.parse(text)


@pytest.mark.parametrize(
    ("pattern", "label", "expected"),
    [
        ("*:ticket:read", "zendesk:ticket:read", True),
        ("*:*:delete", "jira:issue:delete", True),
        ("*:*:delete", "jira:issue:update", False),
        ("jira:issue:write", "jira:issue:create", True),
        ("jira:issue:write", "jira:issue:update", True),
        ("jira:issue:write", "jira:issue:delete", False),
        ("support_*:*:*", "support_eu:ticket:read", True),
        ("support:*:*", "zendesk:ticket:read", True),  # domain
        ("support:*:*", "jira:issue:read", False),
    ],
)
def test_pattern_matching(pattern, label, expected):
    domains = {"support": frozenset({"zendesk", "support_eu"})}
    assert LabelPattern.parse(pattern).matches(Label.parse(label), domains) is expected


def test_pattern_rejects_unknown_operation():
    with pytest.raises(LabelError):
        LabelPattern.parse("*:*:destroy")
