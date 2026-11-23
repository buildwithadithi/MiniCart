import os
from pathlib import Path

import grpc
from dotenv import load_dotenv

from generated import order_pb2, order_pb2_grpc
from generated.grpc_retry import retry_grpc_call


PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")


class OrderServiceClient:
    def __init__(self) -> None:
        host = os.getenv("ORDER_SERVICE_HOST", "localhost")
        port = os.getenv("ORDER_SERVICE_PORT", "50052")
        self.address = os.getenv("ORDER_SERVICE_ADDRESS", f"{host}:{port}")
        self.timeout = float(os.getenv("ORDER_SERVICE_TIMEOUT_SECONDS", "3"))
        if self.timeout <= 0:
            raise ValueError("ORDER_SERVICE_TIMEOUT_SECONDS must be positive.")

        self._channel = grpc.insecure_channel(self.address)
        self._stub = order_pb2_grpc.OrderServiceStub(self._channel)

    def create_order(
        self,
        request: order_pb2.CreateOrderRequest,
        authorization_token: str,
        idempotency_key: str | None = None,
    ) -> order_pb2.CreateOrderResponse:
        metadata = (("authorization", f"Bearer {authorization_token}"),)
        if idempotency_key is not None:
            metadata += (("idempotency-key", idempotency_key),)
        return self._stub.CreateOrder(
            request,
            timeout=self.timeout,
            metadata=metadata,
        )

    def get_order(
        self, order_id: int, authorization_token: str
    ) -> order_pb2.GetOrderResponse:
        return retry_grpc_call(
            lambda timeout_seconds: self._stub.GetOrder(
                order_pb2.GetOrderRequest(order_id=order_id),
                timeout=timeout_seconds,
                metadata=(("authorization", f"Bearer {authorization_token}"),),
            ),
            rpc_name="OrderService.GetOrder",
            timeout_seconds=self.timeout,
        )

    def close(self) -> None:
        self._channel.close()
