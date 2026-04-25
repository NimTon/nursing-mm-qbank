from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import Any, TypeVar

try:
    # openai>=1.x
    from openai import (  # type: ignore
        APIConnectionError,
        APIStatusError,
        APITimeoutError,
        RateLimitError,
    )
except Exception:  # noqa: BLE001
    APIConnectionError = APIStatusError = APITimeoutError = RateLimitError = ()  # type: ignore

T = TypeVar("T")


def _is_timeout_exc(e: BaseException) -> bool:
    if isinstance(e, APITimeoutError):
        return True
    msg = (str(e) or "").lower()
    return "timed out" in msg or "timeout" in msg


def _is_retryable_exc(e: BaseException) -> bool:
    if isinstance(e, (APIConnectionError, APITimeoutError, RateLimitError)):
        return True
    if isinstance(e, APIStatusError):
        # 5xx/429 通常可重试；4xx 大多不可重试
        code = getattr(e, "status_code", None)
        if code in (429, 500, 502, 503, 504):
            return True
        if isinstance(code, int) and 500 <= code <= 599:
            return True
        return False
    # 兜底：某些兼容网关/底层 httpx 抛出来的异常类型不同，按文案做保守判断
    msg = (str(e) or "").lower()
    return any(
        k in msg
        for k in (
            "timeout",
            "timed out",
            "connection",
            "connect",
            "reset",
            "temporarily",
            "try again",
            "rate limit",
            "429",
            "502",
            "503",
            "504",
            "service unavailable",
            "bad gateway",
            "gateway",
        )
    )


def call_with_retries(
    fn: Callable[[], T],
    *,
    tries: int = 3,
    base_sleep_s: float = 1.0,
    max_sleep_s: float = 8.0,
    on_retry: Callable[[int, BaseException, float], Any] | None = None,
) -> T:
    """
    对网络/HTTP 类失败进行最多 tries 次重试（指数退避 + 抖动）。
    - tries: 总尝试次数（包含第一次）
    - on_retry: 当将要重试时回调 (attempt_idx 从 1 开始, exc, sleep_s)
    """
    if tries < 1:
        tries = 1
    for attempt in range(1, tries + 1):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            if attempt >= tries or (not _is_retryable_exc(e)):
                raise
            # exponential backoff with jitter
            sleep_s = min(max_sleep_s, base_sleep_s * (2 ** (attempt - 1)))
            sleep_s = sleep_s * (0.75 + random.random() * 0.5)
            if on_retry is not None:
                on_retry(attempt, e, float(sleep_s))
            time.sleep(float(sleep_s))
    # unreachable
    return fn()


def call_with_retries_timeout(
    fn: Callable[[float], T],
    *,
    base_timeout_s: float,
    tries: int = 3,
    base_sleep_s: float = 1.0,
    max_sleep_s: float = 8.0,
    on_retry: Callable[[int, BaseException, float, float], Any] | None = None,
) -> T:
    """
    带 timeout 递增的重试：
    - 第 1 次使用 base_timeout_s
    - 若第 1 次异常为 timeout，则第 2 次开始使用 2*base_timeout_s（满足“timeout 则第二次等待两倍”）
    - 其余网络/HTTP 可重试错误仍按指数退避重试

    on_retry 回调参数: (attempt_idx, exc, sleep_s, next_timeout_s)
    """
    if tries < 1:
        tries = 1
    timeout_s = float(base_timeout_s)
    next_timeout_s = float(base_timeout_s)
    for attempt in range(1, tries + 1):
        timeout_s = next_timeout_s
        try:
            return fn(timeout_s)
        except Exception as e:  # noqa: BLE001
            if attempt >= tries or (not _is_retryable_exc(e)):
                raise
            # 如果发生 timeout：下一次开始用 2x timeout_seconds（只要求第二次变 2x，这里保持 2x 直到结束）
            if _is_timeout_exc(e):
                next_timeout_s = float(base_timeout_s) * 2.0
            # exponential backoff with jitter
            sleep_s = min(max_sleep_s, base_sleep_s * (2 ** (attempt - 1)))
            sleep_s = sleep_s * (0.75 + random.random() * 0.5)
            if on_retry is not None:
                on_retry(attempt, e, float(sleep_s), float(next_timeout_s))
            time.sleep(float(sleep_s))
    return fn(next_timeout_s)

