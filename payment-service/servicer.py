import logging
from decimal import Decimal, InvalidOperation

import grpc
from google.protobuf.timestamp_pb2 import Timestamp
from sqlalchemy.exc import SQLAlchemyError

from database import SessionLocal
from models import Payment
from generated import payment_pb2, payment_pb2_grpc


logger = logging.getLogger(__name__)
_MAX_AMOUNT = Decimal("10000000000")


def _payment_message(payment: Payment) -> payment_pb2.Payment:
    created_at = Timestamp()
    created_at.FromDatetime(payment.created_at)
    return payment_pb2.Payment(
        id=payment.id,
        order_id=payment.order_id,
        amount=str(payment.amount),
        status=payment.status,
        created_at=created_at,
    )


def _validate_request(order_id: int, amount_text: str) -> Decimal:
    if order_id <= 0:
        raise ValueError("Order ID must be positive.")
    try:
        amount = Decimal(amount_text.strip())
    except (InvalidOperation, AttributeError) as error:
        raise ValueError("Payment amount must be a valid decimal amount.") from error
    if not amount.is_finite() or amount <= 0 or amount >= _MAX_AMOUNT:
        raise ValueError("Payment amount must be greater than zero and fit NUMERIC(12, 2).")
    try:
        has_excess_precision = amount != amount.quantize(Decimal("0.01"))
    except InvalidOperation as error:
        raise ValueError("Payment amount must fit NUMERIC(12, 2).") from error
    if has_excess_precision:
        raise ValueError("Payment amount cannot have more than two decimal places.")
    return amount


class PaymentServiceServicer(payment_pb2_grpc.PaymentServiceServicer):
    def ProcessPayment(self, request, context):
        try:
            amount = _validate_request(request.order_id, request.amount)
        except ValueError as error:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(error))

        status = "SUCCESS" if request.order_id % 2 == 0 else "FAILED"

        try:
            with SessionLocal.begin() as session:
                payment = Payment(
                    order_id=request.order_id,
                    amount=amount,
                    status=status,
                )
                session.add(payment)
                session.flush()
                response = payment_pb2.ProcessPaymentResponse(
                    payment=_payment_message(payment)
                )
        except SQLAlchemyError:
            logger.exception("Database failure while processing a payment.")
            context.abort(
                grpc.StatusCode.UNAVAILABLE,
                "The payment database is temporarily unavailable.",
            )
        except Exception:
            logger.exception("Unexpected failure while processing a payment.")
            context.abort(grpc.StatusCode.INTERNAL, "Unable to process payment.")
        return response

    def GetPaymentStatus(self, request, context):
        if request.payment_id <= 0:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "Payment ID must be positive.")

        try:
            with SessionLocal.begin() as session:
                payment = session.get(Payment, request.payment_id)
                response = (
                    payment_pb2.GetPaymentStatusResponse(
                        payment=_payment_message(payment)
                    )
                    if payment is not None
                    else None
                )
        except SQLAlchemyError:
            logger.exception("Database failure while retrieving payment status.")
            context.abort(
                grpc.StatusCode.UNAVAILABLE,
                "The payment database is temporarily unavailable.",
            )
        except Exception:
            logger.exception("Unexpected failure while retrieving payment status.")
            context.abort(grpc.StatusCode.INTERNAL, "Unable to retrieve payment status.")

        if response is None:
            context.abort(grpc.StatusCode.NOT_FOUND, "Payment was not found.")
        return response
