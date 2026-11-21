from datetime import datetime, timezone

import grpc
import pytest
from fastapi.testclient import TestClient
from google.protobuf.timestamp_pb2 import Timestamp

from generated import order_pb2
from gateway.main import create_app


def _order_message() -> order_pb2.Order:
    created_at = Timestamp()
    created_at.FromDatetime(datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc))
    return order_pb2.Order(
        id=42,
        total_amount="39.90",
        status="PAID",
        created_at=created_at,
        payment_id=88,
        payment_status="SUCCESS",
        items=[
            order_pb2.OrderItem(
                id=7,
                product_id=12,
                quantity=2,
                unit_price="19.95",
            )
        ],
    )


class FakeRpcError(grpc.RpcError):
    def __init__(self, code: grpc.StatusCode):
        self._code = code

    def code(self) -> grpc.StatusCode:
        return self._code

    def details(self) -> str:
        return "Internal downstream details must not be exposed."


class FakeOrderClient:
    def __init__(self):
        self.create_request = None
        self.requested_order_id = None
        self.create_error = None
        self.get_error = None
        self.order = _order_message()

    def create_order(self, request: order_pb2.CreateOrderRequest):
        self.create_request = request
        if self.create_error is not None:
            raise self.create_error
        return order_pb2.CreateOrderResponse(order=self.order)

    def get_order(self, order_id: int):
        self.requested_order_id = order_id
        if self.get_error is not None:
            raise self.get_error
        return order_pb2.GetOrderResponse(order=self.order)

    def close(self):
        pass


@pytest.fixture
def order_client():
    return FakeOrderClient()


@pytest.fixture
def client(order_client):
    with TestClient(create_app(order_client)) as test_client:
        yield test_client


def test_health_endpoint(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_create_order_converts_request_and_response(client, order_client):
    response = client.post(
        "/orders",
        json={"items": [{"product_id": 12, "quantity": 2}]},
    )

    assert response.status_code == 201
    assert order_client.create_request == order_pb2.CreateOrderRequest(
        items=[order_pb2.OrderItemInput(product_id=12, quantity=2)]
    )
    assert response.json() == {
        "id": 42,
        "total_amount": "39.90",
        "status": "PAID",
        "created_at": "2026-01-02T03:04:05Z",
        "items": [
            {
                "id": 7,
                "product_id": 12,
                "quantity": 2,
                "unit_price": "19.95",
            }
        ],
        "payment_id": 88,
        "payment_status": "SUCCESS",
    }


def test_get_order_converts_response(client, order_client):
    response = client.get("/orders/42")

    assert response.status_code == 200
    assert order_client.requested_order_id == 42
    assert response.json()["payment_id"] == 88
    assert response.json()["payment_status"] == "SUCCESS"
    assert response.json()["items"][0]["unit_price"] == "19.95"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"items": []},
        {"items": [{"product_id": 0, "quantity": 1}]},
        {"items": [{"product_id": 1, "quantity": 0}]},
        {"items": [{"product_id": 1, "quantity": 1, "unit_price": "0.01"}]},
    ],
)
def test_invalid_create_order_request_is_rejected(client, order_client, payload):
    response = client.post("/orders", json=payload)

    assert response.status_code == 422
    assert order_client.create_request is None


@pytest.mark.parametrize(
    ("grpc_code", "http_status"),
    [
        (grpc.StatusCode.NOT_FOUND, 404),
        (grpc.StatusCode.UNAVAILABLE, 503),
        (grpc.StatusCode.DEADLINE_EXCEEDED, 504),
    ],
)
def test_order_service_errors_are_mapped_and_sanitized(
    client, order_client, grpc_code, http_status
):
    order_client.get_error = FakeRpcError(grpc_code)

    response = client.get("/orders/42")

    assert response.status_code == http_status
    assert "Internal downstream details" not in response.text


def test_invalid_order_id_is_rejected(client, order_client):
    response = client.get("/orders/0")

    assert response.status_code == 422
    assert order_client.requested_order_id is None


def test_openapi_and_swagger_ui_are_available(client):
    openapi_response = client.get("/openapi.json")
    docs_response = client.get("/docs")

    assert openapi_response.status_code == 200
    assert "/orders" in openapi_response.json()["paths"]
    assert "/orders/{order_id}" in openapi_response.json()["paths"]
    assert "OrderItemInput" in openapi_response.json()["components"]["schemas"]
    assert docs_response.status_code == 200
    assert "swagger-ui" in docs_response.text
