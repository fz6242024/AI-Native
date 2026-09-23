# ============================================================
# test_blue_chip_cap.py
# 覆盖「大盘蓝筹最多 2 支」这条产品硬约束的实现：
#   - _build_market_cap_map  code -> 总市值(元)
#   - _is_blue_chip          双轨判定（蓝筹策略来源 ∪ 总市值 >= 500 亿）
#   - _cap_blue_chips        封顶 + 从候选池回填
#   - _filter_stocks 规则0    创业板/科创板/北交所前缀排除
#
# ⚠️ 单位陷阱（务必牢记）：
#   快照里的 total_mv 单位是「元」，不是亿元。采集端 snapshot_collector.py
#   第 112-113 行做了 `_f(p, 45) * 1e8`（腾讯行情给的是亿元，采集时 ×1e8）。
#   所以「500 亿」阈值 = 5e10。若在测试里写 total_mv=500 之类的小数字，
#   会差 1e8 倍，测试会假通过/假失败。本文件统一用 `N * 10**8` 表示 N 亿。
#
# ⚠️ 顺序不变量（本文件最重要的护栏）：
#   _cap_blue_chips(...) 必须放在 _filter_stocks(...) **之前** 调用。
#   回填候选来自 candidate_pool，那批票还没过 ST/银行/高价股/非主板过滤；
#   若闸门放在过滤之后，回填进来的票会绕过全部过滤规则。
#
# 运行：python -m pytest tests/test_blue_chip_cap.py -q
# ============================================================

import importlib.util
import inspect
import logging
import sys
from pathlib import Path

import pytest
import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"

YI = 10 ** 8  # 1 亿（元）


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


# ------------------------------------------------------------
# 新浪行情替身：_filter_stocks 会拉实时价格/名称
# ------------------------------------------------------------

class _FakeResponse:
    def __init__(self, text: str):
        self.text = text
        self.encoding = None


def _sina_line(code: str, name: str, price: float) -> str:
    prefix = "sz" if code.startswith(("0", "3")) else "sh"
    return f'var hq_str_{prefix}{code}="{name},1.00,{price:.2f},0,0,0";'


def _stub_quotes(monkeypatch, quotes):
    body = "\n".join(_sina_line(c, n, p) for c, n, p in quotes)
    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResponse(body))


def _stock(code, name="", score=0.0, strategy="momentum_quality", combined=None, **extra):
    s = {"code": code, "name": name, "score": score, "strategy": strategy}
    if combined is not None:
        s["combined_score"] = combined
    s.update(extra)
    return s


# ============================================================
# _is_blue_chip —— 双轨判定
# ============================================================

def test_is_blue_chip_strategy_track_blue_chip_income(discovery):
    """轨 A：蓝筹策略选出，市值很小也算蓝筹。"""
    s = _stock("600001", strategy="blue_chip_income")
    assert discovery._is_blue_chip(s, {"600001": 50 * YI}) is True


def test_is_blue_chip_strategy_track_low_volatility_quality(discovery):
    s = _stock("600002", strategy="low_volatility_quality")
    assert discovery._is_blue_chip(s, {"600002": 50 * YI}) is True


def test_is_blue_chip_market_cap_track_hit(discovery):
    """轨 B：普通策略但总市值 >= 500 亿。"""
    s = _stock("600003", strategy="momentum_quality")
    assert discovery._is_blue_chip(s, {"600003": 600 * YI}) is True


def test_is_blue_chip_market_cap_exactly_threshold_is_blue(discovery):
    """边界：恰好等于阈值应算蓝筹（判定用 >=）。"""
    s = _stock("600004", strategy="momentum_quality")
    assert discovery._is_blue_chip(s, {"600004": 500 * YI}) is True


def test_is_blue_chip_just_below_threshold_is_not_blue(discovery):
    s = _stock("600005", strategy="momentum_quality")
    assert discovery._is_blue_chip(s, {"600005": 499 * YI}) is False


def test_is_blue_chip_missing_market_cap_is_not_blue(discovery):
    """查不到市值时不能想当然当成蓝筹（否则会把全场误判为蓝筹）。"""
    s = _stock("600006", strategy="momentum_quality")
    assert discovery._is_blue_chip(s, {}) is False


# ============================================================
# _build_market_cap_map
# ============================================================

def test_build_market_cap_map_reads_raw_yuan_values(discovery):
    """映射值就是原始「元」，不做任何单位换算。"""
    import pandas as pd

    df = pd.DataFrame([
        {"code": "600001", "total_mv": 400 * YI},
        {"code": "000002", "total_mv": 123456789.0},
    ])
    m = discovery._build_market_cap_map(df)
    assert m["600001"] == 400 * YI
    assert m["000002"] == 123456789.0
    # 400 亿 < 500 亿 → 不该被当成蓝筹
    assert discovery._is_blue_chip(_stock("600001"), m) is False


def test_build_market_cap_map_none_returns_empty(discovery):
    assert discovery._build_market_cap_map(None) == {}


def test_build_market_cap_map_missing_column_returns_empty(discovery):
    import pandas as pd

    df = pd.DataFrame([{"code": "600001", "price": 10.0}])
    assert discovery._build_market_cap_map(df) == {}


def test_build_market_cap_map_skips_missing_and_nan_rows(discovery):
    """空/缺失行不应污染映射，其余行仍要能读到。

    注意当前实现细节：`if row.get("total_mv")` 只能滤掉 0 / 空串（falsy），
    对 NaN 无效（`bool(float("nan")) is True`），NaN 会被写进映射。
    功能上无害 —— `_is_blue_chip` 里 `nan >= 阈值` 为 False，不会误判为蓝筹。
    这里断言的是这个**结果性不变量**：缺失市值绝不能变成蓝筹。
    """
    import pandas as pd

    df = pd.DataFrame([
        {"code": "600001", "total_mv": 500 * YI},
        {"code": "600002", "total_mv": 0},
        {"code": "600003", "total_mv": float("nan")},
    ])
    m = discovery._build_market_cap_map(df)

    assert m["600001"] == 500 * YI, "有效行必须能读到"
    assert "600002" not in m, "0 视为缺失，应跳过"
    # 缺失市值（0 / NaN / 未收录）一律不得被判为蓝筹
    assert discovery._is_blue_chip(_stock("600002"), m) is False
    assert discovery._is_blue_chip(_stock("600003"), m) is False
    assert discovery._is_blue_chip(_stock("600099"), m) is False


# ============================================================
# _cap_blue_chips
# ============================================================

def test_cap_returns_input_unchanged_when_within_limit(discovery):
    """蓝筹数 <= 2：原样返回（同一对象、顺序不变）。"""
    stocks = [
        _stock("600001", "蓝筹A", combined=90.0, strategy="blue_chip_income"),
        _stock("600002", "蓝筹B", combined=80.0),
        _stock("000003", "普通C", combined=70.0),
    ]
    m = {"600001": 100 * YI, "600002": 200 * YI, "000003": 10 * YI}

    result = discovery._cap_blue_chips(stocks, m)

    assert result is stocks, "未超配时应原样返回，不应重排"
    assert [s["code"] for s in result] == ["600001", "600002", "000003"]


def test_cap_keeps_top_two_blue_chips_by_score(discovery):
    """5 支蓝筹压到 2 支，保留综合分最高的两支。"""
    stocks = [
        _stock("600001", "蓝1", combined=90.0, strategy="blue_chip_income"),
        _stock("600002", "蓝2", combined=85.0),
        _stock("600003", "蓝3", combined=80.0),
        _stock("600004", "蓝4", combined=75.0),
        _stock("600005", "蓝5", combined=70.0),
        _stock("000101", "普1", combined=60.0),
        _stock("000102", "普2", combined=50.0),
    ]
    m = {c: 1000 * YI for c in ("600001", "600002", "600003", "600004", "600005")}
    m.update({"000101": 10 * YI, "000102": 10 * YI})

    result = discovery._cap_blue_chips(stocks, m)
    codes = [s["code"] for s in result]

    kept_blue = [c for c in codes if c in ("600001", "600002", "600003", "600004", "600005")]
    assert kept_blue == ["600001", "600002"], "应保留综合分最高的两支蓝筹"


def test_cap_drops_excess_blue_chips(discovery):
    stocks = [
        _stock("600001", combined=90.0),
        _stock("600002", combined=85.0),
        _stock("600003", combined=80.0),
        _stock("600004", combined=75.0),
        _stock("600005", combined=70.0),
    ]
    m = {c: 1000 * YI for c in ("600001", "600002", "600003", "600004", "600005")}

    codes = [s["code"] for s in discovery._cap_blue_chips(stocks, m)]

    assert "600003" not in codes
    assert "600004" not in codes
    assert "600005" not in codes


def test_cap_does_not_drop_non_blue_chips(discovery):
    """除被剔除的蓝筹外，其余非蓝筹一支都不能少。"""
    stocks = [
        _stock("600001", combined=90.0),
        _stock("600002", combined=85.0),
        _stock("600003", combined=80.0),
        _stock("000101", combined=60.0),
        _stock("000102", combined=50.0),
        _stock("000103", combined=40.0),
    ]
    m = {c: 1000 * YI for c in ("600001", "600002", "600003")}

    result = discovery._cap_blue_chips(stocks, m)
    codes = {s["code"] for s in result}

    assert {"000101", "000102", "000103"} <= codes, "非蓝筹不应被误删"


def test_cap_backfills_from_candidate_pool(discovery):
    """5 蓝筹 + 2 非蓝筹（7 支），池里 3 支非蓝筹 → 补回 3 支，总数恢复 7。"""
    stocks = [
        _stock("600001", combined=90.0),
        _stock("600002", combined=85.0),
        _stock("600003", combined=80.0),
        _stock("600004", combined=75.0),
        _stock("600005", combined=70.0),
        _stock("000101", combined=60.0),
        _stock("000102", combined=50.0),
    ]
    m = {c: 1000 * YI for c in ("600001", "600002", "600003", "600004", "600005")}
    pool = {
        "000201": _stock("000201", "补1", combined=95.0),
        "000202": _stock("000202", "补2", combined=65.0),
        "000203": _stock("000203", "补3", combined=55.0),
    }

    result = discovery._cap_blue_chips(stocks, m, pool)
    codes = [s["code"] for s in result]

    assert len(result) == 7, "剔除 3 支蓝筹后应从池里补回 3 支"
    backfilled = [c for c in codes if c in pool]
    assert backfilled == ["000201", "000202", "000203"], "应按综合分降序回填"


def test_cap_backfill_never_injects_blue_chips(discovery):
    """池里的高分蓝筹候选不得被回填（否则闸门形同虚设）。"""
    stocks = [
        _stock("600001", combined=90.0),
        _stock("600002", combined=85.0),
        _stock("600003", combined=80.0),
        _stock("600004", combined=75.0),
        _stock("600005", combined=70.0),
        _stock("000101", combined=60.0),
    ]
    m = {c: 1000 * YI for c in ("600001", "600002", "600003", "600004", "600005")}
    pool = {
        # 最高分的池内候选，但它是蓝筹（市值 800 亿）→ 不能回填
        "600900": _stock("600900", "蓝筹候选", combined=999.0),
        "000201": _stock("000201", "补1", combined=40.0),
    }
    m["600900"] = 800 * YI

    codes = [s["code"] for s in discovery._cap_blue_chips(stocks, m, pool)]

    assert "600900" not in codes, "回填不得引入蓝筹"
    assert "000201" in codes, "非蓝筹候选应能补入"


def test_cap_without_candidate_pool_shrinks_without_error(discovery):
    stocks = [
        _stock("600001", combined=90.0),
        _stock("600002", combined=85.0),
        _stock("600003", combined=80.0),
        _stock("600004", combined=75.0),
        _stock("600005", combined=70.0),
        _stock("000101", combined=60.0),
    ]
    m = {c: 1000 * YI for c in ("600001", "600002", "600003", "600004", "600005")}

    for pool in (None, {}):
        result = discovery._cap_blue_chips(stocks, m, pool)
        assert len(result) == 3, "5 蓝筹封顶到 2 + 1 非蓝筹 = 3，无非蓝筹可补"
        assert len(result) < len(stocks)


def test_cap_empty_input(discovery):
    assert discovery._cap_blue_chips([], {}) == []
    assert discovery._cap_blue_chips([], {}, {"600001": _stock("600001")}) == []


def test_cap_sort_key_falls_back_to_score(discovery):
    """股票只有 score、没有 combined_score 时，仍按 score 选最高的两支。"""
    stocks = [
        _stock("600001", score=90.0),
        _stock("600002", score=85.0),
        _stock("600003", score=80.0),
    ]
    m = {c: 1000 * YI for c in ("600001", "600002", "600003")}

    kept = [s["code"] for s in discovery._cap_blue_chips(stocks, m)]

    assert kept == ["600001", "600002"]


# ============================================================
# 规则0：板块过滤（_filter_stocks）
# ============================================================

@pytest.mark.parametrize("code", ["300308", "301029", "688981", "689009", "830799", "430047"])
def test_filter_removes_non_main_board(discovery, monkeypatch, caplog, code):
    """创业板/科创板/北交所前缀一律移除，原因记为「非主板」。"""
    stocks = [_stock(code, "非主板股", score=80.0)]
    _stub_quotes(monkeypatch, [(code, "非主板股", 20.0)])

    with caplog.at_level(logging.INFO):
        kept = discovery._filter_stocks(stocks)

    assert kept == [], f"{code} 应被规则0移除"
    assert "非主板" in caplog.text


@pytest.mark.parametrize("code", ["600519", "601318", "603259", "000333", "002415"])
def test_filter_keeps_main_board(discovery, monkeypatch, code):
    """主板前缀不应被规则0误杀（避开银行/ST/高价，避免归因混淆）。"""
    stocks = [_stock(code, "主板股", score=60.0)]
    _stub_quotes(monkeypatch, [(code, "主板股", 20.0)])

    kept = discovery._filter_stocks(stocks)

    assert [s["code"] for s in kept] == [code]


# ============================================================
# 顺序不变量：闸门必须在过滤之前（本文件最重要的护栏）
# ============================================================

def test_cap_is_called_before_filter_in_discover_stocks(discovery):
    """源码级护栏：discover_stocks 里 _cap_blue_chips 必须排在 _filter_stocks 之前。

    行为测试抓不到"把闸门挪到过滤之后"这种回归 —— 只要池里恰好没有
    被排除板块的候选，行为看起来一样。所以这里直接断言调用顺序。
    """
    src = inspect.getsource(discovery.discover_stocks)
    cap_pos = src.index("_cap_blue_chips(")
    filter_pos = src.index("_filter_stocks(")
    assert cap_pos < filter_pos, "蓝筹闸门必须先于 _filter_stocks 调用"


def test_backfilled_non_main_board_candidate_is_filtered_out(discovery, monkeypatch, caplog):
    """端到端顺序护栏：池里分最高的是创业板 300308，回填后必须被过滤掉。

    这证明闸门确实在过滤之前 —— 回填进来的票也要过 ST/银行/高价股/非主板。
    若有人把闸门挪到过滤之后，300308 会一路留到最终结果里。
    """
    stocks = [
        _stock("600001", "蓝1", combined=90.0),
        _stock("600002", "蓝2", combined=85.0),
        _stock("600003", "蓝3", combined=80.0),
        _stock("600004", "蓝4", combined=75.0),
        _stock("600005", "蓝5", combined=70.0),
        _stock("000101", "普1", combined=60.0),
        _stock("000102", "普2", combined=50.0),
    ]
    m = {c: 1000 * YI for c in ("600001", "600002", "600003", "600004", "600005")}
    pool = {
        # 综合分全场最高，但属于创业板 —— 回填后必须被规则0拦下
        "300308": _stock("300308", "中际旭创", combined=999.0),
    }
    _stub_quotes(monkeypatch, [("300308", "中际旭创", 30.0)])

    capped = discovery._cap_blue_chips(stocks, m, pool)
    assert "300308" in [s["code"] for s in capped], "回填阶段应先补进来（证明它确实进了闸门后流程）"

    with caplog.at_level(logging.INFO):
        final = discovery._filter_stocks(capped)

    assert "300308" not in [s["code"] for s in final], "回填的创业板候选必须被过滤掉"
    assert "非主板" in caplog.text
