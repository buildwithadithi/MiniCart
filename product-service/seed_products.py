import sys
from pathlib import Path

import grpc
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SERVICE_ROOT = Path(__file__).resolve().parent
for root in (str(PROJECT_ROOT), str(SERVICE_ROOT)):
    if root not in sys.path:
        sys.path.insert(0, root)

from database import SessionLocal
from generated import product_pb2, product_pb2_grpc
from models import Product


PRODUCTS = [
    {
        "name": "MacBook Air M3",
        "description": "13-inch Apple laptop with M3 chip, 16 GB RAM, and 256 GB SSD.",
        "price": "89999.00",
        "initial_stock": 25,
    },
    {
        "name": "Samsung Galaxy S24",
        "description": "5G smartphone with a 6.2-inch display and 128 GB storage.",
        "price": "64999.00",
        "initial_stock": 40,
    },
    {
        "name": "Sony WH-1000XM5",
        "description": "Wireless noise-cancelling over-ear headphones with 30-hour battery life.",
        "price": "24999.00",
        "initial_stock": 35,
    },
    {
        "name": "Apple iPad Air",
        "description": "11-inch tablet with M2 chip and 128 GB storage.",
        "price": "59999.00",
        "initial_stock": 30,
    },
    {
        "name": "Logitech MX Master 3S",
        "description": "Ergonomic wireless mouse with quiet clicks and multi-device pairing.",
        "price": "8499.00",
        "initial_stock": 50,
    },
    {
        "name": "Nike Air Force 1",
        "description": "Classic low-top leather sneakers for everyday wear.",
        "price": "7499.00",
        "initial_stock": 60,
    },
    {
        "name": "Levi's 511 Slim Jeans",
        "description": "Stretch-denim slim-fit jeans in a versatile indigo wash.",
        "price": "3499.00",
        "initial_stock": 75,
    },
    {
        "name": "Philips Air Fryer",
        "description": "4.1-litre digital air fryer with Rapid Air technology.",
        "price": "8999.00",
        "initial_stock": 45,
    },
    {
        "name": "Samsung 55-inch QLED TV",
        "description": "55-inch 4K QLED smart television with HDR support.",
        "price": "57999.00",
        "initial_stock": 20,
    },
    {
        "name": "Dyson V12 Detect Slim",
        "description": "Cordless vacuum cleaner with laser dust detection and multiple attachments.",
        "price": "45900.00",
        "initial_stock": 18,
    },
    {
        "name": "boAt Airdopes 141",
        "description": "True wireless earbuds with low-latency gaming mode and 42-hour playback.",
        "price": "1499.00",
        "initial_stock": 100,
    },
    {
        "name": "Apple Watch SE",
        "description": "40 mm GPS smartwatch with fitness tracking and crash detection.",
        "price": "29900.00",
        "initial_stock": 38,
    },
    {
        "name": "Fossil Men's Chronograph Watch",
        "description": "Stainless-steel chronograph watch with a classic three-dial face.",
        "price": "9995.00",
        "initial_stock": 32,
    },
    {
        "name": "Puma Unisex Backpack",
        "description": "Durable 22-litre backpack with a padded laptop compartment.",
        "price": "2199.00",
        "initial_stock": 55,
    },
    {
        "name": "Canon PIXMA G3020",
        "description": "All-in-one refillable ink-tank printer for high-volume home and office use.",
        "price": "18999.00",
        "initial_stock": 24,
    },
]


def load_existing_product_names() -> set[str]:
    with SessionLocal() as session:
        names = session.scalars(select(Product.name)).all()
    return {name.strip().casefold() for name in names if name and name.strip()}


def main() -> int:
    address = "localhost:50051"
    channel = grpc.insecure_channel(address)
    created = 0
    skipped = 0
    failed = 0

    try:
        grpc.channel_ready_future(channel).result(timeout=5)
    except grpc.FutureTimeoutError:
        print(
            f"FAILURE: Product Service is not reachable at {address}. "
            "Start it before running this script."
        )
        channel.close()
        return 1
    except grpc.RpcError as error:
        print(
            f"FAILURE: Could not connect to Product Service at {address}: "
            f"{error.details() if hasattr(error, 'details') else error}"
        )
        channel.close()
        return 1

    try:
        existing_names = load_existing_product_names()
    except (SQLAlchemyError, RuntimeError) as error:
        print(f"FAILURE: Could not read existing products from PostgreSQL: {error}")
        channel.close()
        return 1

    stub = product_pb2_grpc.ProductServiceStub(channel)

    for product in PRODUCTS:
        normalized_name = product["name"].strip().casefold()
        if normalized_name in existing_names:
            print(f"SKIPPED: {product['name']} already exists.")
            skipped += 1
            continue

        request = product_pb2.AddProductRequest(
            name=product["name"],
            description=product["description"],
            price=product["price"],
            initial_stock=product["initial_stock"],
        )

        try:
            response = stub.AddProduct(request, timeout=5)
            created += 1
            product_id = response.product.id if response.HasField("product") else "unknown"
            print(
                f"CREATED: {product['name']} "
                f"(ID: {product_id}, stock: {product['initial_stock']})."
            )
            existing_names.add(normalized_name)
        except grpc.RpcError as error:
            failed += 1
            code = error.code().name if error.code() is not None else "UNKNOWN"
            details = error.details() or str(error)
            print(
                f"FAILED: {product['name']} "
                f"(gRPC {code}: {details})"
            )

    channel.close()
    print("\nSeeding summary")
    print(f"Created: {created}")
    print(f"Skipped: {skipped}")
    print(f"Failed: {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
