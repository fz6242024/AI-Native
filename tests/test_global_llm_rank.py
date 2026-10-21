# ============================================================
# test_global_llm_rank.py
# 覆盖「初选 LLM 排序 9 次 → 1 次」这条免费额度优化的核心实现：
#   - _global_llm_rank  整条初选流程**唯一**的一次 LLM 调用
#
# 为什么这个文件重要：
#   免费档 agnes-ai 实测有效速率约「每分钟 1 次成功请求」。旧实现让 9 个策略
#   各调一次 LLM 排序，背靠背连发 → 必然全部 429，纯浪费额度且拖长总耗时。
#   改成「策略循环内 0 次 + 流程末端 1 次」后，调用次数与策略数解耦。
#   本文件把这条护栏钉死：任何回归到「多次调用」的改动都会被测试拦住。
#
# 实现契约（与被测代码同步，改代码前先看这里）：
#   - 惰性导入 `alphasift.models.Pick` / `alphasift.ranker.rank_candidates_with_metadata`
#   - 惰性导入 `alphasift.config.Config`，用 `Config.from_env()` 取带 provider 前缀的
#     模型名（litellm 认不出裸模型名，会抛 "LLM Provider NOT provided"）；失败则回退
#     llm_config 的值并给裸名补 `openai/` 前缀
#   - `rank_candidates_with_metadata(...)` 返回 LLMRankingResult（.ranked/.errors/.picks）
#   - ⚠️ alphasift 会把限流异常**吞掉**、返回 ranked=False 的降级结果。所以必须
#     把 errors 里的限流文案重新抛出，外层 call_with_retry 的 429 退避才真正生效。
#
# 测试策略：
#   上述导入都是惰性的，所以往 sys.modules 塞假的 alphasift 包即可，无需真的安装
#   上游依赖（A 侧 CI 根本不装 alphasift）。
#
# 运行：python -m pytest tests/test_global_llm_rank.py -q
# ============================================================

import importlib.util
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"


def _load(name: str):
    """按文件路径加载 scripts/ 下的脚本（它们不是包，无法直接 import）。"""
    if name in sys.modules:
        return sys.modules[name]
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def discovery():
    return _load("alphasift_discovery")


# ------------------------------------------------------------
# alphasift 替身
# ------------------------------------------------------------

class _FakePick:
    """与 alphasift.models.Pick 字段一致的替身。"""

    def __init__(self, rank, code, name, final_score, screen_score):
        self.rank = rank
        self.code = code
        self.name = name
        self.final_score = final_score
        self.screen_score = screen_score


class _FakeResult:
    """与 alphasift.ranker.LLMRankingResult 字段一致的替身。"""

    def __init__(self, picks, ranked=True, errors=None, coverage=1.0):
        self.picks = picks
        self.ranked = ranked
        self.errors = list(errors or [])
        self.coverage = coverage


class _RankStub:
    """可编程的 rank_candidates_with_metadata 替身，记录每次调用。"""

    def __init__(self):
        self.calls = []
        self._impl = None

    def set_impl(self, impl):
        """impl(picks) -> _FakeResult 或 _FakePick 列表。"""
        self._impl = impl

    def __call__(self, picks, hints, api_key, model, base_url, **kwargs):
        self.calls.append(
            {
                "codes": [p.code for p in picks],
                "hints": hints,
                "api_key": api_key,
                "model": model,
                "base_url": base_url,
                "kwargs": kwargs,
            }
        )
        if self._impl is None:
            return _FakeResult(list(picks))
        out = self._impl(list(picks))
        if isinstance(out, _FakeResult):
            return out
        return _FakeResult(list(out))


class _FakeConfig:
    """与 alphasift.config.Config 替身：默认什么都不给，逼出回退分支。"""

    values = {"llm_model": None, "llm_base_url": None, "llm_api_key": None}

    def __init__(self, llm_model, llm_base_url, llm_api_key):
        self.llm_model = llm_model
        self.llm_base_url = llm_base_url
        self.llm_api_key = llm_api_key

    @classmethod
    def from_env(cls):
        v = cls.values
        return cls(v["llm_model"], v["llm_base_url"], v["llm_api_key"])


@pytest.fixture
def fake_alphasift(monkeypatch):
    """往 sys.modules 注入假的 alphasift 包，返回 (stub, config_cls)。"""
    models = types.ModuleType("alphasift.models")
    models.Pick = _FakePick
    ranker = types.ModuleType("alphasift.ranker")
    config = types.ModuleType("alphasift.config")
    config.Config = _FakeConfig
    pkg = types.ModuleType("alphasift")
    pkg.models = models
    pkg.ranker = ranker
    pkg.config = config

    for name, mod in (
        ("alphasift", pkg),
        ("alphasift.models", models),
        ("alphasift.ranker", ranker),
        ("alphasift.config", config),
    ):
        monkeypatch.setitem(sys.modules, name, mod)

    stub = _RankStub()
    ranker.rank_candidates_with_metadata = stub
    _FakeConfig.values = {"llm_model": None, "llm_base_url": None, "llm_api_key": None}
    return stub, _FakeConfig


@pytest.fixture(autouse=True)
def _no_sleep(discovery, monkeypatch):
    """call_with_retry 直接执行 fn，避免测试里真的等待限速。"""
    monkeypatch.setattr(discovery, "call_with_retry", lambda fn, **kw: fn())
    monkeypatch.setattr(discovery, "describe_stats", lambda: "stats")


def _stock(code, name="", combined=70.0, score=None, **extra):
    s = {"code": code, "name": name, "combined_score": combined}
    s["score"] = combined if score is None else score
    s.update(extra)
    return s


def _order(picks, codes, scores=None):
    """按给定 code 顺序构造 Pick 列表（scores 可选，按位置对应）。"""
    by = {p.code: p for p in picks}
    out = []
    for i, code in enumerate(codes, start=1):
        src = by.get(code)
        if src is None:
            continue
        final = scores[i - 1] if scores else src.final_score
        out.append(_FakePick(i, code, src.name, final, src.screen_score))
    return out


# ============================================================
# 核心护栏：调用次数
# ============================================================

def test_disabled_makes_zero_llm_calls(discovery, fake_alphasift):
    """enabled=False 时一次 LLM 都不调，原样返回（顺序与对象都不变）。"""
    stub, _ = fake_alphasift
    stocks = [_stock("600001"), _stock("600002")]

    out = discovery._global_llm_rank(stocks, False)

    assert stub.calls == []
    assert out == stocks


def test_empty_list_makes_zero_llm_calls(discovery, fake_alphasift):
    """空清单直接短路，不发起调用。"""
    stub, _ = fake_alphasift
    assert discovery._global_llm_rank([], True) == []
    assert stub.calls == []


def test_success_makes_exactly_one_call(discovery, fake_alphasift):
    """成功路径下 rank_candidates_with_metadata 恰好被调用 1 次 —— 本文件最重要的断言。"""
    stub, _ = fake_alphasift
    stocks = [_stock(f"60000{i}", combined=90 - i) for i in range(1, 6)]

    discovery._global_llm_rank(stocks, True)

    assert len(stub.calls) == 1


def test_one_call_regardless_of_candidate_count(discovery, fake_alphasift, monkeypatch):
    """调用次数与候选数解耦：2 支与 8 支各是 1 次。"""
    stub, _ = fake_alphasift
    monkeypatch.setenv("LLM_MAX_CANDIDATES", "8")

    discovery._global_llm_rank([_stock("600001"), _stock("600002")], True)
    discovery._global_llm_rank([_stock(f"60000{i}") for i in range(1, 9)], True)

    assert len(stub.calls) == 2  # 两次独立调用，各 1 次


# ============================================================
# 重排与分值回写
# ============================================================

def test_reorders_by_llm_result(discovery, fake_alphasift):
    """按 LLM 返回顺序重排，并把 final_score 写回 combined_score。"""
    stub, _ = fake_alphasift
    stocks = [_stock("600001", combined=70), _stock("600002", combined=80)]
    # LLM 认为 600001 更强，与因子分（600002 更高）相反
    stub.set_impl(lambda picks: _order(picks, ["600001", "600002"], [95.0, 60.0]))

    out = discovery._global_llm_rank(stocks, True)

    assert [s["code"] for s in out] == ["600001", "600002"]
    assert out[0]["combined_score"] == 95.0
    assert out[1]["combined_score"] == 60.0


def test_tail_beyond_max_candidates_appended_in_original_order(
    discovery, fake_alphasift, monkeypatch
):
    """超过 LLM_MAX_CANDIDATES 的尾部保持原序追加，且不参与排序。"""
    stub, _ = fake_alphasift
    monkeypatch.setenv("LLM_MAX_CANDIDATES", "2")
    stocks = [_stock(f"60000{i}", combined=90 - i) for i in range(1, 6)]

    out = discovery._global_llm_rank(stocks, True)

    assert stub.calls[0]["codes"] == ["600001", "600002"]
    assert [s["code"] for s in out] == ["600001", "600002", "600003", "600004", "600005"]


def test_uncovered_candidates_are_appended_not_dropped(discovery, fake_alphasift):
    """LLM 只覆盖部分候选时，未覆盖的按原序补回，一支都不能丢。"""
    stub, _ = fake_alphasift
    stocks = [_stock("600001"), _stock("600002"), _stock("600003")]
    stub.set_impl(lambda picks: _order(picks, ["600003"], [99.0]))

    out = discovery._global_llm_rank(stocks, True)

    assert [s["code"] for s in out] == ["600003", "600001", "600002"]
    assert len(out) == 3


def test_duplicate_codes_are_deduped(discovery, fake_alphasift):
    """LLM 重复返回同一 code 时只保留第一次，不产生重复条目。"""
    stub, _ = fake_alphasift
    stocks = [_stock("600001"), _stock("600002")]

    def _impl(picks):
        by = {p.code: p for p in picks}
        a = by["600001"]
        return [
            _FakePick(1, "600001", a.name, 99.0, a.screen_score),
            _FakePick(2, "600001", a.name, 98.0, a.screen_score),
            _FakePick(3, "600002", by["600002"].name, 60.0, by["600002"].screen_score),
        ]

    stub.set_impl(_impl)
    out = discovery._global_llm_rank(stocks, True)

    assert [s["code"] for s in out] == ["600001", "600002"]
    assert len(out) == 2


def test_unknown_codes_from_llm_are_ignored(discovery, fake_alphasift):
    """LLM 编造出候选池以外的 code 时必须丢弃（不得引入新股票）。"""
    stub, _ = fake_alphasift
    stocks = [_stock("600001")]

    def _impl(picks):
        return [
            _FakePick(1, "999999", "编造股", 99.0, 70.0),
            _FakePick(2, "600001", "真实股", 60.0, 70.0),
        ]

    stub.set_impl(_impl)
    out = discovery._global_llm_rank(stocks, True)

    assert [s["code"] for s in out] == ["600001"]


# ============================================================
# 优雅降级 —— 任何失败都不能中断初选
# ============================================================

def test_llm_exception_keeps_original_order(discovery, fake_alphasift):
    """rank_candidates_with_metadata 抛异常时保持原顺序，绝不向上抛。"""
    stub, _ = fake_alphasift

    def _boom(picks):
        raise RuntimeError("connection reset")

    stub.set_impl(_boom)

    stocks = [_stock("600001"), _stock("600002")]
    out = discovery._global_llm_rank(stocks, True)

    assert [s["code"] for s in out] == ["600001", "600002"]


def test_import_failure_keeps_original_order(discovery, monkeypatch):
    """alphasift 导入失败（A 侧未安装）时直接返回原顺序，不报错。"""
    for mod in ("alphasift", "alphasift.models", "alphasift.ranker", "alphasift.config"):
        monkeypatch.setitem(sys.modules, mod, None)
    stocks = [_stock("600001")]

    out = discovery._global_llm_rank(stocks, True)

    assert out == stocks


def test_ranked_false_with_non_rate_limit_keeps_original_order(discovery, fake_alphasift):
    """coverage 不足等非限流降级：返回原顺序与原始分值，不重抛。"""
    stub, _ = fake_alphasift
    stocks = [_stock("600001", combined=88.0), _stock("600002", combined=77.0)]
    stub.set_impl(
        lambda picks: _FakeResult(
            list(picks),
            ranked=False,
            errors=["LLM ranking response coverage below threshold: 0.00; errors=['empty_response']"],
        )
    )

    out = discovery._global_llm_rank(stocks, True)

    assert [s["code"] for s in out] == ["600001", "600002"]
    assert out[0]["combined_score"] == 88.0


def test_empty_picks_result_keeps_original_order(discovery, fake_alphasift):
    """LLM 返回空 picks 时原顺序与分值都不变。"""
    stub, _ = fake_alphasift
    stocks = [_stock("600001", combined=88.0), _stock("600002", combined=77.0)]
    stub.set_impl(lambda picks: _FakeResult([], ranked=False, errors=["empty_response"]))

    out = discovery._global_llm_rank(stocks, True)

    assert [s["code"] for s in out] == ["600001", "600002"]
    assert out[0]["combined_score"] == 88.0


# ============================================================
# 限流重抛 —— alphasift 吞掉 429 的补丁
# ============================================================

def test_rate_limit_in_errors_is_reraised_for_backoff(discovery, fake_alphasift, monkeypatch):
    """ranked=False 且 errors 含限流文案时，必须重抛给 call_with_retry 才能退避。

    这是 alphasift 的坑：它把限流异常吞掉、返回降级结果，外层看不到异常
    也就无从退避。这里断言「确实抛出来了」。
    """
    stub, _ = fake_alphasift
    stub.set_impl(
        lambda picks: _FakeResult(
            list(picks), ranked=False, errors=["Error code: 429 - rate limit exceeded"]
        )
    )
    seen = {"raised": None}

    def _capturing_retry(fn, **kw):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 - 测试需要捕获
            seen["raised"] = e
            raise

    monkeypatch.setattr(discovery, "call_with_retry", _capturing_retry)

    stocks = [_stock("600001")]
    out = discovery._global_llm_rank(stocks, True)

    assert seen["raised"] is not None, "限流文案必须被重抛，否则 429 退避失效"
    assert "429" in str(seen["raised"])
    assert [s["code"] for s in out] == ["600001"]  # 仍优雅降级


def test_ranked_false_without_errors_does_not_reraise(discovery, fake_alphasift, monkeypatch):
    """ranked=False 但 errors 为空时不重抛（避免把正常降级当限流）。"""
    stub, _ = fake_alphasift
    stub.set_impl(lambda picks: _FakeResult(list(picks), ranked=False, errors=[]))
    seen = {"raised": None}

    def _capturing_retry(fn, **kw):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            seen["raised"] = e
            raise

    monkeypatch.setattr(discovery, "call_with_retry", _capturing_retry)
    discovery._global_llm_rank([_stock("600001")], True)

    assert seen["raised"] is None


# ============================================================
# 调用参数契约
# ============================================================

def test_call_contract_max_retries_zero(discovery, fake_alphasift):
    """max_retries 必须为 0：上游的 coverage 重试会额外发请求，
    与「只调 1 次」冲突；限流重试交给外层 call_with_retry。"""
    stub, _ = fake_alphasift

    discovery._global_llm_rank([_stock("600001")], True)

    assert stub.calls[0]["kwargs"]["max_retries"] == 0


def test_call_uses_resolved_base_url(discovery, fake_alphasift):
    """必须把 llm_config 解析出的 base_url 透传，
    否则会退回 api.openai.com（本机永远跑不通的老问题）。"""
    stub, _ = fake_alphasift

    discovery._global_llm_rank([_stock("600001")], True)

    assert stub.calls[0]["base_url"] == discovery.LLM_BASE_URL


def test_model_gets_openai_prefix_when_config_falls_back(discovery, fake_alphasift):
    """Config.from_env() 拿不到值时回退 llm_config，并给裸模型名补 openai/ 前缀。

    litellm 在客户端就要求能识别 provider，裸名会直接抛
    "LLM Provider NOT provided"，根本发不出请求。
    """
    stub, cfg = fake_alphasift
    cfg.values = {"llm_model": None, "llm_base_url": None, "llm_api_key": None}

    discovery._global_llm_rank([_stock("600001")], True)

    model = stub.calls[0]["model"]
    assert model.startswith("openai/")
    assert model == f"openai/{discovery.LLM_MODEL}"


def test_model_gets_openai_prefix_even_when_config_returns_bare_name(
    discovery, fake_alphasift
):
    """Config.from_env() 成功但返回**裸模型名**时，也必须补 openai/ 前缀。

    这是回归测试：早期实现把前缀归一化写在 except 分支里，只要 alphasift 能解析出
    配置就永远不会补前缀，裸名会一路传给 litellm 并在客户端就抛
    "LLM Provider NOT provided" —— 请求根本发不出去，且表现为「静默降级」。
    """
    stub, cfg = fake_alphasift
    cfg.values = {"llm_model": "agnes-2.0-flash", "llm_base_url": None, "llm_api_key": None}

    discovery._global_llm_rank([_stock("600001")], True)

    assert stub.calls[0]["model"] == "openai/agnes-2.0-flash"


def test_provider_prefixed_model_is_left_alone(discovery, fake_alphasift):
    """已带 provider 前缀的模型名不得被二次加前缀。"""
    stub, cfg = fake_alphasift
    cfg.values = {
        "llm_model": "anthropic/claude-3-5-sonnet",
        "llm_base_url": None,
        "llm_api_key": None,
    }

    discovery._global_llm_rank([_stock("600001")], True)

    assert stub.calls[0]["model"] == "anthropic/claude-3-5-sonnet"


def test_alphasift_config_values_take_precedence(discovery, fake_alphasift):
    """alphasift 自己的配置解析优先（与 pipeline 里真正跑通的那条路径同源）。"""
    stub, cfg = fake_alphasift
    cfg.values = {
        "llm_model": "openai/agnes-2.0-flash",
        "llm_base_url": "https://apihub.agnes-ai.com/v1",
        "llm_api_key": "k-from-alphasift",
    }

    discovery._global_llm_rank([_stock("600001")], True)

    assert stub.calls[0]["model"] == "openai/agnes-2.0-flash"
    assert stub.calls[0]["base_url"] == "https://apihub.agnes-ai.com/v1"
    assert stub.calls[0]["api_key"] == "k-from-alphasift"


def test_screen_score_uses_combined_score(discovery, fake_alphasift):
    """screen_score 取 combined_score（含历史加权），使 LLM 排序叠在既有评分之上。"""
    stub, _ = fake_alphasift
    captured = {}

    def _impl(picks):
        captured["picks"] = list(picks)
        return _FakeResult(list(picks))

    stub.set_impl(_impl)
    discovery._global_llm_rank([_stock("600001", combined=93.93, score=84.93)], True)

    assert captured["picks"][0].screen_score == 93.93
    assert captured["picks"][0].final_score == 93.93


def test_hints_forbid_non_main_board(discovery, fake_alphasift):
    """排序提示词必须写死「不能买创业板/科创板/北交所」，与产品约束一致。"""
    stub, _ = fake_alphasift

    discovery._global_llm_rank([_stock("600001")], True)

    hints = stub.calls[0]["hints"]
    for token in ("创业板", "科创板", "北交所"):
        assert token in hints


def test_original_dict_objects_are_reused(discovery, fake_alphasift):
    """重排复用同一批 dict 对象（下游依赖其额外字段），不是拷贝。"""
    stub, _ = fake_alphasift
    s1 = _stock("600001", hist_avg=85.0, hist_count=1)
    s2 = _stock("600002")
    stub.set_impl(lambda picks: _order(picks, ["600002", "600001"], [99.0, 60.0]))

    out = discovery._global_llm_rank([s1, s2], True)

    assert out[0] is s2
    assert out[1] is s1
    assert out[1]["hist_avg"] == 85.0
