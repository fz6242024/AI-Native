# ============================================================
# llm_rate_limit.py
# LLM 全局限速与 429 退避（唯一真相源）
#
# 背景（实测数据，非推测）：
#   agnes-ai 免费档的有效速率约为「每分钟 1 次成功请求」。
#   实测方法：连续调用，第 1 次成功、第 2/3 次立即 429；随后每轮都要
#   等待 32~64 秒才能再次成功（3 轮：32.7s / 64.4s / 60.3s）。
#   官方文档只说「免费用户受 RPM 限制」，未公布具体数值，并建议对 429
#   做指数退避（1s→2s→4s→8s）。
#
# 由此得到一条硬约束：**总耗时下限 = 调用次数 ÷ RPM**。
# 所以「避免 429」与「不增加运行时间」的唯一共同解是**减少调用次数**；
# 单纯降速只会拉长时间。本模块负责其中一半：在既有调用次数下不再浪费配额。
#
# 本模块提供两件事：
#   1) RateLimiter —— 进程级令牌桶，按 LLM_RPM_LIMIT 主动排队，从源头避免 429
#   2) call_with_retry() —— 真撞上 429 时，按 Retry-After / 指数退避重试
#
# 为什么主动限速而不是「撞了再退避」：
#   被拒绝的请求同样占用配额，且连续打满会让后续合法请求也失败。主动排队
#   不改变总耗时下限，但能避免把配额浪费在被拒绝的请求上。
#
# ⚠️ 跨进程局限：B、C 是两个独立机器上的独立进程，无法共享本限速器。
#    若 B/C 用同一个免费 Key，它们会争抢同一个服务端配额池。此时 429 退避
#    会自然让二者互相错开（一方退避时另一方取到令牌），属于可接受的次优解。
#    要彻底解决，需要给 B/C 配置独立的 Key，或让二者串行执行。
#
# 环境变量：
#   LLM_RPM_LIMIT        每分钟允许的请求数，默认 1（实测值）。升级套餐后可调大。
#   LLM_MAX_RETRIES      429 时的最大重试次数，默认 4
#   LLM_RETRY_BASE_SEC   退避基数秒数，默认 5（5→10→20→40）
#   LLM_RETRY_MAX_SEC    单次退避上限秒数，默认 120
# ============================================================

import logging
import os
import threading
import time

log = logging.getLogger(__name__)


def _env_float(name: str, default: float) -> float:
    try:
        raw = os.environ.get(name, "")
        return float(raw) if str(raw).strip() else default
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        raw = os.environ.get(name, "")
        return int(float(raw)) if str(raw).strip() else default
    except (TypeError, ValueError):
        return default


# 实测免费档约 1 次/分钟；升级 Token Plan 后请调大（并在控制台确认 RPM）
LLM_RPM_LIMIT = _env_float("LLM_RPM_LIMIT", 1.0)
LLM_MAX_RETRIES = _env_int("LLM_MAX_RETRIES", 4)
LLM_RETRY_BASE_SEC = _env_float("LLM_RETRY_BASE_SEC", 5.0)
LLM_RETRY_MAX_SEC = _env_float("LLM_RETRY_MAX_SEC", 120.0)


class RateLimiter:
    """令牌桶限速器，rate 单位为「每分钟请求数」。

    acquire() 会阻塞到令牌可用为止。桶容量固定为 1：实测免费档连续两次调用
    必有一次 429，所以不允许突发（burst）。
    """

    def __init__(self, rpm: float, burst: float = 1.0):
        # rpm <= 0 视为不限速
        self.rpm = rpm
        self._interval = (60.0 / rpm) if rpm and rpm > 0 else 0.0
        self._capacity = max(1.0, float(burst))
        self._tokens = self._capacity
        self._last = time.monotonic()
        self._lock = threading.Lock()
        self.total_wait = 0.0
        self.acquire_count = 0

    @property
    def enabled(self) -> bool:
        return self._interval > 0

    def acquire(self) -> float:
        """阻塞直到取到一个令牌，返回本次实际等待秒数。

        刻意在持锁状态下 sleep：这样同一进程内的并发调用会被严格串行化，
        保证全局速率不超过 rpm。限速场景下「阻塞其他调用者」正是期望行为。
        """
        if not self.enabled:
            self.acquire_count += 1
            return 0.0

        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last
            self._tokens = min(self._capacity, self._tokens + elapsed / self._interval)
            self._last = now

            waited = 0.0
            if self._tokens >= 1.0:
                self._tokens -= 1.0
            else:
                waited = (1.0 - self._tokens) * self._interval
                time.sleep(waited)
                self._last = time.monotonic()
                self._tokens = 0.0

            self.total_wait += waited
            self.acquire_count += 1
            return waited


_limiter: RateLimiter | None = None
_limiter_lock = threading.Lock()


def get_limiter() -> RateLimiter:
    """取进程级单例限速器。

    必须是单例：限速要覆盖一个进程内的所有 LLM 调用，各调用点各建一个桶
    等于没限速（每个桶都以为自己可以立刻发一个请求）。
    """
    global _limiter
    if _limiter is None:
        with _limiter_lock:
            if _limiter is None:
                _limiter = RateLimiter(LLM_RPM_LIMIT)
                log.info(
                    "LLM 限速已启用: %.2f 次/分钟（约每 %.0f 秒 1 次）",
                    LLM_RPM_LIMIT,
                    (60.0 / LLM_RPM_LIMIT) if LLM_RPM_LIMIT > 0 else 0.0,
                )
    return _limiter


def reset_limiter() -> None:
    """仅供测试：清掉进程级单例。"""
    global _limiter
    with _limiter_lock:
        _limiter = None


def is_rate_limit_error(exc: BaseException) -> bool:
    """判断异常是否为限流（429）。

    刻意按「状态码 / 异常类名 / 报文关键字」三重匹配，不 import openai 或
    litellm —— 本模块会被 A 与 B/C 两侧共用，不能引入额外的第三方依赖。
    """
    status = getattr(exc, "status_code", None)
    if status == 429:
        return True
    # openai / litellm / httpx 的异常类名各不相同，统一按类名匹配
    if type(exc).__name__ in ("RateLimitError", "RateLimitException"):
        return True
    text = str(exc).lower()
    return "429" in text or "rate limit" in text or "too many requests" in text


def retry_after_seconds(exc: BaseException) -> float | None:
    """从 429 响应头里读 Retry-After（秒）。取不到返回 None。"""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    try:
        raw = headers.get("retry-after")
    except Exception:  # noqa: BLE001 - headers 可能是各种实现
        return None
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return None


def call_with_retry(fn, *, label: str = "LLM", max_retries: int | None = None):
    """先限速、再调用；遇 429 按 Retry-After 或指数退避重试。

    非限流异常原样抛出，交给调用方自己的降级逻辑处理 —— 本函数只解决限流。

    ⚠️ 调用方不要用「失败后改发 N 个更小的请求」来兜底：在限流账号上那是
    放大器（1 次失败 → N 次请求）。限流时应当退避重试同一个请求。
    """
    retries = LLM_MAX_RETRIES if max_retries is None else max_retries
    limiter = get_limiter()
    last_exc: BaseException | None = None

    for attempt in range(retries + 1):
        waited = limiter.acquire()
        if waited >= 1.0:
            log.info("  [限速] 等待 %.0f 秒后发起 %s 请求", waited, label)
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - 需要按类型分流
            if not is_rate_limit_error(exc):
                raise
            last_exc = exc
            if attempt >= retries:
                break
            delay = retry_after_seconds(exc)
            if delay is None:
                delay = min(LLM_RETRY_MAX_SEC, LLM_RETRY_BASE_SEC * (2 ** attempt))
            log.warning(
                "  %s 触发限流（429），%.0f 秒后重试（第 %d/%d 次）",
                label, delay, attempt + 1, retries,
            )
            time.sleep(delay)

    assert last_exc is not None
    raise last_exc


def describe_stats() -> str:
    """返回限速统计，便于在流程末尾打印（让等待时间可见）。"""
    limiter = get_limiter()
    if not limiter.enabled:
        return "LLM 限速未启用（LLM_RPM_LIMIT<=0）"
    return (
        f"LLM 限速统计: 共 {limiter.acquire_count} 次调用，"
        f"累计等待 {limiter.total_wait:.0f} 秒（限速 {limiter.rpm:.2f} 次/分钟）"
    )
