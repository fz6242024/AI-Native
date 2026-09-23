# ============================================================
# test_filter_stocks.py
# 覆盖 _filter_stocks() 的价格过滤规则。
#
# 背景：这里曾有一段死代码
#
#     if price > 100:
#         if score >= 0.85:
#             reasons.append(f"高价({price:.2f}元)但高分豁免")
#         else:
#             reasons.append(f"高价({price:.2f}元)")
#
# 两个问题：
#   1. score 是 0-100 量纲，`score >= 0.85` 恒为真 —— "豁免"分支从未被绕过；
#   2. 两个分支都往 reasons 里 append，而 reasons 非空即被移除 ——
#      所以高价股无论多高分都会被删，日志却谎称"但高分豁免"。
# 线上实际后果：误删了全场最高分的药明康德（83.2957）。
#
# 产品决定（2026-09-24）：高价股**暂时一律排除**，不保留任何形似豁免的分支。
#
# ⚠️ 注意：只断言"高价股被移除"是抓不到这个 bug 的 —— 旧代码的**行为**恰好
#    也是"高价股全删"，两者行为一致。真正的回归护栏是那条**日志断言**
#    （见 test_high_price_log_does_not_claim_exemption）。
#
# 运行：python -m pytest tests/test_filter_stocks.py -q
# ============================================================

import importlib.util
import logging
import sys
from pathlib import Path

import pytest
import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"


def _load(name: str):
    """按文件路径加载 scripts/ 下的脚本（它们不是包，无法直接 import）。"""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def discovery():
    return _load("alphasift_discovery")


class _FakeResponse:
    """够用的 requests.Response 替身：_filter_stocks 只读 .text 并设置 .encoding。"""

    def __init__(self, text: str):
        self.text = text
        self.encoding = None


def _sina_line(code: str, name: str, price: float) -> str:
    """拼一行新浪行情。

    解析器取 parts[0] 当名称、parts[2] 当成交价，所以名称放在第 0 位、
    价格放在第 2 位。
    """
    prefix = "sz" if code.startswith(("0", "3")) else "sh"
    return f'var hq_str_{prefix}{code}="{name},1.00,{price:.2f},0,0,0";'


def _stub_quotes(monkeypatch, quotes: list[tuple[str, str, float]]) -> None:
    """把新浪行情接口替换成固定数据。quotes 为 (code, name, price) 列表。"""
    body = "\n".join(_sina_line(c, n, p) for c, n, p in quotes)
    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResponse(body))


# ------------------------------------------------------------
# 行为：高价股排除、低价股保留
# ------------------------------------------------------------

@pytest.mark.parametrize("score", [0.0, 50.0, 83.2957, 100.0])
def test_high_price_stock_is_excluded_for_any_score(discovery, monkeypatch, score):
    """股价 > 100 一律排除，与分数无关（含曾经"被豁免"的高分段）。"""
    stocks = [
        {"code": "600519", "name": "贵州茅台", "score": score},
        {"code": "000333", "name": "美的集团", "score": 60.0},
    ]
    _stub_quotes(
        monkeypatch,
        [("600519", "贵州茅台", 1699.00), ("000333", "美的集团", 58.30)],
    )

    kept = discovery._filter_stocks(stocks)
    codes = [s["code"] for s in kept]

    assert "600519" not in codes, "高价股（>100元）应被排除"
    assert "000333" in codes, "低价股应保留"


def test_price_exactly_100_is_kept(discovery, monkeypatch):
    """边界：阈值是严格大于 100，正好 100 元不该被排除。"""
    stocks = [{"code": "000333", "name": "美的集团", "score": 60.0}]
    _stub_quotes(monkeypatch, [("000333", "美的集团", 100.00)])

    kept = discovery._filter_stocks(stocks)

    assert [s["code"] for s in kept] == ["000333"]


def test_missing_quote_does_not_filter(discovery, monkeypatch):
    """行情缺失时不做价格过滤（否则会把整批股票误杀）。"""
    stocks = [
        {"code": "600519", "name": "贵州茅台", "score": 83.2957},
        {"code": "000333", "name": "美的集团", "score": 60.0},
    ]
    # 行情里只回 000333，600519 拿不到价格
    _stub_quotes(monkeypatch, [("000333", "美的集团", 58.30)])

    kept = discovery._filter_stocks(stocks)
    codes = [s["code"] for s in kept]

    assert "600519" in codes, "无行情数据的股票不应被价格规则误杀"
    assert "000333" in codes


# ------------------------------------------------------------
# 日志：真正的回归护栏
# ------------------------------------------------------------

def test_high_price_log_does_not_claim_exemption(discovery, monkeypatch, caplog):
    """高价股被移除时，日志不得再出现「豁免」字样。

    这是唯一能抓住"死代码复活"的断言：如果哪天有人把
    `if score >= 0.85: ...但高分豁免` 加回来，行为测试仍会通过，
    但这条会失败。
    """
    stocks = [{"code": "600519", "name": "贵州茅台", "score": 83.2957}]
    _stub_quotes(monkeypatch, [("600519", "贵州茅台", 1699.00)])

    with caplog.at_level(logging.INFO):
        discovery._filter_stocks(stocks)

    assert "豁免" not in caplog.text, "日志不应声称高价股被豁免（豁免从未生效）"
    assert "高价(1699.00元)" in caplog.text, "应如实记录移除原因与价格"


def test_stock_and_bank_rules_still_apply(discovery, monkeypatch, caplog):
    """回归：ST 股与银行股两类过滤未被本次改动影响。"""
    stocks = [
        {"code": "600519", "name": "贵州茅台", "score": 80.0},
        {"code": "601398", "name": "工商银行", "score": 80.0},
        {"code": "000333", "name": "美的集团", "score": 60.0},
    ]
    _stub_quotes(
        monkeypatch,
        [
            ("600519", "贵州茅台", 1699.00),
            ("601398", "工商银行", 5.60),
            ("000333", "美的集团", 58.30),
        ],
    )

    with caplog.at_level(logging.INFO):
        kept = discovery._filter_stocks(stocks)

    codes = [s["code"] for s in kept]
    assert "601398" not in codes, "银行股应被排除"
    assert "000333" in codes
    assert "银行股" in caplog.text
