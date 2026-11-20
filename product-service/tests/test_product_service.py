from decimal import Decimal
from uuid import uuid4

import grpc
import pytest
from sqlalchemy import select

from database import SessionLocal
from models import Inventory, Product
from generated import product_pb2


def add_product(product_stub, created_product_ids, initial_stock=12):
    response = product_stub.AddProduct(
        product_pb2.AddProductRequest(
            name=f"Test product {uuid4().hex}",
            description="Created by Product Service integration tests.",
            price="19.95",
            initial_stock=initial_stock,
        ),
        timeout=5,
    )
    created_product_ids.append(response.product.id)
    return response.product


def test_add_product_returns_created_product(product_stub, created_product_ids):
    product = add_product(product_stub, created_product_ids, initial_stock=7)

    assert product.id > 0
    assert product.name.startswith("Test product ")
    assert product.description == "Created by Product Service integration tests."
    assert Decimal(product.price) == Decimal("19.95")
    assert product.created_at.seconds > 0


def test_add_product_creates_inventory_record(product_stub, created_product_ids):
    product = add_product(product_stub, created_product_ids, initial_stock=23)

    with SessionLocal() as session:
        inventory = session.get(Inventory, product.id)

    assert inventory is not None
    assert inventory.available_stock == 23


def test_get_existing_product(product_stub, created_product_ids):
    created = add_product(product_stub, created_product_ids)

    response = product_stub.GetProduct(
        product_pb2.GetProductRequest(product_id=created.id), timeout=5
    )

    assert response.product.id == created.id
    assert response.product.name == created.name
    assert Decimal(response.product.price) == Decimal("19.95")


def test_check_available_stock(product_stub, created_product_ids):
    product = add_product(product_stub, created_product_ids, initial_stock=14)

    response = product_stub.CheckStock(
        product_pb2.CheckStockRequest(product_id=product.id), timeout=5
    )

    assert response.product_exists
    assert response.available_stock == 14


def test_update_stock_successfully(product_stub, created_product_ids):
    product = add_product(product_stub, created_product_ids, initial_stock=5)

    response = product_stub.UpdateStock(
        product_pb2.UpdateStockRequest(product_id=product.id, quantity_delta=8),
        timeout=5,
    )

    assert response.available_stock == 13


def test_update_stock_rejects_negative_result(product_stub, created_product_ids):
    product = add_product(product_stub, created_product_ids, initial_stock=3)

    with pytest.raises(grpc.RpcError) as error:
        product_stub.UpdateStock(
            product_pb2.UpdateStockRequest(
                product_id=product.id, quantity_delta=-4
            ),
            timeout=5,
        )

    assert error.value.code() == grpc.StatusCode.FAILED_PRECONDITION
    stock = product_stub.CheckStock(
        product_pb2.CheckStockRequest(product_id=product.id), timeout=5
    )
    assert stock.available_stock == 3


def test_nonexistent_product_is_handled(product_stub):
    product_id = 2147483647

    with pytest.raises(grpc.RpcError) as get_error:
        product_stub.GetProduct(
            product_pb2.GetProductRequest(product_id=product_id), timeout=5
        )
    assert get_error.value.code() == grpc.StatusCode.NOT_FOUND

    stock = product_stub.CheckStock(
        product_pb2.CheckStockRequest(product_id=product_id), timeout=5
    )
    assert not stock.product_exists
    assert stock.available_stock == 0

    with pytest.raises(grpc.RpcError) as update_error:
        product_stub.UpdateStock(
            product_pb2.UpdateStockRequest(
                product_id=product_id, quantity_delta=1
            ),
            timeout=5,
        )
    assert update_error.value.code() == grpc.StatusCode.NOT_FOUND


def test_changes_are_persisted_in_postgresql(product_stub, created_product_ids):
    product = add_product(product_stub, created_product_ids, initial_stock=31)
    product_stub.UpdateStock(
        product_pb2.UpdateStockRequest(product_id=product.id, quantity_delta=4),
        timeout=5,
    )

    with SessionLocal() as session:
        persisted_product = session.scalar(
            select(Product).where(Product.id == product.id)
        )
        persisted_inventory = session.get(Inventory, product.id)

    assert persisted_product is not None
    assert persisted_product.name == product.name
    assert persisted_product.price == Decimal("19.95")
    assert persisted_inventory is not None
    assert persisted_inventory.available_stock == 35


@pytest.mark.parametrize(
    ("name", "price", "initial_stock"),
    [
        (" ", "19.95", 0),
        ("Valid test product", "not-a-price", 0),
        ("Valid test product", "19.95", -1),
    ],
)
def test_add_product_rejects_invalid_input(product_stub, name, price, initial_stock):
    with pytest.raises(grpc.RpcError) as error:
        product_stub.AddProduct(
            product_pb2.AddProductRequest(
                name=name,
                price=price,
                initial_stock=initial_stock,
            ),
            timeout=5,
        )

    assert error.value.code() == grpc.StatusCode.INVALID_ARGUMENT
