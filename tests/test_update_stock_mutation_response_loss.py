import sys
from concurrent import futures
from importlib import import_module
from pathlib import Path
from uuid import uuid4

import grpc
import pytest
from dotenv import load_dotenv
from sqlalchemy import select


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRODUCT_ROOT = PROJECT_ROOT / "product-service"
ORDER_ROOT = PROJECT_ROOT / "order-service"
for path in (str(PROJECT_ROOT), str(ORDER_ROOT), str(PRODUCT_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

load_dotenv(PROJECT_ROOT / ".env")

for module_name in ("models", "database", "servicer", "clients.product_client"):
    sys.modules.pop(module_name, None)

database_module = import_module("database")
models_module = import_module("models")
servicer_module = import_module("servicer")
product_client_module = import_module("clients.product_client")
generated_product = import_module("generated.product_pb2")
generated_product_grpc = import_module("generated.product_pb2_grpc")

SessionLocal = getattr(database_module, "SessionLocal")
Inventory = getattr(models_module, "Inventory")
ProcessedStockOperation = getattr(models_module, "ProcessedStockOperation")
Product = getattr(models_module, "Product")
ProductServiceServicer = getattr(servicer_module, "ProductServiceServicer")
ProductClient = getattr(product_client_module, "ProductClient")


class LostResponseProductService(ProductServiceServicer):
    def __init__(self):
        super().__init__()
        self.update_calls = 0

    def UpdateStock(self, request, context):
        response = super().UpdateStock(request, context)
        self.update_calls += 1
        if self.update_calls == 1:
            context.abort(
                grpc.StatusCode.UNAVAILABLE,
                "Test-only response loss after committed UpdateStock.",
            )
        return response


def test_update_stock_response_lost_after_commit_retries_once(monkeypatch):
    monkeypatch.setenv("PRODUCT_SERVICE_TIMEOUT_SECONDS", "5")
    monkeypatch.setenv("GRPC_RETRY_INITIAL_DELAY_SECONDS", "0")
    monkeypatch.setenv("GRPC_RETRY_MAX_DELAY_SECONDS", "0")
    monkeypatch.setenv("GRPC_RETRY_JITTER_RATIO", "0")

    with SessionLocal.begin() as session:
        product = Product(
            name=f"Retry response loss {uuid4().hex}",
            description="Test-only response-loss integration",
            price="10.00",
        )
        session.add(product)
        session.flush()
        session.add(Inventory(product_id=product.id, available_stock=10))
        product_id = product.id

    servicer = LostResponseProductService()
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    generated_product_grpc.add_ProductServiceServicer_to_server(servicer, server)
    port = server.add_insecure_port("127.0.0.1:0")
    if port == 0:
        pytest.fail("Could not bind the test-only Product gRPC server.")
    server.start()

    address = f"127.0.0.1:{port}"
    ready_channel = grpc.insecure_channel(address)
    grpc.channel_ready_future(ready_channel).result(timeout=5)
    ready_channel.close()
    client = ProductClient(address=address)
    operation_id = f"stock-response-loss-{uuid4().hex}"

    try:
        response = client.update_stock(
            product_id,
            -3,
            operation_id=operation_id,
        )

        assert servicer.update_calls == 2
        assert response == 7

        with SessionLocal() as session:
            inventory = session.get(Inventory, product_id)
            operations = session.scalars(
                select(ProcessedStockOperation).where(
                    ProcessedStockOperation.operation_id == operation_id
                )
            ).all()

        assert inventory is not None
        assert inventory.available_stock == 7
        assert len(operations) == 1
        assert operations[0].available_stock == 7
    finally:
        client.close()
        server.stop(grace=0).wait()
        with SessionLocal.begin() as session:
            session.execute(
                ProcessedStockOperation.__table__.delete().where(
                    ProcessedStockOperation.product_id == product_id
                )
            )
            session.execute(
                Inventory.__table__.delete().where(
                    Inventory.product_id == product_id
                )
            )
            session.execute(
                Product.__table__.delete().where(Product.id == product_id)
            )
