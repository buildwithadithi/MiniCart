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
SERVICE_ROOT = PROJECT_ROOT / "order-service"
TESTS_ROOT = PROJECT_ROOT / "tests"
for path in (str(PROJECT_ROOT), str(SERVICE_ROOT), str(TESTS_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from database import engine
from database_connections import create_service_database_connections
from models import Order, OrderItem
from generated import auth_pb2, auth_pb2_grpc
from generated import order_pb2_grpc, payment_pb2_grpc, product_pb2_grpc
from servicer import OrderServiceServicer


@pytest.fixture(scope="session")
def service_databases():
    try:
        connections = create_service_database_connections()
    except (RuntimeError, ValueError) as error:
        pytest.fail(
            "MiniCart PostgreSQL configuration is unavailable: "
            f"{error}",
            pytrace=False,
        )
    yield connections
    for service in connections.values():
        service["engine"].dispose()


@pytest.fixture(scope="session")
def require_running_services_and_database():
    addresses = (
        ("Order Service", os.getenv("ORDER_SERVICE_ADDRESS", "127.0.0.1:50052")),
        ("Product Service", os.getenv("PRODUCT_SERVICE_ADDRESS", "127.0.0.1:50051")),
        ("Payment Service", os.getenv("PAYMENT_SERVICE_ADDRESS", "127.0.0.1:50053")),
        ("Authentication Service", os.getenv("AUTH_SERVICE_ADDRESS", "127.0.0.1:50054")),
    )
    for service_name, address in addresses:
        channel = grpc.insecure_channel(address)
        try:
            grpc.channel_ready_future(channel).result(timeout=3)
        except grpc.FutureTimeoutError:
            channel.close()
            pytest.fail(
                f"{service_name} is not reachable at {address}. Start all three "
                "services from the project root using the README commands, then "
                "rerun the tests.",
                pytrace=False,
            )
        channel.close()

    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except (SQLAlchemyError, RuntimeError) as error:
        pytest.fail(
            "The MiniCart PostgreSQL database is not reachable. Start it with "
            "`docker compose up -d postgres` and ensure DB_PASSWORD matches the "
            "existing database role. "
            f"Connection error: {error}",
            pytrace=False,
        )


@pytest.fixture(scope="session")
def auth_token(require_running_services_and_database, service_databases):
    address = os.getenv("AUTH_SERVICE_ADDRESS", "127.0.0.1:50054")
    channel = grpc.insecure_channel(address)
    stub = auth_pb2_grpc.AuthServiceStub(channel)
    email = f"order-tests-{uuid4().hex}@example.com"
    password = "order-tests-password-7431"
    registration = None
    try:
        registration = stub.Register(
            auth_pb2.RegisterRequest(
                name="Order Service Test User",
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
            order_ids = []
            with service_databases["order"]["sessions"].begin() as session:
                order_ids = list(
                    session.execute(
                        text("SELECT id FROM orders WHERE user_id = :user_id"),
                        {"user_id": registration.user_id},
                    ).scalars()
                )
                if order_ids:
                    session.execute(
                        text("DELETE FROM order_items WHERE order_id = ANY(:order_ids)"),
                        {"order_ids": order_ids},
                    )
                    session.execute(
                        text("DELETE FROM orders WHERE id = ANY(:order_ids)"),
                        {"order_ids": order_ids},
                    )
            if order_ids:
                with service_databases["payment"]["sessions"].begin() as session:
                    session.execute(
                        text("DELETE FROM payments WHERE order_id = ANY(:order_ids)"),
                        {"order_ids": order_ids},
                    )
            with service_databases["auth"]["sessions"].begin() as session:
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
def order_stub(require_running_services_and_database, auth_token):
    assert require_running_services_and_database is None
    address = os.getenv("ORDER_SERVICE_ADDRESS", "127.0.0.1:50052")
    channel = grpc.insecure_channel(address)
    yield AuthenticatedOrderStub(order_pb2_grpc.OrderServiceStub(channel), auth_token)
    channel.close()


@pytest.fixture(scope="session")
def product_stub(require_running_services_and_database):
    assert require_running_services_and_database is None
    address = os.getenv("PRODUCT_SERVICE_ADDRESS", "127.0.0.1:50051")
    channel = grpc.insecure_channel(address)
    yield product_pb2_grpc.ProductServiceStub(channel)
    channel.close()


@pytest.fixture(scope="session")
def payment_stub(require_running_services_and_database):
    assert require_running_services_and_database is None
    address = os.getenv("PAYMENT_SERVICE_ADDRESS", "127.0.0.1:50053")
    channel = grpc.insecure_channel(address)
    yield payment_pb2_grpc.PaymentServiceStub(channel)
    channel.close()


@pytest.fixture
def test_records(service_databases):
    records = {"order_ids": [], "product_ids": []}
    yield records

    order_ids = records["order_ids"]
    product_ids = records["product_ids"]
    if not order_ids and not product_ids:
        return

    if order_ids:
        with service_databases["order"]["sessions"].begin() as session:
            session.query(OrderItem).filter(
                OrderItem.order_id.in_(order_ids)
            ).delete(synchronize_session=False)
            session.query(Order).filter(Order.id.in_(order_ids)).delete(
                synchronize_session=False
            )
        with service_databases["payment"]["sessions"].begin() as session:
            session.execute(
                text("DELETE FROM payments WHERE order_id = ANY(:order_ids)"),
                {"order_ids": order_ids},
            )
    if product_ids:
        with service_databases["product"]["sessions"].begin() as session:
            session.execute(
                text("DELETE FROM inventory WHERE product_id = ANY(:product_ids)"),
                {"product_ids": product_ids},
            )
            session.execute(
                text("DELETE FROM products WHERE id = ANY(:product_ids)"),
                {"product_ids": product_ids},
            )


@pytest.fixture
def start_isolated_order_service(require_running_services_and_database, auth_token):
    assert require_running_services_and_database is None
    servers = []

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
            raise RuntimeError("Unable to start isolated Order Service test server.")
        server.start()

        channel = grpc.insecure_channel(f"127.0.0.1:{port}")
        try:
            grpc.channel_ready_future(channel).result(timeout=3)
        except grpc.FutureTimeoutError:
            channel.close()
            server.stop(grace=0).wait()
            servicer.close()
            raise RuntimeError("Isolated Order Service did not become ready.")

        servers.append((server, channel, servicer))
        return AuthenticatedOrderStub(order_pb2_grpc.OrderServiceStub(channel), auth_token)

    yield start

    for server, channel, servicer in servers:
        channel.close()
        server.stop(grace=1).wait()
        servicer.close()
