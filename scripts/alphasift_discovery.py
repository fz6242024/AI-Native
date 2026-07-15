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


def _ensure_alphasift_available() -> bool:
    """确保 AlphaSift 模块可用。"""
    if not os.path.isdir(ALPHASIFT_PROJECT_PATH):
        log.warning("AlphaSift 项目目录不存在: %s", ALPHASIFT_PROJECT_PATH)
        return False

    if ALPHASIFT_PROJECT_PATH not in sys.path:
        sys.path.insert(0, ALPHASIFT_PROJECT_PATH)

    try:
        from alphasift.pipeline import screen  # noqa: F401
        log.info("AlphaSift 模块已就绪")
        return True
    except ImportError as e:
        log.warning("AlphaSift 模块导入失败: %s，回退到模拟数据", e)
        return False


def _run_alphasift_screen(strategy: str, top_n: int = 10, use_llm: bool = False, snapshot_df: object = None) -> list:
    """调用 AlphaSift 的 screen() 函数进行初选。"""
    from alphasift.pipeline import screen
    from alphasift import snapshot as snap_module

    log.info("使用策略 [%s] 运行 AlphaSift 初选 (LLM=%s)...", strategy, use_llm)
    
    # Monkey-patch fetch_snapshot_with_fallback to return cached data
    if snapshot_df is not None:
        original_fetch = snap_module.fetch_snapshot_with_fallback
        
        def patched_fetch(*args, **kwargs):
            # Return the cached DataFrame directly, skipping network
            cached = snapshot_df.copy()
            cached.attrs["snapshot_source"] = "cached"
            cached.attrs["fallback_used"] = True
            cached.attrs["stale"] = True
            cached.attrs["source_errors"] = []
            log.info("  使用缓存快照（跳过网络请求）")
            return cached
        
        snap_module.fetch_snapshot_with_fallback = patched_fetch

    try:
        result = screen(
            strategy=strategy,
            market="cn",
            max_output=top_n,
            use_llm=use_llm,
        )
    except Exception as e:
        log.error("AlphaSift screen() 执行出错: %s", e)
        raise

    picks = result.picks
    log.info("AlphaSift 初选完成，筛选出 %d 支股票", len(picks))

    stocks = []
    for p in picks:
        stocks.append({
            "code": p.code,
            "name": p.name,
            "score": round(p.final_score, 4),
            "strategy": strategy,
        })
        log.info("  %d. %s (%s)  评分: %.4f", len(stocks), p.name, p.code, p.final_score)

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
    # 已知银行股名称关键字（覆盖绝大多数银行）
    BANK_NAME_KEYWORDS = [
        "银行", "农商行", "农信社", "农村商业",
    ]
    # 常见银行股票代码白名单（防止名称判断遗漏）
    KNOWN_BANK_CODES = {
        "000001", "000002", "002142", "002807", "001289", "002966",  # 平安、招商、宁波、杭州、沪农商、长沙
        "001339", "002936", "001267", "600908", "600919", "600926",   # 江苏银行等
        "601002", "601009", "601128", "601166", "601216", "601229",   # 西部、南京、西安、浦发、中信、工行
        "601328", "601398", "601577", "601588", "601658",   # 平安银行、邮储、建行、民生、周行
        "601665", "601788", "601997", "601998",                      # 光大、沪农商、北京、青岛
    }
    for code in codes:
        if code in KNOWN_BANK_CODES:
            industry_map[code] = "银行"
        else:
            industry_map[code] = ""
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
    log.info("=" * 60)
    log.info("AlphaSift 初选开始  |  %s", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    log.info("=" * 60)
    
    # Pre-fetch snapshot once for all strategies
    snapshot_cache = _fetch_and_cache_snapshot(output_file)

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

    # 尝试 AlphaSift 真实初选
    if _ensure_alphasift_available():
        try:
            if strategy:
                # 单策略模式（兼容旧用法）
                stocks = _run_alphasift_screen(strategy, top_n=10, use_llm=use_llm)
                if stocks:
                    log.info("AlphaSift 真实初选完成，选出 %d 支股票", len(stocks))
                else:
                    raise ValueError("AlphaSift 返回空结果")
            else:
                # 新逻辑：遍历所有策略，每个选 Top 1
                used_codes = set()
                all_stocks = []

                for strat_name in STRATEGIES:
                    picks = _run_alphasift_screen(strat_name, top_n=10, use_llm=False,
                                                   snapshot_df=_cached_snapshot_df)

                    if not picks:
                        log.warning("  [%s] 未选出股票，跳过", strat_name)
                        continue

                    # 取该策略的第一支未被占用的股票
                    picked = None
                    for pick in picks:
                        if pick["code"] not in used_codes:
                            picked = pick
                            break

                    if picked:
                        all_stocks.append(picked)
                        used_codes.add(picked["code"])
                        log.info("  [%s] 选中: %s (%s)  评分: %.4f",
                                 strat_name, picked["name"], picked["code"], picked["score"])
                    else:
                        log.warning("  [%s] 所有候选已被占用，跳过", strat_name)

                stocks = all_stocks

                # 如果不足 10 支，用剩余策略补足
                if len(stocks) < 10:
                    log.warning("仅选出 %d 支，尝试用模拟数据补足", len(stocks))
                    mock = _generate_mock_stocks(10 - len(stocks))
                    for m in mock:
                        if m["code"] not in used_codes:
                            stocks.append(m)
                            used_codes.add(m["code"])
                            log.info("  [补足] %s (%s)", m["name"], m["code"])

                log.info("AlphaSift 真实初选完成，共选出 %d 支股票", len(stocks))
        except Exception as e:
            log.error("AlphaSift 真实初选执行出错: %s", e)
            log.info("回退到模拟数据")
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