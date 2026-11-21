import logging
from contextlib import asynccontextmanager
from datetime import timezone
from typing import Callable, TypeVar

import grpc
from fastapi import FastAPI, HTTPException, Path, Request, status

from generated import order_pb2
from gateway.grpc_client import OrderServiceClient
from gateway.schemas import (
    CreateOrderRequest,
    HealthResponse,
    OrderItemResponse,
    OrderResponse,
)


logger = logging.getLogger(__name__)
_T = TypeVar("_T")
_MAX_PATH_ORDER_ID = 9223372036854775807


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
        payment_id=order.payment_id,
        payment_status=order.payment_status,
    )


@asynccontextmanager
async def _lifespan(app: FastAPI):
    yield
    app.state.order_client.close()


def create_app(order_client: OrderServiceClient | None = None) -> FastAPI:
    app = FastAPI(
        title="MiniCart API Gateway",
        description=(
            "REST API for MiniCart orders. Order creation and retrieval are "
            "delegated to the Order Service over gRPC."
        ),
        version="1.0.0",
        lifespan=_lifespan,
    )
    app.state.order_client = (
        order_client if order_client is not None else OrderServiceClient()
    )

    @app.get(
        "/health",
        response_model=HealthResponse,
        summary="Check API Gateway status",
        description="Confirms that the API Gateway process is responding.",
    )
    def health() -> HealthResponse:
        return HealthResponse(status="ok")

    @app.post(
        "/orders",
        response_model=OrderResponse,
        status_code=status.HTTP_201_CREATED,
        summary="Create an order",
        description=(
            "Creates an order through the Order Service. Product prices and "
            "payment processing are handled by downstream services."
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
        body: CreateOrderRequest, request: Request
    ) -> OrderResponse:
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
                request.app.state.order_client.create_order(protobuf_request).order
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
    ) -> OrderResponse:
        return _call_order_service(
            lambda: _order_response(
                request.app.state.order_client.get_order(order_id).order
            )
        )

    return app


app = create_app()
