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


def _run_alphasift_screen(strategy: str, top_n: int = 10, use_llm: bool = True) -> list:
    """调用 AlphaSift 的 screen() 函数进行初选。"""
    from alphasift.pipeline import screen

    log.info("使用策略 [%s] 运行 AlphaSift 初选 (LLM=%s)...", strategy, use_llm)

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
    return [{"code": item["code"], "name": item["name"], "score": 0} for item in selected]


def discover_stocks(output_file: str, strategy: str = None, use_llm: bool = True, stock_filter: str = None):
    """执行初选并输出结果。"""
    log.info("=" * 60)
    log.info("AlphaSift 初选开始  |  %s", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    log.info("=" * 60)

    # 确定使用的策略
    if strategy:
        if strategy not in STRATEGIES:
            log.warning("策略 '%s' 不存在，可用策略: %s", strategy, STRATEGIES)
            strategy = "balanced_alpha"
        log.info("指定策略: %s", strategy)
    else:
        strategy = "balanced_alpha"
        log.info("未指定策略，使用默认: %s", strategy)

    # 尝试 AlphaSift 真实初选
    if _ensure_alphasift_available():
        try:
            stocks = _run_alphasift_screen(strategy, top_n=10, use_llm=use_llm)
            if stocks:
                log.info("AlphaSift 真实初选完成，选出 %d 支股票", len(stocks))
            else:
                raise ValueError("AlphaSift 返回空结果")
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
    
    # 保存为 JSON
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(stocks, f, ensure_ascii=False, indent=2)

    log.info("结果已保存到: %s", output_file)

    # 打印摘要
    log.info("-" * 40)
    log.info("初选结果摘要:")
    for i, s in enumerate(stocks, 1):
        code = s["code"]
        name = s.get("name", "")
        score = s.get("score", 0)
        log.info("  %d. %s (%s)  评分: %.4f", i, name, code, score)
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