# ============================================================
# run_skills_evaluation.py
# Skills 深度评估脚本：通过 LLM API 调用 19 个投研 Skills
# 每个 Skill 对应一个研究框架 prompt，调用 LLM 生成结构化评估
# ============================================================

import argparse
import json
import logging
import os
import sys
import time
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

# ---------- 从 .env 读取 LLM 配置 ----------
LLM_BASE_URL = os.environ.get("LLM_BASE_URL") or os.environ.get("OPENAI_BASE_URL", "")
LLM_API_KEY = os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK", "")

# 支持 LLM_CHANNELS / my_proxy 渠道
LLM_CHANNELS = os.environ.get("LLM_CHANNELS", "")
if LLM_CHANNELS:
    channel = LLM_CHANNELS.split(",")[0].strip()
    base_url_var = f"LLM_{channel.upper()}_BASE_URL"
    api_key_var = f"LLM_{channel.upper()}_API_KEY"
    models_var = f"LLM_{channel.upper()}_MODELS"
    
    if not LLM_BASE_URL:
        LLM_BASE_URL = os.environ.get(base_url_var, "")
    if not LLM_API_KEY:
        LLM_API_KEY = os.environ.get(api_key_var, "")
    if os.environ.get(models_var):
        LLM_MODEL = os.environ.get(models_var).split(",")[0].strip()
    else:
        LLM_MODEL = "openai/agnes-2.0-flash"
else:
    LLM_MODEL = os.environ.get("LITELLM_MODEL", "openai/agnes-2.0-flash")

if not LLM_BASE_URL:
    proxy_base = os.environ.get("LLM_MY_PROXY_BASE_URL", "")
    if proxy_base:
        LLM_BASE_URL = proxy_base
        LLM_API_KEY = os.environ.get("LLM_MY_PROXY_API_KEY", LLM_API_KEY)
        LLM_MODEL = os.environ.get("LLM_MY_PROXY_MODELS", LLM_MODEL)
    else:
        LLM_BASE_URL = "https://api.openai.com/v1"

log.info("LLM 配置: BASE_URL=%s, MODEL=%s", LLM_BASE_URL, LLM_MODEL)

# ---------- 19 个 Skills 及其研究框架 ----------

def load_skills_config():
    """从 skills/prompts/ 目录加载 19 个 Skill 的 prompt 配置。"""
    prompts_dir = Path(__file__).resolve().parent.parent / 'skills' / 'prompts'
    config = {}
    
    if not prompts_dir.is_dir():
        log.warning('Skills prompts 目录不存在: %s，使用空配置', prompts_dir)
        return config
    
    for yaml_file in sorted(prompts_dir.glob('*.yaml')):
        key = yaml_file.stem
        name = key
        prompt_text = ''
        
        # 简单解析 YAML（不需要 pyyaml）
        with open(yaml_file, 'r', encoding='utf-8') as f:
            in_prompt = False
            for line in f:
                stripped = line.rstrip()
                if stripped.startswith('key: '):
                    key = stripped[5:].strip()
                elif stripped.startswith('name: '):
                    name = stripped[6:].strip()
                elif stripped.startswith('prompt: |'):
                    in_prompt = True
                    continue
                elif in_prompt:
                    if stripped.startswith('  '):
                        prompt_text += stripped[2:] + '\n'
                    else:
                        in_prompt = False
        
        if prompt_text:
            config[key] = {
                'name': name,
                'prompt': prompt_text.strip(),
            }
    
    log.info('加载了 %d 个 Skills prompt 文件', len(config))
    return config


SKILLS_CONFIG = load_skills_config()



def call_skill_llm(stock_code: str, stock_name: str, skill_key: str) -> dict:
    """调用 LLM 执行单个 Skill 的评估。"""
    config = SKILLS_CONFIG[skill_key]
    skill_name = config["name"]
    
    # 替换模板变量
    prompt = config["prompt"].format(code=stock_code, name=stock_name)
    
    try:
        from openai import OpenAI
        
        client = OpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY)
        
        response = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": "你是一个专业的A股投研分析师。请严格按要求的JSON格式输出评估结果，不要输出其他内容。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
            max_tokens=500,
        )
        
        content = response.choices[0].message.content.strip()
        
        # 提取 JSON
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0].strip()
        elif "```" in content:
            content = content.split("```")[1].split("```")[0].strip()
        
        result = json.loads(content)
        return {
            "stock_code": stock_code,
            "skill_executed": skill_key,
            "skill_name": skill_name,
            "score": int(result.get("score", 50)),
            "judgment": result.get("judgment", "暂无评估"),
            "core_risk": result.get("core_risk", "暂无风险"),
        }
        
    except Exception as e:
        log.warning("Skill [%s] 调用失败 (%s): %s", skill_name, stock_code, e)
        return {
            "stock_code": stock_code,
            "skill_executed": skill_key,
            "skill_name": skill_name,
            "score": 50,
            "judgment": f"调用失败: {str(e)[:50]}",
            "core_risk": "LLM调用异常",
        }


def evaluate_single_stock(stock_code: str) -> dict:
    """对单支股票运行全部 19 个 Skills。"""
    stock_name = STOCK_NAME_MAP.get(stock_code, stock_code)
    log.info("  [%s] %s 开始评估 19 个 Skills...", stock_code, stock_name)
    
    skill_results = []
    total_score = 0
    failed_count = 0
    
    for idx, (key, config) in enumerate(SKILLS_CONFIG.items(), 1):
        skill_name = config["name"]
        log.info("    [%d/%d] %s...", idx, len(SKILLS_CONFIG), skill_name)
        
        result = call_skill_llm(stock_code, stock_name, key)
        skill_results.append(result)
        total_score += result["score"]
        
        if result["score"] == 50 and "调用失败" in result["judgment"]:
            failed_count += 1
        
        # 每次调用间隔 2 秒，避免触发频率限制
        time.sleep(2)
    
    avg_score = round(total_score / len(SKILLS_CONFIG))
    
    sorted_skills = sorted(skill_results, key=lambda x: x["score"], reverse=True)
    highlights = [f"{s['skill_name']}（{s['score']}分）" for s in sorted_skills[:3]]
    risks = list(set(s["core_risk"] for s in skill_results if s["core_risk"] != "LLM调用异常"))
    
    return {
        "stock_code": stock_code,
        "stock_name": stock_name,
        "final_score": avg_score,
        "skill_results": skill_results,
        "highlights": highlights,
        "risks": risks,
        "llm_failed_count": failed_count,
    }


# ---------- 股票名称映射 ----------
STOCK_NAME_MAP = {
    "601288": "农业银行", "601398": "工商银行", "601166": "兴业银行",
    "601328": "交通银行", "601211": "国泰海通", "601688": "华泰证券",
    "601601": "中国太保", "600036": "招商银行", "601318": "中国平安",
    "601988": "中国银行", "600519": "贵州茅台", "000858": "五粮液",
    "000001": "平安银行", "002415": "海康威视", "600900": "长江电力",
    "000333": "美的集团", "601888": "中国中免", "600276": "恒瑞医药",
}


def run_evaluation(stocks: list) -> list:
    """对股票列表逐一运行 Skills。"""
    log.info("=" * 60)
    log.info("19 Skills 深度评估开始  |  %s", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    log.info("LLM 模型: %s", LLM_MODEL)
    log.info("=" * 60)
    log.info("股票数量: %d, Skills: %d, 总调用: %d", len(stocks), len(SKILLS_CONFIG), len(stocks) * len(SKILLS_CONFIG))
    log.info("预计耗时: 约 %d 分钟", len(stocks) * len(SKILLS_CONFIG) * 2 // 60 + 1)
    log.info("")
    
    all_results = []
    for idx, stock_code in enumerate(stocks, 1):
        log.info("--- 第 %d/%d 支 ---", idx, len(stocks))
        result = evaluate_single_stock(stock_code)
        all_results.append(result)
    
    log.info("")
    log.info("=" * 60)
    log.info("评估汇总:")
    for r in all_results:
        failed = r.get("llm_failed_count", 0)
        status = " (部分Skill失败)" if failed > 0 else ""
        log.info("  %s (%s)  →  综合得分: %d%s", r["stock_code"], r["stock_name"], r["final_score"], status)
    log.info("=" * 60)
    
    return all_results


def save_results(results: list, output_file: str) -> None:
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    log.info("结果已保存到: %s", output_file)


def main():
    parser = argparse.ArgumentParser(
        description="对股票列表运行 19 个 Skills 深度评估（调用真实 LLM）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
示例:
  python run_skills_evaluation.py --stocks '[\"600519\",\"000858\"]'

注意:
  每支股票调用 19 次 LLM（每个 Skill 一次），每次间隔 2 秒。
  2 支股票共 38 次调用，预计耗时约 2 分钟。
        """,
    )
    parser.add_argument("--stocks", required=True, help="股票列表 JSON 字符串")
    parser.add_argument("--output", default="analysis_result.json", help="输出文件路径")
    args = parser.parse_args()
    
    try:
        stocks = json.loads(args.stocks)
    except json.JSONDecodeError as e:
        log.error("JSON 解析失败: %s", e)
        sys.exit(1)
    
    if not stocks:
        log.error("股票列表为空")
        sys.exit(1)
    
    results = run_evaluation(stocks)
    save_results(results, args.output)
    log.info("全部完成！")


if __name__ == "__main__":
    main()