import os
import sys
from pathlib import Path

import grpc
import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SERVICE_ROOT = PROJECT_ROOT / "product-service"
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(SERVICE_ROOT))

from database import SessionLocal, engine
from models import Inventory, Product
from generated import product_pb2_grpc


@pytest.fixture(scope="session", autouse=True)
def require_running_service_and_database():
    address = os.getenv("PRODUCT_SERVICE_ADDRESS", "127.0.0.1:50051")
    channel = grpc.insecure_channel(address)
    try:
        grpc.channel_ready_future(channel).result(timeout=3)
    except grpc.FutureTimeoutError:
        channel.close()
        pytest.fail(
            "Product Service is not reachable at "
            f"{address}. Start it from the project root in another terminal with "
            "`python product-service/server.py`, then rerun the tests.",
            pytrace=False,
        )

    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except (SQLAlchemyError, RuntimeError) as error:
        channel.close()
        pytest.fail(
            "The MiniCart PostgreSQL database is not reachable. Start it with "
            "`docker compose up -d postgres` and ensure DB_PASSWORD is configured "
            "in the environment or project .env file and matches the password "
            "of the existing database role. "
            f"Connection error: {error}",
            pytrace=False,
        )
    channel.close()


@pytest.fixture(scope="session")
def product_stub():
    address = os.getenv("PRODUCT_SERVICE_ADDRESS", "127.0.0.1:50051")
    channel = grpc.insecure_channel(address)
    yield product_pb2_grpc.ProductServiceStub(channel)
    channel.close()


@pytest.fixture
def created_product_ids():
    product_ids = []
    yield product_ids

    if product_ids:
        with SessionLocal.begin() as session:
            session.query(Inventory).filter(
                Inventory.product_id.in_(product_ids)
            ).delete(synchronize_session=False)
            session.query(Product).filter(Product.id.in_(product_ids)).delete(
                synchronize_session=False
            )
