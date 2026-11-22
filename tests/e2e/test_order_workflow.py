from decimal import Decimal
import re
from uuid import uuid4

import grpc
import pytest
from sqlalchemy import text

from database import SessionLocal
from generated import order_pb2, payment_pb2, product_pb2


def add_product(product_stub, created_records, *, price="19.95", stock=10):
    response = product_stub.AddProduct(
        product_pb2.AddProductRequest(
            name=f"E2E product {uuid4().hex}",
            description="Created by MiniCart end-to-end tests.",
            price=price,
            initial_stock=stock,
        ),
        timeout=5,
    )
    created_records["product_ids"].append(response.product.id)
    return response.product


def create_order(order_stub, product_id, quantity):
    return order_stub.CreateOrder(
        order_pb2.CreateOrderRequest(
            items=[
                order_pb2.OrderItemInput(
                    product_id=product_id,
                    quantity=quantity,
                    unit_price="0.01",
                )
            ]
        ),
        timeout=10,
    )


def order_id_from_error(error):
    match = re.search(r"Order (\d+):", error.details() or "")
    if match is None:
        raise AssertionError("Order Service error did not include its created order ID.")
    return int(match.group(1))


def order_count():
    with SessionLocal() as session:
        return session.execute(text("SELECT count(*) FROM orders")).scalar_one()


def prepare_next_payment_outcome(
    order_stub, product_stub, created_records, desired_status
):
    desired_parity = 0 if desired_status == "SUCCESS" else 1
    for _ in range(2):
        product = add_product(product_stub, created_records, price="1.00", stock=2)
        response = create_order(order_stub, product.id, quantity=1)
        created_records["order_ids"].append(response.order.id)
        if response.order.id % 2 != desired_parity:
            return
    raise AssertionError(f"Unable to prepare an order for payment status {desired_status}.")


def test_successful_end_to_end_order_workflow(
    product_stub, order_stub, payment_stub, created_records, service_databases
):
    prepare_next_payment_outcome(
        order_stub, product_stub, created_records, "SUCCESS"
    )

    created_product = add_product(product_stub, created_records)
    retrieved_product = product_stub.GetProduct(
        product_pb2.GetProductRequest(product_id=created_product.id),
        timeout=5,
    ).product
    initial_stock = product_stub.CheckStock(
        product_pb2.CheckStockRequest(product_id=created_product.id),
        timeout=5,
    )

    assert retrieved_product.id == created_product.id
    assert Decimal(retrieved_product.price) == Decimal("19.95")
    assert initial_stock.product_exists
    assert initial_stock.available_stock == 10

    response = create_order(order_stub, created_product.id, quantity=2)
    order = response.order
    created_records["order_ids"].append(order.id)

    expected_total = Decimal(retrieved_product.price) * 2
    assert Decimal(order.total_amount) == expected_total
    assert order.items[0].product_id == created_product.id
    assert order.items[0].quantity == 2
    assert Decimal(order.items[0].unit_price) == Decimal(retrieved_product.price)
    assert order.status == "PAID"
    assert order.payment_status == "SUCCESS"
    assert order.payment_id > 0

    payment = payment_stub.GetPaymentStatus(
        payment_pb2.GetPaymentStatusRequest(payment_id=order.payment_id),
        timeout=5,
    ).payment
    assert payment.id == order.payment_id
    assert payment.order_id == order.id
    assert Decimal(payment.amount) == expected_total
    assert payment.status == order.payment_status

    remaining_stock = product_stub.CheckStock(
        product_pb2.CheckStockRequest(product_id=created_product.id),
        timeout=5,
    )
    assert remaining_stock.available_stock == 8

    persisted_order = order_stub.GetOrder(
        order_pb2.GetOrderRequest(order_id=order.id),
        timeout=5,
    ).order
    assert persisted_order.id == order.id
    assert persisted_order.status == "PAID"
    assert persisted_order.payment_id == payment.id
    assert persisted_order.payment_status == "SUCCESS"
    assert len(persisted_order.items) == 1

    with service_databases["product"]["sessions"]() as session:
        product_row = session.execute(
            text("SELECT name, price FROM products WHERE id = :id"),
            {"id": created_product.id},
        ).one()
        inventory_row = session.execute(
            text("SELECT available_stock FROM inventory WHERE product_id = :id"),
            {"id": created_product.id},
        ).one()
    with service_databases["order"]["sessions"]() as session:
        order_row = session.execute(
            text(
                "SELECT total_amount, status, payment_id, payment_status "
                "FROM orders WHERE id = :id"
            ),
            {"id": order.id},
        ).one()
        item_rows = session.execute(
            text(
                "SELECT product_id, quantity, unit_price FROM order_items "
                "WHERE order_id = :id"
            ),
            {"id": order.id},
        ).all()
    with service_databases["payment"]["sessions"]() as session:
        payment_row = session.execute(
            text(
                "SELECT id, order_id, amount, status FROM payments WHERE id = :id"
            ),
            {"id": order.payment_id},
        ).one()

    assert product_row.name == created_product.name
    assert product_row.price == Decimal("19.95")
    assert inventory_row.available_stock == 8
    assert order_row.total_amount == expected_total
    assert order_row.status == "PAID"
    assert order_row.payment_id == payment.id
    assert order_row.payment_status == "SUCCESS"
    assert len(item_rows) == 1
    assert item_rows[0].product_id == created_product.id
    assert item_rows[0].quantity == 2
    assert item_rows[0].unit_price == Decimal("19.95")
    assert payment_row.id == order_row.payment_id
    assert payment_row.order_id == order.id
    assert payment_row.amount == order_row.total_amount
    assert payment_row.status == order_row.payment_status


def test_failed_payment_keeps_stock_and_persists_payment(
    product_stub, order_stub, payment_stub, created_records, service_databases
):
    prepare_next_payment_outcome(
        order_stub, product_stub, created_records, "FAILED"
    )
    product = add_product(product_stub, created_records, price="8.75", stock=10)

    response = create_order(order_stub, product.id, quantity=2)
    order = response.order
    created_records["order_ids"].append(order.id)

    assert order.id % 2 == 1
    assert order.status == "PAYMENT_FAILED"
    assert order.payment_status == "FAILED"
    assert order.payment_id > 0
    assert Decimal(order.total_amount) == Decimal("17.50")

    payment = payment_stub.GetPaymentStatus(
        payment_pb2.GetPaymentStatusRequest(payment_id=order.payment_id),
        timeout=5,
    ).payment
    stock = product_stub.CheckStock(
        product_pb2.CheckStockRequest(product_id=product.id),
        timeout=5,
    )
    persisted_order = order_stub.GetOrder(
        order_pb2.GetOrderRequest(order_id=order.id), timeout=5
    ).order

    assert payment.id == order.payment_id
    assert payment.order_id == order.id
    assert payment.status == "FAILED"
    assert Decimal(payment.amount) == Decimal("17.50")
    assert stock.available_stock == 10
    assert persisted_order.payment_id == payment.id
    assert persisted_order.payment_status == payment.status
    assert persisted_order.status == "PAYMENT_FAILED"

    with service_databases["order"]["sessions"]() as session:
        order_row = session.execute(
            text(
                "SELECT status, payment_id, payment_status FROM orders "
                "WHERE id = :order_id"
            ),
            {"order_id": order.id},
        ).one()
    with service_databases["payment"]["sessions"]() as session:
        payment_row = session.execute(
            text(
                "SELECT id, amount, status FROM payments WHERE order_id = :order_id"
            ),
            {"order_id": order.id},
        ).one()

    assert order_row.status == "PAYMENT_FAILED"
    assert order_row.payment_id == payment_row.id == order.payment_id
    assert order_row.payment_status == payment_row.status == "FAILED"
    assert payment_row.amount == Decimal("17.50")


def test_nonexistent_product_returns_not_found(order_stub):
    orders_before = order_count()
    with pytest.raises(grpc.RpcError) as error:
        create_order(order_stub, product_id=9223372036854775807, quantity=1)

    assert error.value.code() == grpc.StatusCode.NOT_FOUND
    assert order_count() == orders_before


def test_insufficient_stock_returns_resource_exhausted(
    product_stub, order_stub, created_records
):
    product = add_product(product_stub, created_records, stock=1)
    orders_before = order_count()

    with pytest.raises(grpc.RpcError) as error:
        create_order(order_stub, product.id, quantity=2)

    assert error.value.code() == grpc.StatusCode.RESOURCE_EXHAUSTED
    assert order_count() == orders_before
    stock = product_stub.CheckStock(
        product_pb2.CheckStockRequest(product_id=product.id), timeout=5
    )
    assert stock.available_stock == 1


@pytest.mark.parametrize("quantity", [0, -1])
def test_invalid_quantity_returns_invalid_argument(
    product_stub, order_stub, created_records, quantity
):
    product = add_product(product_stub, created_records)
    orders_before = order_count()

    with pytest.raises(grpc.RpcError) as error:
        create_order(order_stub, product.id, quantity)

    assert error.value.code() == grpc.StatusCode.INVALID_ARGUMENT
    assert order_count() == orders_before


def test_product_service_unavailable(isolated_order_stub, monkeypatch):
    monkeypatch.setenv("PRODUCT_SERVICE_TIMEOUT_SECONDS", "0.25")
    order_stub = isolated_order_stub(product_address="127.0.0.1:1")
    orders_before = order_count()

    with pytest.raises(grpc.RpcError) as error:
        create_order(order_stub, product_id=1, quantity=1)

    assert error.value.code() in (
        grpc.StatusCode.UNAVAILABLE,
        grpc.StatusCode.DEADLINE_EXCEEDED,
    )
    assert order_count() == orders_before


def test_payment_service_unavailable(
    isolated_order_stub,
    product_stub,
    created_records,
    monkeypatch,
    service_databases,
):
    monkeypatch.setenv("PAYMENT_SERVICE_TIMEOUT_SECONDS", "0.25")
    order_stub = isolated_order_stub(payment_address="127.0.0.1:1")
    product = add_product(product_stub, created_records, stock=10)

    with pytest.raises(grpc.RpcError) as error:
        create_order(order_stub, product.id, quantity=2)

    assert error.value.code() in (
        grpc.StatusCode.UNAVAILABLE,
        grpc.StatusCode.DEADLINE_EXCEEDED,
    )
    order_id = order_id_from_error(error.value)
    created_records["order_ids"].append(order_id)

    stock = product_stub.CheckStock(
        product_pb2.CheckStockRequest(product_id=product.id), timeout=5
    )
    with service_databases["order"]["sessions"]() as session:
        order_row = session.execute(
            text("SELECT status, payment_id, payment_status FROM orders WHERE id = :id"),
            {"id": order_id},
        ).one()
    with service_databases["payment"]["sessions"]() as session:
        payment_count = session.execute(
            text("SELECT count(*) FROM payments WHERE order_id = :id"),
            {"id": order_id},
        ).scalar_one()

    assert order_row.status == "PAYMENT_FAILED"
    assert order_row.payment_id is None
    assert order_row.payment_status is None
    assert payment_count == 0
    assert stock.available_stock == 10
