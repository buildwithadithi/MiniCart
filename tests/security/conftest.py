import os
import sys
from pathlib import Path
from uuid import uuid4

import grpc
import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ORDER_SERVICE_ROOT = PROJECT_ROOT / "order-service"
for path in (str(PROJECT_ROOT), str(ORDER_SERVICE_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from database import SessionLocal, engine
from generated import product_pb2_grpc


def _address(service: str, port: str) -> str:
    return os.getenv(f"{service}_SERVICE_ADDRESS", f"127.0.0.1:{port}")


@pytest.fixture(scope="session", autouse=True)
def require_running_security_stack():
    addresses = (
        ("Product Service", _address("PRODUCT", "50051")),
        ("Order Service", _address("ORDER", "50052")),
        ("Payment Service", _address("PAYMENT", "50053")),
        ("Authentication Service", _address("AUTH", "50054")),
    )
    for service_name, address in addresses:
        channel = grpc.insecure_channel(address)
        try:
            grpc.channel_ready_future(channel).result(timeout=3)
        except grpc.FutureTimeoutError:
            pytest.fail(
                f"{service_name} is unavailable at {address}; start it manually "
                "before running security integration tests.",
                pytrace=False,
            )
        finally:
            channel.close()

    gateway_url = os.getenv("MINICART_GATEWAY_URL", "http://127.0.0.1:8000").rstrip("/")
    try:
        response = httpx.get(f"{gateway_url}/health", timeout=3)
    except httpx.HTTPError as error:
        pytest.fail(
            f"API Gateway is unavailable at {gateway_url}: {error}",
            pytrace=False,
        )
    if response.status_code != 200:
        pytest.fail(
            f"API Gateway health returned HTTP {response.status_code}, expected 200.",
            pytrace=False,
        )

    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            connection.execute(text("SELECT user_id FROM orders LIMIT 0"))
            connection.execute(text("SELECT id FROM users LIMIT 0"))
    except (SQLAlchemyError, RuntimeError) as error:
        pytest.fail(
            "PostgreSQL is unavailable or the auth/ownership migrations have not "
            f"been applied: {error}",
            pytrace=False,
        )


@pytest.fixture
def security_client():
    gateway_url = os.getenv("MINICART_GATEWAY_URL", "http://127.0.0.1:8000").rstrip("/")
    with httpx.Client(base_url=gateway_url, timeout=12) as client:
        yield client


@pytest.fixture(scope="session")
def product_stub():
    channel = grpc.insecure_channel(_address("PRODUCT", "50051"))
    try:
        yield product_pb2_grpc.ProductServiceStub(channel)
    finally:
        channel.close()


@pytest.fixture
def security_records():
    records = {"user_ids": set(), "product_id": None, "order_id": None}
    yield records

    with SessionLocal.begin() as session:
        order_id = records["order_id"]
        product_id = records["product_id"]
        if order_id is None and product_id is not None:
            order_id = session.execute(
                text(
                    "SELECT DISTINCT order_id FROM order_items "
                    "WHERE product_id = :product_id"
                ),
                {"product_id": product_id},
            ).scalar_one_or_none()
        if order_id is not None:
            session.execute(
                text("DELETE FROM payments WHERE order_id = :order_id"),
                {"order_id": order_id},
            )
            session.execute(
                text("DELETE FROM order_items WHERE order_id = :order_id"),
                {"order_id": order_id},
            )
            session.execute(
                text("DELETE FROM orders WHERE id = :order_id"),
                {"order_id": order_id},
            )
        if product_id is not None:
            session.execute(
                text("DELETE FROM inventory WHERE product_id = :product_id"),
                {"product_id": product_id},
            )
            session.execute(
                text("DELETE FROM products WHERE id = :product_id"),
                {"product_id": product_id},
            )
        if records["user_ids"]:
            session.execute(
                text("DELETE FROM users WHERE id = ANY(:user_ids)"),
                {"user_ids": list(records["user_ids"])},
            )


@pytest.fixture
def user_credentials():
    unique = uuid4().hex
    return {
        "name": f"Security test {unique}",
        "email": f"security-{unique}@example.com",
        "password": f"Security-password-{unique}",
    }
