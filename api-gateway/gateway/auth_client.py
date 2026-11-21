import os
from pathlib import Path

import grpc
from dotenv import load_dotenv

from generated import auth_pb2, auth_pb2_grpc


PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")


class AuthServiceClient:
    def __init__(self) -> None:
        host = os.getenv("AUTH_SERVICE_HOST", "localhost")
        port = os.getenv("AUTH_SERVICE_PORT", "50054")
        self.address = os.getenv("AUTH_SERVICE_ADDRESS", f"{host}:{port}")
        self.timeout = float(os.getenv("AUTH_SERVICE_TIMEOUT_SECONDS", "5"))
        if self.timeout <= 0:
            raise ValueError("AUTH_SERVICE_TIMEOUT_SECONDS must be positive.")
        self._channel = grpc.insecure_channel(self.address)
        self._stub = auth_pb2_grpc.AuthServiceStub(self._channel)

    def register(
        self, request: auth_pb2.RegisterRequest
    ) -> auth_pb2.RegisterResponse:
        return self._stub.Register(request, timeout=self.timeout)

    def login(self, request: auth_pb2.LoginRequest) -> auth_pb2.LoginResponse:
        return self._stub.Login(request, timeout=self.timeout)

    def close(self) -> None:
        self._channel.close()
