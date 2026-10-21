# ============================================================
# test_bias_penalty_and_defensive.py
# 覆盖初选模型优化：
#   - _build_snapshot_metrics_map 从快照安全提取指标
#   - _apply_defensive_factor_weight 防御型因子加权
#   - _apply_short_term_bias_penalty 短线涨幅乖离扣分（防高位追高）
#   - 幂等性与综合分 (combined_score) 正确合并
# ============================================================

import importlib.util
import sys
from pathlib import Path

import pytest
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"


def _load(name: str):
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


# ------------------------------------------------------------
# 1. _build_snapshot_metrics_map 测试
# ------------------------------------------------------------

def test_build_snapshot_metrics_map_from_dataframe(discovery):
    df = pd.DataFrame([
        {
            "code": "000001",
            "change_pct": 2.5,
            "turnover_rate": 1.2,
            "pe_ratio": 6.5,
            "pb_ratio": 0.6,
            "total_mv": 1.5e11,
        },
        {
            "code": "600519",
            "change_pct": -0.8,
            "turnover_rate": 0.4,
            "pe_ratio": 28.0,
            "pb_ratio": 8.0,
            "total_mv": 2.0e12,
        },
    ])
    metrics = discovery._build_snapshot_metrics_map(df)
    assert len(metrics) == 2
    assert metrics["000001"]["change_pct"] == 2.5
    assert metrics["000001"]["pe_ratio"] == 6.5
    assert metrics["600519"]["pb_ratio"] == 8.0


def test_build_snapshot_metrics_map_from_dict(discovery):
    payload = {
        "stocks": [
            {
                "code": "002011",
                "change_pct": 6.8,
                "turnover_rate": 12.5,
                "pe_ratio": 18.0,
                "pb_ratio": 2.1,
                "total_mv": 1.2e10,
            }
        ]
    }
    metrics = discovery._build_snapshot_metrics_map(payload)
    assert "002011" in metrics
    assert metrics["002011"]["change_pct"] == 6.8
    assert metrics["002011"]["turnover_rate"] == 12.5


def test_build_snapshot_metrics_map_handles_none_and_empty(discovery):
    assert discovery._build_snapshot_metrics_map(None) == {}
    assert discovery._build_snapshot_metrics_map(pd.DataFrame()) == {}
    assert discovery._build_snapshot_metrics_map({}) == {}


# ------------------------------------------------------------
# 2. _apply_defensive_factor_weight 测试
# ------------------------------------------------------------

def test_defensive_factor_strategy_bonus(discovery):
    """防御型策略来源（low_volatility_quality / quality_value / blue_chip_income）加 2 分。"""
    stocks = [
        {"code": "600000", "score": 75.0, "strategy": "low_volatility_quality"},
        {"code": "600001", "score": 75.0, "strategy": "quality_value"},
        {"code": "600002", "score": 75.0, "strategy": "blue_chip_income"},
        {"code": "600003", "score": 75.0, "strategy": "capital_heat"},
    ]
    out = discovery._apply_defensive_factor_weight(stocks, {})
    assert out[0]["combined_score"] == 77.0
    assert out[1]["combined_score"] == 77.0
    assert out[2]["combined_score"] == 77.0
    assert out[3]["combined_score"] == 75.0
    assert out[0]["defensive_bonus"] == 2.0
    assert out[3]["defensive_bonus"] == 0.0


def test_defensive_factor_valuation_and_turnover_bonus(discovery):
    """低估值安全边际 (0<PE<=25, 0<PB<=2.5) +2 分，筹码温和换手 (0.5<=turnover<=5.0) +1 分。"""
    stocks = [
        {
            "code": "600100",
            "score": 70.0,
            "strategy": "balanced_alpha",
            "pe_ratio": 15.0,
            "pb_ratio": 1.5,
            "turnover_rate": 2.0,
        }
    ]
    out = discovery._apply_defensive_factor_weight(stocks, {})
    # 策略非防御(0) + 低估值(+2) + 温和换手(+1) = +3 分
    assert out[0]["defensive_bonus"] == 3.0
    assert out[0]["combined_score"] == 73.0
    assert len(out[0]["defensive_tags"]) == 2


def test_defensive_factor_bonus_capped_at_five(discovery):
    """防御总加成封顶 5.0 分。"""
    stocks = [
        {
            "code": "600200",
            "score": 70.0,
            "strategy": "low_volatility_quality",  # +2
            "pe_ratio": 12.0,                     # +2
            "pb_ratio": 1.2,
            "turnover_rate": 2.5,                 # +1
        }
    ]
    out = discovery._apply_defensive_factor_weight(stocks, {})
    assert out[0]["defensive_bonus"] == 5.0
    assert out[0]["combined_score"] == 75.0


def test_defensive_factor_weight_is_idempotent(discovery):
    """多次调用 _apply_defensive_factor_weight 不会导致分数累积膨胀。"""
    stocks = [
        {
            "code": "600300",
            "score": 70.0,
            "strategy": "quality_value",
            "pe_ratio": 18.0,
            "pb_ratio": 1.8,
            "turnover_rate": 1.5,
        }
    ]
    out1 = discovery._apply_defensive_factor_weight(stocks, {})
    first_score = out1[0]["combined_score"]
    out2 = discovery._apply_defensive_factor_weight(stocks, {})
    assert out2[0]["combined_score"] == first_score == 75.0


# ------------------------------------------------------------
# 3. _apply_short_term_bias_penalty 测试
# ------------------------------------------------------------

def test_short_term_bias_penalty_pulse_levels(discovery):
    """当日急涨超买惩罚梯度：>=9.0% 扣 8分，>=7.0% 扣 5分，>=5.0% 扣 2分。"""
    stocks = [
        {"code": "000001", "score": 80.0, "change_pct": 9.8},   # 逼近涨停扣 8分
        {"code": "000002", "score": 80.0, "change_pct": 7.5},   # 涨幅偏大扣 5分
        {"code": "000003", "score": 80.0, "change_pct": 5.5},   # 偏离均线扣 2分
        {"code": "000004", "score": 80.0, "change_pct": 2.1},   # 正常涨幅不扣分
    ]
    out = discovery._apply_short_term_bias_penalty(stocks, {})
    assert out[0]["bias_penalty"] == 8.0
    assert out[0]["combined_score"] == 72.0
    assert "逼近涨停" in out[0]["bias_reasons"][0]

    assert out[1]["bias_penalty"] == 5.0
    assert out[1]["combined_score"] == 75.0

    assert out[2]["bias_penalty"] == 2.0
    assert out[2]["combined_score"] == 78.0

    assert out[3]["bias_penalty"] == 0.0
    assert out[3]["combined_score"] == 80.0


def test_short_term_bias_penalty_turnover_overheat(discovery):
    """换手率过热高位博弈惩罚：>=20% 扣 3分；>=15% 且涨幅>=5% 扣 1.5分。"""
    stocks = [
        {"code": "000005", "score": 80.0, "change_pct": 3.0, "turnover_rate": 22.0},  # 扣 3.0
        {"code": "000006", "score": 80.0, "change_pct": 6.0, "turnover_rate": 16.5},  # 涨幅>=5%扣2 + 换手>=15%扣1.5 = 3.5
    ]
    out = discovery._apply_short_term_bias_penalty(stocks, {})
    assert out[0]["bias_penalty"] == 3.0
    assert out[0]["combined_score"] == 77.0

    assert out[1]["bias_penalty"] == 3.5
    assert out[1]["combined_score"] == 76.5


def test_short_term_bias_penalty_capped_at_ten(discovery):
    """乖离惩罚上限 10.0 分。"""
    stocks = [
        {"code": "000007", "score": 80.0, "change_pct": 9.9, "turnover_rate": 25.0}  # 8 + 3 = 11 -> 封顶 10
    ]
    out = discovery._apply_short_term_bias_penalty(stocks, {})
    assert out[0]["bias_penalty"] == 10.0
    assert out[0]["combined_score"] == 70.0


def test_short_term_bias_penalty_is_idempotent(discovery):
    """多次调用 _apply_short_term_bias_penalty 不会导致分数重复扣减。"""
    stocks = [{"code": "000008", "score": 80.0, "change_pct": 7.8}]
    out1 = discovery._apply_short_term_bias_penalty(stocks, {})
    first = out1[0]["combined_score"]
    out2 = discovery._apply_short_term_bias_penalty(stocks, {})
    assert out2[0]["combined_score"] == first == 75.0


# ------------------------------------------------------------
# 4. 全链路整合加权与排序翻转验证
# ------------------------------------------------------------

def test_combined_score_integration_and_flip(discovery):
    """验证高位冲高标的被扣分、稳健防御标的被提拔的策略翻转效应。

    场景：
      股票 A（高位热点股）：初始初选分 82.0 分，但当日大涨 +9.5%，换手率 21%，短线处于极端过热。
      股票 B（低波稳健股）：初始初选分 78.0 分，当日涨幅 +1.5%，换手率 2.0%，PE=12, PB=1.1，策略 low_volatility_quality。
    结果：
      优化前：A(82) > B(78)
      优化后：A 扣 10分(72分) < B 加 5分(83分)，B 成功超越 A 排在第一，避免追高接盘！
    """
    stocks = [
        {
            "code": "000009",
            "name": "高位过热股",
            "score": 82.0,
            "strategy": "capital_heat",
            "change_pct": 9.5,
            "turnover_rate": 21.0,
            "pe_ratio": 65.0,
            "pb_ratio": 7.0,
        },
        {
            "code": "600009",
            "name": "低波稳健股",
            "score": 78.0,
            "strategy": "low_volatility_quality",
            "change_pct": 1.5,
            "turnover_rate": 2.0,
            "pe_ratio": 12.0,
            "pb_ratio": 1.1,
        },
    ]

    # 1. 历史加权（假设均无历史评分）
    stocks = discovery._apply_history_weight(stocks, {})
    assert stocks[0]["combined_score"] == 82.0
    assert stocks[1]["combined_score"] == 78.0

    # 2. 防御因子加权
    stocks = discovery._apply_defensive_factor_weight(stocks, {})
    assert stocks[0]["defensive_bonus"] == 0.0
    assert stocks[1]["defensive_bonus"] == 5.0  # 策略(2) + 低估值(2) + 换手(1)

    # 3. 短线乖离惩罚
    stocks = discovery._apply_short_term_bias_penalty(stocks, {})
    assert stocks[0]["bias_penalty"] == 10.0  # 封顶 10
    assert stocks[1]["bias_penalty"] == 0.0

    # 最终综合分验证
    assert stocks[0]["combined_score"] == 72.0
    assert stocks[1]["combined_score"] == 83.0

    # 排序翻转：稳健股优先排第一
    ranked = sorted(stocks, key=lambda s: s["combined_score"], reverse=True)
    assert ranked[0]["code"] == "600009"
    assert ranked[1]["code"] == "000009"
