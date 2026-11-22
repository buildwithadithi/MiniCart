from datetime import datetime, timezone
import time

import pytest
from fastapi.testclient import TestClient
from google.protobuf.timestamp_pb2 import Timestamp
from cryptography.hazmat.primitives.asymmetric import rsa

from generated import auth_pb2, order_pb2
from gateway.main import create_app


class FakeAuthClient:
    def __init__(self):
        self.register_request = None
        self.login_request = None

    def register(self, request):
        self.register_request = request
        created_at = Timestamp()
        created_at.FromDatetime(datetime(2026, 1, 2, tzinfo=timezone.utc))
        return auth_pb2.RegisterResponse(
            user_id=7,
            name=request.name,
            email=request.email,
            created_at=created_at,
        )

    def login(self, request):
        self.login_request = request
        return auth_pb2.LoginResponse(
            access_token="signed-token",
            token_type="Bearer",
            expires_in=900,
        )

    def close(self):
        pass


class FakeOrderClient:
    def __init__(self):
        self.authorization_token = None
        self.request = None

    def create_order(self, request, authorization_token):
        self.request = request
        self.authorization_token = authorization_token
        created_at = Timestamp()
        created_at.FromDatetime(datetime(2026, 1, 2, tzinfo=timezone.utc))
        return order_pb2.CreateOrderResponse(
            order=order_pb2.Order(
                id=1,
                total_amount="1.00",
                status="CONFIRMED",
                created_at=created_at,
            )
        )

    def get_order(self, order_id, authorization_token):
        self.authorization_token = authorization_token
        created_at = Timestamp()
        created_at.FromDatetime(datetime(2026, 1, 2, tzinfo=timezone.utc))
        return order_pb2.GetOrderResponse(
            order=order_pb2.Order(
                id=order_id,
                total_amount="1.00",
                status="CONFIRMED",
                created_at=created_at,
            )
        )

    def close(self):
        pass


@pytest.fixture
def auth_app(token_verifier):
    auth_client = FakeAuthClient()
    order_client = FakeOrderClient()
    app = create_app(
        order_client,
        auth_client=auth_client,
        token_verifier=token_verifier,
    )
    return app, auth_client, order_client


def test_registration_and_login_are_forwarded_to_auth_service(auth_app):
    app, auth_client, _ = auth_app
    with TestClient(app) as client:
        registration = client.post(
            "/auth/register",
            json={
                "name": "  Ada  ",
                "email": "Ada@example.com",
                "password": "a-very-long-password",
            },
        )
        login = client.post(
            "/auth/login",
            json={"email": "Ada@example.com", "password": "a-very-long-password"},
        )

    assert registration.status_code == 201
    assert registration.json()["id"] == 7
    assert auth_client.register_request == auth_pb2.RegisterRequest(
        name="Ada",
        email="Ada@example.com",
        password="a-very-long-password",
    )
    assert login.status_code == 200
    assert login.json() == {
        "access_token": "signed-token",
        "token_type": "Bearer",
        "expires_in": 900,
    }


def test_missing_token_and_identity_headers_are_rejected(auth_app):
    app, _, order_client = auth_app
    with TestClient(app) as client:
        create_response = client.post(
            "/orders",
            headers={"x-user-id": "7"},
            json={"items": [{"product_id": 1, "quantity": 1}]},
        )
        retrieve_response = client.get(
            "/orders/1",
            headers={"x-user-id": "7"},
        )

    assert create_response.status_code == 401
    assert retrieve_response.status_code == 401
    assert order_client.request is None


def test_invalid_and_expired_tokens_are_rejected(auth_app, token_factory):
    app, _, order_client = auth_app
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    invalid_signature = token_factory(private_key=other_key)
    expired = token_factory(expires_at=int(time.time()) - 1)

    with TestClient(app) as client:
        invalid_response = client.get(
            "/orders/1",
            headers={"Authorization": f"Bearer {invalid_signature}"},
        )
        expired_response = client.get(
            "/orders/1",
            headers={"Authorization": f"Bearer {expired}"},
        )

    assert invalid_response.status_code == 401
    assert expired_response.status_code == 401
    assert order_client.authorization_token is None


def test_verified_bearer_is_forwarded_without_trusting_identity_headers(
    auth_app, token_factory
):
    app, _, order_client = auth_app
    token = token_factory(user_id=42)
    with TestClient(app) as client:
        response = client.post(
            "/orders",
            headers={
                "Authorization": f"Bearer {token}",
                "x-user-id": "999",
            },
            json={"items": [{"product_id": 1, "quantity": 1}]},
        )

    assert response.status_code == 201
    assert order_client.authorization_token == token


def test_order_body_cannot_supply_owner_id(auth_app, token_factory):
    app, _, order_client = auth_app
    token = token_factory(user_id=42)
    with TestClient(app) as client:
        response = client.post(
            "/orders",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "user_id": 999,
                "items": [{"product_id": 1, "quantity": 1}],
            },
        )

    assert response.status_code == 422
    assert order_client.request is None
