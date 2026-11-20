import os
import sys
from pathlib import Path

import grpc
import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SERVICE_ROOT = PROJECT_ROOT / "payment-service"
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(SERVICE_ROOT))

from database import SessionLocal, engine
from models import Payment
from generated import payment_pb2_grpc


@pytest.fixture(scope="session", autouse=True)
def require_running_service_and_database():
    address = os.getenv("PAYMENT_SERVICE_ADDRESS", "127.0.0.1:50053")
    channel = grpc.insecure_channel(address)
    try:
        grpc.channel_ready_future(channel).result(timeout=3)
    except grpc.FutureTimeoutError:
        channel.close()
        pytest.fail(
            "Payment Service is not reachable at "
            f"{address}. Start it from the project root in another terminal with "
            "`python payment-service/server.py`, then rerun the tests.",
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
            "in the environment or project .env file and matches the existing "
            "database role. "
            f"Connection error: {error}",
            pytrace=False,
        )
    channel.close()


@pytest.fixture(scope="session")
def payment_stub():
    address = os.getenv("PAYMENT_SERVICE_ADDRESS", "127.0.0.1:50053")
    channel = grpc.insecure_channel(address)
    yield payment_pb2_grpc.PaymentServiceStub(channel)
    channel.close()


@pytest.fixture
def created_payment_ids():
    payment_ids = []
    yield payment_ids

    if payment_ids:
        with SessionLocal.begin() as session:
            session.query(Payment).filter(
                Payment.id.in_(payment_ids)
            ).delete(synchronize_session=False)
