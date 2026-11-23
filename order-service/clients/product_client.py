import os
import uuid

import grpc

from generated import product_pb2, product_pb2_grpc
from generated.grpc_retry import retry_grpc_call


class ProductClient:
    def __init__(self, address: str | None = None):
        self.address = address or os.getenv(
            "PRODUCT_SERVICE_ADDRESS",
            f"localhost:{os.getenv('PRODUCT_SERVICE_PORT', '50051')}",
        )
        self.timeout = float(os.getenv("PRODUCT_SERVICE_TIMEOUT_SECONDS", "3"))
        if self.timeout <= 0:
            raise ValueError("PRODUCT_SERVICE_TIMEOUT_SECONDS must be positive.")
        self._channel = grpc.insecure_channel(self.address)
        self._stub = product_pb2_grpc.ProductServiceStub(self._channel)

    def get_product(self, product_id: int) -> product_pb2.Product:
        response = retry_grpc_call(
            lambda timeout_seconds: self._stub.GetProduct(
                product_pb2.GetProductRequest(product_id=product_id),
                timeout=timeout_seconds,
            ),
            rpc_name="ProductService.GetProduct",
            timeout_seconds=self.timeout,
        )
        return response.product

    def check_stock(self, product_id: int) -> product_pb2.CheckStockResponse:
        return retry_grpc_call(
            lambda timeout_seconds: self._stub.CheckStock(
                product_pb2.CheckStockRequest(product_id=product_id),
                timeout=timeout_seconds,
            ),
            rpc_name="ProductService.CheckStock",
            timeout_seconds=self.timeout,
        )

    def update_stock(
        self,
        product_id: int,
        quantity_delta: int,
        operation_id: str | None = None,
    ) -> int:
        stable_operation_id = operation_id or str(uuid.uuid4())
        request = product_pb2.UpdateStockRequest(
            product_id=product_id,
            quantity_delta=quantity_delta,
            operation_id=stable_operation_id,
        )
        response = retry_grpc_call(
            lambda timeout_seconds: self._stub.UpdateStock(
                request,
                timeout=timeout_seconds,
            ),
            rpc_name="ProductService.UpdateStock",
            timeout_seconds=self.timeout,
        )
        return response.available_stock

    def close(self) -> None:
        self._channel.close()
