# ============================================================
# test_llm_config.py
# 覆盖 scripts/llm_config.py（LLM 配置解析的唯一真相源）：
#   - resolve_llm_config() 的完整解析链（渠道 / LLM_MY_PROXY_* / LLM_BASE_URL / OPENAI_*）
#   - apply_llm_env() 导出到 os.environ 的行为（只写非空值、幂等、不回传密钥明文）
#   - 与 scripts/run_skills_evaluation.py 的解析结果一致性护栏
#     （该文件部署在 B/C 私有仓库、不能改，只能靠测试锁住语义，防未来漂移）
#   - 回归：alphasift_discovery 以渠道风格 env 加载后，模块级 LLM_BASE_URL
#     必须是渠道地址，而不是 https://api.openai.com/v1
#
# 运行：
#   python -m pytest tests/test_llm_config.py -p no:cacheprovider -q
# ============================================================

import importlib.util
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"

# scripts/ 不是包，按路径加载前先把目录放进 sys.path（供脚本内部 `import llm_config` 使用）。
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def _load(name: str, fresh: bool = False):
    """按文件路径加载 scripts/ 下的脚本（它们不是包，无法直接 import）。"""
    if not fresh and name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def llm_config():
    return _load("llm_config")


@pytest.fixture
def no_dotenv(monkeypatch):
    """阻止被测脚本在 import 时读取真实 .env，保证测试环境完全可控。"""
    import dotenv

    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: None)


# ------------------------------------------------------------
# 1. resolve_llm_config 解析链矩阵
# ------------------------------------------------------------

def test_resolve_channel_style(llm_config):
    env = {
        "LLM_CHANNELS": "my_proxy",
        "LLM_MY_PROXY_BASE_URL": "https://x/v1",
        "LLM_MY_PROXY_API_KEY": "k",
        "LLM_MY_PROXY_MODELS": "agnes-2.0-flash,other",
    }
    assert llm_config.resolve_llm_config(env) == ("https://x/v1", "k", "agnes-2.0-flash")


def test_resolve_channel_takes_first_segment(llm_config):
    env = {
        "LLM_CHANNELS": "first,second",
        "LLM_FIRST_BASE_URL": "https://first/v1",
        "LLM_FIRST_API_KEY": "k1",
        "LLM_FIRST_MODELS": "m1",
        "LLM_SECOND_BASE_URL": "https://second/v1",
        "LLM_SECOND_API_KEY": "k2",
        "LLM_SECOND_MODELS": "m2",
    }
    assert llm_config.resolve_llm_config(env) == ("https://first/v1", "k1", "m1")


def test_resolve_my_proxy_style_without_channels(llm_config):
    # 注意：只有渠道风格 LLM_<CHANNEL>_MODELS 会「逗号取首段」；
    # LLM_MY_PROXY_MODELS 沿用原实现，取原值（与 run_skills_evaluation.py 一致，勿改）。
    env = {
        "LLM_MY_PROXY_BASE_URL": "https://proxy/v1",
        "LLM_MY_PROXY_API_KEY": "pk",
        "LLM_MY_PROXY_MODELS": "agnes-2.0-flash",
    }
    assert llm_config.resolve_llm_config(env) == ("https://proxy/v1", "pk", "agnes-2.0-flash")


def test_resolve_generic_base_url(llm_config):
    env = {"LLM_BASE_URL": "https://generic/v1", "LLM_API_KEY": "gk"}
    base_url, api_key, model = llm_config.resolve_llm_config(env)
    assert base_url == "https://generic/v1"
    assert api_key == "gk"
    assert model == "agnes-3.0-flash"


def test_resolve_openai_fallback(llm_config):
    env = {"OPENAI_BASE_URL": "https://oai/v1", "OPENAI_API_KEY": "ok"}
    assert llm_config.resolve_llm_config(env) == ("https://oai/v1", "ok", "agnes-3.0-flash")


def test_resolve_empty_env_uses_openai_default_base(llm_config):
    base_url, api_key, model = llm_config.resolve_llm_config({})
    assert base_url == "https://api.openai.com/v1"
    assert api_key == ""
    assert model == "agnes-3.0-flash"


def test_resolve_falls_back_when_channel_incomplete(llm_config):
    """LLM_CHANNELS 非空但渠道变量全空时，仍应回退到 LLM_BASE_URL（加固点）。"""
    env = {
        "LLM_CHANNELS": "my_proxy",
        "LLM_BASE_URL": "https://generic/v1",
        "LLM_API_KEY": "gk",
    }
    assert llm_config.resolve_llm_config(env) == ("https://generic/v1", "gk", "agnes-3.0-flash")


def test_resolve_litellm_model_override(llm_config):
    env = {"LLM_BASE_URL": "https://g/v1", "LLM_API_KEY": "k", "LITELLM_MODEL": "lit-model"}
    assert llm_config.resolve_llm_config(env)[2] == "lit-model"


def test_resolve_llm_model_env_overrides_litellm(llm_config):
    env = {
        "LLM_BASE_URL": "https://g/v1",
        "LLM_API_KEY": "k",
        "LITELLM_MODEL": "lit-model",
        "LLM_MODEL": "env-model",
    }
    assert llm_config.resolve_llm_config(env)[2] == "env-model"


# ------------------------------------------------------------
# 2. apply_llm_env：渠道风格必须导出成 LLM_BASE_URL（本 bug 的直接回归护栏）
# ------------------------------------------------------------

def test_apply_llm_env_exports_channel_base_url(monkeypatch, llm_config):
    fake = {
        "LLM_CHANNELS": "my_proxy",
        "LLM_MY_PROXY_BASE_URL": "https://apihub.agnes-ai.com/v1",
        "LLM_MY_PROXY_API_KEY": "secret-key",
        "LLM_MY_PROXY_MODELS": "agnes-2.0-flash",
    }
    monkeypatch.setattr(os, "environ", dict(fake))

    exported = llm_config.apply_llm_env()

    assert os.environ["LLM_BASE_URL"] == "https://apihub.agnes-ai.com/v1"
    assert os.environ["LLM_API_KEY"] == "secret-key"
    assert os.environ["LLM_MODEL"] == "agnes-2.0-flash"
    # 返回值不回传密钥明文
    assert exported["LLM_BASE_URL"] == "https://apihub.agnes-ai.com/v1"
    assert exported["LLM_API_KEY"] == "已配置"
    assert "secret-key" not in str(exported)


# ------------------------------------------------------------
# 3. apply_llm_env：只写非空值（不得写入空 LLM_API_KEY）
# ------------------------------------------------------------

def test_apply_llm_env_skips_empty_values(monkeypatch, llm_config):
    monkeypatch.setattr(os, "environ", {})

    exported = llm_config.apply_llm_env()

    assert "LLM_API_KEY" not in os.environ
    assert "LLM_API_KEY" not in exported
    # base_url 与 model 有非空回退值，应被写入
    assert os.environ["LLM_BASE_URL"] == "https://api.openai.com/v1"
    assert os.environ["LLM_MODEL"] == "agnes-3.0-flash"


# ------------------------------------------------------------
# 4. apply_llm_env：不覆盖已有的显式 LLM_BASE_URL / 幂等
# ------------------------------------------------------------

def test_apply_llm_env_preserves_explicit_base_url(monkeypatch, llm_config):
    monkeypatch.setattr(
        os, "environ", {"LLM_BASE_URL": "https://explicit/v1", "LLM_API_KEY": "k"}
    )

    llm_config.apply_llm_env()
    assert os.environ["LLM_BASE_URL"] == "https://explicit/v1"


def test_apply_llm_env_is_idempotent(monkeypatch, llm_config):
    monkeypatch.setattr(
        os,
        "environ",
        {
            "LLM_CHANNELS": "my_proxy",
            "LLM_MY_PROXY_BASE_URL": "https://x/v1",
            "LLM_MY_PROXY_API_KEY": "k",
            "LLM_MY_PROXY_MODELS": "agnes-2.0-flash",
        },
    )

    llm_config.apply_llm_env()
    first = dict(os.environ)
    llm_config.apply_llm_env()
    assert dict(os.environ) == first


# ------------------------------------------------------------
# 5. 一致性护栏：与 run_skills_evaluation.py 解析结果一致
#    （该文件不能改，靠本测试锁住两者语义不漂移）
# ------------------------------------------------------------

CONSISTENCY_ENVS = [
    {},
    {
        "LLM_CHANNELS": "my_proxy",
        "LLM_MY_PROXY_BASE_URL": "https://x/v1",
        "LLM_MY_PROXY_API_KEY": "k",
        "LLM_MY_PROXY_MODELS": "agnes-2.0-flash,other",
    },
    {
        "LLM_CHANNELS": "my_proxy",
        "LLM_MY_PROXY_BASE_URL": "https://x/v1",
        "LLM_MY_PROXY_API_KEY": "k",
    },
    {
        "LLM_MY_PROXY_BASE_URL": "https://proxy/v1",
        "LLM_MY_PROXY_API_KEY": "pk",
        "LLM_MY_PROXY_MODELS": "agnes-2.0-flash",
    },
    {"LLM_BASE_URL": "https://generic/v1", "LLM_API_KEY": "gk"},
    {"OPENAI_BASE_URL": "https://oai/v1", "OPENAI_API_KEY": "ok"},
]


@pytest.mark.parametrize("env", CONSISTENCY_ENVS)
def test_consistent_with_run_skills_evaluation(monkeypatch, llm_config, env):
    monkeypatch.setattr(os, "environ", dict(env))
    # 用 no-op 顶掉模块级 load_dotenv，避免真实 .env 污染测试环境
    import dotenv

    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: None)

    spec = importlib.util.spec_from_file_location(
        "run_skills_evaluation", SCRIPTS / "run_skills_evaluation.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    expected = llm_config.resolve_llm_config(env)
    assert (mod.LLM_BASE_URL, mod.LLM_API_KEY, mod.LLM_MODEL) == expected


# ------------------------------------------------------------
# 6. 回归：alphasift_discovery 以渠道风格 env 加载后导出正确 base_url
# ------------------------------------------------------------

def test_alphasift_discovery_exports_channel_base_url(monkeypatch, llm_config, no_dotenv):
    monkeypatch.setattr(
        os,
        "environ",
        {
            "LLM_CHANNELS": "my_proxy",
            "LLM_MY_PROXY_BASE_URL": "https://apihub.agnes-ai.com/v1",
            "LLM_MY_PROXY_API_KEY": "k",
            "LLM_MY_PROXY_MODELS": "agnes-2.0-flash",
        },
    )

    mod = _load("alphasift_discovery", fresh=True)

    assert mod.LLM_BASE_URL == "https://apihub.agnes-ai.com/v1"
    assert mod.LLM_BASE_URL != "https://api.openai.com/v1"
    assert mod.LLM_MODEL == "agnes-2.0-flash"
