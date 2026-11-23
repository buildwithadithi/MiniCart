import grpc
import pytest

from generated import order_pb2, order_pb2_grpc
from gateway import grpc_client


class FakeRpcError(grpc.RpcError):
    def __init__(self, code):
        self._code = code

    def code(self):
        return self._code

    def details(self):
        return "controlled retry failure"


class FakeChannel:
    def close(self):
        pass


class FakeOrderStub:
    def __init__(self, channel):
        self.channel = channel
        self.calls = []

    def CreateOrder(self, request, *, timeout, metadata):
        self.calls.append(("create", request, timeout, metadata))
        return order_pb2.CreateOrderResponse()

    def GetOrder(self, request, *, timeout, metadata):
        self.calls.append(("get", request, timeout, metadata))
        return order_pb2.GetOrderResponse()


def test_order_client_sends_bearer_token_as_grpc_authorization_metadata(monkeypatch):
    stub = None

    def create_channel(address):
        assert address == "localhost:50052"
        return FakeChannel()

    class StubFactory(FakeOrderStub):
        def __init__(self, channel):
            super().__init__(channel)
            nonlocal stub
            stub = self

    monkeypatch.setattr(grpc_client.grpc, "insecure_channel", create_channel)
    monkeypatch.setattr(
        order_pb2_grpc,
        "OrderServiceStub",
        StubFactory,
    )
    client = grpc_client.OrderServiceClient()
    token = "verified.jwt.token"

    client.create_order(order_pb2.CreateOrderRequest(), token)
    client.get_order(23, token)
    client.close()

    assert stub is not None
    assert stub.calls[0][0] == "create"
    assert stub.calls[0][3] == (("authorization", f"Bearer {token}"),)
    assert stub.calls[1][0] == "get"
    assert stub.calls[1][3] == (("authorization", f"Bearer {token}"),)


@pytest.mark.parametrize(
    "status",
    [grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED],
)
def test_create_order_retries_with_same_request_key_and_metadata(
    monkeypatch,
    status,
):
    class RetryStub:
        def __init__(self):
            self.calls = []

        def CreateOrder(self, request, *, timeout, metadata):
            self.calls.append((request, timeout, metadata))
            if len(self.calls) == 1:
                raise FakeRpcError(status)
            return order_pb2.CreateOrderResponse(
                order=order_pb2.Order(id=42)
            )

        def close(self):
            pass

    stub = RetryStub()
    monkeypatch.setenv("ORDER_SERVICE_TIMEOUT_SECONDS", "2")
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

    client = grpc_client.OrderServiceClient()
    request = order_pb2.CreateOrderRequest(
        items=[order_pb2.OrderItemInput(product_id=7, quantity=2)]
    )
    response = client.create_order(
        request,
        "verified.jwt.token",
        "create-order-101",
    )

    assert response.order.id == 42
    assert len(stub.calls) == 2
    for attempted_request, _timeout, metadata in stub.calls:
        assert attempted_request == request
        assert metadata == (
            ("authorization", "Bearer verified.jwt.token"),
            ("idempotency-key", "create-order-101"),
        )
    client.close()


def test_create_order_exhaustion_uses_four_attempts(monkeypatch):
    class RetryStub:
        def __init__(self):
            self.calls = []

        def CreateOrder(self, request, *, timeout, metadata):
            self.calls.append((request, timeout, metadata))
            raise FakeRpcError(grpc.StatusCode.UNAVAILABLE)

        def close(self):
            pass

    stub = RetryStub()
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

    client = grpc_client.OrderServiceClient()
    request = order_pb2.CreateOrderRequest()
    with pytest.raises(FakeRpcError) as raised:
        client.create_order(request, "verified.jwt.token", "create-order-exhausted")

    assert raised.value.code() == grpc.StatusCode.UNAVAILABLE
    assert len(stub.calls) == 4
    assert len({metadata[1] for _request, _timeout, metadata in stub.calls}) == 1
    client.close()


def test_create_order_permanent_error_is_not_retried(monkeypatch):
    class PermanentStub:
        def __init__(self):
            self.calls = 0

        def CreateOrder(self, request, *, timeout, metadata):
            self.calls += 1
            raise FakeRpcError(grpc.StatusCode.ALREADY_EXISTS)

        def close(self):
            pass

    stub = PermanentStub()
    monkeypatch.setenv("ORDER_SERVICE_TIMEOUT_SECONDS", "2")
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

    client = grpc_client.OrderServiceClient()
    with pytest.raises(FakeRpcError) as raised:
        client.create_order(
            order_pb2.CreateOrderRequest(),
            "verified.jwt.token",
            "create-order-permanent",
        )

    assert raised.value.code() == grpc.StatusCode.ALREADY_EXISTS
    assert stub.calls == 1
    client.close()
