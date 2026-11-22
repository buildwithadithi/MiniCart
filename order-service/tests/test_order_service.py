from decimal import Decimal
from uuid import uuid4

import grpc
import pytest
from sqlalchemy import select

from database import SessionLocal
from models import Order, OrderItem
from generated import order_pb2, product_pb2


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


def test_order_creation_uses_product_service_prices(
    order_stub, product_stub, test_records
):
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
    order = response.order
    test_records["order_ids"].append(order.id)

    assert order.id > 0
    assert order.status == "CONFIRMED"
    assert order.total_amount == "25.00"
    assert len(order.items) == 1
    assert order.items[0].product_id == product.id
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
    product = create_product(product_stub, test_records, stock=100)
    order = create_order(
        order_stub,
        test_records,
        [
            order_pb2.OrderItemInput(
                product_id=product.id,
                quantity=2,
                unit_price="0.01",
            )
        ],
    ).order
    test_records["order_ids"].append(order.id)

    with SessionLocal() as session:
        persisted_order = session.get(Order, order.id)
        persisted_items = list(
            session.scalars(
                select(OrderItem).where(OrderItem.order_id == order.id)
            )
        )

    assert persisted_order is not None
    assert persisted_order.status == "CONFIRMED"
    assert persisted_order.total_amount == Decimal("25.00")
    assert len(persisted_items) == 1
    assert persisted_items[0].unit_price == Decimal("12.50")


def test_confirmed_order_reduces_stock(
    order_stub, product_stub, test_records
):
    product = create_product(product_stub, test_records, stock=100)
    order = create_order(
        order_stub,
        test_records,
        [
            order_pb2.OrderItemInput(
                product_id=product.id,
                quantity=2,
                unit_price="0.01",
            )
        ],
    ).order
    test_records["order_ids"].append(order.id)

    stock = product_stub.CheckStock(
        product_pb2.CheckStockRequest(product_id=product.id), timeout=5
    )
    fetched = order_stub.GetOrder(
        order_pb2.GetOrderRequest(order_id=order.id), timeout=5
    )

    assert fetched.order.status == "CONFIRMED"
    assert stock.available_stock == 98


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

def test_get_order_returns_not_found(order_stub):
    with pytest.raises(grpc.RpcError) as error:
        order_stub.GetOrder(
            order_pb2.GetOrderRequest(order_id=9223372036854775807),
            timeout=5,
        )

    assert error.value.code() == grpc.StatusCode.NOT_FOUND
