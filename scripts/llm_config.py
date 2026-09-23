# ============================================================
# llm_config.py
# LLM 配置解析的唯一真相源（single source of truth）。
#
# 背景：本仓库同时存在两种 LLM 配置风格，而各脚本过去各写一套解析，
# 导致「线上能走通、只有本机跑不通」：
#   - CI 工作流直接注入：LLM_BASE_URL / LLM_API_KEY / LLM_MODEL（来自 secrets）
#   - 本机 .env 用渠道风格：LLM_CHANNELS=my_proxy
#       + LLM_MY_PROXY_BASE_URL / LLM_MY_PROXY_API_KEY / LLM_MY_PROXY_MODELS，
#       且没有 LLM_BASE_URL
# 只认一种风格的脚本，在另一种环境里必然失效。
#
# 本模块把解析链收敛到一处：
#   resolve_llm_config()  —— 纯解析，返回 (base_url, api_key, model)
#   apply_llm_env()       —— 把解析结果导出到 os.environ，供 alphasift 这类
#                            「自己读环境变量」的第三方库使用
#
# 解析链（与 scripts/run_skills_evaluation.py:38-79 语义一致）：
#   1. LLM_CHANNELS（逗号分隔取第一段）→ LLM_<CHANNEL大写>_BASE_URL / _API_KEY / _MODELS
#   2. LLM_MY_PROXY_BASE_URL / _API_KEY / _MODELS
#   3. LLM_BASE_URL / LLM_API_KEY
#   4. OPENAI_BASE_URL / OPENAI_API_KEY（base_url 兜底 https://api.openai.com/v1）
# 其中 _MODELS 取逗号分隔第一段；LITELLM_MODEL 与 LLM_MODEL 可覆盖模型名。
# ============================================================

import os

DEFAULT_MODEL = "agnes-3.0-flash"
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"


def resolve_llm_config(env=None) -> tuple:
    """解析 LLM 配置，返回 (base_url, api_key, model)。

    env 默认取 os.environ；显式传入映射便于测试。

    解析链见模块头注释；顺序为：
      1. LLM_CHANNELS（逗号分隔取第一段）→ LLM_<CHANNEL大写>_BASE_URL / _API_KEY / _MODELS
      2. LLM_MY_PROXY_BASE_URL / _API_KEY / _MODELS
      3. LLM_BASE_URL / LLM_API_KEY
      4. OPENAI_BASE_URL / OPENAI_API_KEY（base_url 兜底 https://api.openai.com/v1）
    其中 _MODELS 取逗号分隔第一段；LITELLM_MODEL 与 LLM_MODEL 可覆盖模型名。
    """
    if env is None:
        env = os.environ

    base_url = ""
    api_key = ""
    model = DEFAULT_MODEL
    model_from_channel = False

    channels = env.get("LLM_CHANNELS", "")
    if channels:
        channel = channels.split(",")[0].strip()
        base_url_var = f"LLM_{channel.upper()}_BASE_URL"
        api_key_var = f"LLM_{channel.upper()}_API_KEY"
        models_var = f"LLM_{channel.upper()}_MODELS"

        base_url = env.get(base_url_var, "")
        api_key = env.get(api_key_var, "")
        if env.get(models_var):
            model = env.get(models_var).split(",")[0].strip()
            model_from_channel = True

    # 渠道未配置，或渠道变量缺失 —— 继续走通用回退。
    # 加固（原实现的超集）：原实现中 LLM_CHANNELS 非空却拿不到渠道变量时，
    # 会停在空 base_url 直接失败、不再回退；这里补上回退，渠道正常时行为完全一致。
    if not base_url or not api_key:
        proxy_base = env.get("LLM_MY_PROXY_BASE_URL", "")
        if proxy_base and not base_url:
            base_url = proxy_base
            if not api_key:
                api_key = env.get("LLM_MY_PROXY_API_KEY", "")
            if not model_from_channel:
                model = env.get("LLM_MY_PROXY_MODELS", model)

        if not base_url:
            base_url = env.get("LLM_BASE_URL", "")
        if not api_key:
            api_key = env.get("LLM_API_KEY", "")

        if not api_key:
            api_key = env.get("OPENAI_API_KEY", "")
        if not base_url:
            base_url = env.get("LLM_BASE_URL") or env.get("OPENAI_BASE_URL", DEFAULT_OPENAI_BASE_URL)

        model = env.get("LITELLM_MODEL", model)
        _env_model = env.get("LLM_MODEL", "").strip()
        if _env_model:
            model = _env_model

    return base_url, api_key, model


def apply_llm_env(env=None) -> dict:
    """把解析结果导出到 os.environ，返回实际导出的键值（供日志用）。

    目的：让 alphasift 这类「自己读环境变量」的第三方库也能拿到正确配置。
    注意 alphasift 的 _resolve_llm_base_url() 只认 LLM_BASE_URL / OPENAI_BASE_URL，
    完全不用 LLM_CHANNELS 渠道；本机 .env 只有渠道风格、没有 LLM_BASE_URL，
    所以必须由本函数把渠道 base_url 导出成 LLM_BASE_URL，否则 LLM 排序必然失败。

    env 仅作为解析来源（默认 os.environ）；写入目标始终是 os.environ。

    行为约定：
      - 只写 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL 三个键；
      - **只写非空值**：空值不写入 os.environ，避免覆盖下游本来可用的回退；
      - 返回实际写入的 dict；api_key 不回传明文，只标记是否已配置；
      - 幂等：重复调用结果一致（解析是 env 的纯函数，渠道分支始终优先）。
    """
    base_url, api_key, model = resolve_llm_config(env)

    exported = {}
    if base_url:
        os.environ["LLM_BASE_URL"] = base_url
        exported["LLM_BASE_URL"] = base_url
    if api_key:
        os.environ["LLM_API_KEY"] = api_key
        # 不回传密钥明文，日志只报是否配置
        exported["LLM_API_KEY"] = "已配置"
    if model:
        os.environ["LLM_MODEL"] = model
        exported["LLM_MODEL"] = model
    return exported
