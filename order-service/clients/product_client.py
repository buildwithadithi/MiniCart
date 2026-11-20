import os

import grpc

from generated import product_pb2, product_pb2_grpc


class ProductClient:
    def __init__(self, address: str | None = None):
        self.address = address or os.getenv(
            "PRODUCT_SERVICE_ADDRESS",
            f"localhost:{os.getenv('PRODUCT_SERVICE_PORT', '50051')}",
        )
        self.timeout = float(os.getenv("PRODUCT_SERVICE_TIMEOUT_SECONDS", "3"))
        self._channel = grpc.insecure_channel(self.address)
        self._stub = product_pb2_grpc.ProductServiceStub(self._channel)

    def get_product(self, product_id: int) -> product_pb2.Product:
        response = self._stub.GetProduct(
            product_pb2.GetProductRequest(product_id=product_id),
            timeout=self.timeout,
        )
        return response.product

    def check_stock(self, product_id: int) -> product_pb2.CheckStockResponse:
        return self._stub.CheckStock(
            product_pb2.CheckStockRequest(product_id=product_id),
            timeout=self.timeout,
        )

    def update_stock(self, product_id: int, quantity_delta: int) -> int:
        response = self._stub.UpdateStock(
            product_pb2.UpdateStockRequest(
                product_id=product_id,
                quantity_delta=quantity_delta,
            ),
            timeout=self.timeout,
        )
        return response.available_stock

    def close(self) -> None:
        self._channel.close()
