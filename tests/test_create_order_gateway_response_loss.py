import sys
import time
import os
import psycopg2
from concurrent import futures
from importlib import import_module
from pathlib import Path
from uuid import uuid4

import grpc
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from dotenv import load_dotenv
from fastapi.testclient import TestClient
from sqlalchemy import select


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ORDER_ROOT = PROJECT_ROOT / "order-service"
PRODUCT_ROOT = PROJECT_ROOT / "product-service"
GATEWAY_ROOT = PROJECT_ROOT / "api-gateway"
for path in (str(PROJECT_ROOT), str(GATEWAY_ROOT), str(PRODUCT_ROOT), str(ORDER_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

load_dotenv(PROJECT_ROOT / ".env")

for module_name in (
    "models",
    "database",
    "servicer",
    "clients.product_client",
    "security_config",
):
    sys.modules.pop(module_name, None)

database_module = import_module("database")
models_module = import_module("models")
servicer_module = import_module("servicer")
security_module = import_module("security_config")
product_client_module = import_module("clients.product_client")
gateway_module = import_module("gateway.main")
grpc_client_module = import_module("gateway.grpc_client")
generated_order = import_module("generated.order_pb2")
generated_order_grpc = import_module("generated.order_pb2_grpc")
generated_product = import_module("generated.product_pb2")
generated_product_grpc = import_module("generated.product_pb2_grpc")

SessionLocal = getattr(database_module, "SessionLocal")
Order = getattr(models_module, "Order")
OrderItem = getattr(models_module, "OrderItem")
OrderIdempotency = getattr(models_module, "OrderIdempotency")
OrderServiceServicer = getattr(servicer_module, "OrderServiceServicer")
JwtVerifier = getattr(security_module, "JwtVerifier")
ProductClient = getattr(product_client_module, "ProductClient")
create_app = getattr(gateway_module, "create_app")


class LostResponseCreateOrderService(OrderServiceServicer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.create_calls = 0
        self.create_keys = []

    def CreateOrder(self, request, context):
        self.create_calls += 1
        for entry in context.invocation_metadata():
            if entry.key.lower() == "idempotency-key":
                self.create_keys.append(entry.value)
                break
        response = super().CreateOrder(request, context)
        if self.create_calls == 1:
            context.abort(
                grpc.StatusCode.UNAVAILABLE,
                "Test-only response loss after CreateOrder commit.",
            )
        return response


class FakeAuthClient:
    def close(self):
        pass


def test_gateway_create_order_response_loss_replays_one_order(monkeypatch):
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
    user_id = 900000000 + int(uuid4().hex[:6], 16)
    now = int(time.time())
    token = jwt.encode(
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

    product_address = "127.0.0.1:50051"
    product_channel = grpc.insecure_channel(product_address)
    grpc.channel_ready_future(product_channel).result(timeout=5)
    product_stub = generated_product_grpc.ProductServiceStub(product_channel)
    suffix = uuid4().hex
    created_product = product_stub.AddProduct(
        generated_product.AddProductRequest(
            name=f"Gateway response loss {suffix}",
            description="CreateOrder response-loss test",
            price="12.50",
            initial_stock=10,
        ),
        timeout=5,
    )
    product_id = created_product.product.id

    product_client = ProductClient(address=product_address)
    servicer = LostResponseCreateOrderService(
        token_verifier=verifier,
        product_client=product_client,
    )
    order_server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    generated_order_grpc.add_OrderServiceServicer_to_server(
        servicer,
        order_server,
    )
    order_port = order_server.add_insecure_port("127.0.0.1:0")
    if order_port == 0:
        pytest.fail("Could not bind test CreateOrder gRPC server.")
    order_server.start()

    gateway_order_client = grpc_client_module.OrderServiceClient()
    gateway_order_client.address = f"127.0.0.1:{order_port}"
    gateway_order_client._channel.close()
    gateway_order_client._channel = grpc.insecure_channel(gateway_order_client.address)
    gateway_order_client._stub = generated_order_grpc.OrderServiceStub(
        gateway_order_client._channel
    )

    idempotency_key = f"gateway-create-loss-{uuid4().hex}"
    try:
        app = create_app(
            gateway_order_client,
            auth_client=FakeAuthClient(),
            token_verifier=verifier,
        )
        with TestClient(app) as gateway:
            gateway.headers["Authorization"] = f"Bearer {token}"
            response = gateway.post(
                "/orders",
                headers={"Idempotency-Key": idempotency_key},
                json={
                    "items": [
                        {"product_id": product_id, "quantity": 2}
                    ]
                },
            )

        assert response.status_code == 201, response.text
        order_id = response.json()["id"]
        assert servicer.create_calls == 2
        assert servicer.create_keys == [idempotency_key, idempotency_key]

        with SessionLocal() as session:
            orders = session.scalars(
                select(Order).where(
                    Order.id == order_id,
                    Order.user_id == user_id,
                )
            ).all()
            items = session.scalars(
                select(OrderItem).where(OrderItem.order_id == order_id)
            ).all()
            records = session.scalars(
                select(OrderIdempotency).where(
                    OrderIdempotency.user_id == user_id,
                    OrderIdempotency.idempotency_key == idempotency_key,
                )
            ).all()

        assert len(orders) == 1
        assert len(items) == 1
        assert len(records) == 1
        assert records[0].status == "COMPLETED"

        stock = product_stub.CheckStock(
            generated_product.CheckStockRequest(product_id=product_id),
            timeout=5,
        )
        assert stock.available_stock == 8
    finally:
        gateway_order_client.close()
        order_server.stop(grace=0).wait()
        servicer.close()

        with SessionLocal.begin() as session:
            session.execute(
                OrderItem.__table__.delete().where(
                    OrderItem.order_id.in_(
                        select(Order.id).where(Order.user_id == user_id)
                    )
                )
            )
            session.execute(
                OrderIdempotency.__table__.delete().where(
                    OrderIdempotency.user_id == user_id
                )
            )
            session.execute(
                Order.__table__.delete().where(Order.user_id == user_id)
            )

        def connect(database):
            return psycopg2.connect(
                host=os.getenv("DB_HOST", "127.0.0.1"),
                port=int(os.getenv("DB_PORT", "5432")),
                user=os.getenv("DB_USER", "minicart_admin"),
                password=os.getenv("DB_PASSWORD"),
                dbname=database,
            )

        with connect(os.getenv("PRODUCT_DB_NAME", "minicart_product_db")) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM processed_stock_operations "
                    "WHERE product_id = %s",
                    (product_id,),
                )
                cursor.execute(
                    "DELETE FROM inventory WHERE product_id = %s",
                    (product_id,),
                )
                cursor.execute(
                    "DELETE FROM products WHERE id = %s",
                    (product_id,),
                )
        product_channel.close()
