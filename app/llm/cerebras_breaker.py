"""
Circuit breaker for Cerebras.

When Cerebras fails (429 / timeout) _THRESHOLD times within _WINDOW_S seconds,
the circuit opens and callers skip Cerebras for _RETRY_AFTER seconds before
trying again. On a successful call the failure history is cleared.

Usage:
    from app.llm.cerebras_breaker import is_open, record_failure, record_success

    if is_open():
        raise RuntimeError("Cerebras circuit open — skipping")
    try:
        result = call_cerebras(...)
        record_success()
    except Exception as e:
        record_failure()
        raise
"""

import time
import threading
from datetime import datetime

_ts = lambda: datetime.now().strftime("%H:%M:%S")

_lock          = threading.Lock()
_failure_times: list[float] = []

_WINDOW_S    = 300   # rolling window: 5 min
_THRESHOLD   = 2     # failures in window before tripping
_RETRY_AFTER = 180   # seconds to wait before probing again (3 min)


def record_failure() -> None:
    with _lock:
        now = time.time()
        _failure_times.append(now)
        _failure_times[:] = [t for t in _failure_times if now - t < _WINDOW_S]
    print(f"{_ts()} [Breaker] Cerebras failure recorded ({len(_failure_times)} in window)")


def record_success() -> None:
    with _lock:
        if _failure_times:
            _failure_times.clear()
            print(f"{_ts()} [Breaker] Cerebras recovered — circuit reset")


def is_open() -> bool:
    """Returns True when the circuit is tripped and Cerebras should be skipped."""
    with _lock:
        now = time.time()
        recent = [t for t in _failure_times if now - t < _WINDOW_S]
        if len(recent) >= _THRESHOLD:
            time_since_last = now - max(recent)
            if time_since_last < _RETRY_AFTER:
                return True
            # Probe window — let one request through to test recovery
    return False
