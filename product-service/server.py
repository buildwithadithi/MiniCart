import logging
import os
import sys
from concurrent import futures
from pathlib import Path

import grpc


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from generated import product_pb2_grpc
from servicer import ProductServiceServicer


def serve() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    port = int(os.getenv("PRODUCT_SERVICE_PORT", "50051"))
    if not 1 <= port <= 65535:
        raise ValueError("PRODUCT_SERVICE_PORT must be between 1 and 65535.")

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    product_pb2_grpc.add_ProductServiceServicer_to_server(
        ProductServiceServicer(), server
    )
    bound_port = server.add_insecure_port(f"[::]:{port}")
    if bound_port == 0:
        raise RuntimeError(f"Unable to bind Product Service to port {port}.")

    server.start()
    logging.info("Product Service listening on port %s.", bound_port)
    server.wait_for_termination()


if __name__ == "__main__":
    serve()
