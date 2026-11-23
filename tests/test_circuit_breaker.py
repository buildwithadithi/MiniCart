import grpc
import pytest
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from generated.grpc_retry import RetryPolicy, retry_grpc_call

from generated.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitBreakerOpenError,
    CircuitState,
)


class FakeRpcError(grpc.RpcError):
    def __init__(self, code):
        self._code = code

    def code(self):
        return self._code

    def details(self):
        return "controlled circuit-breaker test"


def test_closed_success_and_failure_threshold():
    now = [0.0]
    breaker = CircuitBreaker(
        CircuitBreakerConfig(
            failure_threshold=2,
            recovery_timeout_seconds=10,
            half_open_max_calls=1,
        ),
        name="test",
        clock=lambda: now[0],
    )

    assert breaker.state == CircuitState.CLOSED
    assert breaker.execute(lambda: "ok") == "ok"
    for _ in range(2):
        with pytest.raises(FakeRpcError):
            breaker.execute(
                lambda: (_ for _ in ()).throw(
                    FakeRpcError(grpc.StatusCode.UNAVAILABLE)
                )
            )

    assert breaker.state == CircuitState.OPEN


def test_open_rejects_immediately_and_timeout_moves_to_half_open():
    now = [0.0]
    breaker = CircuitBreaker(
        CircuitBreakerConfig(1, 10, 1),
        name="test",
        clock=lambda: now[0],
    )
    with pytest.raises(FakeRpcError):
        breaker.execute(
            lambda: (_ for _ in ()).throw(
                FakeRpcError(grpc.StatusCode.UNAVAILABLE)
            )
        )

    calls = 0

    def operation():
        nonlocal calls
        calls += 1
        return "recovered"

    with pytest.raises(CircuitBreakerOpenError):
        breaker.execute(operation)
    assert calls == 0

    now[0] = 10
    assert breaker.execute(operation) == "recovered"
    assert breaker.state == CircuitState.CLOSED
    assert calls == 1


def test_failed_half_open_trial_reopens_circuit():
    now = [0.0]
    breaker = CircuitBreaker(
        CircuitBreakerConfig(1, 5, 1),
        name="test",
        clock=lambda: now[0],
    )
    breaker.record_failure()
    now[0] = 5

    assert breaker.state == CircuitState.HALF_OPEN
    with pytest.raises(FakeRpcError):
        breaker.execute(
            lambda: (_ for _ in ()).throw(
                FakeRpcError(grpc.StatusCode.UNAVAILABLE)
            )
        )
    assert breaker.state == CircuitState.OPEN


def test_half_open_limits_concurrent_trial_calls():
    now = [0.0]
    breaker = CircuitBreaker(
        CircuitBreakerConfig(1, 1, 2),
        name="test",
        clock=lambda: now[0],
    )
    breaker.record_failure()
    now[0] = 1
    assert breaker.state == CircuitState.HALF_OPEN

    trial_started = Event()
    release_trials = Event()

    def operation():
        trial_started.set()
        assert release_trials.wait(timeout=1)
        return "ok"

    with ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(breaker.execute, operation)
        assert trial_started.wait(timeout=1)
        second = pool.submit(breaker.execute, operation)
        third = pool.submit(breaker.execute, operation)
        outcomes = []
        try:
            third.result(timeout=1)
            outcomes.append("unexpected-third")
        except CircuitBreakerOpenError:
            outcomes.append("open")
        finally:
            release_trials.set()
        for future in (first, second):
            try:
                outcomes.append(future.result(timeout=1))
            except CircuitBreakerOpenError:
                outcomes.append("open")

    assert outcomes.count("open") == 1


def test_permanent_error_does_not_open_circuit():
    breaker = CircuitBreaker(
        CircuitBreakerConfig(1, 10, 1),
        name="test",
        clock=lambda: 0.0,
    )

    with pytest.raises(FakeRpcError):
        breaker.execute(
            lambda: (_ for _ in ()).throw(
                FakeRpcError(grpc.StatusCode.INVALID_ARGUMENT)
            )
        )

    assert breaker.state == CircuitState.CLOSED


def test_config_values_are_validated():
    with pytest.raises(ValueError):
        CircuitBreakerConfig(
            failure_threshold=0,
            recovery_timeout_seconds=1,
            half_open_max_calls=1,
        )


def test_retry_sequence_counts_as_one_failure_and_open_rejects_without_retry():
    breaker = CircuitBreaker(
        CircuitBreakerConfig(1, 10, 1),
        name="retry-test",
        clock=lambda: 0.0,
    )
    calls = 0

    def exhausted_operation(*, timeout_seconds):
        nonlocal calls
        calls += 1
        raise FakeRpcError(grpc.StatusCode.UNAVAILABLE)

    policy = RetryPolicy(
        max_attempts=4,
        initial_retry_delay_seconds=0,
        multiplier=2,
        max_retry_delay_seconds=0,
        jitter_ratio=0,
    )

    with pytest.raises(FakeRpcError):
        breaker.execute(
            lambda: retry_grpc_call(
                exhausted_operation,
                rpc_name="test.retry",
                timeout_seconds=5,
                policy=policy,
                sleep=lambda _delay: None,
            )
        )
    assert breaker.state == CircuitState.OPEN
    assert calls == 4

    open_calls = 0
    with pytest.raises(CircuitBreakerOpenError):
        breaker.execute(
            lambda: retry_grpc_call(
                lambda **_kwargs: open_calls,
                rpc_name="test.open",
                timeout_seconds=5,
                policy=policy,
            )
        )
    assert open_calls == 0
    assert calls == 4


def test_successful_retry_closes_half_open_and_breakers_are_independent():
    now = [0.0]
    gateway_breaker = CircuitBreaker(
        CircuitBreakerConfig(1, 5, 1),
        name="gateway-order",
        clock=lambda: now[0],
    )
    product_breaker = CircuitBreaker(
        CircuitBreakerConfig(1, 5, 1),
        name="order-product",
        clock=lambda: now[0],
    )

    gateway_breaker.record_failure()
    now[0] = 5
    assert gateway_breaker.state == CircuitState.HALF_OPEN

    attempts = 0

    def eventually_successful(*, timeout_seconds):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise FakeRpcError(grpc.StatusCode.UNAVAILABLE)
        return "ok"

    result = gateway_breaker.execute(
        lambda: retry_grpc_call(
            eventually_successful,
            rpc_name="test.recovery",
            timeout_seconds=5,
            policy=RetryPolicy(4, 0, 2, 0, 0),
            sleep=lambda _delay: None,
        )
    )

    assert result == "ok"
    assert gateway_breaker.state == CircuitState.CLOSED
    assert product_breaker.state == CircuitState.CLOSED
