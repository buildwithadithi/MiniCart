import sys
import time
from concurrent import futures
from pathlib import Path
import grpc
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
PROJECT_ROOT = Path(__file__).resolve().parents[2]
GATEWAY_ROOT = PROJECT_ROOT / "api-gateway"
for path in (str(PROJECT_ROOT), str(GATEWAY_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from fastapi.testclient import TestClient
from generated import order_pb2, order_pb2_grpc
from gateway.grpc_client import OrderServiceClient
from gateway.main import create_app
from gateway.security import JwtVerifier


class CountingOrderServicer(order_pb2_grpc.OrderServiceServicer):
    def __init__(self):
        self.get_calls = 0

    def GetOrder(self, request, context):
        self.get_calls += 1
        return order_pb2.GetOrderResponse(
            order=order_pb2.Order(
                id=request.order_id,
                total_amount="12.50",
                status="CONFIRMED",
            )
        )

    def CreateOrder(self, request, context):
        return order_pb2.CreateOrderResponse(
            order=order_pb2.Order(
                id=9001,
                total_amount="12.50",
                status="CONFIRMED",
            )
        )


class CountingOrderStub:
    def __init__(self, stub):
        self.stub = stub
        self.calls = 0

    def GetOrder(self, request, timeout, metadata):
        self.calls += 1
        return self.stub.GetOrder(
            request,
            timeout=timeout,
            metadata=metadata,
        )


class FakeAuthClient:
    def register(self, request):
        raise NotImplementedError

    def login(self, request):
        raise NotImplementedError

    def close(self):
        pass


def _start_order_server(address, servicer):
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    order_pb2_grpc.add_OrderServiceServicer_to_server(servicer, server)
    bound = server.add_insecure_port(address)
    assert bound != 0
    server.start()
    return server


def test_gateway_order_circuit_closed_open_half_open_closed(monkeypatch):
    monkeypatch.setenv("GATEWAY_ORDER_CB_FAILURE_THRESHOLD", "2")
    monkeypatch.setenv("GATEWAY_ORDER_CB_RECOVERY_TIMEOUT_SECONDS", "1")
    monkeypatch.setenv("GATEWAY_ORDER_CB_HALF_OPEN_MAX_CALLS", "1")
    monkeypatch.setenv("ORDER_SERVICE_TIMEOUT_SECONDS", "0.15")
    monkeypatch.setenv("GRPC_RETRY_MAX_ATTEMPTS", "2")
    monkeypatch.setenv("GRPC_RETRY_INITIAL_DELAY_SECONDS", "0")
    monkeypatch.setenv("GRPC_RETRY_MAX_DELAY_SECONDS", "0")
    monkeypatch.setenv("GRPC_RETRY_JITTER_RATIO", "0")

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")
    verifier = JwtVerifier(public_pem, "minicart-auth", "minicart-api")
    now = int(time.time())
    token = jwt.encode(
        {
            "sub": "9001",
            "iss": "minicart-auth",
            "aud": "minicart-api",
            "iat": now,
            "exp": now + 300,
        },
        private_pem,
        algorithm="RS256",
    )

    address = "127.0.0.1:0"
    servicer = CountingOrderServicer()
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    order_pb2_grpc.add_OrderServiceServicer_to_server(servicer, server)
    port = server.add_insecure_port(address)
    assert port != 0
    server.start()
    real_address = f"127.0.0.1:{port}"

    client = OrderServiceClient()
    client.address = real_address
    client._channel.close()
    client._channel = grpc.insecure_channel(real_address)
    underlying_stub = order_pb2_grpc.OrderServiceStub(client._channel)
    counting_stub = CountingOrderStub(underlying_stub)
    setattr(client, "_stub", counting_stub)
    fake_now = [0.0]
    client._circuit_breaker._clock = lambda: fake_now[0]

    app = create_app(
        client,
        auth_client=FakeAuthClient(),
        token_verifier=verifier,
    )

    try:
        with TestClient(app) as gateway:
            gateway.headers["Authorization"] = f"Bearer {token}"

            healthy = gateway.get("/orders/42")
            assert healthy.status_code == 200
            assert servicer.get_calls == 1
            assert client._circuit_breaker.state.value == "CLOSED"

            server.stop(grace=0).wait()
            outage_responses = [
                gateway.get("/orders/43"),
                gateway.get("/orders/44"),
            ]
            assert [response.status_code for response in outage_responses] == [504, 504]
            assert client._circuit_breaker.state.value == "OPEN"
            assert counting_stub.calls == 3

            open_response = gateway.get("/orders/45")
            assert open_response.status_code == 503
            assert counting_stub.calls == 3

            fake_now[0] = 1
            servicer = CountingOrderServicer()
            server = _start_order_server(real_address, servicer)
            grpc.channel_ready_future(client._channel).result(timeout=2)
            recovered = gateway.get("/orders/46")
            assert recovered.status_code == 200
            assert client._circuit_breaker.state.value == "CLOSED"
            assert servicer.get_calls == 1

            normal = gateway.get("/orders/47")
            assert normal.status_code == 200
            assert servicer.get_calls == 2
    finally:
        client.close()
        server.stop(grace=0).wait()
