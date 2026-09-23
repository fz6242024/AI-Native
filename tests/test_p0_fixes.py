# ============================================================
# test_p0_fixes.py
# 覆盖 2026-09-23 代码审查中确认的 4 个 P0 修复：
#   P0-1 collect_skill_scores 下载评分历史的分支探测与回退
#   P0-2 wait_for_workflow 补 conclusion 校验
#   P0-3 排序键与展示键统一为 combined_score
#   P0-4 评估失败（reliable=false）不再被当成低分参与加权
#
# 运行：python -m pytest tests/test_p0_fixes.py -q
# ============================================================

import base64
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
def collector():
    return _load("collect_skill_scores")


@pytest.fixture(scope="module")
def evaluator():
    return _load("run_skills_evaluation")


@pytest.fixture(scope="module")
def discovery():
    return _load("alphasift_discovery")


# ------------------------------------------------------------
# P0-2：wait_for_workflow 必须校验 conclusion
# ------------------------------------------------------------

def _runs_payload(status, conclusion, run_id=42):
    return 200, {
        "workflow_runs": [
            {
                "id": run_id,
                "status": status,
                "conclusion": conclusion,
                "path": ".github/workflows/skills_runner.yml",
            }
        ]
    }


def test_wait_for_workflow_accepts_success(collector, monkeypatch):
    monkeypatch.setattr(collector, "_request", lambda *a, **k: _runs_payload("completed", "success", 7))
    assert collector.wait_for_workflow("owner/repo", "tok") == 7


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out", "startup_failure", None])
def test_wait_for_workflow_rejects_non_success_conclusion(collector, monkeypatch, capsys, conclusion):
    """此前只判 status=="completed"，跑挂也被当成"已完成"，故障被静默吞掉。"""
    monkeypatch.setattr(collector, "_request", lambda *a, **k: _runs_payload("completed", conclusion))
    assert collector.wait_for_workflow("owner/repo", "tok") is None
    out = capsys.readouterr().out
    assert "::warning::" in out, "非 success 结论必须产出 GitHub Actions 告警"


def test_wait_for_workflow_does_not_accept_running(collector, monkeypatch):
    """仍在跑时不应提前返回，而是一直等到超时。"""

    class FakeTime:
        def __init__(self):
            self.t = 0.0
            self.requests = 0

        def time(self):
            return self.t

        def sleep(self, seconds):
            self.t += seconds

    fake = FakeTime()

    def counting_request(*a, **k):
        fake.requests += 1
        return _runs_payload("in_progress", None)

    monkeypatch.setattr(collector, "time", fake)
    monkeypatch.setattr(collector, "_request", counting_request)
    assert collector.wait_for_workflow("owner/repo", "tok", timeout_minutes=1) is None
    assert fake.requests >= 2, "in_progress 期间应反复轮询，不能提前返回 run id"


# ------------------------------------------------------------
# P0-1：候选分支探测与回退
# ------------------------------------------------------------

def test_download_history_file_falls_back_to_main(collector, monkeypatch):
    """硬编码 master 时，仓库用 main 就永远 404 —— 必须回退到 main。"""
    monkeypatch.setattr(collector, "_repo_default_branch", lambda repo, token: None)
    tried = []

    def fake_fetch(repo, token, branch):
        tried.append(branch)
        return [{"date": "2026-09-24", "results": []}] if branch == "main" else None

    monkeypatch.setattr(collector, "_fetch_history_from_branch", fake_fetch)
    result = collector.download_history_file("owner/repo", "tok")
    assert result is not None
    assert tried == ["master", "main"], f"应按候选顺序尝试，实际: {tried}"


def test_download_history_file_prefers_detected_default_branch(collector, monkeypatch):
    """探测到的默认分支应排在 master/main 之前。"""
    monkeypatch.setattr(collector, "_repo_default_branch", lambda repo, token: "trunk")
    tried = []

    def fake_fetch(repo, token, branch):
        tried.append(branch)
        return None

    monkeypatch.setattr(collector, "_fetch_history_from_branch", fake_fetch)
    assert collector.download_history_file("owner/repo", "tok") is None
    assert tried[0] == "trunk", f"默认分支应优先，实际: {tried}"
    assert "master" in tried and "main" in tried


def test_download_history_file_explicit_branch_wins(collector, monkeypatch):
    monkeypatch.setattr(collector, "_repo_default_branch", lambda repo, token: "trunk")
    tried = []
    monkeypatch.setattr(
        collector, "_fetch_history_from_branch",
        lambda repo, token, branch: (tried.append(branch), None)[1],
    )
    collector.download_history_file("owner/repo", "tok", branches=("release",))
    assert tried[0] == "release"


def test_download_history_file_deduplicates_candidates(collector, monkeypatch):
    """显式分支与默认分支相同时不应重复请求。"""
    monkeypatch.setattr(collector, "_repo_default_branch", lambda repo, token: "master")
    tried = []
    monkeypatch.setattr(
        collector, "_fetch_history_from_branch",
        lambda repo, token, branch: (tried.append(branch), None)[1],
    )
    collector.download_history_file("owner/repo", "tok", branches=("master",))
    assert tried == ["master", "main"], f"应去重，实际: {tried}"


def test_download_history_file_returns_none_when_all_miss(collector, monkeypatch):
    monkeypatch.setattr(collector, "_repo_default_branch", lambda repo, token: None)
    monkeypatch.setattr(collector, "_fetch_history_from_branch", lambda repo, token, branch: None)
    assert collector.download_history_file("owner/repo", "tok") is None


def test_fetch_history_returns_none_on_http_error(collector, monkeypatch):
    """HTTP 404 应被吞掉并返回 None（继续试下一个分支），而不是抛异常。"""
    import urllib.error

    def boom(*a, **k):
        raise urllib.error.HTTPError("u", 404, "Not Found", {}, None)

    monkeypatch.setattr(collector, "_request", boom)
    monkeypatch.setattr(collector.urllib.request, "urlopen", boom)
    assert collector._fetch_history_from_branch("owner/repo", "tok", "master") is None


def _payload():
    body = json.dumps([{"date": "2026-09-24", "results": []}]).encode("utf-8")
    return 200, {"content": base64.b64encode(body).decode("ascii")}


def test_fetch_history_retries_transient_api_error(collector, monkeypatch):
    """网络抖动必须重试同一分支。

    否则"网络不通"会被误判成"该分支没有这个文件"，白白试完所有分支后
    报出误导性的"所有候选分支均未取到"——实测本地代理抖动时就是这样。
    """
    monkeypatch.setattr(collector.time, "sleep", lambda s: None)
    calls = []

    def flaky(url, token, method="GET", payload=None):
        calls.append(url)
        if len(calls) == 1:
            raise ConnectionResetError("connection reset by peer")
        return _payload()

    monkeypatch.setattr(collector, "_request", flaky)

    def must_not_fall_back(*a, **k):
        raise AssertionError("API 重试成功后不应再回退到 raw")

    monkeypatch.setattr(collector.urllib.request, "urlopen", must_not_fall_back)

    out = collector._fetch_history_from_branch("owner/repo", "tok", "master")
    assert out == [{"date": "2026-09-24", "results": []}]
    assert len(calls) == 2, f"第一次抖动后应重试同一分支，实际请求 {len(calls)} 次"


def test_fetch_history_does_not_retry_on_404(collector, monkeypatch):
    """404 说明该分支确实没有，应立刻换分支，不做无谓重试。"""
    import urllib.error

    monkeypatch.setattr(collector.time, "sleep", lambda s: None)
    calls = []

    def not_found(url, token, method="GET", payload=None):
        calls.append(url)
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    def raw_not_found(*a, **k):
        raise urllib.error.HTTPError("u", 404, "Not Found", {}, None)

    monkeypatch.setattr(collector, "_request", not_found)
    monkeypatch.setattr(collector.urllib.request, "urlopen", raw_not_found)

    assert collector._fetch_history_from_branch("owner/repo", "tok", "master") is None
    assert len(calls) == 1, f"404 不应重试，实际请求 {len(calls)} 次"


def test_fetch_history_falls_back_to_raw_after_api_exhausted(collector, monkeypatch):
    """API 侧重试耗尽后，仍应尝试 raw 兜底。"""
    monkeypatch.setattr(collector.time, "sleep", lambda s: None)

    def always_reset(*a, **k):
        raise ConnectionResetError("reset")

    monkeypatch.setattr(collector, "_request", always_reset)

    calls = {"n": 0}

    class _Resp:
        def read(self):
            return json.dumps([{"date": "2026-09-24", "results": []}]).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def raw_ok(*a, **k):
        calls["n"] += 1
        return _Resp()

    monkeypatch.setattr(collector.urllib.request, "urlopen", raw_ok)

    assert collector._fetch_history_from_branch("owner/repo", "tok", "master") == [
        {"date": "2026-09-24", "results": []}
    ]
    assert calls["n"] == 1, "raw 一次就成功，不该重复请求"


# ------------------------------------------------------------
# P0-4：评估可靠性判定 + 历史读取跳过不可信记录
# ------------------------------------------------------------

@pytest.mark.parametrize(
    "skill_total, skill_returned, failed, expected",
    [
        (19, 19, 0, True),      # 全成功
        (19, 19, 5, True),      # 5 <= 19*0.3=5.7，仍可信
        (19, 19, 6, False),     # 6 > 5.7，失败过多
        (19, 19, 19, False),    # 全军覆没（就是写 50 分的场景）
        (19, 10, 0, False),     # 返回缺口 9 > 5.7，分数被系统性低估
        (19, 14, 0, True),      # 缺口 5 <= 5.7
        (0, 0, 0, False),       # 无 skill 配置
    ],
)
def test_is_evaluation_reliable(evaluator, skill_total, skill_returned, failed, expected):
    assert evaluator._is_evaluation_reliable(skill_total, skill_returned, failed) is expected


def test_unreliable_ratio_threshold_is_named(evaluator):
    """阈值必须是具名常量，便于日后按数据校准。"""
    assert 0 < evaluator.MAX_UNRELIABLE_RATIO < 1


def _write_history(base: Path, payload, name="2026-09-24_B.json"):
    d = base / "data" / "skill_history"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_load_history_scores_skips_unreliable(discovery, tmp_path):
    """reliable=false 的记录不参与加权 —— 这是 P0-4 的核心。"""
    _write_history(tmp_path, {
        "date": "2026-09-24", "account": "B",
        "results": [
            {"stock_code": "600000", "final_score": 88.0, "reliable": True},
            {"stock_code": "600001", "final_score": 50.0, "reliable": False, "llm_failed_count": 19},
        ],
    })
    scores = discovery._load_history_scores(base_dir=tmp_path)
    assert "600000" in scores and scores["600000"]["avg"] == 88.0
    assert "600001" not in scores, "不可信记录必须被跳过，否则 (50-70)*0.6=-12 会把它降权"


def test_load_history_scores_backward_compatible_without_reliable_field(discovery, tmp_path):
    """老格式没有 reliable 字段，应视为可信（无法判断时不能擅自丢弃）。"""
    _write_history(tmp_path, {
        "date": "2026-09-24", "account": "B",
        "results": [{"stock_code": "600002", "final_score": 75.0}],
    })
    scores = discovery._load_history_scores(base_dir=tmp_path)
    assert scores["600002"]["avg"] == 75.0


def test_load_history_scores_handles_accumulated_format(discovery, tmp_path):
    """run_skills_evaluation 写的是累积列表 [{date, results:[...]}]。"""
    _write_history(tmp_path, [
        {"date": "2026-09-23", "results": [{"stock_code": "600003", "final_score": 60.0}]},
        {"date": "2026-09-24", "results": [{"stock_code": "600003", "final_score": 80.0}]},
    ], name="acc.json")
    scores = discovery._load_history_scores(base_dir=tmp_path)
    assert scores["600003"] == {"avg": 70.0, "count": 2}


def test_load_history_scores_ignores_broken_json(discovery, tmp_path):
    d = tmp_path / "data" / "skill_history"
    d.mkdir(parents=True, exist_ok=True)
    (d / "broken.json").write_text("{not json", encoding="utf-8")
    assert discovery._load_history_scores(base_dir=tmp_path) == {}


def test_load_history_scores_empty_when_no_files(discovery, tmp_path):
    assert discovery._load_history_scores(base_dir=tmp_path) == {}


def test_history_item_score_drops_zero_and_missing(discovery):
    """得分为 0 视为"那次评估崩了"，不是"分数极低"。"""
    assert discovery._history_item_score({"stock_code": "600004", "final_score": 0}) is None
    assert discovery._history_item_score({"stock_code": "600004"}) is None
    assert discovery._history_item_score({"final_score": 70}) is None
    assert discovery._history_item_score({"stock_code": "600004", "final_score": 70}) == ("600004", 70.0)
    # 兼容 code / overall_score 两种旧字段名
    assert discovery._history_item_score({"code": "600005", "overall_score": 66}) == ("600005", 66.0)


# ------------------------------------------------------------
# P0-3：排序键 == 展示键
# ------------------------------------------------------------

def test_display_key_matches_sort_key(discovery):
    """排序用 combined_score，展示也必须用它，否则分数序列不递减、像 bug。"""
    stocks = [
        {"code": "000001", "name": "低价高分红", "score": 80.0},
        {"code": "000002", "name": "被历史高分加持", "score": 60.0},
    ]
    history = {"000002": {"avg": 100.0, "count": 5}}
    weighted = discovery._apply_history_weight(stocks, history)
    ordered = sorted(weighted, key=lambda s: s.get("combined_score", 0), reverse=True)

    shown = [s["combined_score"] for s in ordered]
    assert shown == sorted(shown, reverse=True), "展示键必须与排序键一致且非递增"
    assert ordered[0]["code"] == "000002", "历史高分股应被抬到第一"
    # 原始 score 序列恰恰是不递减的反例 —— 这正是必须改展示口径的原因
    raw = [s["score"] for s in ordered]
    assert raw != sorted(raw, reverse=True), f"原始分序列本就不递减（{raw}），旧展示会像 bug"


def test_apply_history_weight_sets_combined_score_for_every_stock(discovery):
    """没有历史分的股票也要有 combined_score，JSON 才有一致的排序键。"""
    stocks = [{"code": "000003", "name": "无历史", "score": 72.0}]
    out = discovery._apply_history_weight(stocks, {"999999": {"avg": 90.0, "count": 4}})
    assert out[0]["combined_score"] == 72.0
    assert "hist_avg" not in out[0]


def test_apply_history_weight_neutral_line_and_boost(discovery):
    """70 为中性线：恰好 70 不加不减；>=3 次的加减分再加强 20%。"""
    stocks = [{"code": "000004", "name": "A", "score": 70.0}]
    assert discovery._apply_history_weight(stocks, {"000004": {"avg": 70.0, "count": 1}})[0]["combined_score"] == 70.0

    stocks = [{"code": "000005", "name": "B", "score": 70.0}]
    out = discovery._apply_history_weight(stocks, {"000005": {"avg": 90.0, "count": 3}})
    assert out[0]["combined_score"] == pytest.approx(70.0 + 20 * 0.6 * 1.2)


def test_summary_prints_combined_score_not_raw(discovery, caplog):
    """P0-3：摘要的主数字必须是 combined_score（排序键），否则分数序列不递减、像 bug。"""
    import logging

    stocks = [
        {"code": "000002", "name": "B", "score": 60.0, "combined_score": 81.6,
         "hist_avg": 100.0, "hist_count": 5, "strategy": "s1"},
        {"code": "000001", "name": "A", "score": 80.0, "combined_score": 80.0, "strategy": "s2"},
    ]
    with caplog.at_level(logging.INFO):
        discovery._log_summary(stocks)

    text = caplog.text
    assert "综合: 81.6000" in text, "摘要必须展示综合分（排序键）"
    assert "初选: 60.0000" in text, "原始初选分应作为次要信息保留"
    assert "历史均分: 100.00(5次)" in text, "应展示历史加权依据，便于人工核对"
    assert text.index("000002") < text.index("000001"), "展示顺序即排序顺序"


def test_apply_history_weight_without_history_is_identity(discovery):
    stocks = [{"code": "000006", "name": "C", "score": 81.5}]
    out = discovery._apply_history_weight(stocks, {})
    assert out[0]["combined_score"] == 81.5


# ------------------------------------------------------------
# 回归：四个脚本仍可编译（防止语法/缩进类编辑事故）
# ------------------------------------------------------------

@pytest.mark.parametrize(
    "script",
    ["alphasift_discovery.py", "collect_skill_scores.py", "run_skills_evaluation.py"],
)
def test_scripts_compile(script):
    import py_compile
    py_compile.compile(str(SCRIPTS / script), doraise=True)
