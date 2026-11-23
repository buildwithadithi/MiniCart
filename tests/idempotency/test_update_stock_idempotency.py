import sys
from importlib import import_module
from pathlib import Path

import grpc
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PRODUCT_ROOT = PROJECT_ROOT / "product-service"
for path in (str(PRODUCT_ROOT), str(PROJECT_ROOT)):
    if path in sys.path:
        sys.path.remove(path)
    sys.path.insert(0, path)

for module_name in ("models", "database", "servicer"):
    sys.modules.pop(module_name, None)

from database import Base
from generated import product_pb2

product_servicer_module = import_module("servicer")
product_models = import_module("models")
Inventory = getattr(product_models, "Inventory")
ProcessedStockOperation = getattr(product_models, "ProcessedStockOperation")
Product = getattr(product_models, "Product")
ProductServiceServicer = getattr(product_servicer_module, "ProductServiceServicer")


class RpcAbort(Exception):
    def __init__(self, code, details):
        self.code = code
        self.details = details


class FakeContext:
    def abort(self, code, details):
        raise RpcAbort(code, details)


@pytest.fixture
def product_environment(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(product_servicer_module, "SessionLocal", session_factory)

    with session_factory.begin() as session:
        product = Product(name="idempotency test", description="", price="9.99")
        session.add(product)
        session.flush()
        session.add(Inventory(product_id=product.id, available_stock=10))
        product_id = product.id

    servicer = ProductServiceServicer()
    yield session_factory, servicer, product_id
    Base.metadata.drop_all(engine)
    engine.dispose()


def test_new_operation_modifies_stock_once(product_environment):
    session_factory, servicer, product_id = product_environment
    response = servicer.UpdateStock(
        product_pb2.UpdateStockRequest(
            product_id=product_id,
            quantity_delta=-3,
            operation_id="stock-op-1",
        ),
        FakeContext(),
    )

    repeated = servicer.UpdateStock(
        product_pb2.UpdateStockRequest(
            product_id=product_id,
            quantity_delta=-3,
            operation_id="stock-op-1",
        ),
        FakeContext(),
    )

    assert response.available_stock == 7
    assert repeated.available_stock == 7
    with session_factory() as session:
        inventory = session.get(Inventory, product_id)
        operations = session.scalars(select(ProcessedStockOperation)).all()
        assert inventory.available_stock == 7
        assert len(operations) == 1


def test_different_operation_ids_apply_separate_changes(product_environment):
    _, servicer, product_id = product_environment
    first = servicer.UpdateStock(
        product_pb2.UpdateStockRequest(
            product_id=product_id,
            quantity_delta=-2,
            operation_id="stock-op-a",
        ),
        FakeContext(),
    )
    second = servicer.UpdateStock(
        product_pb2.UpdateStockRequest(
            product_id=product_id,
            quantity_delta=-1,
            operation_id="stock-op-b",
        ),
        FakeContext(),
    )
    assert (first.available_stock, second.available_stock) == (8, 7)


def test_reused_operation_id_with_different_payload_is_rejected(product_environment):
    _, servicer, product_id = product_environment
    servicer.UpdateStock(
        product_pb2.UpdateStockRequest(
            product_id=product_id,
            quantity_delta=-2,
            operation_id="stock-op-conflict",
        ),
        FakeContext(),
    )
    with pytest.raises(RpcAbort) as raised:
        servicer.UpdateStock(
            product_pb2.UpdateStockRequest(
                product_id=product_id,
                quantity_delta=-1,
                operation_id="stock-op-conflict",
            ),
            FakeContext(),
        )
    assert raised.value.code == grpc.StatusCode.ALREADY_EXISTS
