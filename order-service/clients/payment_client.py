import os

import grpc

from generated import payment_pb2, payment_pb2_grpc


class PaymentClient:
    def __init__(self, address: str | None = None):
        self.address = address or os.getenv(
            "PAYMENT_SERVICE_ADDRESS",
            f"localhost:{os.getenv('PAYMENT_SERVICE_PORT', '50053')}",
        )
        self.timeout = float(os.getenv("PAYMENT_SERVICE_TIMEOUT_SECONDS", "3"))
        self._channel = grpc.insecure_channel(self.address)
        self._stub = payment_pb2_grpc.PaymentServiceStub(self._channel)

    def process_payment(
        self, order_id: int, amount: str
    ) -> payment_pb2.Payment:
        response = self._stub.ProcessPayment(
            payment_pb2.ProcessPaymentRequest(order_id=order_id, amount=amount),
            timeout=self.timeout,
        )
        return response.payment

    def close(self) -> None:
        self._channel.close()
