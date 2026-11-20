import logging
from decimal import Decimal, InvalidOperation

import grpc
from google.protobuf.timestamp_pb2 import Timestamp
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from clients.payment_client import PaymentClient
from clients.product_client import ProductClient
from database import SessionLocal
from models import Order, OrderItem
from generated import order_pb2, order_pb2_grpc


logger = logging.getLogger(__name__)
_MAX_INT32 = 2147483647
_MAX_AMOUNT = Decimal("9999999999.99")
_MAX_PRICE = Decimal("9999999999.99")


def _price_from_product(price_text: str) -> Decimal:
    try:
        price = Decimal(price_text)
    except (InvalidOperation, TypeError) as error:
        raise ValueError("Product Service returned an invalid product price.") from error
    if not price.is_finite() or price <= 0 or price > _MAX_PRICE:
        raise ValueError("Product Service returned an invalid product price.")
    try:
        has_excess_precision = price != price.quantize(Decimal("0.01"))
    except InvalidOperation as error:
        raise ValueError("Product Service returned an invalid product price.") from error
    if has_excess_precision:
        raise ValueError("Product Service returned a price with excess precision.")
    return price


def _timestamp(value) -> Timestamp:
    timestamp = Timestamp()
    timestamp.FromDatetime(value)
    return timestamp


def _order_message(order: Order, items: list[OrderItem]) -> order_pb2.Order:
    return order_pb2.Order(
        id=order.id,
        total_amount=str(order.total_amount),
        status=order.status,
        created_at=_timestamp(order.created_at),
        payment_id=order.payment_id or 0,
        payment_status=order.payment_status or "",
        items=[
            order_pb2.OrderItem(
                id=item.id,
                product_id=item.product_id,
                quantity=item.quantity,
                unit_price=str(item.unit_price),
            )
            for item in items
        ],
    )


def _abort_downstream(
    context: grpc.ServicerContext,
    error: grpc.RpcError,
    service_name: str,
    *,
    order_id: int | None = None,
) -> None:
    code = error.code()
    detail = error.details() or "No additional details."
    message = f"{service_name} request failed: {detail}"
    if order_id is not None:
        message = f"Order {order_id}: {message}"

    if code in (
        grpc.StatusCode.INVALID_ARGUMENT,
        grpc.StatusCode.NOT_FOUND,
        grpc.StatusCode.RESOURCE_EXHAUSTED,
        grpc.StatusCode.FAILED_PRECONDITION,
        grpc.StatusCode.UNAVAILABLE,
        grpc.StatusCode.DEADLINE_EXCEEDED,
    ):
        context.abort(code, message)
    context.abort(grpc.StatusCode.INTERNAL, message)


class OrderServiceServicer(order_pb2_grpc.OrderServiceServicer):
    def __init__(
        self,
        product_address: str | None = None,
        payment_address: str | None = None,
    ):
        self.product_client = ProductClient(product_address)
        self.payment_client = PaymentClient(payment_address)

    def close(self) -> None:
        self.product_client.close()
        self.payment_client.close()

    def CreateOrder(self, request, context):
        if not request.items:
            context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "An order must contain at least one item.",
            )

        required_stock: dict[int, int] = {}
        for item in request.items:
            if item.product_id <= 0:
                context.abort(
                    grpc.StatusCode.INVALID_ARGUMENT,
                    "Product IDs must be positive.",
                )
            if item.quantity <= 0:
                context.abort(
                    grpc.StatusCode.INVALID_ARGUMENT,
                    "Product quantities must be positive.",
                )
            quantity = required_stock.get(item.product_id, 0) + item.quantity
            if quantity > _MAX_INT32:
                context.abort(
                    grpc.StatusCode.INVALID_ARGUMENT,
                    "Requested quantity exceeds the supported maximum.",
                )
            required_stock[item.product_id] = quantity

        product_prices: dict[int, Decimal] = {}
        for product_id, quantity in required_stock.items():
            try:
                product = self.product_client.get_product(product_id)
            except grpc.RpcError as error:
                _abort_downstream(context, error, "Product Service")

            try:
                product_prices[product_id] = _price_from_product(product.price)
            except ValueError as error:
                context.abort(grpc.StatusCode.DATA_LOSS, str(error))

            try:
                stock = self.product_client.check_stock(product_id)
            except grpc.RpcError as error:
                _abort_downstream(context, error, "Product Service")
            if not stock.product_exists:
                context.abort(
                    grpc.StatusCode.NOT_FOUND,
                    f"Product {product_id} was not found in inventory.",
                )
            if stock.available_stock < quantity:
                context.abort(
                    grpc.StatusCode.RESOURCE_EXHAUSTED,
                    f"Product {product_id} has insufficient stock.",
                )

        total_amount = sum(
            (product_prices[item.product_id] * item.quantity for item in request.items),
            start=Decimal("0.00"),
        )
        if total_amount <= 0 or total_amount > _MAX_AMOUNT:
            context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "Order total is outside the supported monetary range.",
            )

        try:
            with SessionLocal.begin() as session:
                order = Order(total_amount=total_amount, status="PENDING")
                session.add(order)
                session.flush()
                items = [
                    OrderItem(
                        order_id=order.id,
                        product_id=item.product_id,
                        quantity=item.quantity,
                        unit_price=product_prices[item.product_id],
                    )
                    for item in request.items
                ]
                session.add_all(items)
                session.flush()
                order_id = order.id
        except SQLAlchemyError:
            logger.exception("Database failure while creating an order.")
            context.abort(
                grpc.StatusCode.UNAVAILABLE,
                "The order database is temporarily unavailable.",
            )
        except Exception:
            logger.exception("Unexpected failure while creating an order.")
            context.abort(grpc.StatusCode.INTERNAL, "Unable to create order.")

        try:
            payment = self.payment_client.process_payment(
                order_id=order_id,
                amount=str(total_amount),
            )
        except grpc.RpcError as error:
            self._set_order_status(order_id, "PAYMENT_FAILED", context)
            _abort_downstream(
                context,
                error,
                "Payment Service",
                order_id=order_id,
            )

        if payment.status not in ("SUCCESS", "FAILED") or payment.id <= 0:
            self._set_order_payment(
                order_id,
                "PAYMENT_FAILED",
                payment.id if payment.id > 0 else None,
                payment.status,
                context,
            )
            context.abort(
                grpc.StatusCode.DATA_LOSS,
                f"Payment Service returned an invalid result for order {order_id}.",
            )

        if payment.status == "FAILED":
            self._set_order_payment(
                order_id, "PAYMENT_FAILED", payment.id, payment.status, context
            )
            return self._get_order_response(order_id, context)

        self._set_order_payment(order_id, "PAID", payment.id, payment.status, context)
        for product_id, quantity in required_stock.items():
            try:
                self.product_client.update_stock(product_id, -quantity)
            except grpc.RpcError as error:
                logger.exception(
                    "Order %s is paid, but stock update failed for product %s.",
                    order_id,
                    product_id,
                )
                _abort_downstream(
                    context,
                    error,
                    "Product Service stock update",
                    order_id=order_id,
                )

        return self._get_order_response(order_id, context)

    def GetOrder(self, request, context):
        if request.order_id <= 0:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "Order ID must be positive.")
        return self._get_order_response(request.order_id, context)

    @staticmethod
    def _set_order_status(order_id: int, status: str, context) -> None:
        try:
            with SessionLocal.begin() as session:
                order = session.get(Order, order_id)
                if order is None:
                    context.abort(
                        grpc.StatusCode.NOT_FOUND,
                        f"Order {order_id} was not found.",
                    )
                order.status = status
        except SQLAlchemyError:
            logger.exception("Database failure while updating order %s.", order_id)
            context.abort(
                grpc.StatusCode.UNAVAILABLE,
                "The order database is temporarily unavailable.",
            )

    @staticmethod
    def _set_order_payment(
        order_id: int,
        status: str,
        payment_id: int | None,
        payment_status: str,
        context,
    ) -> None:
        try:
            with SessionLocal.begin() as session:
                order = session.get(Order, order_id)
                if order is None:
                    context.abort(
                        grpc.StatusCode.NOT_FOUND,
                        f"Order {order_id} was not found.",
                    )
                order.status = status
                order.payment_id = payment_id
                order.payment_status = payment_status
        except SQLAlchemyError:
            logger.exception(
                "Database failure while saving payment details for order %s.",
                order_id,
            )
            context.abort(
                grpc.StatusCode.UNAVAILABLE,
                "The order database is temporarily unavailable.",
            )

    @staticmethod
    def _get_order_response(order_id: int, context):
        try:
            with SessionLocal.begin() as session:
                order = session.get(Order, order_id)
                if order is None:
                    response = None
                else:
                    items = list(
                        session.scalars(
                            select(OrderItem)
                            .where(OrderItem.order_id == order_id)
                            .order_by(OrderItem.id)
                        )
                    )
                    response = order_pb2.GetOrderResponse(
                        order=_order_message(order, items)
                    )
        except SQLAlchemyError:
            logger.exception("Database failure while retrieving order %s.", order_id)
            context.abort(
                grpc.StatusCode.UNAVAILABLE,
                "The order database is temporarily unavailable.",
            )
        except Exception:
            logger.exception("Unexpected failure while retrieving order %s.", order_id)
            context.abort(grpc.StatusCode.INTERNAL, "Unable to retrieve order.")

        if response is None:
            context.abort(grpc.StatusCode.NOT_FOUND, "Order was not found.")
        return response
