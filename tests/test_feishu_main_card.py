# ============================================================
# test_feishu_main_card.py
# 覆盖主节点飞书卡片新增的「短线热点题材」块。
#
# 背景：主仓库除了把初选股票派发给账户 B / C，还需要在飞书卡片里总结
# 短线热点题材并给出逻辑。产品评审确定的版式顺序是：
#
#     初选清单 div → 题材 div（可选） → 触发状态 div
#
# 题材块必须独立成块、位置固定在初选清单下方、触发状态上方。降级（未走
# 模型分析）时必须显式说明，避免用户误以为逻辑是模型给出的。
#
# 边界：账户 B / C 用的 build_card_message() 绝不能带上题材块。
#
# 运行：python -m pytest tests/test_feishu_main_card.py -q
# ============================================================

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"

THEME_TITLE = "今日短线热点题材"
DEGRADED_NOTE = "非模型分析"


def _load(name: str):
    """按文件路径加载 scripts/ 下的脚本（它们不是包，无法直接 import）。

    注意：feishu_notifier 在 import 时会加载 .env 并配置日志，属于模块级副作用，
    这里直接容忍（不改变行为）。
    """
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def notifier():
    return _load("feishu_notifier")


# ------------------------------------------------------------
# 测试夹具
# ------------------------------------------------------------

def _theme_summary(llm_used: bool = False) -> dict:
    """构造 3 个题材的题材总结（第三个 leaders 为空，用来验证「暂无」）。"""
    return {
        "generated_at": "2026-09-24 02:38:34",
        "source": "sina_industry",
        "llm_used": llm_used,
        "market_pulse": {"total": 3496, "up": 823, "down": 2299},
        "themes": [
            {
                "name": "物资外贸",
                "change_pct": 1.27,
                "leaders": ["汇通能源"],
                "logic": "板块涨跌 1.27%，领涨股 汇通能源",
            },
            {
                "name": "摩托车",
                "change_pct": 0.67,
                "leaders": ["宗申动力", "钱江摩托"],
                "logic": "板块涨跌 0.67%，领涨股 宗申动力",
            },
            {
                "name": "水泥行业",
                "change_pct": -0.65,
                "leaders": [],
                "logic": "板块涨跌 -0.65%，暂无领涨股",
            },
        ],
        "summary": "全市场上涨 823 家、下跌 2299 家，涨停 44 家",
    }


def _build(notifier, theme_summary=None):
    return notifier.build_main_card(
        stocks="601318,000001",
        first5="601318",
        last5="000001",
        code_b="201",
        code_c="204",
        theme_summary=theme_summary,
    )


def _div_contents(card: dict) -> list[str]:
    """按顺序取出所有 div 的 lark_md 文本。"""
    contents = []
    for el in card["card"]["elements"]:
        text = el.get("text")
        if isinstance(text, dict) and text.get("tag") == "lark_md":
            contents.append(text.get("content") or "")
    return contents


def _find_index(card: dict, needle: str) -> int:
    """返回含指定特征文本的元素下标，找不到返回 -1。"""
    for i, el in enumerate(card["card"]["elements"]):
        text = el.get("text")
        if isinstance(text, dict) and needle in (text.get("content") or ""):
            return i
    return -1


def _theme_block(card: dict) -> str:
    """取出题材块的文本内容；不存在返回空串。"""
    idx = _find_index(card, THEME_TITLE)
    if idx < 0:
        return ""
    return card["card"]["elements"][idx]["text"]["content"]


# ------------------------------------------------------------
# 1. 完整题材数据渲染
# ------------------------------------------------------------

def test_theme_block_renders_all_three_themes(notifier):
    card = _build(notifier, _theme_summary())
    block = _theme_block(card)

    assert block, "传了题材数据却没有渲染出题材块"

    # 三个题材名都在
    for name in ("物资外贸", "摩托车", "水泥行业"):
        assert name in block, f"题材块缺少题材名：{name}"

    # 板块均涨幅（保留两位小数 + %）
    assert "1.27%" in block
    assert "0.67%" in block
    assert "-0.65%" in block

    # 代表股（多个用 、 连接）
    assert "汇通能源" in block
    assert "宗申动力、钱江摩托" in block
    # leaders 为空时显示「暂无」
    assert "暂无" in block

    # 逻辑文字
    assert "领涨股 汇通能源" in block


def test_theme_block_shows_summary_line(notifier):
    card = _build(notifier, _theme_summary())
    block = _theme_block(card)
    assert "全市场上涨 823 家、下跌 2299 家，涨停 44 家" in block


# ------------------------------------------------------------
# 2. 位置：题材块夹在初选清单与触发状态之间
# ------------------------------------------------------------

def test_theme_block_position_between_picks_and_status(notifier):
    card = _build(notifier, _theme_summary())

    picks_idx = _find_index(card, "初选股票")
    theme_idx = _find_index(card, THEME_TITLE)
    status_idx = _find_index(card, "触发状态")

    assert picks_idx >= 0, "未找到初选清单块"
    assert status_idx >= 0, "未找到触发状态块"
    assert theme_idx >= 0, "未找到题材块"
    assert picks_idx < theme_idx < status_idx, (
        f"题材块位置错误：初选={picks_idx} 题材={theme_idx} 触发={status_idx}"
    )


# ------------------------------------------------------------
# 3 / 4. 缺数据时整块跳过，不产生空壳 div
# ------------------------------------------------------------

def test_none_theme_summary_skips_block(notifier):
    without = _build(notifier)
    with_none = _build(notifier, None)

    assert THEME_TITLE not in json.dumps(with_none, ensure_ascii=False), "None 时不应出现题材块"
    assert len(without["card"]["elements"]) == len(with_none["card"]["elements"]), (
        "None 时元素数量应与不传一致，不应出现空壳 div"
    )


def test_empty_themes_skips_block(notifier):
    empty = _theme_summary()
    empty["themes"] = []

    without = _build(notifier)
    card = _build(notifier, empty)
    assert THEME_TITLE not in json.dumps(card, ensure_ascii=False)
    assert len(without["card"]["elements"]) == len(card["card"]["elements"])


# ------------------------------------------------------------
# 5. 进度条与评分梯队展示测试
# ------------------------------------------------------------

def test_render_progress_bar(notifier):
    assert notifier._render_progress_bar(100, width=10) == "[██████████]"
    assert notifier._render_progress_bar(0, width=10) == "[░░░░░░░░░░]"
    assert notifier._render_progress_bar(50, width=10) == "[█████░░░░░]"
    assert notifier._render_progress_bar(86, width=10) == "[█████████░]"
    assert notifier._render_progress_bar("invalid") == "—"


def test_build_card_message_contains_ladder(notifier):
    results = [
        {"stock_code": "601668", "stock_name": "中国建筑", "final_score": 86},
        {"stock_code": "002487", "stock_name": "大金重工", "final_score": 79},
        {"stock_code": "002011", "stock_name": "盾安环境", "final_score": 33},
    ]
    card = notifier.build_card_message(results)
    raw = json.dumps(card, ensure_ascii=False)
    assert "候选标的评分梯队直观总览" in raw
    assert "中国建筑" in raw and "大金重工" in raw
    assert "[████████" in raw


# ------------------------------------------------------------
# 5. 降级说明：仅 llm_used=False 时出现
# ------------------------------------------------------------

def test_degraded_note_shown_when_llm_not_used(notifier):
    block = _theme_block(_build(notifier, _theme_summary(llm_used=False)))
    assert DEGRADED_NOTE in block, "llm_used=False 时必须出现降级说明"


def test_degraded_note_absent_when_llm_used(notifier):
    block = _theme_block(_build(notifier, _theme_summary(llm_used=True)))
    assert DEGRADED_NOTE not in block, "llm_used=True 时不应出现降级说明"


def test_degraded_note_shown_when_llm_flag_missing(notifier):
    """字段缺失视为未走模型分析，仍应给出降级说明。"""
    summary = _theme_summary()
    summary.pop("llm_used")
    block = _theme_block(_build(notifier, summary))
    assert DEGRADED_NOTE in block


# ------------------------------------------------------------
# 6. logic 截断到 50 字符
# ------------------------------------------------------------

def test_long_logic_is_truncated(notifier):
    long_logic = "资" * 80  # 80 个中文字符
    summary = _theme_summary()
    summary["themes"] = [{
        "name": "测试题材",
        "change_pct": 1.00,
        "leaders": ["测试股"],
        "logic": long_logic,
    }]

    block = _theme_block(_build(notifier, summary))

    assert "…" in block, "超长 logic 应以省略号收尾"
    assert long_logic not in block, "超长 logic 不应原样出现"
    assert ("资" * 50) in block, "应保留前 50 个字符"


def test_short_logic_not_truncated(notifier):
    summary = _theme_summary()
    summary["themes"] = [{
        "name": "测试题材",
        "change_pct": 1.00,
        "leaders": ["测试股"],
        "logic": "短线资金抱团",
    }]
    block = _theme_block(_build(notifier, summary))
    assert "短线资金抱团" in block
    assert "…" not in block


# ------------------------------------------------------------
# 7. 题材块文案不含 ASCII 字母（守住「不要有英文」）
# ------------------------------------------------------------

def test_theme_block_has_no_ascii_letters(notifier):
    block = _theme_block(_build(notifier, _theme_summary()))
    assert not re.search(r"[A-Za-z]", block), f"题材块出现英文/ASCII 字母：{block!r}"


# ------------------------------------------------------------
# 8. B/C 卡片（build_card_message）不带题材块
# ------------------------------------------------------------

def test_slave_card_has_no_theme_block(notifier):
    results = [{
        "stock_code": "601318",
        "stock_name": "中国平安",
        "final_score": 82.0,
        "highlights": ["估值合理"],
        "risks": ["市场波动"],
    }]
    card = notifier.build_card_message(results)
    rendered = json.dumps(card, ensure_ascii=False)

    assert THEME_TITLE not in rendered, "账户 B/C 卡片不应带题材块"
    assert "板块均涨" not in rendered, "账户 B/C 卡片不应带题材块字段"
