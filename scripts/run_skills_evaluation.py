# ============================================================
# run_skills_evaluation.py
# Skills 深度评估脚本：通过 LLM API 调用 19 个投研 Skills
# 每个 Skill 对应一个研究框架 prompt，调用 LLM 生成结构化评估
# 最终整合生成每支股票的详细分析报告
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

# ---------- 接入全局限速（llm_rate_limit.py 与本脚本一起部署到 B/C）----------
# 必须在 .env 加载之后导入：llm_rate_limit 在导入时读取 LLM_RPM_LIMIT 等变量。
sys.path.insert(0, str(Path(__file__).resolve().parent))
from llm_rate_limit import (  # noqa: E402 - 依赖上面的 .env 加载，故意延后
    call_with_retry,
    describe_stats,
    is_rate_limit_error,
)

# 合并调用未返回 report 时，是否允许再补一次 LLM 报告调用。
# 默认关闭：免费档速率约 1 次/分钟，补报告会让「每股 1 次」退化成 2 次，
# 因此默认改用本地模板报告（0 次额外调用）。确需模型版报告时设 LLM_REPORT_FALLBACK=1。
ENABLE_LLM_REPORT_FALLBACK = os.environ.get("LLM_REPORT_FALLBACK", "").strip().lower() in (
    "1",
    "true",
    "yes",
)

# ---------- 从 .env 读取 LLM 配置 ----------
# 优先检查 LLM_CHANNELS 渠道配置
LLM_CHANNELS = os.environ.get("LLM_CHANNELS", "")
LLM_BASE_URL = ""
LLM_API_KEY = ""
LLM_MODEL = "agnes-3.0-flash"

if LLM_CHANNELS:
    channel = LLM_CHANNELS.split(",")[0].strip()
    base_url_var = f"LLM_{channel.upper()}_BASE_URL"
    api_key_var = f"LLM_{channel.upper()}_API_KEY"
    models_var = f"LLM_{channel.upper()}_MODELS"
    
    LLM_BASE_URL = os.environ.get(base_url_var, "")
    LLM_API_KEY = os.environ.get(api_key_var, "")
    if os.environ.get(models_var):
        LLM_MODEL = os.environ.get(models_var).split(",")[0].strip()
else:
    # 检查 LLM_MY_PROXY_* 渠道
    proxy_base = os.environ.get("LLM_MY_PROXY_BASE_URL", "")
    if proxy_base:
        LLM_BASE_URL = proxy_base
        LLM_API_KEY = os.environ.get("LLM_MY_PROXY_API_KEY", "")
        LLM_MODEL = os.environ.get("LLM_MY_PROXY_MODELS", "agnes-3.0-flash")
    
    # 其次检查 LLM_BASE_URL / LLM_API_KEY
    if not LLM_BASE_URL:
        LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "")
    if not LLM_API_KEY:
        LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
    
    # 最后 fallback 到 OPENAI_*
    if not LLM_API_KEY:
        LLM_API_KEY = os.environ.get("OPENAI_API_KEY", "")
    if not LLM_BASE_URL:
        LLM_BASE_URL = os.environ.get("LLM_BASE_URL") or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
    
    LLM_MODEL = os.environ.get("LITELLM_MODEL", LLM_MODEL)
    # 模型名允许通过环境变量 LLM_MODEL 覆盖（空值回退默认）
    _env_model = os.environ.get("LLM_MODEL", "").strip()
    if _env_model:
        LLM_MODEL = _env_model

FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK", "")

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

# 一次评估里 LLM 调用失败（或没返回）的占比超过该阈值，就认为这次评分不可信。
#
# 背景：失败的 skill 会被记 50 分并计入分母，把整体均分往下拖；若把这种
# "数据不足"的分数当成真实低分写进历史，主节点下次初选会反过来给这支股票
# 减分（bonus = (50 - 70) * 0.6 = -12），与"数据不足"的语义完全相反。
# 因此不可信的评分会被标记 reliable=false，主节点加权时直接跳过。
MAX_UNRELIABLE_RATIO = 0.3


def _is_evaluation_reliable(skill_total: int, skill_returned: int, llm_failed_count: int) -> bool:
    """判断一次深度评分是否可信（供主节点决定要不要拿它做历史加权）。

    任一命中即判为不可信：
      1. 实际返回的 skill 条数缺口超过 30% —— 合并调用被截断时 total_score 只累加了
         返回的部分，却仍按总数取平均，分数被系统性低估；
      2. LLM 调用失败的 skill 占比超过 30% —— 失败项一律记 50 分，会把均分往下拖。
    """
    if skill_total <= 0:
        return False
    if (skill_total - skill_returned) > skill_total * MAX_UNRELIABLE_RATIO:
        return False
    if llm_failed_count > skill_total * MAX_UNRELIABLE_RATIO:
        return False
    return True


def _failed_skill_result(stock_code: str, skill_key: str, reason: str) -> dict:
    """构造一条「失败」占位结果（score=50）。

    reason 必须以「调用失败」开头，才能被 _count_failed / 可靠性判定识别。
    """
    return {
        "stock_code": stock_code,
        "skill_executed": skill_key,
        "skill_name": SKILLS_CONFIG.get(skill_key, {}).get("name", skill_key),
        "score": 50,
        "judgment": reason,
        "core_risk": "LLM调用异常",
    }


def _count_failed(skill_results: list) -> int:
    """统计「调用失败」的 Skill 数（与历史口径一致：score=50 且 judgment 含『调用失败』）。"""
    return sum(
        1 for r in skill_results
        if r.get("score") == 50 and "调用失败" in r.get("judgment", "")
    )


def call_skill_llm(stock_code: str, stock_name: str, skill_key: str) -> dict:
    """调用 LLM 执行单个 Skill 的评估。

    ⚠️ 限流（429）重试耗尽后会**向上抛出**，由调用方（19 次兜底循环）决定是否中止；
    其他异常（如 JSON 解析失败）仍按原逻辑降级为 50 分占位结果。
    """
    config = SKILLS_CONFIG[skill_key]
    skill_name = config["name"]
    
    # 替换模板变量
    prompt = config["prompt"].format(code=stock_code, name=stock_name)
    
    try:
        from openai import OpenAI
        
        client = OpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY)

        def _request():
            return client.chat.completions.create(
                model=LLM_MODEL,
                messages=[
                    {"role": "system", "content": "你是一个专业的A股投研分析师。请严格按要求的JSON格式输出评估结果，不要输出其他内容。"},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
                max_tokens=500,
            )

        response = call_with_retry(_request, label="单Skill")
        
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
        # 限流异常必须向上抛出：调用方据此立即中止 19 次兜底，避免放大 429。
        if is_rate_limit_error(e):
            raise
        log.warning("Skill [%s] 调用失败 (%s): %s", skill_name, stock_code, e)
        return {
            "stock_code": stock_code,
            "skill_executed": skill_key,
            "skill_name": skill_name,
            "score": 50,
            "judgment": f"调用失败: {str(e)[:50]}",
            "core_risk": "LLM调用异常",
        }


def call_all_skills_llm(stock_code: str, stock_name: str) -> tuple:
    """一次 LLM 请求同时拿到全部 19 个 Skill 结果与详细报告。

    Free tier 优化：把原来的「1 次合并 Skills + 1 次详细报告」压成 **1 次调用**，
    返回 (skill_results, report)：
      - skill_results：与 call_skill_llm 兼容的结果列表（漏掉的 Skill 用默认值补全）
      - report：详细报告 dict（与 generate_detailed_report 兼容）；模型未返回则 None

    失败语义（关键，防止 429 被放大）：
      - **限流（429）重试耗尽 → 向上抛出**，调用方绝不能改发 19 次单独请求；
      - 其他失败（如 JSON 解析不了）→ 返回 ([], None)，调用方 fallback 到 19 次。

    输出 token 配额设计：19 个 Skill（judgment ≤80 字 / core_risk ≤40 字）
    约 3,800 token，加详细报告约 2,000 token，max_tokens 设 8192 留足余量。
    """
    skill_keys = list(SKILLS_CONFIG.keys())

    # 构建每个 Skill 的独立 prompt（替换模板变量）
    skill_prompts = []
    for key in skill_keys:
        cfg = SKILLS_CONFIG[key]
        prompt_text = cfg["prompt"].format(code=stock_code, name=stock_name)
        skill_prompts.append((key, cfg["name"], prompt_text))

    # 拼接所有 Skill 的 prompt，并明确要求 LLM 输出 JSON 对象
    sections = []
    for idx, (key, name, prompt_text) in enumerate(skill_prompts, 1):
        sections.append(f"--- Skill {idx}: {name} (key: {key}) ---\n{prompt_text}")
    all_prompts = "\n\n".join(sections)

    output_format = f"""请对以上每个 Skill 分别评估，并据此撰写一份详细投资分析报告，一次性输出如下 JSON 对象：
{{
  "skills": [
    {{
      "skill_key": "key值",
      "skill_name": "Skill名称",
      "score": 0-100的整数,
      "judgment": "80字以内的判断说明",
      "core_risk": "40字以内的核心风险"
    }}
  ],
  "report": {{
    "stock_code": "{stock_code}",
    "stock_name": "{stock_name}",
    "overall_score": 0-100,
    "recommendation": "买入/增持/持有/减持/卖出",
    "summary": "投资摘要，100字以内",
    "sections": {{
      "company_overview": "公司概况与投资摘要",
      "core_competitiveness": "核心竞争力分析",
      "financial_health": "财务健康度",
      "industry_policy": "行业与政策环境",
      "management_governance": "管理层与公司治理",
      "risk_warnings": "风险提示（按严重程度排序）",
      "valuation_advice": "估值与操作建议"
    }},
    "skill_scores": {{
      "avg_score": 平均分,
      "highest_score": {{"skill": "名称", "score": 分数}},
      "lowest_score": {{"skill": "名称", "score": 分数}},
      "failed_count": 失败数量
    }}
  }}
}}
要求：
1. skills 必须覆盖全部 {len(skill_keys)} 个 Skill；
2. report.sections 各小节都要有实质内容（100~300字），不要编造不存在的数据；
3. 严格输出该 JSON 对象，不要输出任何其他文字。"""

    full_prompt = f"""股票信息：{stock_name}（{stock_code}）

请依次评估以下 {len(skill_prompts)} 个投研 Skill，每个 Skill 有独立的研究框架 prompt：

{all_prompts}

{output_format}"""

    try:
        from openai import OpenAI
        client = OpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY)

        def _request():
            return client.chat.completions.create(
                model=LLM_MODEL,
                messages=[
                    {"role": "system", "content": "你是专业的A股投研分析师。请按 JSON 对象格式一次性输出 skills 数组与 report 报告，不要输出其他内容。"},
                    {"role": "user", "content": full_prompt},
                ],
                temperature=0.3,
                max_tokens=8192,
            )

        response = call_with_retry(_request, label="合并Skills")

        raw = response.choices[0].message.content.strip()

        # 提取 JSON
        if "```json" in raw:
            raw = raw.split("```json")[1].split("```")[0].strip()
        elif "```" in raw:
            raw = raw.split("```")[1].split("```")[0].strip()

        parsed = json.loads(raw)
        # 兼容两种返回：新格式 {"skills": [...], "report": {...}}；旧格式裸数组
        if isinstance(parsed, list):
            results, report = parsed, None
        elif isinstance(parsed, dict):
            results = parsed.get("skills", [])
            report = parsed.get("report")
            if not isinstance(report, dict):
                report = None
        else:
            raise ValueError(f"LLM 返回的不是对象或数组: {type(parsed)}")

        if not isinstance(results, list):
            raise ValueError(f"skills 字段不是数组: {type(results)}")

        # 验证并补全每个 Skill 元素
        valid_results = []
        for item in results:
            if not isinstance(item, dict):
                continue
            key = item.get("skill_key", "")
            if key not in SKILLS_CONFIG:
                continue
            valid_results.append({
                "stock_code": stock_code,
                "skill_executed": key,
                "skill_name": item.get("skill_name", SKILLS_CONFIG[key]["name"]),
                "score": int(item.get("score", 50)),
                "judgment": item.get("judgment", "暂无评估"),
                "core_risk": item.get("core_risk", "暂无风险"),
            })

        log.info(
            "合并调用成功：返回 %d/%d 个 Skill 结果，report %s",
            len(valid_results), len(skill_keys), "已包含" if report else "缺失",
        )

        # 如果 LLM 漏掉了某些 Skill，用默认值补全
        returned_keys = {r["skill_executed"] for r in valid_results}
        for key in skill_keys:
            if key not in returned_keys:
                valid_results.append({
                    "stock_code": stock_code,
                    "skill_executed": key,
                    "skill_name": SKILLS_CONFIG[key]["name"],
                    "score": 50,
                    "judgment": "LLM 未返回该 Skill 结果",
                    "core_risk": "LLM调用异常",
                })

        return valid_results, report

    except Exception as e:
        # 限流异常向上抛出：调用方据此跳过本次评估，**不得**改发 19 次单独请求。
        if is_rate_limit_error(e):
            raise
        log.warning("合并调用失败（非限流），将 fallback 到 19 次单独调用: %s", e)
        return [], None


def generate_detailed_report(stock_code: str, stock_name: str, skill_results: list) -> dict:
    """用一次 LLM 调用整合 19 个 Skill 的结果，生成详细分析报告。"""
    # 构建技能结果摘要
    summary_parts = []
    for r in skill_results:
        status = "OK" if r["score"] != 50 or "调用失败" not in r.get("judgment", "") else "FAILED"
        summary_parts.append(
            f"- {r['skill_name']}: {r['score']}分 | {r['judgment']} | 风险: {r['core_risk']} [{status}]"
        )
    skills_summary = "\n".join(summary_parts)
    successful_count = sum(1 for r in skill_results if r["score"] != 50 or "调用失败" not in r.get("judgment", ""))
    
    prompt = f"""请根据以下 19 个投研 Skill 的评估结果，为 {stock_name}（{stock_code}）生成一份详细的投资分析报告。

可用的 Skill 结果（共 {len(skill_results)} 个，{successful_count} 个成功）：
{skills_summary}

请生成一份结构化的投资分析报告，包含以下章节：

## 一、公司概况与投资摘要
- 公司基本信息
- 综合评分（加权平均）
- 一句话投资建议

## 二、核心竞争力分析
- 护城河类型与深度
- 行业地位
- 技术/品牌/成本优势

## 三、财务健康度
- 盈利能力（ROE/毛利率趋势）
- 现金流质量
- 资产负债结构

## 四、行业与政策环境
- 行业生命周期
- 政策导向
- 竞争格局

## 五、管理层与公司治理
- 管理层评价
- 股权结构
- 激励机制

## 六、风险提示
- 核心风险点（按严重程度排序）
- 黑天鹅预警

## 七、估值与操作建议
- 当前估值水平
- 目标价位区间
- 买入/持有/卖出建议

## 八、各 Skill 评估详情
- 列出所有 19 个 Skill 的评分和简要说明

输出格式为 JSON：
{{
    "stock_code": "{stock_code}",
    "stock_name": "{stock_name}",
    "overall_score": 0-100,
    "recommendation": "买入/增持/持有/减持/卖出",
    "summary": "投资摘要，100字以内",
    "sections": {{
        "company_overview": "公司概况与投资摘要",
        "core_competitiveness": "核心竞争力分析",
        "financial_health": "财务健康度",
        "industry_policy": "行业与政策环境",
        "management_governance": "管理层与公司治理",
        "risk_warnings": "风险提示",
        "valuation_advice": "估值与操作建议",
        "skill_details": "各 Skill 评估详情（JSON数组，每个元素包含 skill_name, score, judgment, core_risk）"
    }},
    "skill_scores": {{
        "avg_score": 平均分,
        "highest_score": {{"skill": "名称", "score": 分数}},
        "lowest_score": {{"skill": "名称", "score": 分数}},
        "failed_count": 失败数量
    }}
}}

注意：
1. 如果某些 Skill 调用失败（标记为 FAILED），请在报告中注明
2. 综合评分基于成功 Skill 的平均分
3. 报告要专业、客观，基于数据说话
4. 不要编造不存在的数据"""

    try:
        from openai import OpenAI
        
        client = OpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY)

        def _request():
            return client.chat.completions.create(
                model=LLM_MODEL,
                messages=[
                    {"role": "system", "content": "你是一位资深A股投研分析师，擅长撰写专业的投资研究报告。"},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.2,
                max_tokens=4000,
            )

        response = call_with_retry(_request, label="详细报告")
        
        content = response.choices[0].message.content.strip()
        
        # 提取 JSON
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0].strip()
        elif "```" in content:
            content = content.split("```")[1].split("```")[0].strip()
        
        report = json.loads(content)
        return report
        
    except Exception as e:
        log.error("生成详细报告失败: %s", e)
        return {
            "stock_code": stock_code,
            "stock_name": stock_name,
            "overall_score": 50,
            "recommendation": "数据不足，无法评估",
            "summary": f"详细报告生成失败: {str(e)[:100]}",
            "sections": {},
            "skill_scores": {
                "avg_score": 50,
                "highest_score": None,
                "lowest_score": None,
                "failed_count": len(skill_results) - successful_count
            }
        }


def _recommendation_for_score(score: int) -> str:
    """按综合评分给出中文投资建议（与 LLM 报告口径一致）。"""
    if score >= 80:
        return "买入"
    if score >= 70:
        return "增持"
    if score >= 60:
        return "持有"
    if score >= 50:
        return "减持"
    return "卖出"


def build_template_report(stock_code: str, stock_name: str, skill_results: list) -> dict:
    """本地用 Skill 结果生成模板版详细报告（**0 次额外 LLM 调用**）。

    合并调用拿到 skills 但 report 缺失（如模型截断）时使用，字段与
    generate_detailed_report 完全兼容，保证飞书卡片可正常渲染。
    """
    scores = [r.get("score", 50) for r in skill_results]
    avg_score = round(sum(scores) / len(scores)) if scores else 50
    failed_count = _count_failed(skill_results)
    recommendation = _recommendation_for_score(avg_score)

    ranked = sorted(skill_results, key=lambda r: r.get("score", 50), reverse=True)
    highest = (
        {"skill": ranked[0].get("skill_name", ""), "score": ranked[0].get("score", 50)}
        if ranked else None
    )
    lowest = (
        {"skill": ranked[-1].get("skill_name", ""), "score": ranked[-1].get("score", 50)}
        if ranked else None
    )

    # 各小节基于 Skill 结果做机械归纳，不编造数据。
    ok = [r for r in skill_results if not (r.get("score") == 50 and "调用失败" in r.get("judgment", ""))]
    ok_desc = "；".join(f"{r['skill_name']}（{r['score']}分）" for r in ranked[:5]) or "无"
    risk_desc = "；".join(
        dict.fromkeys(r.get("core_risk", "") for r in skill_results if r.get("core_risk") and r["core_risk"] != "LLM调用异常")
    ) or "暂无明确风险提示"
    skill_detail = "\n".join(
        f"- {r.get('skill_name', '')}：{r.get('score', 50)}分 | {r.get('judgment', '')} | 风险：{r.get('core_risk', '')}"
        for r in skill_results
    )

    sections = {
        "company_overview": (
            f"{stock_name}（{stock_code}）本次经 {len(skill_results)} 个投研维度评估，"
            f"综合评分 {avg_score} 分，投资建议：{recommendation}。"
            f"其中 {len(ok)} 个维度正常返回，{failed_count} 个维度调用失败。"
        ),
        "core_competitiveness": f"评分靠前的维度：{ok_desc}。",
        "financial_health": (
            "财务相关维度得分已并入综合评分；因本报告为自动归纳版，"
            "未展开逐项财务指标，请以各维度原始判断为准。"
        ),
        "industry_policy": (
            "行业与政策维度结论请参见上方各维度判断；"
            "本自动归纳版不额外引入未提供的数据。"
        ),
        "management_governance": (
            "管理层与治理维度结论请参见对应 Skill 的判断与风险提示。"
        ),
        "risk_warnings": risk_desc,
        "valuation_advice": f"综合评分 {avg_score} 分，建议：{recommendation}。",
        "skill_details": skill_detail,
    }

    return {
        "stock_code": stock_code,
        "stock_name": stock_name,
        "overall_score": avg_score,
        "recommendation": recommendation,
        "summary": (
            f"本报告为系统根据 {len(skill_results)} 个投研维度评分结果自动归纳的版本"
            f"（未额外调用模型）。综合评分 {avg_score} 分，建议：{recommendation}。"
        ),
        "sections": sections,
        "skill_scores": {
            "avg_score": avg_score,
            "highest_score": highest,
            "lowest_score": lowest,
            "failed_count": failed_count,
        },
    }


def _normalize_report(report: dict, stock_code: str, stock_name: str, skill_results: list) -> dict:
    """用模板报告的完整骨架兜底，补齐 LLM 报告可能缺失的字段。

    保证飞书卡片依赖的字段（summary / sections / skill_scores 等）永远存在，
    同时保留 LLM 给出的内容（非空值优先）。
    """
    base = build_template_report(stock_code, stock_name, skill_results)
    if not isinstance(report, dict):
        return base

    merged = dict(base)
    for key in ("overall_score", "recommendation", "summary"):
        if report.get(key):
            merged[key] = report[key]

    sections = dict(base["sections"])
    for key, value in (report.get("sections") or {}).items():
        if value:
            sections[key] = value
    merged["sections"] = sections

    skill_scores = dict(base["skill_scores"])
    for key, value in (report.get("skill_scores") or {}).items():
        if value is not None:
            skill_scores[key] = value
    merged["skill_scores"] = skill_scores
    return merged


def _fallback_single_skills(stock_code: str, stock_name: str) -> list:
    """非限流失败时的 19 次单独调用兜底。

    硬上限 = Skill 总数（最多 19 次）；任一次触发限流**立即中止**剩余调用，
    未完成的用失败占位补齐 —— 避免兜底路径本身变成 429 放大器。
    """
    skill_results = []
    aborted = False
    for idx, (key, config) in enumerate(SKILLS_CONFIG.items(), 1):
        if aborted:
            skill_results.append(_failed_skill_result(stock_code, key, "调用失败：兜底已中止"))
            continue

        log.info("    [%d/%d] %s...", idx, len(SKILLS_CONFIG), config["name"])
        try:
            result = call_skill_llm(stock_code, stock_name, key)
        except Exception as e:
            if is_rate_limit_error(e):
                log.warning(
                    "  [%s] 兜底单Skill调用触发限流，立即中止剩余 %d 个 Skill（避免放大 429）",
                    stock_code, len(SKILLS_CONFIG) - len(skill_results),
                )
                aborted = True
                skill_results.append(_failed_skill_result(stock_code, key, "调用失败：限流"))
                continue
            # 防御：call_skill_llm 理论上已吞掉非限流异常
            result = _failed_skill_result(stock_code, key, f"调用失败: {str(e)[:40]}")

        skill_results.append(result)
        time.sleep(5)
    return skill_results


def evaluate_single_stock(stock_code: str) -> dict:
    """对单支股票运行全部 19 个 Skills。

    正常情况下**只发生 1 次 LLM 调用**（合并调用同时返回 skills + report）。
    失败分流（防止 429 放大）：
      - 合并调用限流重试耗尽 → 跳过本次评估，**绝不**回退成 19 次单独请求；
      - 非限流失败（如 JSON 解析不了）→ 保留 19 次单独调用兜底（带硬上限）。
    """
    stock_name = STOCK_NAME_MAP.get(stock_code, stock_code)
    log.info("  [%s] %s 开始评估 19 个 Skills...", stock_code, stock_name)

    skill_results = []
    report = None
    rate_limited = False

    # 优先尝试合并调用（1 次 LLM 请求同时拿到 19 个 Skill 结果 + 详细报告）
    try:
        skill_results, report = call_all_skills_llm(stock_code, stock_name)
    except Exception as e:
        if is_rate_limit_error(e):
            rate_limited = True
            log.error(
                "  [%s] 合并调用限流重试耗尽，跳过本次评估"
                "（不触发 19 次兜底，避免把 429 放大成 19 个请求）", stock_code,
            )
        else:
            log.error("  [%s] 合并调用异常: %s", stock_code, e)
        skill_results, report = [], None

    if rate_limited:
        # 生成失败占位，保持输出结构完整；本次必然 reliable=false。
        skill_results = [
            _failed_skill_result(stock_code, key, "调用失败：限流，已跳过本次评估")
            for key in SKILLS_CONFIG
        ]
    elif not skill_results:
        # 非限流失败：保留 19 次单独调用兜底（硬上限 + 遇限流立即中止）。
        log.info("  [%s] 合并调用非限流失败，fallback 到 19 次单独调用...", stock_code)
        skill_results = _fallback_single_skills(stock_code, stock_name)

    total_score = sum(r["score"] for r in skill_results)
    failed_count = _count_failed(skill_results)
    avg_score = round(total_score / len(SKILLS_CONFIG)) if SKILLS_CONFIG else 50

    sorted_skills = sorted(skill_results, key=lambda x: x["score"], reverse=True)
    highlights = [f"{s['skill_name']}（{s['score']}分）" for s in sorted_skills[:3]]
    risks = list(set(s["core_risk"] for s in skill_results if s["core_risk"] != "LLM调用异常"))

    # 详细报告：合并调用已附带；缺失时默认本地模板生成（0 次额外 LLM 调用）。
    if report is None:
        if ENABLE_LLM_REPORT_FALLBACK and not rate_limited and skill_results:
            log.info("  [%s] report 缺失，按 LLM_REPORT_FALLBACK 补一次模型报告（走限速）...", stock_code)
            report = generate_detailed_report(stock_code, stock_name, skill_results)
        else:
            log.info("  [%s] report 缺失，改用本地模板报告（0 次额外 LLM 调用）", stock_code)
            report = build_template_report(stock_code, stock_name, skill_results)
    detailed_report = _normalize_report(report, stock_code, stock_name, skill_results)

    skill_total = len(SKILLS_CONFIG)
    skill_returned = len(skill_results)
    reliable = _is_evaluation_reliable(skill_total, skill_returned, failed_count)
    if not reliable:
        log.warning(
            "  [%s] 本次评分不可信（返回 %d/%d 个 Skill，失败 %d 个），"
            "标记 reliable=false，主节点不会用它做历史加权",
            stock_code, skill_returned, skill_total, failed_count,
        )

    return {
        "stock_code": stock_code,
        "stock_name": stock_name,
        "final_score": avg_score,
        "skill_results": skill_results,
        "highlights": highlights,
        "risks": risks,
        "llm_failed_count": failed_count,
        # 可靠性元数据：主节点据此跳过不可信的评分（见 _is_evaluation_reliable）
        "skill_total": skill_total,
        "skill_returned": skill_returned,
        "reliable": reliable,
        "detailed_report": detailed_report,
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
    log.info("Skills 数量: %d", len(SKILLS_CONFIG))
    log.info("=" * 60)
    log.info("股票数量: %d, Skills: %d, 总调用: 约 %d 次（每股合并为 1 次）", len(stocks), len(SKILLS_CONFIG), len(stocks))
    log.info("预计耗时: 约 %d 分钟（每股 1 次合并调用，含详细报告）", len(stocks))
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
    # 累计等待时间可见，便于判断免费配额是否够用
    log.info(describe_stats())
    
    return all_results


def save_results(results: list, output_file: str) -> None:
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    log.info("结果已保存到: %s", output_file)

    # 追加到历史评分文件（供主节点下次初选参考）
    base_dir = Path(__file__).resolve().parent.parent
    hist_file = base_dir / "data" / "skill_scores_history.json"
    try:
        hist = []
        if hist_file.exists():
            with open(hist_file, "r", encoding="utf-8") as f:
                hist = json.load(f)
        if not isinstance(hist, list):
            hist = []
        today = datetime.now().strftime("%Y-%m-%d")
        unreliable = [r for r in results if not r.get("reliable", True)]
        if unreliable:
            log.warning(
                "本次 %d/%d 支评分不可信，已标记 reliable=false（主节点加权时跳过）：%s",
                len(unreliable), len(results),
                ", ".join(str(r.get("stock_code", "?")) for r in unreliable),
            )
        entry = {
            "date": today,
            "results": [
                {
                    "stock_code": r["stock_code"],
                    "stock_name": r.get("stock_name", ""),
                    "final_score": r.get(
                        "final_score",
                        (r.get("detailed_report") or {}).get("overall_score", 0),
                    ),
                    # 可靠性元数据：主节点据此跳过不可信的评分
                    # （见 _is_evaluation_reliable 与 alphasift_discovery._load_history_scores）
                    "reliable": r.get("reliable", True),
                    "skill_total": r.get("skill_total", len(SKILLS_CONFIG)),
                    "skill_returned": r.get("skill_returned", 0),
                    "llm_failed_count": r.get("llm_failed_count", 0),
                }
                for r in results
            ],
        }
        hist.append(entry)
        hist_file.parent.mkdir(parents=True, exist_ok=True)
        with open(hist_file, "w", encoding="utf-8") as f:
            json.dump(hist, f, ensure_ascii=False, indent=2)
        log.info("历史评分已追加到: %s", hist_file)
    except Exception as e:
        log.warning("历史评分写入失败: %s", e)


def main():
    parser = argparse.ArgumentParser(
        description="对股票列表运行 19 个 Skills 深度评估（调用真实 LLM）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
示例:
  python run_skills_evaluation.py --stocks '[\"600519\",\"000858\"]'

注意:
  每支股票只调用 1 次 LLM（19 个 Skill 与详细报告合并为一次请求）。
  所有调用共享进程级令牌桶限速（LLM_RPM_LIMIT，默认 1 次/分钟）。
  合并调用限流重试耗尽时会跳过该股票，不会改发 19 次请求（避免放大 429）。
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
