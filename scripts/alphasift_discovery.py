# ============================================================
# alphasift_discovery.py
# AlphaSift 初选脚本：从全市场筛选约 10 支优质股票
# 环境变量从 .env 文件读取（兼容 LLM_CONFIG_GUIDE 变量名）
# ============================================================

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

# ---------- 加载 .env 文件 ----------
_env_path = Path(__file__).resolve().parent.parent / ".env"
if _env_path.exists():
    try:
        from dotenv import load_dotenv
        load_dotenv(_env_path)
        log_debug_msg = f"已从 {_env_path} 加载环境变量"
    except ImportError:
        log_debug_msg = "python-dotenv 未安装，跳过 .env 加载"
else:
    log_debug_msg = f".env 文件不存在于 {_env_path}"

# ---------- 日志配置 ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)
log.info(log_debug_msg)

# ---------- 从 .env 读取配置（兼容多种变量名）----------
LLM_BASE_URL = os.environ.get("LLM_BASE_URL") or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
LLM_API_KEY = os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK", "")
ALPHASIFT_PROJECT_PATH = os.environ.get(
    "ALPHASIFT_PROJECT_PATH",
    r"C:\Users\zf\Documents\github项目\alphasift",
)

log.info("LLM_BASE_URL = %s", LLM_BASE_URL)
log.info("LLM_API_KEY 已配置: %s", bool(LLM_API_KEY))
log.info("FEISHU_WEBHOOK 已配置: %s", bool(FEISHU_WEBHOOK))
log.info("AlphaSift 项目路径 = %s", ALPHASIFT_PROJECT_PATH)

# ---------- 模拟股票池（回退用）----------
_SIMULATED_STOCKS = [
    {"code": "600519", "name": "贵州茅台"},
    {"code": "000858", "name": "五粮液"},
    {"code": "000001", "name": "平安银行"},
    {"code": "600036", "name": "招商银行"},
    {"code": "601318", "name": "中国平安"},
    {"code": "002415", "name": "海康威视"},
    {"code": "600900", "name": "长江电力"},
    {"code": "000333", "name": "美的集团"},
    {"code": "601888", "name": "中国中免"},
    {"code": "600276", "name": "恒瑞医药"},
    {"code": "000651", "name": "格力电器"},
    {"code": "600809", "name": "山西汾酒"},
    {"code": "300750", "name": "宁德时代"},
    {"code": "002714", "name": "牧原股份"},
    {"code": "601012", "name": "隆基绿能"},
]

# ---------- 内置策略列表 ----------
STRATEGIES = [
    "balanced_alpha",           # 均衡多因子（推荐，综合评分）
    "momentum_quality",         # 趋势质量
    "quality_value",            # 稳健价值
    "capital_heat",             # 资金热度
    "volume_breakout",          # 放量突破
    "shrink_pullback",          # 缩量回踩
    "oversold_reversal",        # 超跌反转
    "dual_low",                 # 双低选股
    "blue_chip_income",         # 蓝筹收益质量
    "low_volatility_quality",   # 低波质量
]

# 银行股名称关键字与代码白名单（无网络即可判断）
BANK_KEYWORDS = ["银行", "农商行", "农信社", "农村商业"]
KNOWN_BANK_CODES = {
    "000001", "002142", "002807", "002839", "002936", "002948", "002966", "002958",
    "001227", "001289",
    "600000", "600015", "600016", "600036", "600908", "600919", "600926", "600928",
    "601009", "601128", "601166", "601169", "601229", "601288", "601328", "601398",
    "601528", "601577", "601658", "601665", "601818", "601838", "601860", "601916",
    "601939", "601963", "601988", "601997", "601998",
}


def _is_bank(code: str, name: str) -> bool:
    """银行判断：优先代码白名单，其次剥掉 XD/XR/DR 前缀后按名称关键字判断。"""
    if code in KNOWN_BANK_CODES:
        return True
    nm = name.replace("XD", "").replace("XR", "").replace("DR", "").replace("*ST", "")
    return any(kw in nm for kw in BANK_KEYWORDS)


def _ensure_alphasift_available() -> bool:
    """确保 AlphaSift 模块可用。"""
    # 1) 优先直接导入已安装的 alphasift 包（GitHub Actions 通过 pip 安装）
    try:
        import alphasift.pipeline  # noqa: F401
        log.info("AlphaSift 包已安装，直接使用")
        return True
    except ImportError:
        pass

    # 2) 其次尝试本地项目目录（本地开发场景）
    if os.path.isdir(ALPHASIFT_PROJECT_PATH) and ALPHASIFT_PROJECT_PATH not in sys.path:
        sys.path.insert(0, ALPHASIFT_PROJECT_PATH)

    try:
        from alphasift.pipeline import screen  # noqa: F401
        log.info("AlphaSift 模块已就绪")
        return True
    except ImportError as e:
        log.warning("AlphaSift 模块导入失败: %s，回退到本地快照/模拟数据", e)
        return False


def _run_alphasift_screen(strategy: str, top_n: int = 10, use_llm: bool = False, snapshot_df: object = None) -> list:
    """调用 AlphaSift 的 screen() 函数进行初选。"""
    from alphasift.pipeline import screen
    from alphasift import snapshot as snap_module
    import alphasift.pipeline as pipeline_module

    log.info("使用策略 [%s] 运行 AlphaSift 初选 (LLM=%s)...", strategy, use_llm)
    
    # Monkey-patch fetch_snapshot_with_fallback to return cached data
    if snapshot_df is not None:
        def patched_fetch(*args, **kwargs):
            # Return the cached DataFrame directly, skipping network
            cached = snapshot_df.copy()
            cached.attrs["snapshot_source"] = "cached"
            cached.attrs["fallback_used"] = True
            cached.attrs["stale"] = True
            cached.attrs["source_errors"] = []
            log.info("  使用缓存快照（跳过网络请求）")
            return cached
        # pipeline 模块在导入时绑定的是原始函数，需同步覆盖两个引用
        snap_module.fetch_snapshot_with_fallback = patched_fetch
        pipeline_module.fetch_snapshot_with_fallback = patched_fetch

    try:
        result = screen(
            strategy=strategy,
            market="cn",
            # 多取候选（至少30支），评分层过滤银行后再取前 top_n，
            # 避免 alphasift 因子评分偏好银行（低波/高息/低估值）导致候选全是银行
            max_output=max(top_n * 3, 30),
            use_llm=use_llm,
            # CI 代理环境网络不稳，显式关闭日线/行业/候选上下文等额外网络调用
            daily_enrich=False,
            collect_llm_candidate_context=False,
            industry_provider="none",
            post_analyzers=[],
        )
    except Exception as e:
        log.error("AlphaSift screen() 执行出错: %s", e)
        raise

    picks = result.picks
    log.info("AlphaSift 初选完成，筛选出 %d 支股票", len(picks))

    stocks = []
    for p in picks:
        # 银行股在评分输出层直接排除（银行因子分高，留到选股循环再排已来不及）
        if _is_bank(p.code, p.name):
            continue
        stocks.append({
            "code": p.code,
            "name": p.name,
            "score": round(p.final_score, 4),
            "strategy": strategy,
        })
        log.info("  %d. %s (%s)  评分: %.4f", len(stocks), p.name, p.code, p.final_score)
        if len(stocks) >= top_n:
            break
    if len(stocks) < top_n:
        log.warning("  [%s] 过滤银行后仅剩 %d 支非银行候选", strategy, len(stocks))

    return stocks


def _generate_mock_stocks(count: int = 10) -> list:
    """生成模拟的前 N 支股票列表。"""
    import random

    random.seed(42)
    pool = _SIMULATED_STOCKS[:]
    random.shuffle(pool)
    selected = pool[:count]
    return [{"code": item["code"], "name": item["name"], "score": 0, "strategy": "mock"} for item in selected]


_cached_snapshot_path = None
_cached_snapshot_df = None


def _filter_stocks(stocks):
    """过滤股票：去掉 ST、银行股、高价股。"""
    if not stocks:
        return stocks
    import requests

    # 1. 用新浪接口拉实时价格和名称
    log.info("  正在获取实时行情数据...")
    spot_map = {}  # code -> price
    name_map = {}  # code -> name (from sina)
    codes = [s["code"] for s in stocks]
    
    sina_codes = []
    for c in codes:
        prefix = "sz" if c.startswith(("0", "3")) else "sh"
        sina_codes.append(f"{prefix}{c}")
    
    try:
        url = "https://hq.sinajs.cn/list=" + ",".join(sina_codes)
        headers = {"Referer": "https://finance.sina.com.cn"}
        r = requests.get(url, headers=headers, timeout=15)
        r.encoding = "gbk"
        for line in r.text.strip().split("\n"):
            if "=" not in line:
                continue
            var_part, data_part = line.split("=", 1)
            symbol = var_part.split("_")[-1] if "_" in var_part else ""
            if '"' in data_part:
                data_part = data_part.split('"')[1].split('"')[0]
            parts = data_part.split(",")
            if len(parts) >= 2:
                # 找到对应的原始代码
                for c in codes:
                    prefix = "sz" if c.startswith(("0", "3")) else "sh"
                    if f"{prefix}{c}" == symbol:
                        spot_map[c] = float(parts[2])  # 成交价
                        name_map[c] = parts[0]  # 股票名称
                        break
        log.info("  新浪行情获取成功，覆盖 %d/%d 支股票", len(spot_map), len(codes))
    except Exception as e:
        log.warning("  新浪行情获取失败: %s，跳过价格过滤", e)

    # 2. 行业分类：由于东方财富接口被代理拦截，改用名称判断 + 银行黑名单
    log.info("  正在获取行业分类信息...")
    industry_map = {}
    for code in codes:
        industry_map[code] = "银行" if _is_bank(code, name_map.get(code, "")) else ""
    log.info("  行业分类获取完成，覆盖 %d/%d 支股票", len(industry_map), len(codes))

    # 3. 执行过滤
    filtered = []
    removed_reasons = []
    BANK_KEYWORDS = ["银行", "商业银行"]

    for s in stocks:
        code = s["code"]
        name = s.get("name", "")
        # 用新浪返回的名称覆盖（更准确）
        if code in name_map:
            name = name_map[code]
        score = s.get("score", 0)
        reasons = []

        # 规则1: ST 股
        if "ST" in name.upper() or name.startswith("*ST") or name.startswith("ST"):
            reasons.append("ST股")

        # 规则2: 银行股
        ind = industry_map.get(code, "")
        if any(kw in ind for kw in BANK_KEYWORDS):
            reasons.append("银行股")

        # 规则3: 股价 > 100（高评分可豁免）
        price = spot_map.get(code)
        if price and price > 100:
            if score >= 0.85:
                reasons.append(f"高价({price:.2f}元)但高分豁免")
            else:
                reasons.append(f"高价({price:.2f}元)")

        if reasons:
            joined = ", ".join(reasons)
            removed_reasons.append(f"  移除 {name}({code}): {joined}")
        else:
            filtered.append(s)

    if removed_reasons:
        log.info("  过滤详情:")
        for r in removed_reasons:
            log.info(r)

    log.info("  过滤后: %d -> %d 支", len(stocks), len(filtered))
    return filtered


def _quick_filter_stocks(stocks):
    """轻量过滤：去掉 ST 和银行股（不拉行情，无网络开销）。"""
    filtered = []
    for s in stocks:
        code = s["code"]
        name = s.get("name", "")
        if "ST" in name.upper() or name.startswith("*ST") or name.startswith("ST"):
            continue
        if _is_bank(code, name):
            continue
        filtered.append(s)
    return filtered


def _load_local_snapshot_df():
    """从 data/snapshot_latest.json 读取当日快照，转成 DataFrame 兜底。"""
    import pandas as pd

    path = Path(__file__).resolve().parent.parent / "data" / "snapshot_latest.json"
    if not path.exists():
        log.warning("本地快照不存在: %s", path)
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        rows = payload.get("stocks", [])
        if not rows:
            log.warning("本地快照为空: %s", path)
            return None
        df = pd.DataFrame(rows)
        df.attrs["snapshot_source"] = "local_daily_snapshot"
        df.attrs["fallback_used"] = True
        log.info("本地快照加载: %d 行（来源: %s）", len(df), path)
        return df
    except Exception as e:
        log.warning("本地快照读取失败: %s", e)
        return None


def _run_local_snapshot_screen(snapshot_df=None, top_n: int = 10) -> list:
    """基于本仓库当日快照的简化初选（兜底用，不依赖缺失的财务因子列）。

    规则：排除 ST、银行、停牌(价格为0)、高价股，按当日涨幅与成交量打分排序。
    """
    import numpy as np
    import pandas as pd

    if snapshot_df is None or len(snapshot_df) == 0:
        return []

    df = snapshot_df.copy()
    name_col = "name" if "name" in df.columns else None

    mask = pd.Series(True, index=df.index)
    if name_col:
        mask &= ~df[name_col].astype(str).str.contains(r"ST|退", na=False)
    if "is_bank" in df.columns:
        mask &= ~df["is_bank"].fillna(False).astype(bool)
    if "price" in df.columns:
        price = pd.to_numeric(df["price"], errors="coerce")
        mask &= price > 0
        mask &= price <= 100
    df = df[mask]
    if df.empty:
        log.warning("本地快照简化筛选后无候选")
        return []

    # 打分：当日涨幅占 60%，成交量占比 40%
    score = pd.Series(0.0, index=df.index)
    if "change_pct" in df.columns:
        chg = pd.to_numeric(df["change_pct"], errors="coerce").fillna(0.0)
        score = score + chg * 0.6
    if "volume" in df.columns:
        vol = pd.to_numeric(df["volume"], errors="coerce").fillna(0.0)
        vol_log = np.log1p(vol)
        if vol_log.max() > 0:
            score = score + (vol_log / vol_log.max()) * 40.0

    df = df.assign(_score=score).sort_values("_score", ascending=False).head(top_n)

    stocks = []
    for _, row in df.iterrows():
        stocks.append({
            "code": str(row["code"]),
            "name": row.get("name", "") if name_col else "",
            "score": round(float(row["_score"]), 4),
            "strategy": "local_snapshot",
        })
    log.info("本地快照简化初选完成，选出 %d 支", len(stocks))
    return stocks


def _snapshot_to_alphasift_df(df):
    """把本地快照转成 alphasift 兼容列名（缺列填空值，scorer 有 fillna 保护）。"""
    import pandas as pd

    idx = df.index
    out = pd.DataFrame(index=idx)
    out["symbol"] = df["code"].astype(str)
    out["code"] = df["code"].astype(str)
    out["name"] = df.get("name", pd.Series("", index=idx))
    out["price"] = pd.to_numeric(df["price"], errors="coerce").fillna(0.0)
    out["pricechange"] = pd.Series(0.0, index=idx)
    out["change_pct"] = pd.to_numeric(df.get("change_pct", 0.0), errors="coerce").fillna(0.0)
    out["buy"] = pd.Series(0.0, index=idx)
    out["sell"] = pd.Series(0.0, index=idx)
    out["settlement"] = pd.Series(0.0, index=idx)
    out["open"] = pd.Series(0.0, index=idx)
    out["high"] = pd.Series(0.0, index=idx)
    out["low"] = pd.Series(0.0, index=idx)
    out["volume"] = pd.to_numeric(df.get("volume", 0.0), errors="coerce").fillna(0.0)
    out["amount"] = pd.to_numeric(df.get("amount", 0.0), errors="coerce").fillna(0.0)
    out["ticktime"] = pd.Series("", index=idx)
    out["pe_ratio"] = pd.to_numeric(df.get("pe_ratio", 0.0), errors="coerce").fillna(0.0)
    out["pb_ratio"] = pd.to_numeric(df.get("pb_ratio", 0.0), errors="coerce").fillna(0.0)
    out["total_mv"] = pd.to_numeric(df.get("total_mv", 0.0), errors="coerce").fillna(0.0)
    out["circ_mv"] = pd.to_numeric(df.get("circ_mv", 0.0), errors="coerce").fillna(0.0)
    out["turnover_rate"] = pd.to_numeric(df.get("turnover_rate", 0.0), errors="coerce").fillna(0.0)
    out["volume_ratio"] = pd.to_numeric(df.get("volume_ratio", 1.0), errors="coerce").fillna(1.0)
    return out


def _load_history_scores() -> dict:
    """读取 data/skill_history/ 与 data/skill_scores_history.json 的历史深度评分。"""
    hist_dir = Path(__file__).resolve().parent.parent / "data" / "skill_history"
    scores: dict[str, list[float]] = {}
    files = []
    if hist_dir.is_dir():
        files.extend(sorted(hist_dir.glob("*.json")))
    hist_scores_file = (
        Path(__file__).resolve().parent.parent / "data" / "skill_scores_history.json"
    )
    if hist_scores_file.exists():
        files.append(hist_scores_file)
    for f in files:
        try:
            with open(f, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            continue
        # 兼容两种格式：单次结果列表，或累积列表 [{date, results:[...]}]
        items = data
        if isinstance(data, dict):
            items = data.get("results", [])
        if isinstance(items, list) and items and isinstance(items[0], dict) and "results" in items[0]:
            for entry in items:
                for item in entry.get("results", []):
                    code = item.get("stock_code") or item.get("code")
                    score = item.get("final_score") or item.get("overall_score")
                    if code and score:
                        scores.setdefault(str(code), []).append(float(score))
            continue
        for item in (items if isinstance(items, list) else []):
            code = item.get("stock_code") or item.get("code")
            score = item.get("final_score") or item.get("overall_score")
            if code and score:
                scores.setdefault(str(code), []).append(float(score))
    if not scores:
        return {}
    return {code: sum(v) / len(v) for code, v in scores.items()}


def _fetch_and_cache_snapshot(output_file: str):
    """预取快照并缓存到本地，供后续策略复用。"""
    global _cached_snapshot_path
    
    if not _ensure_alphasift_available():
        log.warning("AlphaSift 不可用，跳过快照缓存")
        return None
    
    from alphasift.snapshot import fetch_snapshot_with_fallback
    from alphasift.config import Config
    from datetime import datetime, timezone
    
    config = Config.from_env()
    sources = config.snapshot_source_priority
    log.info("正在获取全市场快照 (数据源: %s)...", ", ".join(sources))
    
    try:
        result = fetch_snapshot_with_fallback(
            sources,
            fallback_snapshot_path=config.fallback_snapshot_path,
        )
        log.info("快照获取完成: %d 行，来源: %s", len(result), result.attrs.get("snapshot_source", "unknown"))
        
        # Save in AlphaSift's expected fallback format
        cache_path = Path(output_file).parent / '.snapshot_cache.json'
        
        payload = {
            "version": 1,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "metadata": {
                "snapshot_source": result.attrs.get("snapshot_source", "unknown"),
                "row_count": len(result),
            },
            "frame": {
                "columns": list(result.columns),
                "data": result.values.tolist(),
            }
        }
        
        with open(cache_path, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        
        log.info("快照已缓存到: %s (%d 行)", cache_path, len(result))
        _cached_snapshot_path = str(cache_path)
        _cached_snapshot_df = result  # Store DataFrame for monkey-patching
        return _cached_snapshot_path
    except Exception as e:
        log.warning("快照获取失败: %s，将使用 fallback", e)
        return None


def discover_stocks(output_file: str, strategy: str = None, use_llm: bool = True, stock_filter: str = None):
    """执行初选并输出结果。"""
    global _cached_snapshot_df
    log.info("=" * 60)
    log.info("AlphaSift 初选开始  |  %s", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    log.info("=" * 60)
    
    # CI 环境：直接用当日快照，跳过 alphasift 网络抓取（避免长时间超时等待）
    if os.environ.get("ALPHASIFT_USE_LOCAL_SNAPSHOT") == "1":
        local_df = _load_local_snapshot_df()
        if local_df is not None:
            _cached_snapshot_df = local_df
            log.info("按环境变量要求，使用本仓库当日快照（跳过网络抓取）")
        else:
            log.warning("环境变量要求使用本地快照，但本地快照不存在")
    else:
        # Pre-fetch snapshot once for all strategies
        snapshot_cache = _fetch_and_cache_snapshot(output_file)
        if _cached_snapshot_df is None:
            local_df = _load_local_snapshot_df()
            if local_df is not None:
                _cached_snapshot_df = local_df
                log.info("使用本仓库当日快照兜底")

    # 确定使用的策略
    if strategy:
        if strategy not in STRATEGIES:
            log.warning("策略 '%s' 不存在，可用策略: %s", strategy, STRATEGIES)
            strategy = "balanced_alpha"
        log.info("指定策略: %s", strategy)
    else:
        # 默认使用全部 10 个策略，每个选 Top 1
        strategy = None
        log.info("未指定策略，使用全部 %d 个策略各选 Top 1", len(STRATEGIES))
        # Set flag to skip network for all subsequent strategy runs
        os.environ["ALPHASIFT_SKIP_NETWORK"] = "1"

    # 兜底：alphasift 实时快照失败时，用本仓库当日快照
    if _cached_snapshot_df is None:
        local_df = _load_local_snapshot_df()
        if local_df is not None:
            _cached_snapshot_df = local_df
            log.info("使用本仓库当日快照兜底")

    use_local_snapshot = (
        _cached_snapshot_df is not None
        and _cached_snapshot_df.attrs.get("snapshot_source") == "local_daily_snapshot"
    )
    use_llm_rank = use_llm and bool(LLM_API_KEY)

    # 本地快照含完整因子（PE/PB/市值等）时，转成 alphasift 格式走真实多因子打分
    local_snapshot_full = (
        use_local_snapshot
        and "total_mv" in _cached_snapshot_df.columns
        and "pe_ratio" in _cached_snapshot_df.columns
        and "pb_ratio" in _cached_snapshot_df.columns
    )
    raw_local_df = None
    if local_snapshot_full:
        raw_local_df = _cached_snapshot_df.copy()
        _cached_snapshot_df = _snapshot_to_alphasift_df(_cached_snapshot_df)
        _cached_snapshot_df.attrs["snapshot_source"] = "local_daily_snapshot"
        # 写入 alphasift 的 last-good 缓存路径，让 screen() 内部跳过网络直接读当日数据
        try:
            from alphasift.config import Config
            import alphasift.snapshot as snap_module
            cache_path = Config.from_env().fallback_snapshot_path
            if cache_path is None:
                cache_path = Path(output_file).parent / ".snapshot_cache.json"
                os.environ["ALPHASIFT_FALLBACK_SNAPSHOT_PATH"] = str(cache_path)
            snap_module._write_last_good_snapshot(cache_path, _cached_snapshot_df)
            os.environ["ALPHASIFT_SKIP_NETWORK"] = "1"
            log.info("本地快照已写入 alphasift 缓存: %s", cache_path)
        except Exception as e:
            log.warning("写入 alphasift 缓存失败: %s", e)
        log.info("本地快照含完整因子，转 alphasift 格式走真实多因子打分")

    # 尝试 AlphaSift 真实初选
    if _ensure_alphasift_available() and (not use_local_snapshot or local_snapshot_full):
        try:
            if strategy:
                # 单策略模式（兼容旧用法）
                stocks = _run_alphasift_screen(
                    strategy, top_n=10, use_llm=use_llm_rank, snapshot_df=_cached_snapshot_df
                )
                if stocks:
                    log.info("AlphaSift 真实初选完成，选出 %d 支股票", len(stocks))
                else:
                    raise ValueError("AlphaSift 返回空结果")
            else:
                # 遍历所有策略，每个策略选 Top 1（跳过已占用/银行/ST 股）
                used_codes = set()
                all_stocks = []
                candidate_pool = {}  # 所有策略的合格候选（code -> pick），供不足时按分补齐
                for strat_name in STRATEGIES:
                    picks = _run_alphasift_screen(strat_name, top_n=10, use_llm=use_llm_rank,
                                                   snapshot_df=_cached_snapshot_df)

                    if not picks:
                        log.warning("  [%s] 未选出股票，跳过", strat_name)
                        continue

                    # 先收集该策略所有合格候选（非银行/ST）进候选池，再取第一个未被占用的
                    picked = None
                    for pick in sorted(picks, key=lambda x: x.get("score", 0), reverse=True):
                        code = pick["code"]
                        name = pick.get("name", "")
                        if code in KNOWN_BANK_CODES or any(kw in name for kw in BANK_KEYWORDS):
                            continue
                        if "ST" in name.upper():
                            continue
                        # 候选池只保留最高分（同一股票被多策略选中时）
                        if code not in candidate_pool or candidate_pool[code].get("score", 0) < pick.get("score", 0):
                            candidate_pool[code] = pick
                        if picked is None and code not in used_codes:
                            picked = pick

                    if picked:
                        all_stocks.append(picked)
                        used_codes.add(picked["code"])
                        log.info("  [%s] 选中: %s (%s)  评分: %.4f",
                                 strat_name, picked["name"], picked["code"], picked["score"])
                    else:
                        log.warning("  [%s] 候选均被占用或为银行/ST 股，跳过", strat_name)

                stocks = all_stocks

                # 不足 10 支时：先从高分候选池补齐（比本地快照简化打分更优），再考虑兜底
                if len(stocks) < 10:
                    log.warning("仅选出 %d 支，从高分候选池补足", len(stocks))
                    for pick in sorted(candidate_pool.values(), key=lambda x: x.get("score", 0), reverse=True):
                        if len(stocks) >= 10:
                            break
                        code = pick["code"]
                        if code in used_codes:
                            continue
                        stocks.append(pick)
                        used_codes.add(code)
                        log.info("  [候选池补足] %s (%s)  评分: %.4f",
                                 pick["name"], code, pick["score"])
                if len(stocks) < 10:
                    log.warning("候选池不足，尝试用本地快照补足")
                    local_picks = _run_local_snapshot_screen(
                        raw_local_df if raw_local_df is not None else _cached_snapshot_df, 10
                    )
                    for lp in local_picks:
                        if lp["code"] not in used_codes:
                            stocks.append(lp)
                            used_codes.add(lp["code"])
                            log.info("  [本地快照补足] %s (%s)  评分: %.4f",
                                     lp["name"], lp["code"], lp["score"])
                if len(stocks) < 10:
                    log.warning("仍不足 10 支，用模拟数据补足")
                    mock = _generate_mock_stocks(10 - len(stocks))
                    for m in mock:
                        if m["code"] not in used_codes:
                            stocks.append(m)
                            used_codes.add(m["code"])
                            log.info("  [模拟补足] %s (%s)", m["name"], m["code"])

                log.info("AlphaSift 真实初选完成，共选出 %d 支股票", len(stocks))
        except Exception as e:
            log.error("AlphaSift 真实初选执行出错: %s", e)
            log.info("降级到本地快照简化筛选")
            stocks = _run_local_snapshot_screen(
                raw_local_df if raw_local_df is not None else _cached_snapshot_df, 10
            )
            if not stocks:
                log.info("本地快照筛选无结果，使用模拟数据")
                stocks = _generate_mock_stocks()
    elif use_local_snapshot and not local_snapshot_full:
        log.info("使用本地快照简化初选")
        stocks = _run_local_snapshot_screen(_cached_snapshot_df, 10)
        if not stocks:
            log.info("本地快照筛选无结果，使用模拟数据")
            stocks = _generate_mock_stocks()
    else:
        stocks = _generate_mock_stocks()
        log.info("使用模拟数据选出 %d 支股票", len(stocks))

    # 如果指定了过滤条件，只保留这些股票
    if stock_filter:
        codes = [c.strip() for c in stock_filter.split(",")]
        stocks = [s for s in stocks if s.get("code", "") in codes]
        log.info("过滤条件: %s -> 保留 %d 支", stock_filter, len(stocks))
    
    stocks = _filter_stocks(stocks)
    stocks = stocks[:10]

    # 结合历史深度评分：高分股票排前
    history = _load_history_scores()
    if history:
        def hist_key(s):
            h = history.get(s["code"])
            return -h if h else 0.0
        stocks = sorted(stocks, key=hist_key)
        hit = sum(1 for s in stocks if s["code"] in history)
        log.info("结合历史深度评分排序，命中 %d/%d 支", hit, len(stocks))

        # 保存为 JSON (include all fields including strategy)
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(stocks, f, ensure_ascii=False, indent=2)
    log.info("结果已保存到: %s", output_file)
    
    # Debug: print what's being saved
    if stocks:
        log.info("保存的股票字段: %s", list(stocks[0].keys()))

    log.info("结果已保存到: %s", output_file)

    # 打印摘要
    log.info("-" * 40)
    log.info("初选结果摘要:")
    for i, s in enumerate(stocks, 1):
        code = s["code"]
        name = s.get("name", "")
        score = s.get("score", 0)
        strat = s.get("strategy", "")
        log.info("  %d. %s (%s)  评分: %.4f  [策略: %s]", i, name, code, score, strat)
    log.info("-" * 40)
    log.info("初选完成！共选出 %d 支股票。", len(stocks))


def main():
    parser = argparse.ArgumentParser(
        description="AlphaSift 初选脚本：从全市场筛选优质股票",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
示例:
  python alphasift_discovery.py
  python alphasift_discovery.py --strategy balanced_alpha
  python alphasift_discovery.py --output my_stocks.json
  python alphasift_discovery.py --no-llm

可用策略:
  %(default)s

前置条件:
  在项目根目录创建 .env 文件，填入 LLM_API_KEY 或 OPENAI_API_KEY 等变量。
  完整配置说明见 docs/LLM_CONFIG_GUIDE.md
        """,
    )
    parser.add_argument(
        "--output",
        default="top_10_stocks.json",
        help="输出文件路径（默认: top_10_stocks.json）",
    )
    parser.add_argument(
        "--strategy",
        default=None,
        help=f"选股策略名称（默认: balanced_alpha）。可用策略: {', '.join(STRATEGIES)}",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="关闭 LLM 排名，仅使用 L1 因子筛选（更快）",
    )
    parser.add_argument(
        "--filter",
        default=None,
        help="只筛选特定股票代码（逗号分隔），例如: 600519,000858",
    )
    args = parser.parse_args()
    discover_stocks(args.output, args.strategy, use_llm=not args.no_llm)


if __name__ == "__main__":
    main()
