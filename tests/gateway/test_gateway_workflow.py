from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import text

from generated import payment_pb2, product_pb2


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
        json={"items": [{"product_id": product_id, "quantity": quantity}]},
    )


def _prepare_next_order_parity(
    gateway_client, product_stub, created_records, desired_parity: int
) -> None:
    product = _add_product(product_stub, created_records, price="1.00", stock=10)
    preceding_parity = 1 - desired_parity
    for _ in range(2):
        response = _create_order(gateway_client, product.id, quantity=1)
        assert response.status_code == 201, response.text
        order_id = response.json()["id"]
        created_records["order_ids"].add(order_id)
        if order_id % 2 == preceding_parity:
            return
    pytest.fail("Could not arrange the next order ID parity for payment testing.")


def test_successful_order_and_payment_through_gateway(
    gateway_client, product_stub, payment_stub, created_records, database_components
):
    _prepare_next_order_parity(
        gateway_client, product_stub, created_records, desired_parity=0
    )
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
    assert order["id"] % 2 == 0
    assert Decimal(order["total_amount"]) == expected_total
    assert order["status"] == "PAID"
    assert order["payment_status"] == "SUCCESS"
    assert order["payment_id"] > 0
    assert order["items"] == [
        {
            "id": order["items"][0]["id"],
            "product_id": product.id,
            "quantity": 2,
            "unit_price": "19.95",
        }
    ]

    payment = payment_stub.GetPaymentStatus(
        payment_pb2.GetPaymentStatusRequest(payment_id=order["payment_id"]),
        timeout=5,
    ).payment
    assert payment.id == order["payment_id"]
    assert payment.order_id == order["id"]
    assert Decimal(payment.amount) == expected_total
    assert payment.status == order["payment_status"]

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
                "SELECT total_amount, status, payment_id, payment_status "
                "FROM orders WHERE id = :order_id"
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
    with database_components["payment"]["sessions"]() as session:
        payment_row = session.execute(
            text(
                "SELECT id, order_id, amount, status FROM payments WHERE id = :id"
            ),
            {"id": order["payment_id"]},
        ).one()

    assert product_row.name == product.name
    assert product_row.price == expected_total / 2
    assert product_row.available_stock == 8
    assert order_row.total_amount == expected_total
    assert order_row.status == "PAID"
    assert order_row.payment_id == payment_row.id == order["payment_id"]
    assert order_row.payment_status == payment_row.status == "SUCCESS"
    assert len(item_rows) == 1
    assert item_rows[0].product_id == product.id
    assert item_rows[0].quantity == 2
    assert item_rows[0].unit_price == Decimal("19.95")
    assert payment_row.order_id == order["id"]
    assert payment_row.amount == order_row.total_amount


def test_failed_payment_keeps_inventory_unchanged(
    gateway_client, product_stub, payment_stub, created_records, database_components
):
    _prepare_next_order_parity(
        gateway_client, product_stub, created_records, desired_parity=1
    )
    product = _add_product(product_stub, created_records, price="8.75", stock=10)
    initial_stock = product_stub.CheckStock(
        product_pb2.CheckStockRequest(product_id=product.id), timeout=5
    )
    assert initial_stock.available_stock == 10

    response = _create_order(gateway_client, product.id, quantity=2)
    assert response.status_code == 201, response.text
    order = response.json()
    created_records["order_ids"].add(order["id"])

    assert order["id"] % 2 == 1
    assert Decimal(order["total_amount"]) == Decimal("17.50")
    assert order["status"] == "PAYMENT_FAILED"
    assert order["payment_status"] == "FAILED"
    assert order["payment_id"] > 0

    payment = payment_stub.GetPaymentStatus(
        payment_pb2.GetPaymentStatusRequest(payment_id=order["payment_id"]),
        timeout=5,
    ).payment
    stock_after = product_stub.CheckStock(
        product_pb2.CheckStockRequest(product_id=product.id), timeout=5
    )
    assert payment.id == order["payment_id"]
    assert payment.order_id == order["id"]
    assert payment.status == "FAILED"
    assert Decimal(payment.amount) == Decimal("17.50")
    assert stock_after.available_stock == initial_stock.available_stock == 10

    with database_components["order"]["sessions"]() as session:
        order_row = session.execute(
            text(
                "SELECT status, payment_id, payment_status FROM orders "
                "WHERE id = :order_id"
            ),
            {"order_id": order["id"]},
        ).one()
    with database_components["payment"]["sessions"]() as session:
        payment_row = session.execute(
            text(
                "SELECT id, order_id, amount, status FROM payments "
                "WHERE order_id = :order_id"
            ),
            {"order_id": order["id"]},
        ).one()

    assert order_row.status == "PAYMENT_FAILED"
    assert order_row.payment_id == payment_row.id == order["payment_id"]
    assert order_row.payment_status == payment_row.status == "FAILED"
    assert payment_row.amount == Decimal("17.50")


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
    assert retrieved_order["payment_id"] == created_order["payment_id"]
    assert retrieved_order["payment_status"] == created_order["payment_status"]
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


def test_payment_service_unavailable_does_not_reduce_inventory(
    payment_unavailable_gateway,
    product_stub,
    created_records,
    database_components,
):
    product = _add_product(product_stub, created_records, price="5.25", stock=10)
    initial_stock = product_stub.CheckStock(
        product_pb2.CheckStockRequest(product_id=product.id), timeout=5
    )

    response = _create_order(
        payment_unavailable_gateway, product.id, quantity=2
    )

    assert response.status_code == 503
    stock_after = product_stub.CheckStock(
        product_pb2.CheckStockRequest(product_id=product.id), timeout=5
    )
    assert stock_after.available_stock == initial_stock.available_stock == 10

    with database_components["order"]["sessions"]() as session:
        order_row = session.execute(
            text(
                "SELECT o.id, o.status, o.payment_id, o.payment_status "
                "FROM orders AS o JOIN order_items AS i ON i.order_id = o.id "
                "WHERE i.product_id = :product_id"
            ),
            {"product_id": product.id},
        ).one()
    with database_components["payment"]["sessions"]() as session:
        payment_count = session.execute(
            text("SELECT count(*) FROM payments WHERE order_id = :order_id"),
            {"order_id": order_row.id},
        ).scalar_one()

    created_records["order_ids"].add(order_row.id)
    assert order_row.status == "PAYMENT_FAILED"
    assert order_row.payment_id is None
    assert order_row.payment_status is None
    assert payment_count == 0
