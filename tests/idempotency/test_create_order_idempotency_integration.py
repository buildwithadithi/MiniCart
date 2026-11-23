import os
import socket
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import grpc
import httpx
import psycopg2
import pytest
from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")
for path in (str(PROJECT_ROOT),):
    if path not in sys.path:
        sys.path.insert(0, path)

from generated import product_pb2, product_pb2_grpc


GATEWAY_URL = os.getenv("MINICART_GATEWAY_URL", "http://127.0.0.1:8000").rstrip("/")
PRODUCT_ADDRESS = os.getenv("PRODUCT_SERVICE_ADDRESS", "127.0.0.1:50051")


def _tcp_open(host, port):
    with socket.socket() as sock:
        sock.settimeout(1)
        return sock.connect_ex((host, port)) == 0


def _db_connection(database):
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "127.0.0.1"),
        port=int(os.getenv("DB_PORT", "5432")),
        user=os.getenv("DB_USER", "minicart_admin"),
        password=os.getenv("DB_PASSWORD"),
        dbname=database,
        connect_timeout=5,
    )


@pytest.fixture
def create_order_integration():
    if not _tcp_open("127.0.0.1", 8000):
        pytest.fail("API Gateway is not running on 127.0.0.1:8000")
    if not _tcp_open("127.0.0.1", 50051):
        pytest.fail("Product Service is not running on 127.0.0.1:50051")

    suffix = uuid4().hex
    email = f"idempotency-{suffix}@example.com"
    password = f"idempotency-password-{suffix}"
    with httpx.Client(base_url=GATEWAY_URL, timeout=15) as client:
        registered = client.post(
            "/auth/register",
            json={"name": "Idempotency Test", "email": email, "password": password},
        )
        assert registered.status_code == 201, registered.text
        user_id = registered.json()["id"]
        logged_in = client.post(
            "/auth/login",
            json={"email": email, "password": password},
        )
        assert logged_in.status_code == 200, logged_in.text
        token = logged_in.json()["access_token"]

        channel = grpc.insecure_channel(PRODUCT_ADDRESS)
        grpc.channel_ready_future(channel).result(timeout=5)
        stub = product_pb2_grpc.ProductServiceStub(channel)
        created = stub.AddProduct(
            product_pb2.AddProductRequest(
                name=f"Idempotency product {suffix}",
                description="Dedicated idempotency integration test",
                price="12.50",
                initial_stock=20,
            ),
            timeout=5,
        )
        product_id = created.product.id

        yield client, token, product_id, user_id, email

        channel.close()

    with _db_connection(os.getenv("PRODUCT_DB_NAME", "minicart_product_db")) as conn:
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM inventory WHERE product_id = %s", (product_id,))
            cursor.execute("DELETE FROM products WHERE id = %s", (product_id,))

    with _db_connection(os.getenv("ORDER_DB_NAME", "minicart_order_db")) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "DELETE FROM order_items WHERE order_id IN "
                "(SELECT id FROM orders WHERE user_id = %s)",
                (user_id,),
            )
            cursor.execute("DELETE FROM order_idempotency WHERE user_id = %s", (user_id,))
            cursor.execute("DELETE FROM orders WHERE user_id = %s", (user_id,))

    with _db_connection(os.getenv("AUTH_DB_NAME", "minicart_auth_db")) as conn:
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM users WHERE email = %s", (email,))


def test_repeated_and_concurrent_create_order_is_executed_once(
    create_order_integration,
):
    client, token, product_id, user_id, _ = create_order_integration
    key = f"create-{uuid4().hex}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": key,
    }
    body = {"items": [{"product_id": product_id, "quantity": 2}]}
    barrier = Barrier(2)

    def send():
        barrier.wait()
        return client.post("/orders", headers=headers, json=body)

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(lambda _: send(), range(2)))

    assert all(response.status_code == 201 for response in responses), [
        (response.status_code, response.text) for response in responses
    ]
    assert responses[0].json()["id"] == responses[1].json()["id"]

    with _db_connection(os.getenv("ORDER_DB_NAME", "minicart_order_db")) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM orders WHERE user_id = %s", (user_id,))
            row = cursor.fetchone()
            assert row is not None
            assert row[0] == 1
            cursor.execute(
                "SELECT COUNT(*) FROM order_idempotency "
                "WHERE user_id = %s AND idempotency_key = %s",
                (user_id, key),
            )
            row = cursor.fetchone()
            assert row is not None
            assert row[0] == 1

    channel = grpc.insecure_channel(PRODUCT_ADDRESS)
    grpc.channel_ready_future(channel).result(timeout=5)
    stub = product_pb2_grpc.ProductServiceStub(channel)
    stock = stub.CheckStock(
        product_pb2.CheckStockRequest(product_id=product_id),
        timeout=5,
    )
    channel.close()
    assert stock.available_stock == 18


def test_same_key_with_different_payload_is_rejected(create_order_integration):
    client, token, product_id, _, _ = create_order_integration
    key = f"conflict-{uuid4().hex}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": key,
    }

    first = client.post(
        "/orders",
        headers=headers,
        json={"items": [{"product_id": product_id, "quantity": 1}]},
    )
    second = client.post(
        "/orders",
        headers=headers,
        json={"items": [{"product_id": product_id, "quantity": 2}]},
    )

    assert first.status_code == 201
    assert second.status_code == 409


def test_insufficient_stock_leaves_no_idempotency_reservation(
    create_order_integration,
):
    client, token, product_id, user_id, _ = create_order_integration
    key = f"rollback-{uuid4().hex}"
    response = client.post(
        "/orders",
        headers={
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": key,
        },
        json={"items": [{"product_id": product_id, "quantity": 21}]},
    )

    assert response.status_code == 409
    with _db_connection(os.getenv("ORDER_DB_NAME", "minicart_order_db")) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM order_idempotency "
                "WHERE user_id = %s AND idempotency_key = %s",
                (user_id, key),
            )
            row = cursor.fetchone()
            assert row is not None
            assert row[0] == 0
            cursor.execute("SELECT COUNT(*) FROM orders WHERE user_id = %s", (user_id,))
            row = cursor.fetchone()
            assert row is not None
            assert row[0] == 0
