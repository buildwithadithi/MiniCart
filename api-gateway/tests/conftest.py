import sys
import time
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


PROJECT_ROOT = Path(__file__).resolve().parents[2]
GATEWAY_ROOT = PROJECT_ROOT / "api-gateway"
for path in (str(PROJECT_ROOT), str(GATEWAY_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from gateway.security import JwtVerifier


@pytest.fixture
def key_pair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")
    public_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")
    return private_key, private_pem, public_pem


@pytest.fixture
def token_verifier(key_pair):
    return JwtVerifier(key_pair[2], "minicart-auth", "minicart-api")


@pytest.fixture
def access_token(token_factory):
    return token_factory()


@pytest.fixture
def token_factory(key_pair):
    def create_token(*, user_id=42, expires_at=None, private_key=None):
        now = int(time.time())
        issued_at = now if expires_at is None or expires_at > now else now - 60
        return jwt.encode(
            {
                "sub": str(user_id),
                "iss": "minicart-auth",
                "aud": "minicart-api",
                "iat": issued_at,
                "exp": expires_at if expires_at is not None else now + 600,
            },
            private_key or key_pair[0],
            algorithm="RS256",
        )

    return create_token
