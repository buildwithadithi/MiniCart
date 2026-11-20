import logging
from decimal import Decimal, InvalidOperation

import grpc
from google.protobuf.timestamp_pb2 import Timestamp
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from database import SessionLocal
from models import Inventory, Product
from generated import product_pb2, product_pb2_grpc


logger = logging.getLogger(__name__)
_MAX_PRICE = Decimal("10000000000")
_MAX_STOCK = 2147483647


def _product_message(product: Product) -> product_pb2.Product:
    created_at = Timestamp()
    created_at.FromDatetime(product.created_at)
    return product_pb2.Product(
        id=product.id,
        name=product.name,
        description=product.description or "",
        price=str(product.price),
        created_at=created_at,
    )


def _validate_product(request: product_pb2.AddProductRequest) -> Decimal:
    name = request.name.strip()
    if not name:
        raise ValueError("Product name is required.")
    if len(name) > 255:
        raise ValueError("Product name must be at most 255 characters.")
    if request.initial_stock < 0:
        raise ValueError("Initial stock cannot be negative.")
    try:
        price = Decimal(request.price)
    except InvalidOperation as error:
        raise ValueError("Price must be a valid decimal amount.") from error
    if not price.is_finite() or price <= 0 or price >= _MAX_PRICE:
        raise ValueError("Price must be greater than zero and fit NUMERIC(12, 2).")
    try:
        has_excess_precision = price != price.quantize(Decimal("0.01"))
    except InvalidOperation as error:
        raise ValueError("Price must fit NUMERIC(12, 2).") from error
    if has_excess_precision:
        raise ValueError("Price cannot have more than two decimal places.")
    return price


class ProductServiceServicer(product_pb2_grpc.ProductServiceServicer):
    def AddProduct(self, request, context):
        try:
            price = _validate_product(request)
        except ValueError as error:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(error))

        try:
            with SessionLocal.begin() as session:
                product = Product(
                    name=request.name.strip(),
                    description=request.description.strip() or None,
                    price=price,
                )
                session.add(product)
                session.flush()

                inventory = Inventory(
                    product_id=product.id,
                    available_stock=request.initial_stock,
                )
                session.add(inventory)
                session.flush()
                response = product_pb2.AddProductResponse(
                    product=_product_message(product)
                )
        except SQLAlchemyError:
            logger.exception("Database failure while adding a product.")
            context.abort(
                grpc.StatusCode.UNAVAILABLE,
                "The product database is temporarily unavailable.",
            )
        except Exception:
            logger.exception("Unexpected failure while adding a product.")
            context.abort(grpc.StatusCode.INTERNAL, "Unable to add product.")
        return response

    def GetProduct(self, request, context):
        if request.product_id <= 0:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "Product ID must be positive.")

        try:
            with SessionLocal.begin() as session:
                product = session.get(Product, request.product_id)
                response = (
                    product_pb2.GetProductResponse(product=_product_message(product))
                    if product is not None
                    else None
                )
        except SQLAlchemyError:
            logger.exception("Database failure while retrieving a product.")
            context.abort(
                grpc.StatusCode.UNAVAILABLE,
                "The product database is temporarily unavailable.",
            )
        except Exception:
            logger.exception("Unexpected failure while retrieving a product.")
            context.abort(grpc.StatusCode.INTERNAL, "Unable to retrieve product.")

        if response is None:
            context.abort(grpc.StatusCode.NOT_FOUND, "Product was not found.")
        return response

    def CheckStock(self, request, context):
        if request.product_id <= 0:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "Product ID must be positive.")

        try:
            with SessionLocal.begin() as session:
                inventory = session.get(Inventory, request.product_id)
                if inventory is None:
                    response = product_pb2.CheckStockResponse(
                        product_exists=False, available_stock=0
                    )
                else:
                    response = product_pb2.CheckStockResponse(
                        product_exists=True,
                        available_stock=inventory.available_stock,
                    )
        except SQLAlchemyError:
            logger.exception("Database failure while checking product stock.")
            context.abort(
                grpc.StatusCode.UNAVAILABLE,
                "The product database is temporarily unavailable.",
            )
        except Exception:
            logger.exception("Unexpected failure while checking product stock.")
            context.abort(grpc.StatusCode.INTERNAL, "Unable to check product stock.")
        return response

    def UpdateStock(self, request, context):
        if request.product_id <= 0:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "Product ID must be positive.")

        try:
            with SessionLocal.begin() as session:
                inventory = session.scalar(
                    select(Inventory)
                    .where(Inventory.product_id == request.product_id)
                    .with_for_update()
                )
                if inventory is None:
                    response = None
                    failure = (grpc.StatusCode.NOT_FOUND, "Product was not found.")
                else:
                    updated_stock = inventory.available_stock + request.quantity_delta
                    if updated_stock < 0:
                        response = None
                        failure = (
                            grpc.StatusCode.FAILED_PRECONDITION,
                            "Stock cannot be negative.",
                        )
                    elif updated_stock > _MAX_STOCK:
                        response = None
                        failure = (
                            grpc.StatusCode.INVALID_ARGUMENT,
                            "Stock exceeds the supported maximum.",
                        )
                    else:
                        inventory.available_stock = updated_stock
                        session.flush()
                        response = product_pb2.UpdateStockResponse(
                            available_stock=updated_stock
                        )
                        failure = None
        except SQLAlchemyError:
            logger.exception("Database failure while updating product stock.")
            context.abort(
                grpc.StatusCode.UNAVAILABLE,
                "The product database is temporarily unavailable.",
            )
        except Exception:
            logger.exception("Unexpected failure while updating product stock.")
            context.abort(grpc.StatusCode.INTERNAL, "Unable to update product stock.")

        if failure is not None:
            context.abort(*failure)
        return response
