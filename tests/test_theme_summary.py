# ============================================================
# test_theme_summary.py
# 覆盖 scripts/theme_summary.py：
#   - resolve_llm_config() 的解析链（复刻 run_skills_evaluation.py:38-79）
#   - fetch_industry_boards() 的新浪板块解析与降级
#   - compute_market_pulse() / top_gainers() / _extract_json()
#   - build_theme_summary() 的 llm_used 信号与不抛异常保证
#
# 运行：
#   python -m pytest tests/test_theme_summary.py -p no:cacheprovider -q
# ============================================================

import importlib.util
import json
import sys
from pathlib import Path

import pytest

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
def theme():
    return _load("theme_summary")


class _FakeResponse:
    """够用的 requests.Response 替身：只读 .text 并允许设置 .encoding。"""

    def __init__(self, text: str, encoding: str = "gbk"):
        self.text = text
        self.encoding = encoding

    def raise_for_status(self):
        return None


def _sina_body(rows):
    """拼出新浪格式的板块响应体；rows 为逗号分隔的原始行。"""
    payload = ",".join(f'"{i}":"{row}"' for i, row in enumerate(rows))
    return "var S_Finance_bankuai_sinaindustry = {" + payload + "};"


# ------------------------------------------------------------
# 1-4. resolve_llm_config 解析链
# ------------------------------------------------------------

def test_resolve_llm_config_channel(theme):
    env = {
        "LLM_CHANNELS": "my_proxy",
        "LLM_MY_PROXY_BASE_URL": "https://x/v1",
        "LLM_MY_PROXY_API_KEY": "k",
        "LLM_MY_PROXY_MODELS": "agnes-2.0-flash,other",
    }
    assert theme.resolve_llm_config(env) == ("https://x/v1", "k", "agnes-2.0-flash")


def test_resolve_llm_config_proxy_without_channels(theme):
    env = {
        "LLM_MY_PROXY_BASE_URL": "https://proxy/v1",
        "LLM_MY_PROXY_API_KEY": "pk",
        "LLM_MY_PROXY_MODELS": "agnes-2.0-flash",
    }
    assert theme.resolve_llm_config(env) == ("https://proxy/v1", "pk", "agnes-2.0-flash")


def test_resolve_llm_config_generic_base_url(theme):
    env = {"LLM_BASE_URL": "https://generic/v1", "LLM_API_KEY": "gk"}
    base_url, api_key, model = theme.resolve_llm_config(env)
    assert base_url == "https://generic/v1"
    assert api_key == "gk"
    assert model == "agnes-3.0-flash"


def test_resolve_llm_config_falls_back_when_channel_incomplete(theme):
    """LLM_CHANNELS 非空但渠道变量全空时，仍应回退到 LLM_BASE_URL（加固点）。"""
    env = {
        "LLM_CHANNELS": "my_proxy",
        "LLM_BASE_URL": "https://generic/v1",
        "LLM_API_KEY": "gk",
    }
    assert theme.resolve_llm_config(env) == ("https://generic/v1", "gk", "agnes-3.0-flash")


# ------------------------------------------------------------
# 5-7. fetch_industry_boards
# ------------------------------------------------------------

def test_fetch_industry_boards_parses_and_sorts(theme, monkeypatch):
    # 字段位置：[0]node [1]名称 [2]成分数 [3]均价 [4]均涨额 [5]涨跌幅
    #          [7]成交额 [8]领涨股代码 [9]领涨股涨跌 [10]领涨股价 [12]领涨股名
    rows = [
        "new_blhy,玻璃行业,19,10.5,0.10,1.27,1.00,123456,600605,9.99,12.50,xx,汇通能源",
        "new_dzxx,电子信息,80,20.0,0.02,3.45,0.50,88888,000001,5.50,15.00,yy,平安银行",
    ]
    monkeypatch.setattr(
        theme.requests, "get", lambda *a, **k: _FakeResponse(_sina_body(rows))
    )
    boards = theme.fetch_industry_boards()

    assert [b["name"] for b in boards] == ["电子信息", "玻璃行业"]  # 降序
    top = boards[0]
    assert top["node"] == "new_dzxx"
    assert top["count"] == 80
    assert top["change_pct"] == 3.45
    assert top["amount"] == 88888.0
    assert top["leader_code"] == "000001"
    assert top["leader_name"] == "平安银行"
    assert top["leader_change_pct"] == 5.50


def test_fetch_industry_boards_swallows_network_error(theme, monkeypatch):
    def _boom(*a, **k):
        raise ConnectionError("blocked")

    monkeypatch.setattr(theme.requests, "get", _boom)
    assert theme.fetch_industry_boards() == []


def test_fetch_industry_boards_bad_format_returns_empty(theme, monkeypatch):
    monkeypatch.setattr(
        theme.requests, "get", lambda *a, **k: _FakeResponse("no equals sign here")
    )
    assert theme.fetch_industry_boards() == []


# ------------------------------------------------------------
# 8. compute_market_pulse
# ------------------------------------------------------------

def test_compute_market_pulse(theme):
    stocks = [
        {"change_pct": 10.0, "amount": 1e8},
        {"change_pct": 9.8, "amount": 2e8},
        {"change_pct": 1.0, "amount": 0},
        {"change_pct": -2.0, "amount": 0},
        {"change_pct": 0.0, "amount": 0},
    ]
    pulse = theme.compute_market_pulse(stocks)
    assert pulse["total"] == 5
    assert pulse["up"] == 3
    assert pulse["down"] == 1
    assert pulse["flat"] == 1
    assert pulse["limit_up"] == 2
    assert pulse["median_change"] == 1.0
    assert pulse["amount_yi"] == 3.0


# ------------------------------------------------------------
# 9. top_gainers
# ------------------------------------------------------------

def test_top_gainers_sorted_and_limited(theme):
    stocks = [
        {"code": "a", "name": "A", "change_pct": 1.0},
        {"code": "b", "name": "B", "change_pct": 9.9},
        {"code": "c", "name": "C", "change_pct": 5.5},
    ]
    top = theme.top_gainers(stocks, limit=2)
    assert [s["code"] for s in top] == ["b", "c"]


# ------------------------------------------------------------
# 10. _extract_json
# ------------------------------------------------------------

def test_extract_json_fenced(theme):
    raw = '```json\n{"summary": "s", "themes": []}\n```'
    assert theme._extract_json(raw) == {"summary": "s", "themes": []}


def test_extract_json_with_surrounding_text(theme):
    raw = '好的，以下是结果：{"summary": "s", "themes": []} 希望有帮助'
    assert theme._extract_json(raw) == {"summary": "s", "themes": []}


@pytest.mark.parametrize("raw", ["", "not json at all", "[1, 2, 3]", "{broken"])
def test_extract_json_invalid_returns_none(theme, raw):
    assert theme._extract_json(raw) is None


# ------------------------------------------------------------
# 11-12. build_theme_summary
# ------------------------------------------------------------

def test_build_theme_summary_missing_snapshot(theme, monkeypatch, tmp_path):
    monkeypatch.setattr(theme, "fetch_industry_boards", lambda *a, **k: [])
    monkeypatch.setattr(theme, "summarize_with_llm", lambda *a, **k: None)

    summary = theme.build_theme_summary(tmp_path / "nope.json")
    assert summary["source"] == "snapshot_only"
    assert summary["llm_used"] is False
    assert isinstance(summary["themes"], list)


def test_build_theme_summary_llm_used_true(theme, monkeypatch, tmp_path):
    monkeypatch.setattr(theme, "fetch_industry_boards", lambda *a, **k: [])
    fake = {"summary": "科技领涨", "themes": [{"name": "AI", "logic": "算力需求"}]}
    monkeypatch.setattr(theme, "summarize_with_llm", lambda *a, **k: fake)

    summary = theme.build_theme_summary(tmp_path / "nope.json")
    assert summary["llm_used"] is True
    assert summary["themes"] == fake["themes"]
    assert summary["summary"] == "科技领涨"
    assert summary["source"] == "snapshot_only"
