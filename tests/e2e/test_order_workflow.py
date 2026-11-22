from decimal import Decimal
from uuid import uuid4

import grpc
import pytest
from sqlalchemy import text

from database import SessionLocal
from generated import order_pb2, product_pb2


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


def order_count():
    with SessionLocal() as session:
        return session.execute(text("SELECT count(*) FROM orders")).scalar_one()


def test_successful_end_to_end_order_workflow(
    product_stub, order_stub, created_records, service_databases
):
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
    assert order.status == "CONFIRMED"

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
    assert persisted_order.status == "CONFIRMED"
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
                "SELECT total_amount, status FROM orders WHERE id = :id"
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
    assert product_row.name == created_product.name
    assert product_row.price == Decimal("19.95")
    assert inventory_row.available_stock == 8
    assert order_row.total_amount == expected_total
    assert order_row.status == "CONFIRMED"
    assert len(item_rows) == 1
    assert item_rows[0].product_id == created_product.id
    assert item_rows[0].quantity == 2
    assert item_rows[0].unit_price == Decimal("19.95")
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
