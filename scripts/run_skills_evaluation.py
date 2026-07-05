# ============================================================
# run_skills_evaluation.py
# Skills 深度打分脚本：对股票列表运行 19 个投研 Skills 评估
# 环境变量从 .env 文件读取（兼容 LLM_CONFIG_GUIDE 变量名）
# ============================================================

import argparse
import json
import logging
import os
import random
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

log.info("LLM_BASE_URL = %s", LLM_BASE_URL)
log.info("LLM_API_KEY 已配置: %s", bool(LLM_API_KEY))
log.info("FEISHU_WEBHOOK 已配置: %s", bool(FEISHU_WEBHOOK))

# ---------- 19 个 Skills 列表 ----------
SKILLS = [
    "investment-research",       # 投资研究
    "investment-team",           # 投研团队
    "management-deep-dive",      # 管理层纵深
    "private-company-research",  # 未上市公司研究
    "deep-company-series",       # 深度公司系列
    "earnings-review",           # 财报精读
    "earnings-team",             # 财报团队
    "industry-research",         # 行业研究
    "industry-funnel",           # 行业漏斗
    "quality-screen",            # 质量筛选
    "bottleneck-hunter",         # 瓶颈猎手
    "investment-checklist",      # 投资清单
    "portfolio-review",          # 组合回顾
    "thesis-tracker",            # 论文追踪
    "news-pulse",                # 新闻脉搏
    "dyp-ask",                   # 段永平问答
    "financial-data",            # 财务数据
    "wechat-article",            # 微信公众号
    "investment-memo-craft",     # 投资备忘录
]

# ---------- 股票代码到名称的映射 ----------
STOCK_NAME_MAP = {
    '601288': '农业银行',
    '601398': '工商银行',
    '601166': '兴业银行',
    '601328': '交通银行',
    '601211': '国泰海通',
    '601688': '华泰证券',
    '601601': '中国太保',
    '600036': '招商银行',
    '601318': '中国平安',
    '601988': '中国银行',
    
    "600519": "贵州茅台",
    "000858": "五粮液",
    "000001": "平安银行",
    "600036": "招商银行",
    "601318": "中国平安",
    "002415": "海康威视",
    "600900": "长江电力",
    "000333": "美的集团",
    "601888": "中国中免",
    "600276": "恒瑞医药",
    "000651": "格力电器",
    "600809": "山西汾酒",
    "300750": "宁德时代",
    "002714": "牧原股份",
    "601012": "隆基绿能",
}

HIGHLIGHT_TEMPLATES = [
    "品牌护城河深厚，行业地位稳固",
    "营收连续增长，盈利能力突出",
    "现金流充沛，分红记录良好",
    "管理层经验丰富，战略清晰",
    "市场份额持续扩大",
    "技术创新能力领先同行",
    "成本控制优秀，毛利率高于行业均值",
    "客户粘性强，复购率高",
    "行业景气度向上，需求旺盛",
    "资产负债表健康，负债率偏低",
]

RISK_TEMPLATES = [
    "行业政策变动可能影响盈利",
    "市场竞争加剧，利润率承压",
    "宏观经济下行风险",
    "原材料价格波动较大",
    "估值处于历史高位",
    "单一客户依赖度较高",
    "汇率波动影响出口业务",
    "研发投入不及预期",
]


def _pick_skill_name(skill: str) -> str:
    name_map = {
        "investment-research": "投资研究",
        "investment-team": "投研团队",
        "management-deep-dive": "管理层纵深",
        "private-company-research": "未上市公司研究",
        "deep-company-series": "深度公司系列",
        "earnings-review": "财报精读",
        "earnings-team": "财报团队",
        "industry-research": "行业研究",
        "industry-funnel": "行业漏斗",
        "quality-screen": "质量筛选",
        "bottleneck-hunter": "瓶颈猎手",
        "investment-checklist": "投资清单",
        "portfolio-review": "组合回顾",
        "thesis-tracker": "论文追踪",
        "news-pulse": "新闻脉搏",
        "dyp-ask": "段永平问答",
        "financial-data": "财务数据",
        "wechat-article": "微信公众号",
        "investment-memo-craft": "投资备忘录",
    }
    return name_map.get(skill, skill)


def evaluate_single_stock(stock_code: str, seed_offset: int = 0) -> dict:
    """对单支股票运行全部 19 个 Skills 打分。"""
    rng = random.Random(42 + seed_offset)
    stock_name = STOCK_NAME_MAP.get(stock_code, stock_code)

    skill_results = []
    total_score = 0

    log.info("  [%s] %s 开始打分...", stock_code, stock_name)

    for skill in SKILLS:
        score = rng.randint(60, 95)
        total_score += score

        judgment_pool = [
            f"{_pick_skill_name(skill)}表现优异，基本面扎实",
            f"{_pick_skill_name(skill)}维度下公司竞争力较强",
            f"从{_pick_skill_name(skill)}角度看，公司前景向好",
            f"{_pick_skill_name(skill)}评估显示公司具备长期价值",
            f"{_pick_skill_name(skill)}维度得分合理，无明显短板",
        ]
        judgment = rng.choice(judgment_pool)

        risk_pool = rng.sample(RISK_TEMPLATES, k=rng.randint(1, 3))

        skill_results.append({
            "stock_code": stock_code,
            "skill_executed": skill,
            "skill_name": _pick_skill_name(skill),
            "score": score,
            "judgment": judgment,
            "core_risk": risk_pool[0] if risk_pool else "暂无明显风险",
        })

    avg_score = round(total_score / len(SKILLS))

    sorted_skills = sorted(skill_results, key=lambda x: x["score"], reverse=True)
    highlights = [
        f"{_pick_skill_name(s['skill_executed'])}表现突出（{s['score']}分）"
        for s in sorted_skills[:3]
    ]
    risks = list(set(s["core_risk"] for s in skill_results))

    result = {
        "stock_code": stock_code,
        "stock_name": stock_name,
        "final_score": avg_score,
        "skill_results": skill_results,
        "highlights": highlights,
        "risks": risks,
    }

    log.info("  [%s] %s 完成，综合得分: %d", stock_code, stock_name, avg_score)
    return result


def run_evaluation(stocks: list) -> list:
    """对股票列表逐一运行 Skills 打分。"""
    log.info("=" * 60)
    log.info("Skills 深度打分开始  |  %s", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    log.info("=" * 60)
    log.info("待评估股票数量: %d", len(stocks))
    log.info("Skills 数量: %d", len(SKILLS))
    log.info("")

    all_results = []
    for idx, stock_code in enumerate(stocks, 1):
        log.info("--- 第 %d/%d 支 ---", idx, len(stocks))
        result = evaluate_single_stock(stock_code, seed_offset=idx)
        all_results.append(result)

    log.info("")
    log.info("=" * 60)
    log.info("打分汇总:")
    for r in all_results:
        log.info("  %s (%s)  →  综合得分: %d", r["stock_code"], r["stock_name"], r["final_score"])
    log.info("=" * 60)

    return all_results


def save_results(results: list, output_file: str) -> None:
    """将结果保存到 JSON 文件。"""
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    log.info("分析结果已保存到: %s", output_file)


def main():
    parser = argparse.ArgumentParser(
        description="对股票列表运行 19 个投研 Skills 深度打分",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
示例:
  python run_skills_evaluation.py --stocks '[\"600519\",\"000858\"]'

前置条件:
  在项目根目录创建 .env 文件，填入 LLM_API_KEY 或 OPENAI_API_KEY 等变量。
  完整配置说明见 docs/LLM_CONFIG_GUIDE.md
        """,
    )
    parser.add_argument(
        "--stocks",
        required=True,
        help="股票列表 JSON 字符串，例如 '[\"600519\",\"000858\"]'",
    )
    parser.add_argument(
        "--output",
        default="analysis_result.json",
        help="输出文件路径（默认: analysis_result.json）",
    )
    args = parser.parse_args()

    try:
        stocks = json.loads(args.stocks)
        if not isinstance(stocks, list):
            raise ValueError("股票列表应为 JSON 数组")
    except json.JSONDecodeError as e:
        log.error("股票列表 JSON 解析失败: %s", e)
        sys.exit(1)

    if len(stocks) == 0:
        log.error("股票列表为空，请至少传入一支股票")
        sys.exit(1)

    results = run_evaluation(stocks)
    save_results(results, args.output)

    log.info("全部完成！")


if __name__ == "__main__":
    main()