import sys
from concurrent import futures
from importlib import import_module
from pathlib import Path
from uuid import uuid4

import grpc
import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ORDER_ROOT = PROJECT_ROOT / "order-service"
for path in (str(PROJECT_ROOT), str(ORDER_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

for module_name in (
    "models",
    "database",
    "servicer",
    "clients.product_client",
):
    sys.modules.pop(module_name, None)

product_client_module = import_module("clients.product_client")
generated_product = import_module("generated.product_pb2")
generated_product_grpc = import_module("generated.product_pb2_grpc")


class CountingProductServicer(generated_product_grpc.ProductServiceServicer):
    def __init__(self):
        self.get_calls = 0
        self.stock_calls = 0
        self.update_calls = 0
        self.available_stock = 20

    def GetProduct(self, request, context):
        self.get_calls += 1
        return generated_product.GetProductResponse(
            product=generated_product.Product(
                id=request.product_id,
                name="circuit test",
                price="10.00",
            )
        )

    def CheckStock(self, request, context):
        self.stock_calls += 1
        return generated_product.CheckStockResponse(
            product_exists=True,
            available_stock=self.available_stock,
        )

    def UpdateStock(self, request, context):
        self.update_calls += 1
        self.available_stock += request.quantity_delta
        return generated_product.UpdateStockResponse(
            available_stock=self.available_stock
        )


def _start_product_server(address, servicer):
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    generated_product_grpc.add_ProductServiceServicer_to_server(
        servicer,
        server,
    )
    port = server.add_insecure_port(address)
    assert port != 0
    server.start()
    return server, port


def test_order_product_circuit_closed_open_half_open_closed(monkeypatch):
    monkeypatch.setenv("ORDER_PRODUCT_CB_FAILURE_THRESHOLD", "2")
    monkeypatch.setenv("ORDER_PRODUCT_CB_RECOVERY_TIMEOUT_SECONDS", "1")
    monkeypatch.setenv("ORDER_PRODUCT_CB_HALF_OPEN_MAX_CALLS", "1")
    monkeypatch.setenv("PRODUCT_SERVICE_TIMEOUT_SECONDS", "0.15")
    monkeypatch.setenv("GRPC_RETRY_MAX_ATTEMPTS", "2")
    monkeypatch.setenv("GRPC_RETRY_INITIAL_DELAY_SECONDS", "0")
    monkeypatch.setenv("GRPC_RETRY_MAX_DELAY_SECONDS", "0")
    monkeypatch.setenv("GRPC_RETRY_JITTER_RATIO", "0")

    servicer = CountingProductServicer()
    server, port = _start_product_server("127.0.0.1:0", servicer)
    address = f"127.0.0.1:{port}"
    client = product_client_module.ProductClient(address=address)
    fake_now = [0.0]
    client._circuit_breaker._clock = lambda: fake_now[0]

    try:
        product = client.get_product(42)
        assert product.id == 42
        assert servicer.get_calls == 1
        assert client._circuit_breaker.state.value == "CLOSED"

        server.stop(grace=0).wait()

        failures = 0
        for operation in (
            lambda: client.get_product(43),
            lambda: client.check_stock(43),
        ):
            try:
                operation()
            except grpc.RpcError as error:
                assert error.code() in (
                    grpc.StatusCode.UNAVAILABLE,
                    grpc.StatusCode.DEADLINE_EXCEEDED,
                )
                failures += 1

        assert failures == 2
        assert client._circuit_breaker.state.value == "OPEN"

        before_rejection = servicer.get_calls + servicer.stock_calls
        with pytest.raises(grpc.RpcError) as rejected:
            client.check_stock(44)
        assert rejected.value.code() == grpc.StatusCode.UNAVAILABLE
        assert servicer.get_calls + servicer.stock_calls == before_rejection

        fake_now[0] = 1
        servicer = CountingProductServicer()
        server, port = _start_product_server(address, servicer)
        grpc.channel_ready_future(client._channel).result(timeout=2)

        stock = client.check_stock(44)
        assert stock.available_stock == 20
        assert client._circuit_breaker.state.value == "CLOSED"

        operation_id = f"stock-cb-recovery-{uuid4().hex}"
        remaining = client.update_stock(
            44,
            -3,
            operation_id=operation_id,
        )
        assert remaining == 17
        assert servicer.update_calls == 1
    finally:
        client.close()
        server.stop(grace=0).wait()

