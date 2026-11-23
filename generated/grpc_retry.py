import logging
import os
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

import grpc


logger = logging.getLogger(__name__)
_T = TypeVar("_T")
_MINIMUM_ATTEMPT_SECONDS = 0.01


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 4
    initial_retry_delay_seconds: float = 0.2
    multiplier: float = 2.0
    max_retry_delay_seconds: float = 1.0
    jitter_ratio: float = 0.25

    @classmethod
    def from_environment(cls) -> "RetryPolicy":
        return cls(
            max_attempts=_positive_int(
                "GRPC_RETRY_MAX_ATTEMPTS",
                cls.max_attempts,
            ),
            initial_retry_delay_seconds=_non_negative_float(
                "GRPC_RETRY_INITIAL_DELAY_SECONDS",
                cls.initial_retry_delay_seconds,
            ),
            multiplier=_minimum_float(
                "GRPC_RETRY_MULTIPLIER",
                cls.multiplier,
                1.0,
            ),
            max_retry_delay_seconds=_non_negative_float(
                "GRPC_RETRY_MAX_DELAY_SECONDS",
                cls.max_retry_delay_seconds,
            ),
            jitter_ratio=_bounded_float(
                "GRPC_RETRY_JITTER_RATIO",
                cls.jitter_ratio,
                0.0,
                1.0,
            ),
        )


def _positive_int(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value <= 0:
        raise ValueError(f"{name} must be positive.")
    return value


def _non_negative_float(name: str, default: float) -> float:
    value = float(os.getenv(name, str(default)))
    if value < 0:
        raise ValueError(f"{name} must not be negative.")
    return value


def _minimum_float(name: str, default: float, minimum: float) -> float:
    value = float(os.getenv(name, str(default)))
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}.")
    return value


def _bounded_float(name: str, default: float, minimum: float, maximum: float) -> float:
    value = float(os.getenv(name, str(default)))
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}.")
    return value


def calculate_retry_delay(retry_number: int, policy: RetryPolicy) -> float:
    if retry_number < 1:
        raise ValueError("retry_number must be at least 1.")
    delay = policy.initial_retry_delay_seconds * (
        policy.multiplier ** (retry_number - 1)
    )
    return min(delay, policy.max_retry_delay_seconds)


def apply_retry_jitter(
    delay_seconds: float,
    jitter_ratio: float,
    uniform: Callable[[float, float], float] = random.uniform,
) -> float:
    if jitter_ratio < 0 or jitter_ratio > 1:
        raise ValueError("jitter_ratio must be between 0 and 1.")
    if delay_seconds <= 0:
        return 0.0
    lower = delay_seconds * (1.0 - jitter_ratio)
    upper = delay_seconds * (1.0 + jitter_ratio)
    return max(0.0, uniform(lower, upper))


def retry_grpc_call(
    operation: Callable[..., _T],
    *,
    rpc_name: str,
    timeout_seconds: float,
    policy: RetryPolicy | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    uniform: Callable[[float, float], float] = random.uniform,
) -> _T:
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive.")

    retry_policy = policy or RetryPolicy.from_environment()
    if retry_policy.max_attempts < 1:
        raise ValueError("max_attempts must be positive.")

    started_at = clock()
    overall_deadline = started_at + timeout_seconds
    last_error: grpc.RpcError | None = None

    for attempt in range(1, retry_policy.max_attempts + 1):
        remaining = overall_deadline - clock()
        if remaining <= 0:
            if last_error is not None:
                raise last_error
            raise grpc.FutureTimeoutError()

        try:
            return operation(timeout_seconds=max(remaining, _MINIMUM_ATTEMPT_SECONDS))
        except grpc.RpcError as error:
            last_error = error
            code = error.code()
            if code not in (
                grpc.StatusCode.UNAVAILABLE,
                grpc.StatusCode.DEADLINE_EXCEEDED,
            ):
                logger.info(
                    "gRPC retry stopped for non-transient error",
                    extra={"rpc": rpc_name, "attempt": attempt, "grpc_status": code.name},
                )
                raise

            if attempt >= retry_policy.max_attempts:
                logger.warning(
                    "gRPC retry exhausted",
                    extra={"rpc": rpc_name, "attempt": attempt, "grpc_status": code.name},
                )
                raise

            remaining = overall_deadline - clock()
            if remaining <= 0:
                logger.warning(
                    "gRPC retry stopped because overall deadline expired",
                    extra={"rpc": rpc_name, "attempt": attempt, "grpc_status": code.name},
                )
                raise

            base_delay = calculate_retry_delay(attempt, retry_policy)
            delay = apply_retry_jitter(
                base_delay,
                retry_policy.jitter_ratio,
                uniform,
            )
            delay = min(delay, retry_policy.max_retry_delay_seconds)
            if delay + _MINIMUM_ATTEMPT_SECONDS >= remaining:
                logger.info(
                    "gRPC retry stopped because insufficient deadline remains",
                    extra={
                        "rpc": rpc_name,
                        "attempt": attempt,
                        "grpc_status": code.name,
                        "remaining_seconds": round(remaining, 3),
                    },
                )
                raise

            logger.warning(
                "gRPC retry scheduled",
                extra={
                    "rpc": rpc_name,
                    "attempt": attempt,
                    "next_attempt": attempt + 1,
                    "grpc_status": code.name,
                    "delay_seconds": round(delay, 3),
                    "remaining_seconds": round(remaining, 3),
                },
            )
            sleep(delay)

    raise AssertionError("retry loop exhausted without propagating a gRPC error")
