# -*- coding: utf-8 -*-
# ============================================================
# test_skills_single_call.py
# 覆盖 run_skills_evaluation.py 的「单次调用 + 429 不放大」改造。
#
# 背景（实测）：agnes-ai 免费档有效速率约「每分钟 1 次成功请求」，
# 总耗时下限 = 调用次数 ÷ RPM。旧实现有两处放大：
#   1) 合并调用用宽泛 except 吞掉 429 → 立刻改发 19 次单独请求（1 次失败变 19 次）；
#   2) 每股还要额外调 1 次 generate_detailed_report → 每股 2 次调用。
#
# 本文件的护栏：
#   1. 限流失败绝不触发 19 次兜底（核心）
#   2. 非限流失败仍然触发 19 次兜底（且硬上限 19 次）
#   3. 兜底途中遇限流立即中止
#   4. 合并调用成功时每股只发生 1 次 LLM 调用
#   5. report 缺失时走本地模板报告（0 次额外 LLM 调用），字段齐全
#   6. 模板报告能被 feishu_notifier.build_card_message 正常渲染
#
# 运行：python -m pytest tests/test_skills_single_call.py -q
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
def evaluator():
    return _load("run_skills_evaluation")


@pytest.fixture(scope="module")
def notifier():
    return _load("feishu_notifier")


# ------------------------------------------------------------
# LLM 替身：把 call_with_retry 换成一个「计数 + 返回固定 JSON」的桩
# ------------------------------------------------------------

class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]


def _skills_payload(skill_keys, score=72):
    return [
        {
            "skill_key": key,
            "skill_name": key,
            "score": score,
            "judgment": f"{key} 判断",
            "core_risk": "测试风险",
        }
        for key in skill_keys
    ]


def _report_payload(stock_code="600519", stock_name="贵州茅台"):
    return {
        "stock_code": stock_code,
        "stock_name": stock_name,
        "overall_score": 78,
        "recommendation": "增持",
        "summary": "模型版摘要",
        "sections": {"company_overview": "模型版公司概况"},
        "skill_scores": {"avg_score": 72, "failed_count": 0},
    }


def _counting_llm(monkeypatch, evaluator, payload):
    """把 call_with_retry 换成计数桩，返回内容为 payload（dict/list）。"""
    calls = {"n": 0}

    def fake_call_with_retry(fn, *, label="LLM", max_retries=None):
        calls["n"] += 1
        return _FakeResponse(json.dumps(payload, ensure_ascii=False))

    monkeypatch.setattr(evaluator, "call_with_retry", fake_call_with_retry)
    return calls


# ------------------------------------------------------------
# 1. 限流失败绝不触发 19 次兜底（核心护栏）
# ------------------------------------------------------------

def test_rate_limit_does_not_trigger_19_calls(evaluator, monkeypatch):
    """合并调用抛 429 时，绝不能改发 19 次单独请求（那正是 429 放大器）。"""
    def raise_429(*a, **k):
        raise Exception("Error code: 429 - rate limit exceeded")

    monkeypatch.setattr(evaluator, "call_all_skills_llm", raise_429)

    single_calls = {"n": 0}

    def counting_single(*a, **k):
        single_calls["n"] += 1
        return {}

    monkeypatch.setattr(evaluator, "call_skill_llm", counting_single)

    result = evaluator.evaluate_single_stock("600519")

    assert single_calls["n"] == 0, "限流失败触发了 19 次兜底 —— 429 被放大"
    assert result["reliable"] is False
    assert result["llm_failed_count"] == 19
    assert len(result["skill_results"]) == 19
    # 仍要有可渲染的报告
    assert result["detailed_report"]["summary"]


# ------------------------------------------------------------
# 2. 非限流失败仍然触发 19 次兜底（硬上限 19）
# ------------------------------------------------------------

def test_non_rate_limit_failure_triggers_19_fallback(evaluator, monkeypatch):
    monkeypatch.setattr(evaluator, "call_all_skills_llm", lambda *a, **k: ([], None))
    monkeypatch.setattr(evaluator.time, "sleep", lambda s: None)

    calls = {"n": 0}

    def ok_single(stock_code, stock_name, key):
        calls["n"] += 1
        return {
            "stock_code": stock_code,
            "skill_executed": key,
            "skill_name": key,
            "score": 60,
            "judgment": "ok",
            "core_risk": "风险",
        }

    monkeypatch.setattr(evaluator, "call_skill_llm", ok_single)

    result = evaluator.evaluate_single_stock("000858")

    assert calls["n"] == len(evaluator.SKILLS_CONFIG) == 19
    assert result["llm_failed_count"] == 0


def test_fallback_aborts_immediately_on_rate_limit(evaluator, monkeypatch):
    """兜底途中一旦遇限流，立即中止剩余调用（避免兜底本身变成放大器）。"""
    monkeypatch.setattr(evaluator, "call_all_skills_llm", lambda *a, **k: ([], None))
    monkeypatch.setattr(evaluator.time, "sleep", lambda s: None)

    calls = {"n": 0}

    def flaky_single(stock_code, stock_name, key):
        calls["n"] += 1
        if calls["n"] == 3:
            raise Exception("429 too many requests")
        return {
            "stock_code": stock_code,
            "skill_executed": key,
            "skill_name": key,
            "score": 60,
            "judgment": "ok",
            "core_risk": "风险",
        }

    monkeypatch.setattr(evaluator, "call_skill_llm", flaky_single)

    result = evaluator.evaluate_single_stock("000001")

    assert calls["n"] == 3, f"限流后应立刻中止，实际调用 {calls['n']} 次"
    assert len(result["skill_results"]) == 19, "剩余 Skill 应以失败占位补齐"
    assert result["llm_failed_count"] >= 1


# ------------------------------------------------------------
# 3. 合并调用成功时，每股只发生 1 次 LLM 调用
# ------------------------------------------------------------

def test_merged_success_uses_one_llm_call(evaluator, monkeypatch):
    payload = {
        "skills": _skills_payload(list(evaluator.SKILLS_CONFIG.keys())),
        "report": _report_payload(),
    }
    calls = _counting_llm(monkeypatch, evaluator, payload)

    def must_not_call(*a, **k):
        raise AssertionError("合并调用已含 report，不应再调 generate_detailed_report")

    monkeypatch.setattr(evaluator, "generate_detailed_report", must_not_call)

    result = evaluator.evaluate_single_stock("600519")

    assert calls["n"] == 1, f"每股应只调用 1 次 LLM，实际 {calls['n']} 次"
    assert result["final_score"] == 72
    assert result["detailed_report"]["summary"] == "模型版摘要"


def test_call_all_skills_returns_report_and_skills(evaluator, monkeypatch):
    payload = {
        "skills": _skills_payload(list(evaluator.SKILLS_CONFIG.keys())),
        "report": _report_payload(),
    }
    _counting_llm(monkeypatch, evaluator, payload)

    skill_results, report = evaluator.call_all_skills_llm("600519", "贵州茅台")

    assert len(skill_results) == 19
    assert report is not None and report["summary"] == "模型版摘要"


def test_call_all_skills_reraises_rate_limit(evaluator, monkeypatch):
    """限流必须向上抛出，而不是被吞掉后返回空列表触发兜底。"""
    def raise_429(fn, *, label="LLM", max_retries=None):
        raise Exception("429 rate limit")

    monkeypatch.setattr(evaluator, "call_with_retry", raise_429)

    with pytest.raises(Exception, match="429"):
        evaluator.call_all_skills_llm("600519", "贵州茅台")


# ------------------------------------------------------------
# 4. report 缺失 → 本地模板报告（0 次额外 LLM 调用），字段齐全
# ------------------------------------------------------------

def test_missing_report_uses_template_without_extra_call(evaluator, monkeypatch):
    # 只返回 skills（旧格式裸数组），report 缺失
    payload = _skills_payload(list(evaluator.SKILLS_CONFIG.keys()))
    calls = _counting_llm(monkeypatch, evaluator, payload)

    def must_not_call(*a, **k):
        raise AssertionError("report 缺失时应走本地模板，不得再调 LLM")

    monkeypatch.setattr(evaluator, "generate_detailed_report", must_not_call)

    result = evaluator.evaluate_single_stock("600519")
    report = result["detailed_report"]

    assert calls["n"] == 1, "模板报告不应产生额外 LLM 调用"
    assert "自动归纳" in report["summary"]
    # 字段齐全（与 generate_detailed_report 兼容）
    assert set(report) >= {
        "stock_code", "stock_name", "overall_score", "recommendation",
        "summary", "sections", "skill_scores",
    }
    assert set(report["sections"]) >= {
        "company_overview", "core_competitiveness", "financial_health",
        "industry_policy", "management_governance", "risk_warnings",
        "valuation_advice",
    }
    assert set(report["skill_scores"]) >= {
        "avg_score", "highest_score", "lowest_score", "failed_count",
    }
    assert report["skill_scores"]["highest_score"]["skill"]
    assert report["skill_scores"]["lowest_score"]["skill"]


def test_normalize_report_fills_missing_fields(evaluator):
    """LLM 报告缺字段时，用模板骨架补齐，保证飞书渲染不崩。"""
    skills = evaluator.build_template_report("600519", "贵州茅台", [])["sections"]
    partial = {"summary": "只有摘要"}
    merged = evaluator._normalize_report(partial, "600519", "贵州茅台", [])

    assert merged["summary"] == "只有摘要"
    assert set(merged["sections"]) >= set(skills)
    assert set(merged["skill_scores"]) >= {"avg_score", "failed_count"}


# ------------------------------------------------------------
# 5. 模板报告能被 feishu_notifier 正常渲染
# ------------------------------------------------------------

def test_template_report_renders_in_feishu_card(evaluator, notifier):
    skill_results = [
        {
            "stock_code": "600519",
            "skill_executed": key,
            "skill_name": key,
            "score": 75,
            "judgment": "判断",
            "core_risk": "风险",
        }
        for key in evaluator.SKILLS_CONFIG
    ]
    report = evaluator.build_template_report("600519", "贵州茅台", skill_results)

    result = {
        "stock_code": "600519",
        "stock_name": "贵州茅台",
        "final_score": 75,
        "highlights": ["a（75分）"],
        "risks": ["风险"],
        "llm_failed_count": 0,
        "detailed_report": report,
    }

    card = notifier.build_card_message([result])
    text = json.dumps(card, ensure_ascii=False)

    assert card["msg_type"] == "interactive"
    assert "详细分析报告" in text
    assert "自动归纳" in text
    assert "综合评分" in text
