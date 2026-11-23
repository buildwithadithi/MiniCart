import sys
import time
from collections import namedtuple
from pathlib import Path
from uuid import uuid4

import grpc
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ORDER_ROOT = PROJECT_ROOT / "order-service"
for path in (str(PROJECT_ROOT), str(ORDER_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

import servicer as order_servicer_module
from database import Base
from generated import order_pb2, product_pb2
from models import Order, OrderIdempotency
from security_config import JwtVerifier
from servicer import OrderServiceServicer


MetadataEntry = namedtuple("MetadataEntry", ("key", "value"))


class RpcAbort(Exception):
    def __init__(self, code, details):
        self.code = code
        self.details = details


class FakeContext:
    def __init__(self, idempotency_key=None):
        self._metadata = []
        if idempotency_key:
            self._metadata.append(
                type("MetadataEntry", (), {"key": "idempotency-key", "value": idempotency_key})()
            )

    def invocation_metadata(self):
        return tuple(self._metadata)

    def abort(self, code, details):
        raise RpcAbort(code, details)


class FakeProductClient:
    def __init__(self):
        self.stock = {7: 10, 8: 10}
        self.get_product_calls = 0
        self.check_stock_calls = 0
        self.update_stock_calls = []

    def get_product(self, product_id):
        self.get_product_calls += 1
        return product_pb2.Product(id=product_id, name="test", price="2.50")

    def check_stock(self, product_id):
        self.check_stock_calls += 1
        return product_pb2.CheckStockResponse(
            product_exists=True,
            available_stock=self.stock.get(product_id, 0),
        )

    def update_stock(
        self,
        product_id,
        quantity_delta,
        operation_id=None,
    ):
        self.update_stock_calls.append((product_id, quantity_delta, operation_id))
        self.stock[product_id] += quantity_delta
        return self.stock[product_id]

    def close(self):
        pass


class StockUpdateFailure(grpc.RpcError):
    def code(self):
        return grpc.StatusCode.UNAVAILABLE

    def details(self):
        return "Product Service is temporarily unavailable."


@pytest.fixture
def order_environment(monkeypatch):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(order_servicer_module, "SessionLocal", session_factory)
    verifier = JwtVerifier(public_key, "minicart-auth", "minicart-api")

    def token_for(user_id):
        now = int(time.time())
        return jwt.encode(
            {
                "sub": str(user_id),
                "iss": "minicart-auth",
                "aud": "minicart-api",
                "iat": now,
                "exp": now + 300,
            },
            private_pem,
            algorithm="RS256",
        )

    product_client = FakeProductClient()
    servicer = OrderServiceServicer(
        token_verifier=verifier,
        product_client=product_client,
    )
    yield session_factory, servicer, product_client, token_for
    servicer.close()
    Base.metadata.drop_all(engine)
    engine.dispose()


def _context(token_for, user_id, idempotency_key):
    token = token_for(user_id)
    return type("MetadataContext", (), {
        "invocation_metadata": lambda self: (
            MetadataEntry("authorization", f"Bearer {token}"),
            MetadataEntry("idempotency-key", idempotency_key),
        ),
        "abort": lambda self, code, details: (_ for _ in ()).throw(RpcAbort(code, details)),
    })()


def test_new_key_creates_one_order_and_repeat_returns_original(order_environment):
    session_factory, servicer, product_client, token_for = order_environment
    key = f"create-{uuid4().hex}"
    request = order_pb2.CreateOrderRequest(
        items=[order_pb2.OrderItemInput(product_id=7, quantity=2)]
    )

    first = servicer.CreateOrder(request, _context(token_for, 101, key))
    second = servicer.CreateOrder(request, _context(token_for, 101, key))

    assert second.order.id == first.order.id
    assert len(product_client.update_stock_calls) == 1
    assert product_client.stock[7] == 8
    with session_factory() as session:
        assert len(session.scalars(select(Order)).all()) == 1


def test_same_key_with_different_payload_is_rejected(order_environment):
    _, servicer, _, token_for = order_environment
    key = f"payload-{uuid4().hex}"
    first = order_pb2.CreateOrderRequest(
        items=[order_pb2.OrderItemInput(product_id=7, quantity=1)]
    )
    second = order_pb2.CreateOrderRequest(
        items=[order_pb2.OrderItemInput(product_id=7, quantity=2)]
    )

    servicer.CreateOrder(first, _context(token_for, 101, key))
    with pytest.raises(RpcAbort) as raised:
        servicer.CreateOrder(second, _context(token_for, 101, key))

    assert raised.value.code == grpc.StatusCode.ALREADY_EXISTS


def test_idempotency_is_scoped_to_authenticated_user(order_environment):
    _, servicer, _, token_for = order_environment
    key = f"user-{uuid4().hex}"
    request = order_pb2.CreateOrderRequest(
        items=[order_pb2.OrderItemInput(product_id=7, quantity=1)]
    )

    servicer.CreateOrder(request, _context(token_for, 101, key))
    second = servicer.CreateOrder(request, _context(token_for, 202, key))

    assert second.order.id > 0


def test_failed_processing_does_not_leave_processing_record(order_environment):
    session_factory, servicer, _, token_for = order_environment
    key = f"failed-{uuid4().hex}"

    class FailingProductClient(FakeProductClient):
        def update_stock(self, product_id, quantity_delta, operation_id=None):
            raise StockUpdateFailure()

    servicer.product_client = FailingProductClient()
    request = order_pb2.CreateOrderRequest(
        items=[order_pb2.OrderItemInput(product_id=7, quantity=1)]
    )

    with pytest.raises(RpcAbort) as raised:
        servicer.CreateOrder(request, _context(token_for, 101, key))
    assert raised.value.code == grpc.StatusCode.UNAVAILABLE
    with session_factory() as session:
        record = session.scalar(
            select(OrderIdempotency).where(
                OrderIdempotency.idempotency_key == key
            )
        )
    assert record is None


def test_compensation_uses_a_distinct_operation_id(order_environment):
    _, servicer, _, token_for = order_environment

    class FailingSecondProductClient(FakeProductClient):
        def update_stock(self, product_id, quantity_delta, operation_id=None):
            self.update_stock_calls.append((product_id, quantity_delta, operation_id))
            if product_id == 8 and quantity_delta < 0:
                raise StockUpdateFailure()
            self.stock[product_id] += quantity_delta
            return self.stock[product_id]

    product_client = FailingSecondProductClient()
    servicer.product_client = product_client
    request = order_pb2.CreateOrderRequest(
        items=[
            order_pb2.OrderItemInput(product_id=7, quantity=1),
            order_pb2.OrderItemInput(product_id=8, quantity=1),
        ]
    )

    with pytest.raises(RpcAbort):
        servicer.CreateOrder(request, _context(token_for, 101, f"comp-{uuid4().hex}"))

    assert [(product_id, quantity) for product_id, quantity, _ in product_client.update_stock_calls] == [
        (7, -1),
        (8, -1),
        (7, 1),
    ]
    operation_ids = [operation_id for _, _, operation_id in product_client.update_stock_calls]
    assert all(operation_ids)
    assert len(set(operation_ids)) == len(operation_ids)
    assert product_client.stock == {7: 10, 8: 10}


def test_failed_compensation_blocks_replaying_the_same_key(order_environment):
    session_factory, servicer, _, token_for = order_environment
    key = f"compensation-failed-{uuid4().hex}"

    class FailingCompensationClient(FakeProductClient):
        def update_stock(self, product_id, quantity_delta, operation_id=None):
            self.update_stock_calls.append((product_id, quantity_delta, operation_id))
            if (product_id == 8 and quantity_delta < 0) or (
                product_id == 7 and quantity_delta > 0
            ):
                raise StockUpdateFailure()
            self.stock[product_id] += quantity_delta
            return self.stock[product_id]

    product_client = FailingCompensationClient()
    servicer.product_client = product_client
    request = order_pb2.CreateOrderRequest(
        items=[
            order_pb2.OrderItemInput(product_id=7, quantity=1),
            order_pb2.OrderItemInput(product_id=8, quantity=1),
        ]
    )

    with pytest.raises(RpcAbort) as first_error:
        servicer.CreateOrder(request, _context(token_for, 101, key))
    assert first_error.value.code == grpc.StatusCode.INTERNAL

    with session_factory() as session:
        record = session.scalar(
            select(OrderIdempotency).where(
                OrderIdempotency.idempotency_key == key
            )
        )
        assert record.status == "FAILED"

    update_count = len(product_client.update_stock_calls)
    with pytest.raises(RpcAbort) as repeated_error:
        servicer.CreateOrder(request, _context(token_for, 101, key))
    assert repeated_error.value.code == grpc.StatusCode.FAILED_PRECONDITION
    assert len(product_client.update_stock_calls) == update_count
