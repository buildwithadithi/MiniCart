from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import text

from generated import product_pb2


def _add_product(product_stub, created_records, *, price="19.95", stock=10):
    response = product_stub.AddProduct(
        product_pb2.AddProductRequest(
            name=f"Gateway integration product {uuid4().hex}",
            description="Created by MiniCart Gateway integration tests.",
            price=price,
            initial_stock=stock,
        ),
        timeout=5,
    )
    product = response.product
    created_records["product_ids"].add(product.id)
    return product


def _create_order(gateway_client, product_id: int, quantity: int):
    return gateway_client.post(
        "/orders",
        headers={"Idempotency-Key": f"gateway-test-{uuid4().hex}"},
        json={"items": [{"product_id": product_id, "quantity": quantity}]},
    )


def test_successful_order_through_gateway(
    gateway_client, product_stub, created_records, database_components
):
    product = _add_product(product_stub, created_records)
    retrieved_product = product_stub.GetProduct(
        product_pb2.GetProductRequest(product_id=product.id), timeout=5
    ).product
    initial_stock = product_stub.CheckStock(
        product_pb2.CheckStockRequest(product_id=product.id), timeout=5
    )
    assert retrieved_product.id == product.id
    assert Decimal(retrieved_product.price) == Decimal("19.95")
    assert initial_stock.product_exists
    assert initial_stock.available_stock == 10

    response = _create_order(gateway_client, product.id, quantity=2)
    assert response.status_code == 201, response.text
    order = response.json()
    created_records["order_ids"].add(order["id"])

    expected_total = Decimal(retrieved_product.price) * 2
    assert Decimal(order["total_amount"]) == expected_total
    assert order["status"] == "CONFIRMED"
    assert order["items"] == [
        {
            "id": order["items"][0]["id"],
            "product_id": product.id,
            "quantity": 2,
            "unit_price": "19.95",
        }
    ]

    remaining_stock = product_stub.CheckStock(
        product_pb2.CheckStockRequest(product_id=product.id), timeout=5
    )
    assert remaining_stock.available_stock == 8

    with database_components["product"]["sessions"]() as session:
        product_row = session.execute(
            text(
                "SELECT p.name, p.price, i.available_stock "
                "FROM products AS p JOIN inventory AS i ON i.product_id = p.id "
                "WHERE p.id = :product_id"
            ),
            {"product_id": product.id},
        ).one()
    with database_components["order"]["sessions"]() as session:
        order_row = session.execute(
            text(
                "SELECT total_amount, status FROM orders WHERE id = :order_id"
            ),
            {"order_id": order["id"]},
        ).one()
        item_rows = session.execute(
            text(
                "SELECT product_id, quantity, unit_price FROM order_items "
                "WHERE order_id = :order_id"
            ),
            {"order_id": order["id"]},
        ).all()

    assert product_row.name == product.name
    assert product_row.price == expected_total / 2
    assert product_row.available_stock == 8
    assert order_row.total_amount == expected_total
    assert order_row.status == "CONFIRMED"
    assert len(item_rows) == 1
    assert item_rows[0].product_id == product.id
    assert item_rows[0].quantity == 2
    assert item_rows[0].unit_price == Decimal("19.95")


def test_nonexistent_product_returns_not_found(gateway_client):
    response = _create_order(
        gateway_client, product_id=9223372036854775807, quantity=1
    )

    assert response.status_code == 404


def test_insufficient_stock_returns_conflict(
    gateway_client, product_stub, created_records
):
    product = _add_product(product_stub, created_records, stock=1)

    response = _create_order(gateway_client, product.id, quantity=2)

    assert response.status_code == 409


@pytest.mark.parametrize("quantity", [0, -1])
def test_invalid_quantity_is_rejected_by_gateway(gateway_client, quantity):
    response = _create_order(gateway_client, product_id=1, quantity=quantity)

    assert response.status_code == 422


def test_retrieve_existing_order_through_gateway(
    gateway_client, product_stub, created_records
):
    product = _add_product(product_stub, created_records)
    create_response = _create_order(gateway_client, product.id, quantity=1)
    assert create_response.status_code == 201, create_response.text
    created_order = create_response.json()
    created_records["order_ids"].add(created_order["id"])

    get_response = gateway_client.get(f"/orders/{created_order['id']}")

    assert get_response.status_code == 200, get_response.text
    retrieved_order = get_response.json()
    assert retrieved_order["id"] == created_order["id"]
    assert retrieved_order["total_amount"] == created_order["total_amount"]
    assert retrieved_order["status"] == created_order["status"]
    assert retrieved_order["items"] == created_order["items"]


def test_product_service_unavailable_returns_service_unavailable(
    product_unavailable_gateway,
):
    order_service_probe = product_unavailable_gateway.get(
        "/orders/9223372036854775807"
    )
    assert order_service_probe.status_code == 404, order_service_probe.text

    response = _create_order(
        product_unavailable_gateway,
        product_id=9223372036854775807,
        quantity=1,
    )

    assert response.status_code == 503
