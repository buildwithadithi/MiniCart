from generated import order_pb2, order_pb2_grpc
from gateway import grpc_client


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
