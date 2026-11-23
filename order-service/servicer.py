import logging
import hashlib
import json
import uuid
import re
import time
from decimal import Decimal, InvalidOperation
from typing import NoReturn, Protocol

import grpc
from google.protobuf.timestamp_pb2 import Timestamp
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from clients.product_client import ProductClient
from database import SessionLocal
from models import Order, OrderIdempotency, OrderItem
from generated import order_pb2, order_pb2_grpc, product_pb2
from security import authenticated_user_id
from security_config import JwtVerifier


logger = logging.getLogger(__name__)
_MAX_INT32 = 2147483647
_MAX_AMOUNT = Decimal("9999999999.99")
_MAX_PRICE = Decimal("9999999999.99")


class ProductClientProtocol(Protocol):
    def get_product(self, product_id: int, /) -> product_pb2.Product: ...

    def check_stock(
        self, product_id: int, /
    ) -> product_pb2.CheckStockResponse: ...

    def update_stock(
        self,
        product_id: int,
        quantity_delta: int,
        operation_id: str | None = None,
    ) -> int: ...

    def close(self) -> None: ...


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


_IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _metadata_value(context, key: str) -> str | None:
    for entry in context.invocation_metadata():
        if entry.key.lower() == key.lower():
            return entry.value
    return None


def _create_order_payload_hash(request) -> str:
    payload = {
        "items": [
            {
                "product_id": item.product_id,
                "quantity": item.quantity,
                "unit_price": item.unit_price,
            }
            for item in request.items
        ]
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _order_response_from_json(response_json: str) -> order_pb2.Order:
    payload = json.loads(response_json)
    timestamp = Timestamp()
    timestamp.FromJsonString(payload["created_at"])
    return order_pb2.Order(
        id=payload["id"],
        total_amount=payload["total_amount"],
        status=payload["status"],
        created_at=timestamp,
        items=[
            order_pb2.OrderItem(
                id=item["id"],
                product_id=item["product_id"],
                quantity=item["quantity"],
                unit_price=item["unit_price"],
            )
            for item in payload["items"]
        ],
    )


def _order_response_to_json(order: Order, items: list[OrderItem]) -> str:
    payload = {
        "id": order.id,
        "total_amount": str(order.total_amount),
        "status": order.status,
        "created_at": _timestamp(order.created_at).ToJsonString(),
        "items": [
            {
                "id": item.id,
                "product_id": item.product_id,
                "quantity": item.quantity,
                "unit_price": str(item.unit_price),
            }
            for item in items
        ],
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _order_message(order: Order, items: list[OrderItem]) -> order_pb2.Order:
    return order_pb2.Order(
        id=order.id,
        total_amount=str(order.total_amount),
        status=order.status,
        created_at=_timestamp(order.created_at),
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
) -> NoReturn:
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
        *,
        token_verifier: JwtVerifier | None = None,
        product_client: ProductClientProtocol | None = None,
    ):
        self.token_verifier = (
            token_verifier
            if token_verifier is not None
            else JwtVerifier.from_environment()
        )
        self.product_client = (
            product_client if product_client is not None else ProductClient(product_address)
        )

    def close(self) -> None:
        self.product_client.close()

    def _wait_for_completed_idempotency(
        self,
        idempotency_key: str,
        user_id: int,
    ) -> OrderIdempotency | None:
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            try:
                with SessionLocal.begin() as session:
                    record = session.scalar(
                        select(OrderIdempotency).where(
                            OrderIdempotency.user_id == user_id,
                            OrderIdempotency.idempotency_key == idempotency_key,
                        )
                    )
                    if record is not None and record.status == "COMPLETED":
                        session.expunge(record)
                        return record
                    if record is not None and record.status == "FAILED":
                        session.expunge(record)
                        return record
                    if record is None:
                        return None
            except SQLAlchemyError:
                return None
            time.sleep(0.05)
        return None

    def _reserve_idempotency(
        self,
        idempotency_key: str,
        user_id: int,
        payload_hash: str,
        context,
    ) -> order_pb2.CreateOrderResponse | None:
        existing_response = None
        existing_processing = False
        try:
            with SessionLocal.begin() as session:
                existing = session.scalar(
                    select(OrderIdempotency)
                    .where(
                        OrderIdempotency.user_id == user_id,
                        OrderIdempotency.idempotency_key == idempotency_key,
                    )
                )
                if existing is not None:
                    if existing.request_payload_hash != payload_hash:
                        context.abort(
                            grpc.StatusCode.ALREADY_EXISTS,
                            "Idempotency key was already used with a different request.",
                        )
                    if existing.status == "COMPLETED" and existing.response_json:
                        existing_response = existing
                        session.expunge(existing)
                    elif existing.status == "COMPLETED":
                        context.abort(
                            grpc.StatusCode.INTERNAL,
                            "Stored idempotency response is incomplete.",
                        )
                    elif existing.status == "PROCESSING":
                        existing_processing = True
                    elif existing.status == "FAILED":
                        context.abort(
                            grpc.StatusCode.FAILED_PRECONDITION,
                            "A previous request with this key requires reconciliation.",
                        )
                    else:
                        context.abort(
                            grpc.StatusCode.FAILED_PRECONDITION,
                            "The request with this idempotency key cannot be replayed.",
                        )
                else:
                    session.add(
                        OrderIdempotency(
                            idempotency_key=idempotency_key,
                            user_id=user_id,
                            request_payload_hash=payload_hash,
                            status="PROCESSING",
                        )
                    )
        except IntegrityError:
            try:
                with SessionLocal.begin() as session:
                    existing = session.scalar(
                        select(OrderIdempotency).where(
                            OrderIdempotency.user_id == user_id,
                            OrderIdempotency.idempotency_key == idempotency_key,
                        )
                    )
                    if existing is None:
                        context.abort(
                            grpc.StatusCode.INTERNAL,
                            "Unable to resolve duplicate idempotency key.",
                        )
                    if existing.request_payload_hash != payload_hash:
                        context.abort(
                            grpc.StatusCode.ALREADY_EXISTS,
                            "Idempotency key was already used with a different request.",
                        )
                    if existing.status == "COMPLETED" and existing.response_json:
                        existing_response = existing
                    elif existing.status == "COMPLETED":
                        context.abort(
                            grpc.StatusCode.INTERNAL,
                            "Stored idempotency response is incomplete.",
                        )
                    elif existing.status == "FAILED":
                        context.abort(
                            grpc.StatusCode.FAILED_PRECONDITION,
                            "A previous request with this key requires reconciliation.",
                        )
                    elif existing.status == "PROCESSING":
                        existing_processing = True
                    else:
                        context.abort(
                            grpc.StatusCode.FAILED_PRECONDITION,
                            "The request with this idempotency key cannot be replayed.",
                        )
            except SQLAlchemyError:
                context.abort(
                    grpc.StatusCode.UNAVAILABLE,
                    "The order database is temporarily unavailable.",
                )
        except SQLAlchemyError:
            context.abort(
                grpc.StatusCode.UNAVAILABLE,
                "The order database is temporarily unavailable.",
            )
        if existing_processing:
            completed = self._wait_for_completed_idempotency(
                idempotency_key,
                user_id,
            )
            if completed is not None and completed.status == "FAILED":
                context.abort(
                    grpc.StatusCode.FAILED_PRECONDITION,
                    "A previous request with this key requires reconciliation.",
                )
            if completed is None:
                context.abort(
                    grpc.StatusCode.ALREADY_EXISTS,
                    "A request with this idempotency key is already in progress.",
                )
            existing_response = completed
        if existing_response is not None:
            try:
                response_json = existing_response.response_json
                if response_json is None:
                    context.abort(
                        grpc.StatusCode.INTERNAL,
                        "Stored idempotency response is incomplete.",
                    )
                order = _order_response_from_json(response_json)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                context.abort(
                    grpc.StatusCode.INTERNAL,
                    "Stored idempotency response is invalid.",
                )
            return order_pb2.CreateOrderResponse(order=order)
        return None

    def _confirm_idempotent_order(
        self,
        order_id: int,
        idempotency_key: str,
        user_id: int,
    ) -> order_pb2.CreateOrderResponse | None:
        try:
            with SessionLocal.begin() as session:
                order = session.get(Order, order_id)
                if order is None:
                    return None
                record = session.scalar(
                    select(OrderIdempotency).where(
                        OrderIdempotency.user_id == user_id,
                        OrderIdempotency.idempotency_key == idempotency_key,
                    )
                )
                if record is None:
                    return None
                items = list(
                    session.scalars(
                        select(OrderItem)
                        .where(OrderItem.order_id == order_id)
                        .order_by(OrderItem.id)
                    )
                )
                order.status = "CONFIRMED"
                response = order_pb2.CreateOrderResponse(
                    order=_order_message(order, items)
                )
                record.status = "COMPLETED"
                record.order_id = order_id
                record.response_json = _order_response_to_json(order, items)
        except SQLAlchemyError:
            logger.exception("Database failure confirming idempotent order.")
            return None
        return response

    def _release_idempotency(
        self,
        idempotency_key: str | None,
        user_id: int,
    ) -> None:
        if idempotency_key is None:
            return
        try:
            with SessionLocal.begin() as session:
                record = session.scalar(
                    select(OrderIdempotency).where(
                        OrderIdempotency.user_id == user_id,
                        OrderIdempotency.idempotency_key == idempotency_key,
                        OrderIdempotency.status == "PROCESSING",
                    )
                )
                if record is not None:
                    session.delete(record)
        except SQLAlchemyError:
            logger.exception("Database failure releasing order idempotency record.")

    def _mark_idempotency_failed(
        self,
        idempotency_key: str | None,
        user_id: int,
        order_id: int,
    ) -> None:
        if idempotency_key is None:
            return
        try:
            with SessionLocal.begin() as session:
                record = session.scalar(
                    select(OrderIdempotency).where(
                        OrderIdempotency.user_id == user_id,
                        OrderIdempotency.idempotency_key == idempotency_key,
                        OrderIdempotency.status == "PROCESSING",
                    )
                )
                if record is not None:
                    record.status = "FAILED"
                    record.order_id = order_id
        except SQLAlchemyError:
            logger.exception("Database failure marking order idempotency as failed.")

    def CreateOrder(self, request, context):
        user_id = authenticated_user_id(context, self.token_verifier)
        idempotency_key = _metadata_value(context, "idempotency-key")
        payload_hash = _create_order_payload_hash(request)

        if idempotency_key is not None:
            if not _IDEMPOTENCY_KEY_PATTERN.fullmatch(idempotency_key):
                context.abort(
                    grpc.StatusCode.INVALID_ARGUMENT,
                    "Idempotency-Key must be 1-128 characters using letters, digits, '.', '_' or '-'.",
                )
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

        if idempotency_key is not None:
            repeated_response = self._reserve_idempotency(
                idempotency_key,
                user_id,
                payload_hash,
                context,
            )
            if repeated_response is not None:
                return repeated_response

        product_prices: dict[int, Decimal] = {}
        for product_id, quantity in required_stock.items():
            product = None
            try:
                product = self.product_client.get_product(product_id)
            except grpc.RpcError as error:
                self._release_idempotency(idempotency_key, user_id)
                _abort_downstream(context, error, "Product Service")
            if product is None:
                self._release_idempotency(idempotency_key, user_id)
                context.abort(
                    grpc.StatusCode.INTERNAL,
                    "Product Service returned no product result.",
                )

            try:
                product_prices[product_id] = _price_from_product(product.price)
            except ValueError as error:
                self._release_idempotency(idempotency_key, user_id)
                context.abort(grpc.StatusCode.DATA_LOSS, str(error))

            stock = None
            try:
                stock = self.product_client.check_stock(product_id)
            except grpc.RpcError as error:
                self._release_idempotency(idempotency_key, user_id)
                _abort_downstream(context, error, "Product Service")
            if stock is None:
                self._release_idempotency(idempotency_key, user_id)
                context.abort(
                    grpc.StatusCode.INTERNAL,
                    "Product Service returned no stock result.",
                )
            if not stock.product_exists:
                self._release_idempotency(idempotency_key, user_id)
                context.abort(
                    grpc.StatusCode.NOT_FOUND,
                    f"Product {product_id} was not found in inventory.",
                )
            if stock.available_stock < quantity:
                self._release_idempotency(idempotency_key, user_id)
                context.abort(
                    grpc.StatusCode.RESOURCE_EXHAUSTED,
                    f"Product {product_id} has insufficient stock.",
                )

        total_amount = sum(
            (product_prices[item.product_id] * item.quantity for item in request.items),
            start=Decimal("0.00"),
        )
        if total_amount <= 0 or total_amount > _MAX_AMOUNT:
            self._release_idempotency(idempotency_key, user_id)
            context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "Order total is outside the supported monetary range.",
            )

        order_id: int | None = None
        try:
            with SessionLocal.begin() as session:
                order = Order(
                    total_amount=total_amount,
                    status="PENDING",
                    user_id=user_id,
                )
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
                if idempotency_key is not None:
                    idempotency_record = session.scalar(
                        select(OrderIdempotency).where(
                            OrderIdempotency.user_id == user_id,
                            OrderIdempotency.idempotency_key == idempotency_key,
                        )
                    )
                    if idempotency_record is None:
                        raise RuntimeError(
                            "Reserved order idempotency record was not found."
                        )
                    idempotency_record.order_id = order.id
        except SQLAlchemyError:
            self._release_idempotency(idempotency_key, user_id)
            logger.exception("Database failure while creating an order.")
            context.abort(
                grpc.StatusCode.UNAVAILABLE,
                "The order database is temporarily unavailable.",
            )
        except Exception:
            self._release_idempotency(idempotency_key, user_id)
            logger.exception("Unexpected failure while creating an order.")
            context.abort(grpc.StatusCode.INTERNAL, "Unable to create order.")

        if order_id is None:
            context.abort(
                grpc.StatusCode.INTERNAL,
                "The order database did not return an order ID.",
            )
        assert order_id is not None

        updated_stock: list[tuple[int, int]] = []
        for product_id, quantity in required_stock.items():
            operation_id = str(uuid.uuid4())
            try:
                self.product_client.update_stock(
                    product_id,
                    -quantity,
                    operation_id=operation_id,
                )
            except grpc.RpcError as error:
                rollback_failures = self._restore_inventory(order_id, updated_stock)
                if not self._set_order_status(order_id, "FAILED", context):
                    if rollback_failures:
                        self._mark_idempotency_failed(
                            idempotency_key,
                            user_id,
                            order_id,
                        )
                    else:
                        self._release_idempotency(idempotency_key, user_id)
                    context.abort(
                        grpc.StatusCode.UNAVAILABLE,
                        "The order database is temporarily unavailable.",
                    )
                if rollback_failures:
                    self._mark_idempotency_failed(
                        idempotency_key,
                        user_id,
                        order_id,
                    )
                    context.abort(
                        grpc.StatusCode.INTERNAL,
                        f"Order {order_id} failed and inventory rollback failed "
                        f"for products {rollback_failures}.",
                    )
                self._release_idempotency(idempotency_key, user_id)
                _abort_downstream(
                    context,
                    error,
                    "Product Service inventory update",
                    order_id=order_id,
                )
            updated_stock.append((product_id, quantity))

        if idempotency_key is None:
            if not self._set_order_status(order_id, "CONFIRMED", context):
                rollback_failures = self._restore_inventory(order_id, updated_stock)
                if not self._set_order_status(order_id, "FAILED", context):
                    context.abort(
                        grpc.StatusCode.UNAVAILABLE,
                        "The order database is temporarily unavailable.",
                    )
                if rollback_failures:
                    context.abort(
                        grpc.StatusCode.INTERNAL,
                        f"Order {order_id} could not be confirmed and inventory rollback "
                        f"failed for products {rollback_failures}.",
                    )
                context.abort(
                    grpc.StatusCode.UNAVAILABLE,
                    f"Order {order_id} could not be confirmed; inventory was restored.",
                )
            response = self._get_order_response(order_id, user_id, context)
        else:
            response = self._confirm_idempotent_order(
                order_id,
                idempotency_key,
                user_id,
            )
            if response is None:
                rollback_failures = self._restore_inventory(order_id, updated_stock)
                self._set_order_status(order_id, "FAILED", context)
                if rollback_failures:
                    self._mark_idempotency_failed(
                        idempotency_key,
                        user_id,
                        order_id,
                    )
                    context.abort(
                        grpc.StatusCode.INTERNAL,
                        f"Order {order_id} could not be confirmed and inventory rollback "
                        f"failed for products {rollback_failures}.",
                    )
                self._release_idempotency(idempotency_key, user_id)
                context.abort(
                    grpc.StatusCode.UNAVAILABLE,
                    f"Order {order_id} could not be confirmed; inventory was restored.",
                )
        return response

    def GetOrder(self, request, context):
        user_id = authenticated_user_id(context, self.token_verifier)
        if request.order_id <= 0:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "Order ID must be positive.")
        return self._get_order_response(request.order_id, user_id, context)

    @staticmethod
    def _set_order_status(order_id: int, status: str, context) -> bool:
        try:
            with SessionLocal.begin() as session:
                order = session.get(Order, order_id)
                if order is None:
                    context.abort(
                        grpc.StatusCode.NOT_FOUND,
                        f"Order {order_id} was not found.",
                    )
                if order is not None:
                    order.status = status
        except SQLAlchemyError:
            logger.exception("Database failure while updating order %s.", order_id)
            return False
        return True

    def _restore_inventory(
        self, order_id: int, updated_stock: list[tuple[int, int]]
    ) -> list[int]:
        failed_product_ids = []
        for product_id, quantity in reversed(updated_stock):
            try:
                self.product_client.update_stock(
                    product_id,
                    quantity,
                    operation_id=str(uuid.uuid4()),
                )
            except grpc.RpcError:
                logger.exception(
                    "Order %s inventory rollback failed for product %s.",
                    order_id,
                    product_id,
                )
                failed_product_ids.append(product_id)
        return failed_product_ids

    @staticmethod
    def _get_order_response(order_id: int, user_id: int, context):
        response = None
        try:
            with SessionLocal.begin() as session:
                order = session.scalar(
                    select(Order).where(
                        Order.id == order_id,
                        Order.user_id == user_id,
                    )
                )
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
