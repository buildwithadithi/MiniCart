import sys
import time
from concurrent import futures
from pathlib import Path

import grpc
import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ORDER_SERVICE_ROOT = PROJECT_ROOT / "order-service"
GATEWAY_ROOT = PROJECT_ROOT / "api-gateway"
for path in (str(PROJECT_ROOT), str(ORDER_SERVICE_ROOT), str(GATEWAY_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from clients.product_client import ProductClient
from gateway.grpc_client import OrderServiceClient
from generated import (
    order_pb2,
    order_pb2_grpc,
    product_pb2,
    product_pb2_grpc,
)
from generated.grpc_retry import (
    RetryPolicy,
    apply_retry_jitter,
    calculate_retry_delay,
    retry_grpc_call,
)


class FakeRpcError(grpc.RpcError):
    def __init__(self, code, details="test gRPC failure"):
        self._code = code
        self._details = details

    def code(self):
        return self._code

    def details(self):
        return self._details


def _policy(**overrides):
    values = {
        "max_attempts": 4,
        "initial_retry_delay_seconds": 0.2,
        "multiplier": 2,
        "max_retry_delay_seconds": 1.0,
        "jitter_ratio": 0.25,
    }
    values.update(overrides)
    return RetryPolicy(**values)


@pytest.mark.parametrize("status", [
    grpc.StatusCode.UNAVAILABLE,
    grpc.StatusCode.DEADLINE_EXCEEDED,
])
@pytest.mark.parametrize("operation_name", ["GetProduct", "CheckStock"])
def test_product_read_rpc_transient_failure_then_success(
    monkeypatch,
    operation_name,
    status,
):
    class FakeChannel:
        def close(self):
            pass

    class FakeStub:
        def __init__(self):
            self.calls = 0

        def GetProduct(self, request, timeout):
            self.calls += 1
            if self.calls == 1:
                raise FakeRpcError(status)
            return product_pb2.GetProductResponse(
                product=product_pb2.Product(id=request.product_id, name="retry product")
            )

        def CheckStock(self, request, timeout):
            self.calls += 1
            if self.calls == 1:
                raise FakeRpcError(status)
            return product_pb2.CheckStockResponse(
                product_exists=True,
                available_stock=9,
            )

    stub = FakeStub()
    monkeypatch.setenv("PRODUCT_SERVICE_TIMEOUT_SECONDS", "1")
    monkeypatch.setenv("GRPC_RETRY_INITIAL_DELAY_SECONDS", "0")
    monkeypatch.setenv("GRPC_RETRY_MAX_DELAY_SECONDS", "0")
    monkeypatch.setenv("GRPC_RETRY_JITTER_RATIO", "0")
    monkeypatch.setattr(
        product_pb2_grpc,
        "ProductServiceStub",
        lambda channel: stub,
    )
    monkeypatch.setattr(
        __import__("clients.product_client", fromlist=["grpc"]).grpc,
        "insecure_channel",
        lambda address: FakeChannel(),
    )

    client = ProductClient(address="fake:50051")
    if operation_name == "GetProduct":
        response = client.get_product(42)
        assert response.id == 42
    else:
        response = client.check_stock(42)
        assert response.available_stock == 9
    assert stub.calls == 2


@pytest.mark.parametrize("operation_name", ["GetProduct", "CheckStock"])
def test_product_read_rpc_all_unavailable_uses_exactly_four_attempts(
    monkeypatch,
    operation_name,
):
    class FakeChannel:
        def close(self):
            pass

    class FakeStub:
        def __init__(self):
            self.calls = 0

        def GetProduct(self, request, timeout):
            self.calls += 1
            raise FakeRpcError(grpc.StatusCode.UNAVAILABLE)

        def CheckStock(self, request, timeout):
            self.calls += 1
            raise FakeRpcError(grpc.StatusCode.UNAVAILABLE)

    stub = FakeStub()
    monkeypatch.setenv("PRODUCT_SERVICE_TIMEOUT_SECONDS", "5")
    monkeypatch.setenv("GRPC_RETRY_INITIAL_DELAY_SECONDS", "0")
    monkeypatch.setenv("GRPC_RETRY_MAX_DELAY_SECONDS", "0")
    monkeypatch.setenv("GRPC_RETRY_JITTER_RATIO", "0")
    monkeypatch.setattr(
        product_pb2_grpc,
        "ProductServiceStub",
        lambda channel: stub,
    )
    monkeypatch.setattr(
        __import__("clients.product_client", fromlist=["grpc"]).grpc,
        "insecure_channel",
        lambda address: FakeChannel(),
    )

    client = ProductClient(address="fake:50051")
    with pytest.raises(FakeRpcError) as raised:
        if operation_name == "GetProduct":
            client.get_product(42)
        else:
            client.check_stock(42)

    assert raised.value.code() == grpc.StatusCode.UNAVAILABLE
    assert stub.calls == 4


@pytest.mark.parametrize("status", [
    grpc.StatusCode.UNAVAILABLE,
    grpc.StatusCode.DEADLINE_EXCEEDED,
])
def test_gateway_get_order_transient_failure_then_success(monkeypatch, status):
    from gateway import grpc_client

    class FakeChannel:
        def close(self):
            pass

    class FakeStub:
        def __init__(self):
            self.calls = []

        def GetOrder(self, request, timeout, metadata):
            self.calls.append((request, timeout, metadata))
            if len(self.calls) == 1:
                raise FakeRpcError(status)
            return order_pb2.GetOrderResponse(
                order=order_pb2.Order(id=request.order_id, status="CONFIRMED")
            )

    stub = FakeStub()
    monkeypatch.setenv("ORDER_SERVICE_TIMEOUT_SECONDS", "1")
    monkeypatch.setenv("GRPC_RETRY_INITIAL_DELAY_SECONDS", "0")
    monkeypatch.setenv("GRPC_RETRY_MAX_DELAY_SECONDS", "0")
    monkeypatch.setenv("GRPC_RETRY_JITTER_RATIO", "0")
    monkeypatch.setattr(
        grpc_client.grpc,
        "insecure_channel",
        lambda address: FakeChannel(),
    )
    monkeypatch.setattr(
        order_pb2_grpc,
        "OrderServiceStub",
        lambda channel: stub,
    )

    client = OrderServiceClient()
    response = client.get_order(23, "verified.jwt.token")
    assert response.order.id == 23
    assert len(stub.calls) == 2
    assert stub.calls[0][2] == (("authorization", "Bearer verified.jwt.token"),)


@pytest.mark.parametrize("status", [
    grpc.StatusCode.UNAVAILABLE,
    grpc.StatusCode.DEADLINE_EXCEEDED,
])
def test_gateway_get_order_all_attempts_fail_final_error(monkeypatch, status):
    from gateway import grpc_client

    class FakeChannel:
        def close(self):
            pass

    class FakeStub:
        def __init__(self):
            self.calls = 0

        def GetOrder(self, request, timeout, metadata):
            self.calls += 1
            raise FakeRpcError(status)

    stub = FakeStub()
    monkeypatch.setenv("ORDER_SERVICE_TIMEOUT_SECONDS", "5")
    monkeypatch.setenv("GRPC_RETRY_INITIAL_DELAY_SECONDS", "0")
    monkeypatch.setenv("GRPC_RETRY_MAX_DELAY_SECONDS", "0")
    monkeypatch.setenv("GRPC_RETRY_JITTER_RATIO", "0")
    monkeypatch.setattr(
        grpc_client.grpc,
        "insecure_channel",
        lambda address: FakeChannel(),
    )
    monkeypatch.setattr(
        order_pb2_grpc,
        "OrderServiceStub",
        lambda channel: stub,
    )

    client = OrderServiceClient()
    with pytest.raises(FakeRpcError) as raised:
        client.get_order(23, "verified.jwt.token")

    assert raised.value.code() == status
    assert stub.calls == 4


def test_retry_exhaustion_attempt_count_is_exactly_four_and_no_fifth():
    calls = 0
    sleeps = []

    def operation(*, timeout_seconds):
        nonlocal calls
        calls += 1
        raise FakeRpcError(grpc.StatusCode.UNAVAILABLE)

    with pytest.raises(FakeRpcError):
        retry_grpc_call(
            operation,
            rpc_name="test.Operation",
            timeout_seconds=10,
            policy=_policy(jitter_ratio=0),
            sleep=sleeps.append,
        )

    assert calls == 4
    assert len(sleeps) == 3


def test_retry_delays_match_exponential_backoff_with_jitter():
    now = [0.0]
    calls = 0
    sleeps = []

    def operation(*, timeout_seconds):
        nonlocal calls
        calls += 1
        if calls == 4:
            return "success"
        raise FakeRpcError(grpc.StatusCode.UNAVAILABLE)

    def fake_sleep(delay):
        sleeps.append(delay)
        now[0] += delay

    assert retry_grpc_call(
        operation,
        rpc_name="test.Operation",
        timeout_seconds=10,
        policy=_policy(jitter_ratio=0),
        sleep=fake_sleep,
        clock=lambda: now[0],
    ) == "success"

    assert sleeps == pytest.approx([0.2, 0.4, 0.8])


def test_jitter_is_bounded_and_does_not_change_exponential_caps():
    policy = _policy(jitter_ratio=0.25)
    for retry_number, base in ((1, 0.2), (2, 0.4), (3, 0.8), (4, 1.0)):
        calculated = calculate_retry_delay(retry_number, policy)
        jittered = apply_retry_jitter(calculated, policy.jitter_ratio)
        assert jittered >= calculated * 0.75
        assert jittered <= calculated * 1.25
        assert calculated == pytest.approx(base)


def test_deadline_expires_before_next_attempt_no_sleep_or_attempt():
    now = [0.0]
    calls = 0
    sleeps = []

    def operation(*, timeout_seconds):
        nonlocal calls
        calls += 1
        now[0] = 1.0
        raise FakeRpcError(grpc.StatusCode.DEADLINE_EXCEEDED)

    with pytest.raises(FakeRpcError) as raised:
        retry_grpc_call(
            operation,
            rpc_name="test.Operation",
            timeout_seconds=1.0,
            policy=_policy(initial_retry_delay_seconds=0.2, jitter_ratio=0),
            sleep=sleeps.append,
            clock=lambda: now[0],
        )

    assert raised.value.code() == grpc.StatusCode.DEADLINE_EXCEEDED
    assert calls == 1
    assert sleeps == []


def test_remaining_deadline_cannot_accommodate_backoff_and_attempt():
    now = [0.0]
    calls = 0
    sleeps = []

    def operation(*, timeout_seconds):
        nonlocal calls
        calls += 1
        now[0] = 0.5
        raise FakeRpcError(grpc.StatusCode.UNAVAILABLE)

    def fake_sleep(delay):
        sleeps.append(delay)
        now[0] += delay

    with pytest.raises(FakeRpcError) as raised:
        retry_grpc_call(
            operation,
            rpc_name="test.Operation",
            timeout_seconds=0.7,
            policy=_policy(initial_retry_delay_seconds=0.2, jitter_ratio=0),
            sleep=fake_sleep,
            clock=lambda: now[0],
        )

    assert raised.value.code() == grpc.StatusCode.UNAVAILABLE
    assert calls == 1
    assert sleeps == []


class _TransientProductService(product_pb2_grpc.ProductServiceServicer):
    def __init__(self, status):
        self.status = status
        self.calls = 0

    def GetProduct(self, request, context):
        self.calls += 1
        if self.calls == 1:
            context.abort(self.status, "controlled transient failure")
        return product_pb2.GetProductResponse(
            product=product_pb2.Product(id=request.product_id, name="network result")
        )

    def CheckStock(self, request, context):
        self.calls += 1
        if self.calls == 1:
            context.abort(self.status, "controlled transient failure")
        return product_pb2.CheckStockResponse(
            product_exists=True,
            available_stock=6,
        )


@pytest.mark.parametrize("rpc_name", ["GetProduct", "CheckStock"])
def test_real_in_process_grpc_channel_retries_product_read(monkeypatch, rpc_name):
    monkeypatch.setenv("PRODUCT_SERVICE_TIMEOUT_SECONDS", "2")
    monkeypatch.setenv("GRPC_RETRY_INITIAL_DELAY_SECONDS", "0")
    monkeypatch.setenv("GRPC_RETRY_MAX_DELAY_SECONDS", "0")
    monkeypatch.setenv("GRPC_RETRY_JITTER_RATIO", "0")

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
    servicer = _TransientProductService(grpc.StatusCode.UNAVAILABLE)
    product_pb2_grpc.add_ProductServiceServicer_to_server(servicer, server)
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()

    try:
        client = ProductClient(address=f"127.0.0.1:{port}")
        if rpc_name == "GetProduct":
            response = client.get_product(55)
            assert response.id == 55
        else:
            response = client.check_stock(55)
            assert response.available_stock == 6
        assert servicer.calls == 2
        client.close()
    finally:
        server.stop(grace=0).wait()


@pytest.mark.parametrize("status", [
    grpc.StatusCode.UNAVAILABLE,
    grpc.StatusCode.DEADLINE_EXCEEDED,
])
def test_update_stock_mutation_retries_same_request(monkeypatch, status):
    class FakeChannel:
        def close(self):
            pass

    class FakeStub:
        def __init__(self):
            self.calls = []

        def UpdateStock(self, request, timeout):
            self.calls.append((request, timeout))
            if len(self.calls) == 1:
                raise FakeRpcError(status)
            return product_pb2.UpdateStockResponse(available_stock=7)

    stub = FakeStub()
    monkeypatch.setenv("PRODUCT_SERVICE_TIMEOUT_SECONDS", "2")
    monkeypatch.setenv("GRPC_RETRY_INITIAL_DELAY_SECONDS", "0")
    monkeypatch.setenv("GRPC_RETRY_MAX_DELAY_SECONDS", "0")
    monkeypatch.setenv("GRPC_RETRY_JITTER_RATIO", "0")
    monkeypatch.setattr(
        product_pb2_grpc,
        "ProductServiceStub",
        lambda channel: stub,
    )
    monkeypatch.setattr(
        __import__("clients.product_client", fromlist=["grpc"]).grpc,
        "insecure_channel",
        lambda address: FakeChannel(),
    )

    client = ProductClient(address="fake:50051")
    operation_id = "stock-op-101"
    response = client.update_stock(42, -3, operation_id=operation_id)

    assert response == 7
    assert len(stub.calls) == 2
    for request, _timeout in stub.calls:
        assert request.product_id == 42
        assert request.quantity_delta == -3
        assert request.operation_id == operation_id


def test_update_stock_retry_exhaustion_uses_four_attempts(monkeypatch):
    class FakeChannel:
        def close(self):
            pass

    class FakeStub:
        def __init__(self):
            self.calls = []

        def UpdateStock(self, request, timeout):
            self.calls.append((request, timeout))
            raise FakeRpcError(grpc.StatusCode.UNAVAILABLE)

    stub = FakeStub()
    monkeypatch.setenv("PRODUCT_SERVICE_TIMEOUT_SECONDS", "5")
    monkeypatch.setenv("GRPC_RETRY_INITIAL_DELAY_SECONDS", "0")
    monkeypatch.setenv("GRPC_RETRY_MAX_DELAY_SECONDS", "0")
    monkeypatch.setenv("GRPC_RETRY_JITTER_RATIO", "0")
    monkeypatch.setattr(
        product_pb2_grpc,
        "ProductServiceStub",
        lambda channel: stub,
    )
    monkeypatch.setattr(
        __import__("clients.product_client", fromlist=["grpc"]).grpc,
        "insecure_channel",
        lambda address: FakeChannel(),
    )

    client = ProductClient(address="fake:50051")
    operation_id = "stock-op-exhaustion"
    with pytest.raises(FakeRpcError) as raised:
        client.update_stock(42, -3, operation_id=operation_id)

    assert raised.value.code() == grpc.StatusCode.UNAVAILABLE
    assert len(stub.calls) == 4
    assert {request.operation_id for request, _ in stub.calls} == {operation_id}
    assert {request.product_id for request, _ in stub.calls} == {42}
    assert {request.quantity_delta for request, _ in stub.calls} == {-3}


def test_update_stock_non_retryable_error_stops_after_one_attempt(monkeypatch):
    class FakeChannel:
        def close(self):
            pass

    class FakeStub:
        def __init__(self):
            self.calls = 0

        def UpdateStock(self, request, timeout):
            self.calls += 1
            raise FakeRpcError(grpc.StatusCode.INVALID_ARGUMENT)

    stub = FakeStub()
    monkeypatch.setenv("PRODUCT_SERVICE_TIMEOUT_SECONDS", "2")
    monkeypatch.setenv("GRPC_RETRY_INITIAL_DELAY_SECONDS", "0")
    monkeypatch.setenv("GRPC_RETRY_MAX_DELAY_SECONDS", "0")
    monkeypatch.setenv("GRPC_RETRY_JITTER_RATIO", "0")
    monkeypatch.setattr(
        product_pb2_grpc,
        "ProductServiceStub",
        lambda channel: stub,
    )
    monkeypatch.setattr(
        __import__("clients.product_client", fromlist=["grpc"]).grpc,
        "insecure_channel",
        lambda address: FakeChannel(),
    )

    client = ProductClient(address="fake:50051")
    with pytest.raises(FakeRpcError) as raised:
        client.update_stock(42, -3, operation_id="stock-op-permanent")

    assert raised.value.code() == grpc.StatusCode.INVALID_ARGUMENT
    assert stub.calls == 1


def test_compensation_update_retry_reuses_stable_operation_id(monkeypatch):
    class FakeChannel:
        def close(self):
            pass

    class FakeStub:
        def __init__(self):
            self.calls = []

        def UpdateStock(self, request, timeout):
            self.calls.append((request, timeout))
            if len(self.calls) == 1:
                raise FakeRpcError(grpc.StatusCode.UNAVAILABLE)
            return product_pb2.UpdateStockResponse(available_stock=6)

    stub = FakeStub()
    monkeypatch.setenv("PRODUCT_SERVICE_TIMEOUT_SECONDS", "2")
    monkeypatch.setenv("GRPC_RETRY_INITIAL_DELAY_SECONDS", "0")
    monkeypatch.setenv("GRPC_RETRY_MAX_DELAY_SECONDS", "0")
    monkeypatch.setenv("GRPC_RETRY_JITTER_RATIO", "0")
    monkeypatch.setattr(
        product_pb2_grpc,
        "ProductServiceStub",
        lambda channel: stub,
    )
    monkeypatch.setattr(
        __import__("clients.product_client", fromlist=["grpc"]).grpc,
        "insecure_channel",
        lambda address: FakeChannel(),
    )

    client = ProductClient(address="fake:50051")
    operation_id = "compensation-op-101"
    response = client.update_stock(42, 1, operation_id=operation_id)

    assert response == 6
    assert len(stub.calls) == 2
    assert {request.operation_id for request, _ in stub.calls} == {operation_id}
    assert {request.quantity_delta for request, _ in stub.calls} == {1}

