import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import TypeVar

import grpc


logger = logging.getLogger(__name__)
_T = TypeVar("_T")


class CircuitState(str, Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitBreakerOpenError(grpc.RpcError):
    def code(self) -> grpc.StatusCode:
        return grpc.StatusCode.UNAVAILABLE

    def details(self) -> str:
        return "Circuit breaker is open."


@dataclass(frozen=True)
class CircuitBreakerConfig:
    failure_threshold: int
    recovery_timeout_seconds: float
    half_open_max_calls: int

    def __post_init__(self) -> None:
        if self.failure_threshold <= 0:
            raise ValueError("failure_threshold must be positive.")
        if self.recovery_timeout_seconds < 0:
            raise ValueError(
                "recovery_timeout_seconds must not be negative."
            )
        if self.half_open_max_calls <= 0:
            raise ValueError("half_open_max_calls must be positive.")

    @classmethod
    def from_environment(
        cls,
        prefix: str,
        *,
        failure_threshold: int = 5,
        recovery_timeout_seconds: float = 15.0,
        half_open_max_calls: int = 2,
    ) -> "CircuitBreakerConfig":
        threshold = int(
            os.getenv(
                f"{prefix}_FAILURE_THRESHOLD",
                str(failure_threshold),
            )
        )
        recovery = float(
            os.getenv(
                f"{prefix}_RECOVERY_TIMEOUT_SECONDS",
                str(recovery_timeout_seconds),
            )
        )
        half_open = int(
            os.getenv(
                f"{prefix}_HALF_OPEN_MAX_CALLS",
                str(half_open_max_calls),
            )
        )
        if threshold <= 0:
            raise ValueError(f"{prefix}_FAILURE_THRESHOLD must be positive.")
        if recovery < 0:
            raise ValueError(
                f"{prefix}_RECOVERY_TIMEOUT_SECONDS must not be negative."
            )
        if half_open <= 0:
            raise ValueError(f"{prefix}_HALF_OPEN_MAX_CALLS must be positive.")
        return cls(
            failure_threshold=threshold,
            recovery_timeout_seconds=recovery,
            half_open_max_calls=half_open,
        )


class CircuitBreaker:
    def __init__(
        self,
        config: CircuitBreakerConfig,
        *,
        name: str,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.name = name
        self._clock = clock
        self._lock = threading.Lock()
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._opened_at: float | None = None
        self._half_open_calls = 0

    @property
    def state(self) -> CircuitState:
        with self._lock:
            self._refresh_open_state()
            return self._state

    def _refresh_open_state(self) -> None:
        if self._state != CircuitState.OPEN:
            return
        if self._opened_at is None:
            return
        if self._clock() - self._opened_at >= self.config.recovery_timeout_seconds:
            self._state = CircuitState.HALF_OPEN
            self._half_open_calls = 0
            logger.info(
                "Circuit breaker entered half-open state",
                extra={"circuit": self.name},
            )

    def execute(
        self,
        operation: Callable[[], _T],
        *,
        failure_predicate: Callable[[grpc.RpcError], bool] | None = None,
    ) -> _T:
        with self._lock:
            self._refresh_open_state()
            if self._state == CircuitState.OPEN:
                raise CircuitBreakerOpenError()
            if self._state == CircuitState.HALF_OPEN:
                if self._half_open_calls >= self.config.half_open_max_calls:
                    raise CircuitBreakerOpenError()
                self._half_open_calls += 1

        try:
            result = operation()
        except grpc.RpcError as error:
            if isinstance(error, CircuitBreakerOpenError):
                raise
            code = error.code()
            if code in (
                grpc.StatusCode.UNAVAILABLE,
                grpc.StatusCode.DEADLINE_EXCEEDED,
            ):
                should_count = (
                    failure_predicate(error)
                    if failure_predicate is not None
                    else True
                )
                if should_count:
                    self.record_failure()
            raise
        except BaseException:
            raise

        self.record_success()
        return result

    def record_success(self) -> None:
        with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                self._state = CircuitState.CLOSED
                self._failure_count = 0
                self._opened_at = None
                self._half_open_calls = 0
                logger.info(
                    "Circuit breaker closed after successful trial",
                    extra={"circuit": self.name},
                )
            elif self._state == CircuitState.CLOSED:
                self._failure_count = 0

    def record_failure(self) -> None:
        with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                self._state = CircuitState.OPEN
                self._opened_at = self._clock()
                self._half_open_calls = 0
                logger.warning(
                    "Circuit breaker reopened after failed trial",
                    extra={"circuit": self.name},
                )
                return
            if self._state == CircuitState.CLOSED:
                self._failure_count += 1
                if self._failure_count >= self.config.failure_threshold:
                    self._state = CircuitState.OPEN
                    self._opened_at = self._clock()
                    logger.warning(
                        "Circuit breaker opened",
                        extra={
                            "circuit": self.name,
                            "failure_count": self._failure_count,
                        },
                    )

    def is_retry_allowed(self) -> bool:
        with self._lock:
            self._refresh_open_state()
            return self._state != CircuitState.OPEN
