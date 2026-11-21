from argon2 import PasswordHasher
import grpc
import jwt
import pytest

from generated import auth_pb2
from auth_models import User
from auth_security import JwtVerifier
from cryptography.hazmat.primitives.asymmetric import rsa


def _register(auth_stub, *, email="user@example.com", password="correct-horse-42"):
    return auth_stub.Register(
        auth_pb2.RegisterRequest(
            name="Test User",
            email=email,
            password=password,
        ),
        timeout=3,
    )


def test_registration_hashes_password_and_normalizes_email(auth_stub, session_factory):
    response = _register(auth_stub, email="User@Example.com")

    assert response.user_id > 0
    assert response.email == "user@example.com"
    with session_factory() as session:
        user = session.get(User, response.user_id)
        assert user is not None
        assert user.password_hash != "correct-horse-42"
        assert PasswordHasher().verify(user.password_hash, "correct-horse-42")


def test_duplicate_email_is_rejected(auth_stub):
    _register(auth_stub)

    with pytest.raises(grpc.RpcError) as error:
        _register(auth_stub, email="USER@example.com")

    assert error.value.code() == grpc.StatusCode.ALREADY_EXISTS


def test_login_returns_verifiable_jwt(auth_stub, key_pair):
    _, public_key = key_pair
    registered = _register(auth_stub)
    response = auth_stub.Login(
        auth_pb2.LoginRequest(
            email="USER@example.com",
            password="correct-horse-42",
        ),
        timeout=3,
    )

    principal = JwtVerifier(public_key).verify(response.access_token)
    assert principal.user_id == registered.user_id
    assert response.token_type == "Bearer"
    assert response.expires_in == 60


def test_login_rejects_incorrect_password(auth_stub):
    _register(auth_stub)

    with pytest.raises(grpc.RpcError) as error:
        auth_stub.Login(
            auth_pb2.LoginRequest(
                email="user@example.com",
                password="wrong-password-42",
            ),
            timeout=3,
        )

    assert error.value.code() == grpc.StatusCode.UNAUTHENTICATED


def test_jwt_verifier_rejects_expired_and_invalid_signature(key_pair):
    private_key, public_key = key_pair
    now = 1_700_000_000
    expired = jwt.encode(
        {
            "sub": "1",
            "iss": "minicart-auth",
            "aud": "minicart-api",
            "iat": now - 60,
            "exp": now - 1,
        },
        private_key,
        algorithm="RS256",
    )
    verifier = JwtVerifier(public_key)

    with pytest.raises(jwt.ExpiredSignatureError):
        verifier.verify(expired)

    other_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged = jwt.encode(
        {
            "sub": "1",
            "iss": "minicart-auth",
            "aud": "minicart-api",
            "iat": now,
            "exp": now + 60,
        },
        other_private_key,
        algorithm="RS256",
    )
    with pytest.raises(jwt.InvalidSignatureError):
        verifier.verify(forged)
