from decimal import Decimal

import grpc
import pytest

from database import SessionLocal
from models import Payment
from generated import payment_pb2


def process_payment(payment_stub, created_payment_ids, order_id, amount):
    response = payment_stub.ProcessPayment(
        payment_pb2.ProcessPaymentRequest(
            order_id=order_id,
            amount=amount,
        ),
        timeout=5,
    )
    created_payment_ids.append(response.payment.id)
    return response.payment


def test_successful_payment_processing(payment_stub, created_payment_ids):
    payment = process_payment(payment_stub, created_payment_ids, 200002, "49.95")

    assert payment.id > 0
    assert payment.order_id == 200002
    assert Decimal(payment.amount) == Decimal("49.95")
    assert payment.status == "SUCCESS"
    assert payment.created_at.seconds > 0


def test_failed_payment_processing(payment_stub, created_payment_ids):
    payment = process_payment(payment_stub, created_payment_ids, 200001, "25.00")

    assert payment.status == "FAILED"
    assert payment.order_id == 200001


def test_payment_record_saved_in_postgresql(payment_stub, created_payment_ids):
    payment = process_payment(payment_stub, created_payment_ids, 200004, "75.50")

    with SessionLocal() as session:
        persisted = session.get(Payment, payment.id)

    assert persisted is not None
    assert persisted.order_id == 200004
    assert persisted.amount == Decimal("75.50")
    assert persisted.status == "SUCCESS"


def test_payment_status_is_persisted(payment_stub, created_payment_ids):
    created = process_payment(payment_stub, created_payment_ids, 200003, "12.25")

    status = payment_stub.GetPaymentStatus(
        payment_pb2.GetPaymentStatusRequest(payment_id=created.id),
        timeout=5,
    )

    assert status.payment.id == created.id
    assert status.payment.status == "FAILED"
    assert Decimal(status.payment.amount) == Decimal("12.25")


@pytest.mark.parametrize("amount", ["0", "-1.00", "not-a-number", "1.123"])
def test_invalid_payment_amount(payment_stub, amount):
    with pytest.raises(grpc.RpcError) as error:
        payment_stub.ProcessPayment(
            payment_pb2.ProcessPaymentRequest(order_id=200010, amount=amount),
            timeout=5,
        )

    assert error.value.code() == grpc.StatusCode.INVALID_ARGUMENT


@pytest.mark.parametrize("order_id", [0, -1])
def test_invalid_order_id(payment_stub, order_id):
    with pytest.raises(grpc.RpcError) as error:
        payment_stub.ProcessPayment(
            payment_pb2.ProcessPaymentRequest(order_id=order_id, amount="10.00"),
            timeout=5,
        )

    assert error.value.code() == grpc.StatusCode.INVALID_ARGUMENT


def test_retrieving_payment_details(payment_stub, created_payment_ids):
    created = process_payment(payment_stub, created_payment_ids, 200008, "33.30")

    response = payment_stub.GetPaymentStatus(
        payment_pb2.GetPaymentStatusRequest(payment_id=created.id),
        timeout=5,
    )

    assert response.payment.id == created.id
    assert response.payment.order_id == 200008
    assert response.payment.status == "SUCCESS"


def test_database_persistence_after_processing(payment_stub, created_payment_ids):
    created = process_payment(payment_stub, created_payment_ids, 200009, "88.88")

    response = payment_stub.GetPaymentStatus(
        payment_pb2.GetPaymentStatusRequest(payment_id=created.id),
        timeout=5,
    )

    with SessionLocal() as session:
        persisted = session.get(Payment, response.payment.id)

    assert persisted is not None
    assert persisted.status == "FAILED"


def test_missing_payment_returns_not_found(payment_stub):
    with pytest.raises(grpc.RpcError) as error:
        payment_stub.GetPaymentStatus(
            payment_pb2.GetPaymentStatusRequest(payment_id=2147483647),
            timeout=5,
        )

    assert error.value.code() == grpc.StatusCode.NOT_FOUND
