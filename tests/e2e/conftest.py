import os
import sys
from concurrent import futures
from pathlib import Path
from uuid import uuid4

import grpc
import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ORDER_SERVICE_ROOT = PROJECT_ROOT / "order-service"
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(ORDER_SERVICE_ROOT))

from database import SessionLocal, engine
from models import Order, OrderItem
from generated import auth_pb2, auth_pb2_grpc
from generated import order_pb2_grpc, payment_pb2_grpc, product_pb2_grpc
from servicer import OrderServiceServicer


def _service_addresses() -> tuple[tuple[str, str], ...]:
    return (
        ("Product Service", os.getenv("PRODUCT_SERVICE_ADDRESS", "127.0.0.1:50051")),
        ("Order Service", os.getenv("ORDER_SERVICE_ADDRESS", "127.0.0.1:50052")),
        ("Payment Service", os.getenv("PAYMENT_SERVICE_ADDRESS", "127.0.0.1:50053")),
        (
            "Authentication Service",
            os.getenv("AUTH_SERVICE_ADDRESS", "127.0.0.1:50054"),
        ),
    )


@pytest.fixture(scope="session", autouse=True)
def require_services_and_database():
    for service_name, address in _service_addresses():
        channel = grpc.insecure_channel(address)
        try:
            grpc.channel_ready_future(channel).result(timeout=3)
        except grpc.FutureTimeoutError:
            channel.close()
            pytest.fail(
                f"{service_name} is not reachable at {address}. Start all three "
                "services using the commands in README.md, then rerun the tests.",
                pytrace=False,
            )
        channel.close()

    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except (SQLAlchemyError, RuntimeError) as error:
        pytest.fail(
            "MiniCart PostgreSQL is not reachable. Start it with "
            "`docker compose up -d postgres` and ensure DB_PASSWORD matches the "
            "existing database role. "
            f"Connection error: {error}",
            pytrace=False,
        )


@pytest.fixture(scope="session")
def product_stub():
    channel = grpc.insecure_channel(_service_addresses()[0][1])
    yield product_pb2_grpc.ProductServiceStub(channel)
    channel.close()


@pytest.fixture(scope="session")
def auth_token():
    address = os.getenv("AUTH_SERVICE_ADDRESS", "127.0.0.1:50054")
    channel = grpc.insecure_channel(address)
    stub = auth_pb2_grpc.AuthServiceStub(channel)
    email = f"e2e-tests-{uuid4().hex}@example.com"
    password = "e2e-tests-password-7431"
    registration = None
    try:
        registration = stub.Register(
            auth_pb2.RegisterRequest(
                name="MiniCart E2E Test User",
                email=email,
                password=password,
            ),
            timeout=5,
        )
        token = stub.Login(
            auth_pb2.LoginRequest(email=email, password=password),
            timeout=5,
        ).access_token
        yield token
    finally:
        channel.close()
        if registration is not None:
            with SessionLocal.begin() as session:
                order_ids = list(
                    session.execute(
                        text("SELECT id FROM orders WHERE user_id = :user_id"),
                        {"user_id": registration.user_id},
                    ).scalars()
                )
                if order_ids:
                    session.execute(
                        text("DELETE FROM payments WHERE order_id = ANY(:order_ids)"),
                        {"order_ids": order_ids},
                    )
                    session.execute(
                        text("DELETE FROM order_items WHERE order_id = ANY(:order_ids)"),
                        {"order_ids": order_ids},
                    )
                    session.execute(
                        text("DELETE FROM orders WHERE id = ANY(:order_ids)"),
                        {"order_ids": order_ids},
                    )
                session.execute(
                    text("DELETE FROM users WHERE id = :user_id"),
                    {"user_id": registration.user_id},
                )


class AuthenticatedOrderStub:
    def __init__(self, stub, token):
        self._stub = stub
        self._metadata = (("authorization", f"Bearer {token}"),)

    def CreateOrder(self, request, **kwargs):
        kwargs["metadata"] = self._metadata
        return self._stub.CreateOrder(request, **kwargs)

    def GetOrder(self, request, **kwargs):
        kwargs["metadata"] = self._metadata
        return self._stub.GetOrder(request, **kwargs)


@pytest.fixture(scope="session")
def order_stub(auth_token):
    channel = grpc.insecure_channel(_service_addresses()[1][1])
    yield AuthenticatedOrderStub(order_pb2_grpc.OrderServiceStub(channel), auth_token)
    channel.close()


@pytest.fixture(scope="session")
def payment_stub():
    channel = grpc.insecure_channel(_service_addresses()[2][1])
    yield payment_pb2_grpc.PaymentServiceStub(channel)
    channel.close()


@pytest.fixture
def created_records():
    records = {"order_ids": [], "product_ids": []}
    yield records

    order_ids = records["order_ids"]
    product_ids = records["product_ids"]
    with SessionLocal.begin() as session:
        if order_ids:
            session.execute(
                text("DELETE FROM payments WHERE order_id = ANY(:order_ids)"),
                {"order_ids": order_ids},
            )
            session.query(OrderItem).filter(
                OrderItem.order_id.in_(order_ids)
            ).delete(synchronize_session=False)
            session.query(Order).filter(Order.id.in_(order_ids)).delete(
                synchronize_session=False
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


@pytest.fixture
def isolated_order_stub(auth_token):
    instances = []

    def start(product_address=None, payment_address=None):
        servicer = OrderServiceServicer(
            product_address=product_address,
            payment_address=payment_address,
        )
        server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
        order_pb2_grpc.add_OrderServiceServicer_to_server(servicer, server)
        port = server.add_insecure_port("127.0.0.1:0")
        if port == 0:
            servicer.close()
            raise RuntimeError("Unable to start isolated E2E Order Service.")
        server.start()

        channel = grpc.insecure_channel(f"127.0.0.1:{port}")
        try:
            grpc.channel_ready_future(channel).result(timeout=3)
        except grpc.FutureTimeoutError:
            channel.close()
            server.stop(grace=0).wait()
            servicer.close()
            raise RuntimeError("Isolated E2E Order Service did not become ready.")

        instances.append((server, channel, servicer))
        return AuthenticatedOrderStub(order_pb2_grpc.OrderServiceStub(channel), auth_token)

    yield start

    for server, channel, servicer in instances:
        channel.close()
        server.stop(grace=1).wait()
        servicer.close()
