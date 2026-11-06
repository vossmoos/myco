import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from myco_mcp.identity import AgentKeyVerifier, NoUserTokens, UserTokenError, UserTokenValidator
from myco_mcp.settings import OidcSettings

ISSUER = "https://idp.example.com"
AUDIENCE = "myco"


class StaticJwks:
    def __init__(self, public_key):
        self.public_key = public_key

    def get_signing_key_from_jwt(self, token):
        return jwt.PyJWK.from_dict(jwt.algorithms.RSAAlgorithm.to_jwk(self.public_key, as_dict=True))


@pytest.fixture
def keys():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048), rsa.generate_private_key(
        public_exponent=65537, key_size=2048
    )


@pytest.fixture
def validator(keys):
    v = UserTokenValidator(OidcSettings(issuer=ISSUER, audience=AUDIENCE))
    v._jwks = StaticJwks(keys[0].public_key())
    return v


def token(key, **overrides):
    claims = {"iss": ISSUER, "aud": AUDIENCE, "sub": "u1", "exp": int(time.time()) + 300, "groups": ["sales"]}
    claims.update(overrides)
    return jwt.encode(claims, key, algorithm="RS256")


async def test_valid_token_yields_claims(validator, keys):
    claims = await validator.claims(token(keys[0]))
    assert claims["sub"] == "u1" and claims["groups"] == ["sales"]


@pytest.mark.parametrize(
    "overrides", [{"aud": "other"}, {"iss": "https://evil.example.com"}, {"exp": int(time.time()) - 3600}]
)
async def test_invalid_claims_are_rejected(validator, keys, overrides):
    with pytest.raises(UserTokenError):
        await validator.claims(token(keys[0], **overrides))


async def test_wrong_signing_key_is_rejected(validator, keys):
    with pytest.raises(UserTokenError):
        await validator.claims(token(keys[1]))


async def test_tokens_refused_without_oidc():
    with pytest.raises(UserTokenError, match="not configured"):
        await NoUserTokens().claims("anything")


async def test_agent_keys():
    verifier = AgentKeyVerifier({"DEFAULT": "k" * 20, "BOT": "b" * 20})
    assert (await verifier.verify_token("b" * 20)).client_id == "BOT"
    assert await verifier.verify_token("x" * 20) is None
