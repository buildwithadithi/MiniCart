import time

import grpc
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import servicer as order_servicer_module
from database import Base
from generated import order_pb2, product_pb2
from models import Order
from security_config import JwtVerifier
from servicer import OrderServiceServicer


class RpcAbort(Exception):
    def __init__(self, code, details):
        self.code = code
        self.details = details


class FakeContext:
    def __init__(self, metadata=()):
        self._metadata = metadata

    def invocation_metadata(self):
        return self._metadata

    def abort(self, code, details):
        raise RpcAbort(code, details)


class FakeProductClient:
    def get_product(self, product_id: int, /):
        return product_pb2.Product(
            id=product_id,
            name="test",
            price="2.50",
        )

    def check_stock(self, _product_id: int, /):
        return product_pb2.CheckStockResponse(
            product_exists=True,
            available_stock=5,
        )

    def update_stock(self, _product_id: int, quantity_delta: int, /):
        return 5 + quantity_delta

    def close(self):
        pass


class StockUpdateUnavailable(grpc.RpcError):
    def code(self):
        return grpc.StatusCode.UNAVAILABLE

    def details(self):
        return "Product Service is unavailable."


@pytest.fixture
def order_test_environment(monkeypatch):
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

    servicer = OrderServiceServicer(
        token_verifier=verifier,
        product_client=FakeProductClient(),
    )
    yield session_factory, servicer, token_for
    servicer.close()
    Base.metadata.drop_all(engine)
    engine.dispose()


def _metadata(token, *extra):
    return (("authorization", f"Bearer {token}"), *extra)


def test_create_order_uses_verified_subject_not_identity_metadata(
    order_test_environment,
):
    session_factory, servicer, token_for = order_test_environment
    context = FakeContext(_metadata(token_for(101), ("x-user-id", "202")))
    request = order_pb2.CreateOrderRequest(
        items=[order_pb2.OrderItemInput(product_id=7, quantity=2)]
    )

    response = servicer.CreateOrder(request, context)

    with session_factory() as session:
        persisted = session.get(Order, response.order.id)
    assert response.order.status == "CONFIRMED"
    assert persisted is not None
    assert persisted.user_id == 101


def test_inventory_failure_restores_prior_updates_and_fails_order(
    order_test_environment,
):
    session_factory, servicer, token_for = order_test_environment

    class FailingProductClient(FakeProductClient):
        def __init__(self):
            self.stock = {7: 5, 8: 5}
            self.updates = []

        def update_stock(self, product_id: int, quantity_delta: int, /):
            self.updates.append((product_id, quantity_delta))
            if product_id == 8 and quantity_delta < 0:
                raise StockUpdateUnavailable()
            self.stock[product_id] += quantity_delta
            return self.stock[product_id]

    product_client = FailingProductClient()
    servicer.product_client = product_client
    request = order_pb2.CreateOrderRequest(
        items=[
            order_pb2.OrderItemInput(product_id=7, quantity=1),
            order_pb2.OrderItemInput(product_id=8, quantity=1),
        ]
    )

    with pytest.raises(RpcAbort) as error:
        servicer.CreateOrder(request, FakeContext(_metadata(token_for(101))))

    assert error.value.code == grpc.StatusCode.UNAVAILABLE
    assert product_client.updates == [(7, -1), (8, -1), (7, 1)]
    assert product_client.stock == {7: 5, 8: 5}
    with session_factory() as session:
        persisted = session.scalar(select(Order))
    assert persisted is not None
    assert persisted.status == "FAILED"


def test_order_owner_can_retrieve_order(order_test_environment):
    _, servicer, token_for = order_test_environment
    create_context = FakeContext(_metadata(token_for(101)))
    created = servicer.CreateOrder(
        order_pb2.CreateOrderRequest(
            items=[order_pb2.OrderItemInput(product_id=7, quantity=1)]
        ),
        create_context,
    ).order

    retrieved = servicer.GetOrder(
        order_pb2.GetOrderRequest(order_id=created.id),
        FakeContext(_metadata(token_for(101))),
    )

    assert retrieved.order.id == created.id


def test_other_user_cannot_retrieve_order(order_test_environment):
    _, servicer, token_for = order_test_environment
    created = servicer.CreateOrder(
        order_pb2.CreateOrderRequest(
            items=[order_pb2.OrderItemInput(product_id=7, quantity=1)]
        ),
        FakeContext(_metadata(token_for(101))),
    ).order

    with pytest.raises(RpcAbort) as error:
        servicer.GetOrder(
            order_pb2.GetOrderRequest(order_id=created.id),
            FakeContext(_metadata(token_for(202))),
        )

    assert error.value.code == grpc.StatusCode.NOT_FOUND
    assert str(created.id) not in error.value.details
    assert error.value.details == "Order was not found."


@pytest.mark.parametrize(
    "metadata",
    [
        (),
        (("x-user-id", "101"),),
        (("authorization", "Bearer invalid"),),
    ],
)
def test_missing_or_invalid_bearer_is_rejected(order_test_environment, metadata):
    _, servicer, _ = order_test_environment

    with pytest.raises(RpcAbort) as error:
        servicer.GetOrder(
            order_pb2.GetOrderRequest(order_id=1),
            FakeContext(metadata),
        )

    assert error.value.code == grpc.StatusCode.UNAUTHENTICATED
