# -*- coding: utf-8 -*-
"""llm_rate_limit 模块测试。

重点覆盖两条本 bug 的护栏：
  1. 限速器确实会「拦住」连续的请求（而不是各建一个桶、等于没限速）
  2. call_with_retry 遇 429 会重试，遇非限流错误立刻抛出（不吞异常）
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

_spec = importlib.util.spec_from_file_location("llm_rate_limit", _SCRIPTS / "llm_rate_limit.py")
rl = importlib.util.module_from_spec(_spec)
sys.modules["llm_rate_limit"] = rl
_spec.loader.exec_module(rl)


class FakeTime:
    """可推进的假时钟，避免测试真的 sleep。"""

    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture
def fake_time(monkeypatch):
    ft = FakeTime()
    monkeypatch.setattr(rl, "time", ft)
    return ft


# ------------------------------------------------------------
# RateLimiter
# ------------------------------------------------------------

def test_limiter_disabled_when_rpm_zero(fake_time):
    limiter = rl.RateLimiter(0)
    assert limiter.enabled is False
    assert limiter.acquire() == 0.0
    assert limiter.acquire() == 0.0
    assert fake_time.slept == []


def test_limiter_first_call_does_not_wait(fake_time):
    limiter = rl.RateLimiter(60)  # 1 次/秒
    assert limiter.acquire() == 0.0


def test_limiter_second_call_waits_one_interval(fake_time):
    """这是核心护栏：连续两次调用必须被拦住，否则等于没限速。"""
    limiter = rl.RateLimiter(60)  # 每 60/60 = 1 秒 1 次
    limiter.acquire()
    waited = limiter.acquire()
    assert waited == pytest.approx(1.0, abs=0.01)
    assert fake_time.slept == pytest.approx([1.0], abs=0.01)


def test_limiter_paces_many_calls(fake_time):
    """3 次调用（第 1 次不等待）在 rpm=60 下应累计等待 2 秒。"""
    limiter = rl.RateLimiter(60)
    for _ in range(3):
        limiter.acquire()
    assert limiter.total_wait == pytest.approx(2.0, abs=0.05)
    assert limiter.acquire_count == 3


def test_limiter_allows_after_enough_elapsed_time(fake_time):
    """距上次调用已超过间隔时，不应再等待。"""
    limiter = rl.RateLimiter(60)
    limiter.acquire()
    fake_time.now += 5.0  # 手动推进 5 秒
    assert limiter.acquire() == 0.0


def test_get_limiter_is_singleton(monkeypatch):
    """限速器必须是进程级单例，否则各调用点各建一个桶 = 没限速。"""
    rl.reset_limiter()
    a = rl.get_limiter()
    b = rl.get_limiter()
    assert a is b
    rl.reset_limiter()


def test_describe_stats_does_not_crash(monkeypatch):
    rl.reset_limiter()
    assert isinstance(rl.describe_stats(), str)
    rl.reset_limiter()


# ------------------------------------------------------------
# 429 识别
# ------------------------------------------------------------

def test_is_rate_limit_error_by_status_code():
    exc = Exception("boom")
    exc.status_code = 429
    assert rl.is_rate_limit_error(exc) is True


def test_is_rate_limit_error_by_class_name():
    class RateLimitError(Exception):
        pass

    assert rl.is_rate_limit_error(RateLimitError("x")) is True


def test_is_rate_limit_error_by_message():
    assert rl.is_rate_limit_error(Exception("Error code: 429 - too many")) is True
    assert rl.is_rate_limit_error(Exception("You've reached the API rate limit")) is True


def test_is_rate_limit_error_false_for_other_errors():
    assert rl.is_rate_limit_error(ValueError("bad json")) is False
    assert rl.is_rate_limit_error(TimeoutError("timed out")) is False


def test_retry_after_reads_header():
    exc = Exception("429")
    exc.response = types.SimpleNamespace(headers={"retry-after": "12"})
    assert rl.retry_after_seconds(exc) == 12.0


def test_retry_after_none_when_missing():
    assert rl.retry_after_seconds(Exception("429")) is None


# ------------------------------------------------------------
# call_with_retry
# ------------------------------------------------------------

def test_call_with_retry_returns_on_first_success(fake_time, monkeypatch):
    monkeypatch.setattr(rl, "get_limiter", lambda: rl.RateLimiter(0))
    assert rl.call_with_retry(lambda: "ok") == "ok"


def test_call_with_retry_retries_on_429_then_succeeds(fake_time, monkeypatch):
    monkeypatch.setattr(rl, "get_limiter", lambda: rl.RateLimiter(0))
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise Exception("Error code: 429 - rate limit")
        return "recovered"

    assert rl.call_with_retry(flaky, label="测试") == "recovered"
    assert calls["n"] == 3
    # 退避应为 5s → 10s（指数）
    assert fake_time.slept == pytest.approx([5.0, 10.0], abs=0.01)


def test_call_with_retry_raises_non_rate_limit_immediately(fake_time, monkeypatch):
    """非限流错误必须原样抛出，不能重试、不能吞掉。"""
    monkeypatch.setattr(rl, "get_limiter", lambda: rl.RateLimiter(0))
    calls = {"n": 0}

    def broken():
        calls["n"] += 1
        raise ValueError("JSON 解析失败")

    with pytest.raises(ValueError):
        rl.call_with_retry(broken)
    assert calls["n"] == 1
    assert fake_time.slept == []


def test_call_with_retry_raises_after_exhausting_retries(fake_time, monkeypatch):
    monkeypatch.setattr(rl, "get_limiter", lambda: rl.RateLimiter(0))

    def always_429():
        raise Exception("429 rate limit")

    with pytest.raises(Exception):
        rl.call_with_retry(always_429, max_retries=2)


def test_call_with_retry_honors_retry_after_header(fake_time, monkeypatch):
    monkeypatch.setattr(rl, "get_limiter", lambda: rl.RateLimiter(0))
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            exc = Exception("429")
            exc.response = types.SimpleNamespace(headers={"retry-after": "7"})
            raise exc
        return "ok"

    assert rl.call_with_retry(flaky) == "ok"
    # 应优先用 Retry-After=7，而不是退避基数 5
    assert fake_time.slept == pytest.approx([7.0], abs=0.01)
