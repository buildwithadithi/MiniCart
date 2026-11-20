import os
import sys
from concurrent import futures
from pathlib import Path

import grpc
import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SERVICE_ROOT = PROJECT_ROOT / "order-service"
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(SERVICE_ROOT))

from clients.payment_client import PaymentClient
from clients.product_client import ProductClient
from database import SessionLocal, engine
from models import Order, OrderItem
from generated import order_pb2_grpc, product_pb2_grpc
from generated import payment_pb2_grpc
from servicer import OrderServiceServicer


@pytest.fixture(scope="session", autouse=True)
def require_running_services_and_database():
    addresses = (
        ("Order Service", os.getenv("ORDER_SERVICE_ADDRESS", "127.0.0.1:50052")),
        ("Product Service", os.getenv("PRODUCT_SERVICE_ADDRESS", "127.0.0.1:50051")),
        ("Payment Service", os.getenv("PAYMENT_SERVICE_ADDRESS", "127.0.0.1:50053")),
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
def order_stub():
    address = os.getenv("ORDER_SERVICE_ADDRESS", "127.0.0.1:50052")
    channel = grpc.insecure_channel(address)
    yield order_pb2_grpc.OrderServiceStub(channel)
    channel.close()


@pytest.fixture(scope="session")
def product_stub():
    address = os.getenv("PRODUCT_SERVICE_ADDRESS", "127.0.0.1:50051")
    channel = grpc.insecure_channel(address)
    yield product_pb2_grpc.ProductServiceStub(channel)
    channel.close()


@pytest.fixture(scope="session")
def payment_stub():
    address = os.getenv("PAYMENT_SERVICE_ADDRESS", "127.0.0.1:50053")
    channel = grpc.insecure_channel(address)
    yield payment_pb2_grpc.PaymentServiceStub(channel)
    channel.close()


@pytest.fixture
def test_records():
    records = {"order_ids": [], "product_ids": []}
    yield records

    order_ids = records["order_ids"]
    product_ids = records["product_ids"]
    if not order_ids and not product_ids:
        return

    with SessionLocal.begin() as session:
        if order_ids:
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

    if order_ids:
        with SessionLocal.begin() as session:
            session.execute(
                text("DELETE FROM payments WHERE order_id = ANY(:order_ids)"),
                {"order_ids": order_ids},
            )


@pytest.fixture
def start_isolated_order_service():
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
        return order_pb2_grpc.OrderServiceStub(channel)

    yield start

    for server, channel, servicer in servers:
        channel.close()
        server.stop(grace=1).wait()
        servicer.close()
