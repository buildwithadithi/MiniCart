from decimal import Decimal
import re
from uuid import uuid4

import grpc
import pytest
from sqlalchemy import select

from database import SessionLocal
from models import Order, OrderItem
from generated import order_pb2, payment_pb2, product_pb2


def create_product(product_stub, test_records, price="12.50", stock=100):
    response = product_stub.AddProduct(
        product_pb2.AddProductRequest(
            name=f"Order integration test {uuid4().hex}",
            description="Created by Order Service integration tests.",
            price=price,
            initial_stock=stock,
        ),
        timeout=5,
    )
    test_records["product_ids"].append(response.product.id)
    return response.product


def create_order(order_stub, test_records, items):
    return order_stub.CreateOrder(
        order_pb2.CreateOrderRequest(items=items),
        timeout=10,
    )


def create_order_with_status(
    order_stub, product_stub, test_records, expected_status
):
    for _ in range(3):
        product = create_product(product_stub, test_records, stock=100)
        response = create_order(
            order_stub,
            test_records,
            [
                order_pb2.OrderItemInput(
                    product_id=product.id,
                    quantity=2,
                    unit_price="0.01",
                )
            ],
        )
        test_records["order_ids"].append(response.order.id)
        if response.order.status == expected_status:
            return response.order, product.id
    raise AssertionError(
        f"Could not create an order with status {expected_status} after three attempts."
    )


def test_successful_order_creation(
    order_stub, product_stub, payment_stub, test_records
):
    order, product_id = create_order_with_status(
        order_stub, product_stub, test_records, "PAID"
    )

    assert order.id > 0
    assert order.status == "PAID"
    assert order.payment_id > 0
    assert order.payment_status == "SUCCESS"
    payment = payment_stub.GetPaymentStatus(
        payment_pb2.GetPaymentStatusRequest(payment_id=order.payment_id),
        timeout=5,
    )
    assert payment.payment.order_id == order.id
    assert payment.payment.status == "SUCCESS"
    assert order.total_amount == "25.00"
    assert len(order.items) == 1
    assert order.items[0].product_id == product_id
    assert order.items[0].quantity == 2
    assert order.items[0].unit_price == "12.50"


def test_products_are_fetched_and_total_uses_service_prices(
    order_stub, product_stub, test_records
):
    first = create_product(product_stub, test_records, price="19.95")
    second = create_product(product_stub, test_records, price="5.25")

    response = create_order(
        order_stub,
        test_records,
        [
            order_pb2.OrderItemInput(
                product_id=first.id, quantity=2, unit_price="0.01"
            ),
            order_pb2.OrderItemInput(
                product_id=second.id, quantity=3, unit_price="0.01"
            ),
        ],
    )
    test_records["order_ids"].append(response.order.id)

    assert Decimal(response.order.total_amount) == Decimal("55.65")
    assert [Decimal(item.unit_price) for item in response.order.items] == [
        Decimal("19.95"),
        Decimal("5.25"),
    ]


def test_insufficient_stock_is_rejected(order_stub, product_stub, test_records):
    product = create_product(product_stub, test_records, stock=1)

    with pytest.raises(grpc.RpcError) as error:
        create_order(
            order_stub,
            test_records,
            [
                order_pb2.OrderItemInput(
                    product_id=product.id, quantity=2, unit_price="12.50"
                )
            ],
        )

    assert error.value.code() == grpc.StatusCode.RESOURCE_EXHAUSTED


def test_invalid_product_id_is_rejected(order_stub, test_records):
    with pytest.raises(grpc.RpcError) as error:
        create_order(
            order_stub,
            test_records,
            [
                order_pb2.OrderItemInput(
                    product_id=9223372036854775807,
                    quantity=1,
                    unit_price="10.00",
                )
            ],
        )

    assert error.value.code() == grpc.StatusCode.NOT_FOUND


@pytest.mark.parametrize("quantity", [0, -1])
def test_invalid_quantities_are_rejected(
    order_stub, product_stub, test_records, quantity
):
    product = create_product(product_stub, test_records)

    with pytest.raises(grpc.RpcError) as error:
        create_order(
            order_stub,
            test_records,
            [
                order_pb2.OrderItemInput(
                    product_id=product.id,
                    quantity=quantity,
                    unit_price="12.50",
                )
            ],
        )

    assert error.value.code() == grpc.StatusCode.INVALID_ARGUMENT


def test_order_and_items_are_persisted(order_stub, product_stub, test_records):
    order, _ = create_order_with_status(
        order_stub, product_stub, test_records, "PAID"
    )

    with SessionLocal() as session:
        persisted_order = session.get(Order, order.id)
        persisted_items = list(
            session.scalars(
                select(OrderItem).where(OrderItem.order_id == order.id)
            )
        )

    assert persisted_order is not None
    assert persisted_order.status == "PAID"
    assert persisted_order.payment_id is not None
    assert persisted_order.payment_status == "SUCCESS"
    assert persisted_order.total_amount == Decimal("25.00")
    assert len(persisted_items) == 1
    assert persisted_items[0].unit_price == Decimal("12.50")


def test_successful_payment_marks_paid_and_reduces_stock(
    order_stub, product_stub, test_records
):
    order, product_id = create_order_with_status(
        order_stub, product_stub, test_records, "PAID"
    )

    stock = product_stub.CheckStock(
        product_pb2.CheckStockRequest(product_id=product_id), timeout=5
    )
    fetched = order_stub.GetOrder(
        order_pb2.GetOrderRequest(order_id=order.id), timeout=5
    )

    assert fetched.order.status == "PAID"
    assert fetched.order.payment_id > 0
    assert fetched.order.payment_status == "SUCCESS"
    assert stock.available_stock == 98


def test_failed_payment_marks_order_failed_without_reducing_stock(
    order_stub, product_stub, payment_stub, test_records
):
    order, product_id = create_order_with_status(
        order_stub, product_stub, test_records, "PAYMENT_FAILED"
    )

    stock = product_stub.CheckStock(
        product_pb2.CheckStockRequest(product_id=product_id), timeout=5
    )
    fetched = order_stub.GetOrder(
        order_pb2.GetOrderRequest(order_id=order.id), timeout=5
    )

    assert fetched.order.status == "PAYMENT_FAILED"
    assert fetched.order.payment_id > 0
    assert fetched.order.payment_status == "FAILED"
    payment = payment_stub.GetPaymentStatus(
        payment_pb2.GetPaymentStatusRequest(payment_id=fetched.order.payment_id),
        timeout=5,
    )
    assert payment.payment.order_id == order.id
    assert payment.payment.status == "FAILED"
    with SessionLocal() as session:
        persisted_order = session.get(Order, order.id)
    assert persisted_order is not None
    assert persisted_order.payment_id == fetched.order.payment_id
    assert persisted_order.payment_status == "FAILED"
    assert stock.available_stock == 100


def test_product_service_unavailable(
    start_isolated_order_service, test_records, monkeypatch
):
    monkeypatch.setenv("PRODUCT_SERVICE_TIMEOUT_SECONDS", "0.25")
    order_stub = start_isolated_order_service(
        product_address="127.0.0.1:1",
    )

    with pytest.raises(grpc.RpcError) as error:
        create_order(
            order_stub,
            test_records,
            [
                order_pb2.OrderItemInput(
                    product_id=1, quantity=1, unit_price="1.00"
                )
            ],
        )

    assert error.value.code() in (
        grpc.StatusCode.UNAVAILABLE,
        grpc.StatusCode.DEADLINE_EXCEEDED,
    )


def test_payment_service_unavailable(
    start_isolated_order_service,
    product_stub,
    test_records,
    monkeypatch,
):
    monkeypatch.setenv("PAYMENT_SERVICE_TIMEOUT_SECONDS", "0.25")
    order_stub = start_isolated_order_service(
        payment_address="127.0.0.1:1",
    )
    product = create_product(product_stub, test_records)

    with pytest.raises(grpc.RpcError) as error:
        create_order(
            order_stub,
            test_records,
            [
                order_pb2.OrderItemInput(
                    product_id=product.id,
                    quantity=1,
                    unit_price="12.50",
                )
            ],
        )

    assert error.value.code() in (
        grpc.StatusCode.UNAVAILABLE,
        grpc.StatusCode.DEADLINE_EXCEEDED,
    )
    match = re.search(r"Order (\d+):", error.value.details() or "")
    assert match is not None
    order_id = int(match.group(1))
    test_records["order_ids"].append(order_id)

    with SessionLocal() as session:
        persisted_order = session.get(Order, order_id)

    stock = product_stub.CheckStock(
        product_pb2.CheckStockRequest(product_id=product.id), timeout=5
    )
    assert persisted_order is not None
    assert persisted_order.status == "PAYMENT_FAILED"
    assert stock.available_stock == 100


def test_get_order_returns_not_found(order_stub):
    with pytest.raises(grpc.RpcError) as error:
        order_stub.GetOrder(
            order_pb2.GetOrderRequest(order_id=9223372036854775807),
            timeout=5,
        )

    assert error.value.code() == grpc.StatusCode.NOT_FOUND
