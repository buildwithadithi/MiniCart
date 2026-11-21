import os
import sys
from pathlib import Path
from uuid import uuid4

import grpc
import httpx
import pytest
from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ORDER_SERVICE_ROOT = PROJECT_ROOT / "order-service"
for path in (str(PROJECT_ROOT), str(ORDER_SERVICE_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

load_dotenv(PROJECT_ROOT / ".env")


def _service_addresses() -> tuple[tuple[str, str], ...]:
    return (
        (
            "Product Service",
            os.getenv(
                "PRODUCT_SERVICE_ADDRESS",
                f"{os.getenv('PRODUCT_SERVICE_HOST', '127.0.0.1')}:"
                f"{os.getenv('PRODUCT_SERVICE_PORT', '50051')}",
            ),
        ),
        (
            "Order Service",
            os.getenv(
                "ORDER_SERVICE_ADDRESS",
                f"{os.getenv('ORDER_SERVICE_HOST', '127.0.0.1')}:"
                f"{os.getenv('ORDER_SERVICE_PORT', '50052')}",
            ),
        ),
        (
            "Payment Service",
            os.getenv(
                "PAYMENT_SERVICE_ADDRESS",
                f"{os.getenv('PAYMENT_SERVICE_HOST', '127.0.0.1')}:"
                f"{os.getenv('PAYMENT_SERVICE_PORT', '50053')}",
            ),
        ),
        (
            "Authentication Service",
            os.getenv(
                "AUTH_SERVICE_ADDRESS",
                f"{os.getenv('AUTH_SERVICE_HOST', '127.0.0.1')}:"
                f"{os.getenv('AUTH_SERVICE_PORT', '50054')}",
            ),
        ),
    )


def _gateway_url() -> str:
    return os.getenv("MINICART_GATEWAY_URL", "http://127.0.0.1:8000").rstrip("/")


@pytest.fixture(scope="session")
def database_components():
    try:
        from database import SessionLocal, engine
    except (RuntimeError, ValueError) as error:
        pytest.fail(
            "MiniCart PostgreSQL configuration is unavailable. Check DB settings "
            f"in the project .env file. Details: {error}",
            pytrace=False,
        )
    return SessionLocal, engine


@pytest.fixture(scope="session", autouse=True)
def require_running_stack(database_components):
    for service_name, address in _service_addresses():
        channel = grpc.insecure_channel(address)
        try:
            grpc.channel_ready_future(channel).result(timeout=3)
        except grpc.FutureTimeoutError:
            pytest.fail(
                f"{service_name} is not reachable at {address}. Start it manually "
                "before running the Gateway integration tests.",
                pytrace=False,
            )
        finally:
            channel.close()

    _, engine = database_components
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            connection.execute(text("SELECT user_id FROM orders LIMIT 0"))
            connection.execute(text("SELECT id FROM users LIMIT 0"))
    except SQLAlchemyError as error:
        pytest.fail(
            "PostgreSQL or the authentication/ownership schema is unavailable. "
            "Start it with `docker compose up -d postgres`, apply the documented "
            "Authentication and Order Service migrations, and verify DB settings "
            "in .env. "
            f"Connection error: {error}",
            pytrace=False,
        )

    gateway_url = _gateway_url()
    try:
        response = httpx.get(f"{gateway_url}/health", timeout=3)
    except httpx.HTTPError as error:
        pytest.fail(
            f"FastAPI Gateway is not reachable at {gateway_url}. Start it "
            f"manually before running the tests. Connection error: {error}",
            pytrace=False,
        )
    if response.status_code != 200:
        pytest.fail(
            f"FastAPI Gateway health check at {gateway_url}/health returned "
            f"HTTP {response.status_code}, expected 200.",
            pytrace=False,
        )


@pytest.fixture(scope="session")
def product_stub():
    address = _service_addresses()[0][1]
    channel = grpc.insecure_channel(address)
    from generated import product_pb2_grpc

    yield product_pb2_grpc.ProductServiceStub(channel)
    channel.close()


@pytest.fixture(scope="session")
def payment_stub():
    address = _service_addresses()[2][1]
    channel = grpc.insecure_channel(address)
    from generated import payment_pb2_grpc

    yield payment_pb2_grpc.PaymentServiceStub(channel)
    channel.close()


@pytest.fixture
def gateway_client(created_records):
    client = httpx.Client(base_url=_gateway_url(), timeout=12)
    email = f"gateway-tests-{uuid4().hex}@example.com"
    password = "gateway-tests-password-7431"
    registration = client.post(
        "/auth/register",
        json={
            "name": "Gateway Integration Test User",
            "email": email,
            "password": password,
        },
    )
    assert registration.status_code == 201, registration.text
    created_records["user_ids"].add(registration.json()["id"])
    login = client.post(
        "/auth/login",
        json={"email": email, "password": password},
    )
    assert login.status_code == 200, login.text
    client.headers["Authorization"] = (
        f"Bearer {login.json()['access_token']}"
    )
    yield client
    client.close()


def _optional_outage_gateway(env_name: str, authorization: str) -> httpx.Client:
    gateway_url = os.getenv(env_name)
    if not gateway_url:
        pytest.skip(
            f"{env_name} is not set. Start the isolated Order Service and "
            "Gateway for this downstream-outage scenario as documented in README.md."
        )

    try:
        client = httpx.Client(
            base_url=gateway_url.rstrip("/"),
            timeout=15,
            headers={"Authorization": authorization},
        )
    except ValueError as error:
        pytest.fail(
            f"{env_name} must be a valid Gateway HTTP URL. Details: {error}",
            pytrace=False,
        )
    try:
        response = client.get("/health")
    except httpx.HTTPError as error:
        client.close()
        pytest.fail(
            f"Outage-test Gateway {env_name}={gateway_url} is not reachable. "
            f"Start it manually. Connection error: {error}",
            pytrace=False,
        )
    if response.status_code != 200:
        client.close()
        pytest.fail(
            f"Outage-test Gateway at {gateway_url} returned HTTP "
            f"{response.status_code} from /health, expected 200.",
            pytrace=False,
        )
    return client


@pytest.fixture
def product_unavailable_gateway(gateway_client):
    client = _optional_outage_gateway(
        "MINICART_PRODUCT_UNAVAILABLE_GATEWAY_URL",
        gateway_client.headers["Authorization"],
    )
    yield client
    client.close()


@pytest.fixture
def payment_unavailable_gateway(gateway_client):
    client = _optional_outage_gateway(
        "MINICART_PAYMENT_UNAVAILABLE_GATEWAY_URL",
        gateway_client.headers["Authorization"],
    )
    yield client
    client.close()


@pytest.fixture
def created_records(database_components):
    records = {"order_ids": set(), "product_ids": set(), "user_ids": set()}
    yield records

    session_factory, _ = database_components
    product_ids = list(records["product_ids"])
    order_ids = set(records["order_ids"])
    with session_factory.begin() as session:
        if product_ids:
            related_order_ids = session.execute(
                text(
                    "SELECT DISTINCT order_id FROM order_items "
                    "WHERE product_id = ANY(:product_ids)"
                ),
                {"product_ids": product_ids},
            ).scalars()
            order_ids.update(related_order_ids)

        if order_ids:
            order_id_list = list(order_ids)
            session.execute(
                text("DELETE FROM payments WHERE order_id = ANY(:order_ids)"),
                {"order_ids": order_id_list},
            )
            session.execute(
                text("DELETE FROM order_items WHERE order_id = ANY(:order_ids)"),
                {"order_ids": order_id_list},
            )
            session.execute(
                text("DELETE FROM orders WHERE id = ANY(:order_ids)"),
                {"order_ids": order_id_list},
            )

        if product_ids:
            session.execute(
                text("DELETE FROM inventory WHERE product_id = ANY(:product_ids)"),
                {"product_ids": product_ids},
            )
            session.execute(
                text("DELETE FROM products WHERE id = ANY(:product_ids)"),
                {"product_ids": product_ids},
            )
        if records["user_ids"]:
            session.execute(
                text("DELETE FROM users WHERE id = ANY(:user_ids)"),
                {"user_ids": list(records["user_ids"])},
            )
