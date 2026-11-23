import os
import socket
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import grpc
import psycopg2
import pytest
from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")
for path in (str(PROJECT_ROOT),):
    if path not in sys.path:
        sys.path.insert(0, path)

from generated import product_pb2, product_pb2_grpc


PRODUCT_ADDRESS = os.getenv("PRODUCT_SERVICE_ADDRESS", "127.0.0.1:50051")


def _tcp_open(host, port):
    with socket.socket() as sock:
        sock.settimeout(1)
        return sock.connect_ex((host, port)) == 0


def _db_connection():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "127.0.0.1"),
        port=int(os.getenv("DB_PORT", "5432")),
        user=os.getenv("DB_USER", "minicart_admin"),
        password=os.getenv("DB_PASSWORD"),
        dbname=os.getenv("PRODUCT_DB_NAME", "minicart_product_db"),
        connect_timeout=5,
    )


@pytest.fixture
def update_stock_integration():
    if not _tcp_open("127.0.0.1", 50051):
        pytest.fail("Product Service is not running on 127.0.0.1:50051")

    suffix = uuid4().hex
    channel = grpc.insecure_channel(PRODUCT_ADDRESS)
    grpc.channel_ready_future(channel).result(timeout=5)
    stub = product_pb2_grpc.ProductServiceStub(channel)
    created = stub.AddProduct(
        product_pb2.AddProductRequest(
            name=f"Stock idempotency {suffix}",
            description="Dedicated stock idempotency integration test",
            price="5.25",
            initial_stock=20,
        ),
        timeout=5,
    )
    product_id = created.product.id

    yield stub, product_id

    channel.close()
    with _db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "DELETE FROM processed_stock_operations WHERE product_id = %s",
                (product_id,),
            )
            cursor.execute("DELETE FROM inventory WHERE product_id = %s", (product_id,))
            cursor.execute("DELETE FROM products WHERE id = %s", (product_id,))


def test_concurrent_duplicate_operation_modifies_stock_once(update_stock_integration):
    stub, product_id = update_stock_integration
    operation_id = f"stock-{uuid4().hex}"
    barrier = Barrier(4)

    def send():
        barrier.wait()
        return stub.UpdateStock(
            product_pb2.UpdateStockRequest(
                product_id=product_id,
                quantity_delta=-3,
                operation_id=operation_id,
            ),
            timeout=5,
        )

    with ThreadPoolExecutor(max_workers=4) as executor:
        responses = list(executor.map(lambda _: send(), range(4)))

    assert {response.available_stock for response in responses} == {17}
    with _db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT available_stock FROM inventory WHERE product_id = %s",
                (product_id,),
            )
            row = cursor.fetchone()
            assert row is not None, "No inventory record found for product_id"
            assert row[0] == 17
            cursor.execute(
                "SELECT COUNT(*) FROM processed_stock_operations "
                "WHERE operation_id = %s",
                (operation_id,),
            )
            row = cursor.fetchone()
            assert row is not None, "No processed_stock_operations record found for operation_id"
            assert row[0] == 1


def test_repeated_operation_returns_original_result_and_new_id_applies_again(
    update_stock_integration,
):
    stub, product_id = update_stock_integration
    operation_id = f"sequential-{uuid4().hex}"
    request = product_pb2.UpdateStockRequest(
        product_id=product_id,
        quantity_delta=-2,
        operation_id=operation_id,
    )
    first = stub.UpdateStock(request, timeout=5)
    repeated = stub.UpdateStock(request, timeout=5)
    second_operation = stub.UpdateStock(
        product_pb2.UpdateStockRequest(
            product_id=product_id,
            quantity_delta=-1,
            operation_id=f"sequential-{uuid4().hex}",
        ),
        timeout=5,
    )

    assert (first.available_stock, repeated.available_stock) == (18, 18)
    assert second_operation.available_stock == 17


def test_failed_stock_update_does_not_create_processed_record(
    update_stock_integration,
):
    stub, product_id = update_stock_integration
    operation_id = f"failed-{uuid4().hex}"

    with pytest.raises(grpc.RpcError) as raised:
        stub.UpdateStock(
            product_pb2.UpdateStockRequest(
                product_id=product_id,
                quantity_delta=-21,
                operation_id=operation_id,
            ),
            timeout=5,
        )
    assert raised.value.code() == grpc.StatusCode.FAILED_PRECONDITION

    with _db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT available_stock FROM inventory WHERE product_id = %s",
                (product_id,),
            )
            row = cursor.fetchone()
            assert row is not None, "No inventory record found for product_id"
            assert row[0] == 20
            cursor.execute(
                "SELECT COUNT(*) FROM processed_stock_operations "
                "WHERE operation_id = %s",
                (operation_id,),
            )
            row = cursor.fetchone()
            assert row is not None, "No processed operation count returned"
            assert row[0] == 0
