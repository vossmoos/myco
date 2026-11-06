import pytest

from myco_mcp.settings import Settings, SettingsError, agent_env_name


def test_sources_and_agents_from_env():
    settings = Settings.from_env(
        {
            "MYCO_SOURCE__ZENDESK__DOMAIN": "acme.zendesk.com",
            "MYCO_SOURCE__ZENDESK__API_KEY": "k",
            "MYCO_SOURCE__SUPPORT_EU__TYPE": "zendesk",
            "MYCO_SOURCE__SUPPORT_EU__DOMAIN": "acme-eu.zendesk.com",
            "MYCO_AGENT__SUPPORT_BOT__KEY": "x" * 32,
        }
    )
    by_name = {s.name: s for s in settings.sources}
    assert by_name["zendesk"].type == "zendesk"
    assert by_name["support_eu"].type == "zendesk"
    assert by_name["support_eu"].values == {"domain": "acme-eu.zendesk.com"}
    assert settings.agent_keys == {"SUPPORT_BOT": "x" * 32}
    assert agent_env_name("support-bot") == "SUPPORT_BOT"


@pytest.mark.parametrize(
    "env",
    [
        {"MYCO_AGENT__BOT__KEY": "short"},
        {"MYCO_AGENT__A__KEY": "x" * 20, "MYCO_AGENT__B__KEY": "x" * 20},
        {"MYCO_AGENT__BOT__TOKEN": "x" * 20},
        {"MYCO_OIDC__ISSUER": "https://idp.example.com"},
        {"MYCO_SOURCE__ZENDESK": "x"},
    ],
)
def test_invalid_env_is_rejected(env):
    with pytest.raises(SettingsError):
        Settings.from_env(env)
