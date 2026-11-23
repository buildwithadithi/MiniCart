import logging
import re
from contextlib import asynccontextmanager
from datetime import timezone
from typing import Callable, Protocol, TypeVar

import grpc
import jwt
from fastapi import Depends, FastAPI, Header, HTTPException, Path, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from generated import auth_pb2
from generated import order_pb2
from gateway.auth_client import AuthServiceClient
from gateway.grpc_client import OrderServiceClient
from gateway.security import JwtVerifier, VerifiedBearer
from gateway.schemas import (
    CreateOrderRequest,
    HealthResponse,
    LoginRequest,
    LoginResponse,
    OrderItemResponse,
    OrderResponse,
    RegisterRequest,
    RegisterResponse,
)


logger = logging.getLogger(__name__)
_T = TypeVar("_T")
_MAX_PATH_ORDER_ID = 9223372036854775807
_bearer_scheme = HTTPBearer(auto_error=False)
_IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class OrderClientProtocol(Protocol):
    def create_order(
        self,
        request: order_pb2.CreateOrderRequest,
        authorization_token: str,
        idempotency_key: str | None = None,
    ) -> order_pb2.CreateOrderResponse: ...

    def get_order(
        self, order_id: int, authorization_token: str
    ) -> order_pb2.GetOrderResponse: ...

    def close(self) -> None: ...


class AuthClientProtocol(Protocol):
    def register(
        self, request: auth_pb2.RegisterRequest
    ) -> auth_pb2.RegisterResponse: ...

    def login(self, request: auth_pb2.LoginRequest) -> auth_pb2.LoginResponse: ...

    def close(self) -> None: ...


def _http_error(error: grpc.RpcError) -> HTTPException:
    code = error.code()
    errors = {
        grpc.StatusCode.INVALID_ARGUMENT: (
            status.HTTP_400_BAD_REQUEST,
            "The Order Service rejected the request.",
        ),
        grpc.StatusCode.NOT_FOUND: (
            status.HTTP_404_NOT_FOUND,
            "The requested resource was not found.",
        ),
        grpc.StatusCode.RESOURCE_EXHAUSTED: (
            status.HTTP_409_CONFLICT,
            "The request cannot be completed because a resource is unavailable.",
        ),
        grpc.StatusCode.FAILED_PRECONDITION: (
            status.HTTP_409_CONFLICT,
            "The request conflicts with the current resource state.",
        ),
        grpc.StatusCode.ALREADY_EXISTS: (
            status.HTTP_409_CONFLICT,
            "The requested resource already exists.",
        ),
        grpc.StatusCode.UNAUTHENTICATED: (
            status.HTTP_401_UNAUTHORIZED,
            "The request could not be authenticated.",
        ),
        grpc.StatusCode.PERMISSION_DENIED: (
            status.HTTP_403_FORBIDDEN,
            "The request is not permitted.",
        ),
        grpc.StatusCode.UNAVAILABLE: (
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "The Order Service is unavailable.",
        ),
        grpc.StatusCode.DEADLINE_EXCEEDED: (
            status.HTTP_504_GATEWAY_TIMEOUT,
            "The Order Service request timed out.",
        ),
        grpc.StatusCode.OUT_OF_RANGE: (
            422,
            "The request contains a value outside the supported range.",
        ),
    }
    http_status, detail = errors.get(
        code,
        (
            status.HTTP_502_BAD_GATEWAY,
            "The Order Service could not complete the request.",
        ),
    )
    return HTTPException(status_code=http_status, detail=detail)


def _call_order_service(operation: Callable[[], _T]) -> _T:
    try:
        return operation()
    except grpc.RpcError as error:
        raise _http_error(error) from None
    except Exception as error:
        logger.exception("Unexpected error while handling an Order Service request.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An internal API Gateway error occurred.",
        ) from error


def _call_auth_service(operation: Callable[[], _T]) -> _T:
    try:
        return operation()
    except grpc.RpcError as error:
        errors = {
            grpc.StatusCode.INVALID_ARGUMENT: (
                status.HTTP_400_BAD_REQUEST,
                "The authentication request is invalid.",
            ),
            grpc.StatusCode.ALREADY_EXISTS: (
                status.HTTP_409_CONFLICT,
                "An account with that email address already exists.",
            ),
            grpc.StatusCode.UNAUTHENTICATED: (
                status.HTTP_401_UNAUTHORIZED,
                "Invalid email or password.",
            ),
            grpc.StatusCode.UNAVAILABLE: (
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "The Authentication Service is unavailable.",
            ),
            grpc.StatusCode.DEADLINE_EXCEEDED: (
                status.HTTP_504_GATEWAY_TIMEOUT,
                "The Authentication Service request timed out.",
            ),
        }
        http_status, detail = errors.get(
            error.code(),
            (
                status.HTTP_502_BAD_GATEWAY,
                "The Authentication Service could not complete the request.",
            ),
        )
        raise HTTPException(status_code=http_status, detail=detail) from None
    except Exception as error:
        logger.exception("Unexpected error while handling an Authentication Service request.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An internal API Gateway error occurred.",
        ) from error


def _authenticated_bearer(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> VerifiedBearer:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="A Bearer token is required.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    verifier: JwtVerifier | None = request.app.state.jwt_verifier
    if verifier is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="JWT verification is not configured.",
        )
    try:
        user_id = verifier.verify(credentials.credentials)
    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="The Bearer token is invalid or expired.",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None
    return VerifiedBearer(user_id=user_id, token=credentials.credentials)


def _order_response(order: order_pb2.Order) -> OrderResponse:
    return OrderResponse(
        id=order.id,
        total_amount=order.total_amount,
        status=order.status,
        created_at=order.created_at.ToDatetime(tzinfo=timezone.utc),
        items=[
            OrderItemResponse(
                id=item.id,
                product_id=item.product_id,
                quantity=item.quantity,
                unit_price=item.unit_price,
            )
            for item in order.items
        ],
    )


def create_app(
    order_client: OrderClientProtocol | None = None,
    auth_client: AuthClientProtocol | None = None,
    token_verifier: JwtVerifier | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if app.state.jwt_verifier is None:
            app.state.jwt_verifier = JwtVerifier.from_environment()
        yield
        app.state.order_client.close()
        app.state.auth_client.close()

    app = FastAPI(
        title="MiniCart API Gateway",
        description=(
            "REST authentication is delegated to the Authentication Service. "
            "Protected order operations are verified by the Gateway and "
            "delegated to the Order Service over gRPC."
        ),
        version="1.0.0",
        lifespan=lifespan,
    )
    app.state.order_client = (
        order_client if order_client is not None else OrderServiceClient()
    )
    app.state.auth_client = auth_client if auth_client is not None else AuthServiceClient()
    app.state.jwt_verifier = token_verifier

    @app.get(
        "/health",
        response_model=HealthResponse,
        summary="Check API Gateway status",
        description="Confirms that the API Gateway process is responding.",
    )
    def health() -> HealthResponse:
        return HealthResponse(status="ok")

    @app.post(
        "/auth/register",
        response_model=RegisterResponse,
        status_code=status.HTTP_201_CREATED,
        summary="Register an account",
        description="Creates an account through the Authentication Service.",
        responses={
            400: {"description": "Registration input is invalid."},
            409: {"description": "The email address is already registered."},
            503: {"description": "Authentication Service is unavailable."},
        },
    )
    def register(body: RegisterRequest, request: Request) -> RegisterResponse:
        response = _call_auth_service(
            lambda: request.app.state.auth_client.register(
                auth_pb2.RegisterRequest(
                    name=body.name,
                    email=str(body.email),
                    password=body.password,
                )
            )
        )
        return RegisterResponse(
            id=response.user_id,
            name=response.name,
            email=response.email,
            created_at=response.created_at.ToDatetime(tzinfo=timezone.utc),
        )

    @app.post(
        "/auth/login",
        response_model=LoginResponse,
        summary="Log in",
        description="Verifies credentials through the Authentication Service and returns a JWT.",
        responses={
            401: {"description": "Email or password is invalid."},
            503: {"description": "Authentication Service is unavailable."},
        },
    )
    def login(body: LoginRequest, request: Request) -> LoginResponse:
        response = _call_auth_service(
            lambda: request.app.state.auth_client.login(
                auth_pb2.LoginRequest(
                    email=str(body.email),
                    password=body.password,
                )
            )
        )
        return LoginResponse(
            access_token=response.access_token,
            token_type=response.token_type,
            expires_in=response.expires_in,
        )

    @app.post(
        "/orders",
        response_model=OrderResponse,
        status_code=status.HTTP_201_CREATED,
        summary="Create an order",
        description=(
            "Creates an order through the Order Service. Product prices and "
            "inventory are handled by downstream services."
        ),
        responses={
            400: {"description": "Invalid request rejected by the Order Service."},
            409: {"description": "Insufficient resources or conflicting state."},
            422: {"description": "Request validation failed."},
            503: {"description": "Order Service is unavailable."},
            504: {"description": "Order Service timed out."},
        },
    )
    def create_order(
        body: CreateOrderRequest,
        request: Request,
        principal: VerifiedBearer = Depends(_authenticated_bearer),
        idempotency_key: str = Header(alias="Idempotency-Key"),
    ) -> OrderResponse:
        if not _IDEMPOTENCY_KEY_PATTERN.fullmatch(idempotency_key):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Idempotency-Key must be 1-128 characters using letters, digits, '.', '_' or '-'.",
            )
        protobuf_request = order_pb2.CreateOrderRequest(
            items=[
                order_pb2.OrderItemInput(
                    product_id=item.product_id,
                    quantity=item.quantity,
                )
                for item in body.items
            ]
        )
        return _call_order_service(
            lambda: _order_response(
                request.app.state.order_client.create_order(
                    protobuf_request,
                    principal.token,
                    idempotency_key,
                ).order
            )
        )

    @app.get(
        "/orders/{order_id}",
        response_model=OrderResponse,
        summary="Get an order",
        description="Retrieves an order and its items by order ID.",
        responses={
            404: {"description": "Order was not found."},
            422: {"description": "Order ID must be a positive integer."},
            503: {"description": "Order Service is unavailable."},
            504: {"description": "Order Service timed out."},
        },
    )
    def get_order(
        request: Request,
        order_id: int = Path(gt=0, le=_MAX_PATH_ORDER_ID),
        principal: VerifiedBearer = Depends(_authenticated_bearer),
    ) -> OrderResponse:
        return _call_order_service(
            lambda: _order_response(
                request.app.state.order_client.get_order(
                    order_id, principal.token
                ).order
            )
        )

    return app


app = create_app()
