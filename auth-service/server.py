import logging
import os
import sys
from concurrent import futures
from pathlib import Path

import grpc


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SERVICE_ROOT = Path(__file__).resolve().parent
for path in (str(PROJECT_ROOT), str(SERVICE_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from generated import auth_pb2_grpc
from servicer import AuthServiceServicer


def serve() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    port = int(os.getenv("AUTH_SERVICE_PORT", "50054"))
    if not 1 <= port <= 65535:
        raise ValueError("AUTH_SERVICE_PORT must be between 1 and 65535.")

    servicer = AuthServiceServicer()
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    auth_pb2_grpc.add_AuthServiceServicer_to_server(servicer, server)
    bound_port = server.add_insecure_port(f"[::]:{port}")
    if bound_port == 0:
        raise RuntimeError(f"Unable to bind Authentication Service to port {port}.")

    server.start()
    logging.info("Authentication Service listening on port %s.", bound_port)
    try:
        server.wait_for_termination()
    finally:
        server.stop(grace=5).wait()


if __name__ == "__main__":
    serve()
