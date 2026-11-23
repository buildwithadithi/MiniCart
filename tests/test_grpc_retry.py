import sys
from pathlib import Path

import grpc
import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ORDER_SERVICE_ROOT = PROJECT_ROOT / "order-service"
GATEWAY_ROOT = PROJECT_ROOT / "api-gateway"
for path in (str(PROJECT_ROOT), str(ORDER_SERVICE_ROOT), str(GATEWAY_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from generated import order_pb2, order_pb2_grpc, product_pb2
from generated.grpc_retry import (
    RetryPolicy,
    apply_retry_jitter,
    calculate_retry_delay,
    retry_grpc_call,
)


class FakeRpcError(grpc.RpcError):
    def __init__(self, code):
        self._code = code

    def code(self):
        return self._code

    def details(self):
        return "fake gRPC failure"


def test_first_attempt_succeeds_without_retry():
    calls = []

    def operation(*, timeout_seconds):
        calls.append(timeout_seconds)
        return "ok"

    assert retry_grpc_call(
        operation,
        rpc_name="test.Operation",
        timeout_seconds=1,
        policy=RetryPolicy(jitter_ratio=0),
    ) == "ok"
    assert len(calls) == 1


def test_unavailable_then_success():
    calls = []
    sleeps = []
    now = [0.0]

    def operation(*, timeout_seconds):
        calls.append(timeout_seconds)
        if len(calls) == 1:
            raise FakeRpcError(grpc.StatusCode.UNAVAILABLE)
        return "ok"

    def sleep(delay):
        sleeps.append(delay)
        now[0] += delay

    assert retry_grpc_call(
        operation,
        rpc_name="test.Operation",
        timeout_seconds=2,
        policy=RetryPolicy(jitter_ratio=0),
        sleep=sleep,
        clock=lambda: now[0],
    ) == "ok"
    assert len(calls) == 2
    assert sleeps == pytest.approx([0.2])


def test_multiple_transient_failures_then_success():
    calls = []

    def operation(*, timeout_seconds):
        calls.append(timeout_seconds)
        if len(calls) < 4:
            raise FakeRpcError(grpc.StatusCode.UNAVAILABLE)
        return "ok"

    assert retry_grpc_call(
        operation,
        rpc_name="test.Operation",
        timeout_seconds=5,
        policy=RetryPolicy(jitter_ratio=0),
        sleep=lambda _delay: None,
    ) == "ok"
    assert len(calls) == 4


def test_all_attempts_fail_propagates_final_error():
    final_error = FakeRpcError(grpc.StatusCode.UNAVAILABLE)
    calls = 0

    def operation(*, timeout_seconds):
        nonlocal calls
        calls += 1
        raise final_error

    with pytest.raises(FakeRpcError) as raised:
        retry_grpc_call(
            operation,
            rpc_name="test.Operation",
            timeout_seconds=5,
            policy=RetryPolicy(jitter_ratio=0),
            sleep=lambda _delay: None,
        )

    assert raised.value is final_error
    assert calls == 4


@pytest.mark.parametrize(
    "status",
    [
        grpc.StatusCode.INVALID_ARGUMENT,
        grpc.StatusCode.NOT_FOUND,
        grpc.StatusCode.FAILED_PRECONDITION,
        grpc.StatusCode.PERMISSION_DENIED,
        grpc.StatusCode.UNAUTHENTICATED,
        grpc.StatusCode.RESOURCE_EXHAUSTED,
        grpc.StatusCode.INTERNAL,
        grpc.StatusCode.CANCELLED,
    ],
)
def test_non_retryable_error_makes_no_retry(status):
    calls = 0

    def operation(*, timeout_seconds):
        nonlocal calls
        calls += 1
        raise FakeRpcError(status)

    with pytest.raises(FakeRpcError) as raised:
        retry_grpc_call(
            operation,
            rpc_name="test.Operation",
            timeout_seconds=1,
            policy=RetryPolicy(jitter_ratio=0),
            sleep=lambda _delay: pytest.fail("must not sleep"),
        )

    assert raised.value.code() == status
    assert calls == 1


def test_deadline_exceeded_without_remaining_time_does_not_retry():
    calls = 0

    def operation(*, timeout_seconds):
        nonlocal calls
        calls += 1
        raise FakeRpcError(grpc.StatusCode.DEADLINE_EXCEEDED)

    with pytest.raises(FakeRpcError):
        retry_grpc_call(
            operation,
            rpc_name="test.Operation",
            timeout_seconds=0.005,
            policy=RetryPolicy(initial_retry_delay_seconds=0.2, jitter_ratio=0),
            sleep=lambda _delay: pytest.fail("must not sleep"),
        )

    assert calls == 1


def test_overall_deadline_is_respected():
    now = [0.0]
    calls = 0

    def operation(*, timeout_seconds):
        nonlocal calls
        calls += 1
        assert timeout_seconds <= 0.31
        raise FakeRpcError(grpc.StatusCode.UNAVAILABLE)

    def sleep(delay):
        now[0] += delay

    with pytest.raises(FakeRpcError):
        retry_grpc_call(
            operation,
            rpc_name="test.Operation",
            timeout_seconds=0.3,
            policy=RetryPolicy(
                max_attempts=4,
                initial_retry_delay_seconds=0.2,
                max_retry_delay_seconds=0.2,
                jitter_ratio=0,
            ),
            sleep=sleep,
            clock=lambda: now[0],
        )

    assert calls == 2


def test_exponential_delay_calculation():
    policy = RetryPolicy(
        initial_retry_delay_seconds=0.2,
        multiplier=2,
        max_retry_delay_seconds=1.0,
        jitter_ratio=0,
    )
    assert calculate_retry_delay(1, policy) == pytest.approx(0.2)
    assert calculate_retry_delay(2, policy) == pytest.approx(0.4)
    assert calculate_retry_delay(3, policy) == pytest.approx(0.8)
    assert calculate_retry_delay(4, policy) == pytest.approx(1.0)


def test_jitter_stays_between_configured_bounds():
    assert apply_retry_jitter(
        0.4,
        0.25,
        uniform=lambda lower, upper: lower,
    ) == pytest.approx(0.3)
    assert apply_retry_jitter(
        0.4,
        0.25,
        uniform=lambda lower, upper: upper,
    ) == pytest.approx(0.5)


def test_cancellation_style_error_stops_retries():
    calls = 0

    def operation(*, timeout_seconds):
        nonlocal calls
        calls += 1
        raise FakeRpcError(grpc.StatusCode.CANCELLED)

    with pytest.raises(FakeRpcError):
        retry_grpc_call(
            operation,
            rpc_name="test.Operation",
            timeout_seconds=1,
            sleep=lambda _delay: pytest.fail("must not sleep"),
        )

    assert calls == 1


def test_get_product_client_retries_unavailable(monkeypatch):
    from clients.product_client import ProductClient
    from generated import product_pb2_grpc

    class FakeChannel:
        def close(self):
            pass

    class FakeStub:
        def __init__(self, channel):
            self.calls = 0

        def GetProduct(self, request, timeout):
            self.calls += 1
            if self.calls == 1:
                raise FakeRpcError(grpc.StatusCode.UNAVAILABLE)
            return product_pb2.GetProductResponse(
                product=product_pb2.Product(id=request.product_id)
            )

    monkeypatch.setenv("PRODUCT_SERVICE_TIMEOUT_SECONDS", "1")
    monkeypatch.setenv("GRPC_RETRY_INITIAL_DELAY_SECONDS", "0")
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

    stub = FakeStub(None)
    client = ProductClient(address="fake:50051")
    product = client.get_product(42)
    assert product.id == 42
    assert stub.calls == 2


def test_check_stock_client_retries_unavailable(monkeypatch):
    from clients.product_client import ProductClient
    from generated import product_pb2_grpc

    class FakeChannel:
        def close(self):
            pass

    class FakeStub:
        def __init__(self):
            self.calls = 0

        def CheckStock(self, request, timeout):
            self.calls += 1
            if self.calls == 1:
                raise FakeRpcError(grpc.StatusCode.UNAVAILABLE)
            return product_pb2.CheckStockResponse(
                product_exists=True,
                available_stock=7,
            )

    stub = FakeStub()
    monkeypatch.setenv("PRODUCT_SERVICE_TIMEOUT_SECONDS", "1")
    monkeypatch.setenv("GRPC_RETRY_INITIAL_DELAY_SECONDS", "0")
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
    stock = client.check_stock(42)
    assert stock.available_stock == 7
    assert stub.calls == 2


def test_gateway_get_order_retries_and_preserves_metadata(monkeypatch):
    from gateway.grpc_client import OrderServiceClient
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
                raise FakeRpcError(grpc.StatusCode.UNAVAILABLE)
            return order_pb2.GetOrderResponse()

    stub = FakeStub()
    monkeypatch.setenv("ORDER_SERVICE_TIMEOUT_SECONDS", "1")
    monkeypatch.setenv("GRPC_RETRY_INITIAL_DELAY_SECONDS", "0")
    monkeypatch.setenv("GRPC_RETRY_JITTER_RATIO", "0")
    monkeypatch.setattr(grpc_client.grpc, "insecure_channel", lambda address: FakeChannel())
    monkeypatch.setattr(
        order_pb2_grpc,
        "OrderServiceStub",
        lambda channel: stub,
    )

    client = OrderServiceClient()
    client.get_order(23, "verified.jwt.token")
    assert len(stub.calls) == 2
    assert stub.calls[0][0].order_id == 23
    assert stub.calls[0][2] == (("authorization", "Bearer verified.jwt.token"),)
