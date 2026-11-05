# ============================================================
# feishu_notifier.py
# 飞书推送脚本：将分析结果推送到飞书群聊
# 环境变量从 .env 文件读取（兼容 LLM_CONFIG_GUIDE 变量名）
# ============================================================

import argparse
import json
import logging
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

try:
    import requests
except ImportError:
    requests = None

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
FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK", "")
FEISHU_WEBHOOK_B = os.environ.get("FEISHU_WEBHOOK_B", "")
FEISHU_WEBHOOK_C = os.environ.get("FEISHU_WEBHOOK_C", "")
FEISHU_WEBHOOK_MAIN = os.environ.get("FEISHU_WEBHOOK_MAIN") or FEISHU_WEBHOOK

log.info("FEISHU_WEBHOOK 已配置: %s", bool(FEISHU_WEBHOOK))
log.info("FEISHU_WEBHOOK_MAIN 已配置: %s", bool(FEISHU_WEBHOOK_MAIN))


# ---------- 中文显示映射与股票名称查询 ----------
# 飞书卡片面向中文用户，正文里尽量不出现英文。所有"英文键 → 中文名"的映射
# 集中放在这里，避免散落在拼接字符串里（散着放的结果就是漏掉几处）。

STOCK_NAME_MAP = {
    "601288": "农业银行", "601398": "工商银行", "601166": "兴业银行",
    "601328": "交通银行", "601211": "国泰海通", "601688": "华泰证券",
    "601601": "中国太保", "600036": "招商银行", "601318": "中国平安",
    "601988": "中国银行", "600519": "贵州茅台", "000858": "五粮液",
    "000001": "平安银行", "002415": "海康威视", "600900": "长江电力",
    "000333": "美的集团", "601888": "中国中免", "600276": "恒瑞医药",
    "000651": "格力电器", "600809": "山西汾酒", "300750": "宁德时代",
    "002714": "牧原股份", "601012": "隆基绿能", "601918": "新集能源",
    "601668": "中国建筑", "600028": "中国石化",
}


def _load_snapshot_names() -> dict[str, str]:
    """尝试从本地快照文件加载股票代码 -> 股票名称映射。"""
    names: dict[str, str] = {}
    candidates = [
        Path(__file__).resolve().parent.parent / "data" / "snapshot_latest.json",
        Path(__file__).resolve().parent.parent / "data" / "snapshot_cache.json",
    ]
    for p in candidates:
        if p.is_file():
            try:
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                stocks = data.get("stocks") if isinstance(data, dict) else data
                if isinstance(stocks, list):
                    for s in stocks:
                        if isinstance(s, dict) and s.get("code") and s.get("name"):
                            names[str(s["code"]).strip().zfill(6)] = str(s["name"]).strip()
                if names:
                    break
            except Exception:
                continue
    return names


def fetch_stock_names(codes: list[str]) -> dict[str, str]:
    """批量获取 A 股股票代码对应的真实中文名称（支持深市、沪市、北交所）。

    采用多级保障：
      1. 内存字典缓存（STOCK_NAME_MAP）；
      2. 本地快照文件；
      3. 腾讯行情接口批量查询（qt.gtimg.cn，速度快、免密、覆盖全市场）；
      4. 新浪行情接口备用（hq.sinajs.cn）；
      5. 兜底返回代码本身。
    """
    result: dict[str, str] = {}
    to_fetch: list[str] = []

    for raw_code in codes:
        c = str(raw_code).strip()
        if not c:
            continue
        code = c.zfill(6) if c.isdigit() and len(c) < 6 else c
        if code in STOCK_NAME_MAP:
            result[code] = STOCK_NAME_MAP[code]
        else:
            to_fetch.append(code)

    if not to_fetch:
        return result

    # 尝试从本地快照匹配
    snap_names = _load_snapshot_names()
    still_missing: list[str] = []
    for code in to_fetch:
        if code in snap_names:
            name = snap_names[code]
            STOCK_NAME_MAP[code] = name
            result[code] = name
        else:
            still_missing.append(code)

    if not still_missing:
        return result

    # 尝试从腾讯实时行情接口批量查询
    try:
        query_symbols = []
        for c in still_missing:
            prefix = "sz" if c.startswith(("0", "3")) else ("bj" if c.startswith(("4", "8", "9")) else "sh")
            query_symbols.append(f"{prefix}{c}")

        url = f"https://qt.gtimg.cn/q={','.join(query_symbols)}"
        req = urllib.request.Request(
            url,
            headers={
                "Referer": "https://stockapp.finance.qq.com",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            },
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            text = resp.read().decode("gbk", errors="ignore")
            for line in text.split(";"):
                parts = line.strip().split("~")
                if len(parts) > 2:
                    name = parts[1].strip()
                    code = parts[2].strip()
                    if name and code:
                        STOCK_NAME_MAP[code] = name
                        result[code] = name
    except Exception as e:
        log.warning("腾讯行情接口查询股票名称失败: %s，尝试备用接口", e)

    # 检查是否还有缺失的标的，尝试新浪接口
    sina_missing = [c for c in still_missing if c not in result]
    if sina_missing:
        try:
            query_symbols = []
            for c in sina_missing:
                prefix = "sz" if c.startswith(("0", "3")) else ("bj" if c.startswith(("4", "8", "9")) else "sh")
                query_symbols.append(f"{prefix}{c}")
            url = f"http://hq.sinajs.cn/list={','.join(query_symbols)}"
            req = urllib.request.Request(
                url,
                headers={
                    "Referer": "https://finance.sina.com.cn",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                },
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                text = resp.read().decode("gbk", errors="ignore")
                for line in text.strip().split("\n"):
                    if "=" not in line:
                        continue
                    left, right = line.split("=", 1)
                    sym = left.split("str_")[-1].strip()
                    code = sym[2:] if len(sym) > 2 else sym
                    content = right.strip('"; \r\n')
                    if content:
                        parts = content.split(",")
                        if parts and parts[0].strip():
                            name = parts[0].strip()
                            STOCK_NAME_MAP[code] = name
                            result[code] = name
        except Exception as e:
            log.warning("新浪行情接口查询股票名称失败: %s", e)

    # 兜底：仍然无法获取的，回退为代码本身
    for code in still_missing:
        if code not in result:
            result[code] = code

    return result


def get_stock_name(stock_code: str) -> str:
    """获取单个股票代码的中文名称。"""
    c = str(stock_code).strip()
    code = c.zfill(6) if c.isdigit() and len(c) < 6 else c
    if code in STOCK_NAME_MAP:
        return STOCK_NAME_MAP[code]
    names = fetch_stock_names([code])
    return names.get(code, code)


def _format_stock_tokens_with_names(stocks_csv_or_list: str) -> str:
    """把逗号或空格分隔的股票代码串转成带中文名称的字符串，如 '新集能源（601918）、中国建筑（601668）'。"""
    if not stocks_csv_or_list:
        return ""
    raw_tokens = [s.strip() for s in stocks_csv_or_list.replace("，", ",").split(",") if s.strip()]
    if not raw_tokens:
        return stocks_csv_or_list
    names_map = fetch_stock_names(raw_tokens)
    formatted = []
    for c in raw_tokens:
        nm = names_map.get(c)
        if nm and nm != c:
            formatted.append(f"{nm}（{c}）")
        else:
            formatted.append(c)
    return "、".join(formatted)

# 详细报告各小节标题。与 run_skills_evaluation.py 里给 LLM 的 sections 定义一致，
# 此前是用 section_key.replace("_", " ").title() 直接生成的（Company Overview 之类）。
SECTION_TITLES = {
    "company_overview": "公司概况与投资摘要",
    "core_competitiveness": "核心竞争力分析",
    "financial_health": "财务健康度",
    "industry_policy": "行业与政策环境",
    "management_governance": "管理层与公司治理",
    "risk_warnings": "风险提示",
    "valuation_advice": "估值与操作建议",
    "skill_details": "各维度评估详情",
}

# 评分档位。此前直接打印 green / yellow / red 三个英文色名。
SCORE_GRADES = ((80, "🟢 优选"), (70, "🔵 良好"), (60, "🟡 观察"), (0, "🔴 谨慎"))


def _score_grade(score) -> str:
    """把分数映射成中文档位标签，无法解析时返回占位符。"""
    try:
        value = float(score)
    except (TypeError, ValueError):
        return "—"
    for threshold, label in SCORE_GRADES:
        if value >= threshold:
            return label
    return "—"


def _render_progress_bar(score, width: int = 10) -> str:
    """生成紧凑的字符进度条，如 [████████░░]。"""
    try:
        val = max(0.0, min(100.0, float(score)))
    except (TypeError, ValueError):
        return "—"
    filled = int(round(val / 100.0 * width))
    return "[" + "█" * filled + "░" * (width - filled) + "]"


def _load_skill_names() -> dict:
    """读取 skills/prompts/*.yaml 的 key -> 中文 name 映射。

    这些 YAML 的 name 字段本身就是中文（如 bottleneck-hunter -> 瓶颈猎手），
    直接复用即可，不必另建一份会漂移的对照表。目录不存在（例如在精简过的
    从节点上运行）时返回空映射，调用方会退回原样显示。
    """
    prompts_dir = Path(__file__).resolve().parent.parent / "skills" / "prompts"
    names: dict[str, str] = {}
    if not prompts_dir.is_dir():
        return names
    for yaml_file in sorted(prompts_dir.glob("*.yaml")):
        key = yaml_file.stem
        display = ""
        try:
            with open(yaml_file, "r", encoding="utf-8") as fh:
                for line in fh:
                    stripped = line.rstrip()
                    if stripped.startswith("key: "):
                        key = stripped[5:].strip()
                    elif stripped.startswith("name: "):
                        display = stripped[6:].strip()
                        break
        except OSError:
            continue
        if display:
            names[key] = display
    return names


SKILL_NAMES = _load_skill_names()


def _skill_display_name(raw) -> str:
    """把 skill 键名翻成中文；已是中文或未知键名则原样返回。"""
    text = str(raw or "").strip()
    if not text:
        return "未知"
    return SKILL_NAMES.get(text, text)


def _section_title(key: str) -> str:
    """小节键名 → 中文标题；未知键名做兜底处理，不直接暴露下划线英文。"""
    if key in SECTION_TITLES:
        return SECTION_TITLES[key]
    return key.replace("_", " ").strip() or key


def _text_or(value, placeholder: str = "暂无"):
    """None / 空串显示占位符。

    刻意不用 `value or placeholder` —— 0 和 False 是合法取值（比如失败数 0、
    得分 0），被 `or` 吞掉会显示成"暂无"，属于静默失真。
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return placeholder
    return value


def build_card_message(results: list) -> dict:
    """构建飞书富文本卡片消息，包含详细报告。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    stock_count = len(results)

    # 预处理：确保所有结果中的 stock_name 为真实中文股票名称
    need_fetch = [
        str(r.get("stock_code", "")).strip() for r in results
        if not r.get("stock_name") or str(r.get("stock_name")).strip() == str(r.get("stock_code")).strip()
    ]
    if need_fetch:
        fetched = fetch_stock_names(need_fetch)
        for r in results:
            code = str(r.get("stock_code", "")).strip()
            name = str(r.get("stock_name", "")).strip()
            if not name or name == code:
                r["stock_name"] = fetched.get(code, code)

    card = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": f"\U0001f916 AI 投研多源流矩阵诊断报告（{now}）",
                },
                "template": "violet",
            },
            "elements": [],
        },
    }

    elements = card["card"]["elements"]

    # 摘要块：显示格式为 股票名（股票代码）
    summary_parts = []
    for r in results:
        code = str(r.get("stock_code", "")).strip()
        name = str(r.get("stock_name", "")).strip()
        if name and name != code:
            summary_parts.append(f"{name}（{code}）")
        else:
            summary_parts.append(code)
    stock_summary_text = "、".join(summary_parts)

    elements.append({
        "tag": "div",
        "text": {
            "tag": "lark_md",
            "content": f"本次股票分析共 {stock_count} 支：{stock_summary_text}",
        },
    })

    # 评分梯队直观对比看板
    sorted_res = sorted(results, key=lambda x: float(x.get("final_score", 0)), reverse=True)
    ladder_lines = ["**📊 候选标的评分梯队直观总览：**"]
    medals = ["🥇", "🥈", "🥉"]
    for idx, r in enumerate(sorted_res, start=1):
        icon = medals[idx - 1] if idx <= 3 else "  "
        code = str(r.get("stock_code", "")).strip()
        name = str(r.get("stock_name", "")).strip() or code
        score = r.get("final_score", 0)
        grade = _score_grade(score)
        bar = _render_progress_bar(score, width=8)
        ladder_lines.append(f"{icon} `{code}` **{name:<4}** {bar} **{score:>2}**分 {grade}")

    elements.append({
        "tag": "div",
        "text": {
            "tag": "lark_md",
            "content": "\n".join(ladder_lines),
        },
    })
    elements.append({"tag": "hr"})

    for r in results:
        stock_code = str(r["stock_code"]).strip()
        stock_name = str(r.get("stock_name", "")).strip() or get_stock_name(stock_code)
        final_score = r["final_score"]
        highlights = r.get("highlights", [])
        risks = r.get("risks", [])
        failed = r.get("llm_failed_count", 0)
        
        # 评分档位与直观进度条
        grade = _score_grade(final_score)
        bar = _render_progress_bar(final_score, width=10)

        # 基础信息块（标的标题带股票名与股票代码）
        header_title = f"{stock_name}（{stock_code}）" if stock_name and stock_name != stock_code else stock_code
        basic_content = (
            f"**{header_title}**\n"
            f"综合评分：**{final_score}** 分 {bar}　{grade}\n"
        )
        if failed > 0:
            basic_content += f"*（{failed} 个维度调用失败）*\n"
        basic_content += "\n**亮点维度：**\n" + "\n".join(f"- {h}" for h in highlights) + "\n"
        basic_content += "\n**核心风险：**\n" + "\n".join(f"- {risk}" for risk in risks)

        elements.append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": basic_content,
            },
        })

        # 详细报告块（如果有）
        detailed = r.get("detailed_report")
        if detailed and detailed.get("summary"):
            report_content = "\n\n---\n\n"
            report_title = f"{stock_name}（{stock_code}）" if stock_name and stock_name != stock_code else stock_name
            report_content += f"**\U0001f4d8 详细分析报告 - {report_title}**\n\n"
            report_content += f"**投资建议：** {_text_or(detailed.get('recommendation'))}\n"
            report_content += f"**综合评分：** {_text_or(detailed.get('overall_score'))}/100\n\n"
            report_content += f"**摘要：** {detailed['summary']}\n\n"
            
            sections = detailed.get("sections", {})
            for section_key in ["company_overview", "core_competitiveness", "financial_health", 
                                "industry_policy", "management_governance", "risk_warnings", 
                                "valuation_advice"]:
                section_text = sections.get(section_key, "")
                if section_text and section_text != "{}":
                    # 截取前 500 字符避免超长
                    display_text = str(section_text)[:500]
                    report_content += "**" + _section_title(section_key) + "**\n" + display_text + "\n\n"

            # 维度评分详情
            skill_scores = detailed.get("skill_scores", {})
            if skill_scores:
                report_content += "**各维度评分统计：**\n"
                report_content += f"- 平均分：{_text_or(skill_scores.get('avg_score'))}\n"
                highest = skill_scores.get("highest_score")
                if highest:
                    report_content += (
                        f"- 最高分：{_skill_display_name(highest.get('skill'))}"
                        f"（{_text_or(highest.get('score'))} 分）\n"
                    )
                lowest = skill_scores.get("lowest_score")
                if lowest:
                    report_content += (
                        f"- 最低分：{_skill_display_name(lowest.get('skill'))}"
                        f"（{_text_or(lowest.get('score'))} 分）\n"
                    )
                report_content += f"- 失败项：{skill_scores.get('failed_count', 0)}\n"
            
            elements.append({
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": report_content,
                },
            })

        elements.append({"tag": "hr"})

    elements.append({
        "tag": "note",
        "elements": [
            {
                "tag": "plain_text",
                "content": "数据基于 AlphaSift 初选 + 19 个深度投研维度打分流程",
            }
        ],
    })

    return card


def _truncate_text(text, limit: int = 50) -> str:
    """把文本截断到 limit 个字符，超出部分以省略号收尾。"""
    value = str(text or "").strip()
    if len(value) > limit:
        return value[:limit] + "…"
    return value


def _format_change_pct(value) -> str:
    """涨跌幅保留两位小数并带百分号；无法解析时显示占位符。"""
    try:
        return f"{float(value):.2f}%"
    except (TypeError, ValueError):
        return "暂无"


def _build_theme_block(theme_summary) -> str:
    """把题材总结数据渲染成飞书卡片里的一段 markdown 文案。

    返回空串表示不渲染题材块（数据缺失或题材为空），调用方据此跳过整个 div，
    避免出现空壳块。文案全部使用中文。
    """
    if not isinstance(theme_summary, dict):
        return ""

    themes = theme_summary.get("themes") or []
    if not themes:
        return ""

    lines = ["**\U0001f525 今日短线热点题材**"]

    summary = str(theme_summary.get("summary") or "").strip()
    if summary:
        lines.append(summary)

    for idx, theme in enumerate(themes[:3], start=1):
        name = str(theme.get("name") or "未知").strip()
        leaders = [str(x).strip() for x in (theme.get("leaders") or []) if str(x).strip()]
        leaders_text = "、".join(leaders) if leaders else "暂无"
        logic_text = _truncate_text(theme.get("logic"), 50)

        lines.append(f"**{idx}. {name}**")
        lines.append(f"板块均涨：{_format_change_pct(theme.get('change_pct'))}")
        lines.append(f"代表股：{leaders_text}")
        lines.append(f"逻辑：{logic_text}")

    # 降级说明：非模型分析时必须让用户知道逻辑只是盘面数据复述，避免误导。
    if not theme_summary.get("llm_used", False):
        lines.append("（逻辑为盘面数据自动归纳，非模型分析）")

    return "\n".join(lines)


def build_main_card(
    stocks: str,
    first5: str,
    last5: str,
    code_b: str,
    code_c: str,
    theme_summary: dict | None = None,
) -> dict:
    """构建主节点通知卡片（初选结果 + 题材总结 + 触发状态）。

    版式顺序固定为：初选清单 div → 题材 div（可选）→ 触发状态 div。
    题材块必须独立成块，位置由产品评审确定，不要自行调整。
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    status_b = "成功" if code_b in ("201", "204") else f"失败（{code_b}）"
    status_c = "成功" if code_c in ("201", "204") else f"失败（{code_c}）"

    stocks_fmt = _format_stock_tokens_with_names(stocks)
    first5_fmt = _format_stock_tokens_with_names(first5)
    last5_fmt = _format_stock_tokens_with_names(last5)

    elements = [
        # 第一段：初选清单
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": (
                    f"本次股票初选共 {len(stocks.split(','))} 支\n"
                    f"**初选股票：** {stocks_fmt or stocks}\n"
                    f"\n"
                    f"**前 5 支 → 账户 B：** {first5_fmt or first5}\n"
                    f"**后 5 支 → 账户 C：** {last5_fmt or last5}"
                ),
            },
        },
    ]

    # 第二段：题材总结（无数据时整块跳过，卡片退回两段式结构）
    theme_content = _build_theme_block(theme_summary)
    if theme_content:
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": theme_content},
        })

    # 第三段：触发状态
    elements.append({
        "tag": "div",
        "text": {
            "tag": "lark_md",
            "content": (
                f"**触发状态：**\n"
                f"- 账户 B：{status_b}\n"
                f"- 账户 C：{status_c}"
            ),
        },
    })

    elements.append({"tag": "hr"})
    elements.append({
        "tag": "note",
        "elements": [
            {"tag": "plain_text", "content": "数据基于 AlphaSift 初选 + 19 个深度投研维度打分流程"}
        ],
    })

    card = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": f"\U0001f4cb AlphaSift 初选完成（{now}）",
                },
                "template": "blue",
            },
            "elements": elements,
        },
    }
    return card


def build_alert_card(
    node: str,
    error: str,
    run_url: str = "",
    details: str = "",
) -> dict:
    """构建节点运行失败告警卡片（红色警报主题）。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    content_lines = [
        f"**🚨 故障节点：** {node}",
        f"**❌ 异常原因：** {error}",
        f"**⏰ 告警时间：** {now}",
    ]
    if details:
        content_lines.append(f"**📝 详细信息：** {details}")

    elements: list[dict] = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": "\n".join(content_lines),
            },
        }
    ]

    if run_url:
        elements.append({
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {
                        "tag": "plain_text",
                        "content": "查看 GitHub Actions 运行记录",
                    },
                    "type": "danger",
                    "url": run_url,
                }
            ],
        })

    elements.append({"tag": "hr"})
    elements.append({
        "tag": "note",
        "elements": [
            {
                "tag": "plain_text",
                "content": "请检查 GitHub Secrets (PAT)、LLM API 余额或 Actions 日志进行排查",
            }
        ],
    })

    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": f"\U0001f6a8 节点运行失败告警 - {node}（{now}）",
                },
                "template": "red",
            },
            "elements": elements,
        },
    }


def build_backtest_card(summary: dict) -> dict:
    """构建每日投研报告 T+1 实盘回测卡片。"""
    if "error" in summary:
        return build_alert_card(
            node="每日T+1回测器",
            error=summary["error"],
        )

    rep_date = summary.get("report_date", "未知")
    bt_date = summary.get("backtest_date", "未知")
    bench_ret = summary.get("benchmark_return_pct", 0.0)
    avg_ret = summary.get("portfolio_avg_return_pct", 0.0)
    avg_max = summary.get("portfolio_avg_max_return_pct", 0.0)
    alpha = summary.get("portfolio_alpha_pct", 0.0)
    win_rate = summary.get("win_rate_pct", 0.0)
    beat_rate = summary.get("beat_bench_rate_pct", 0.0)

    header_template = "turquoise" if alpha >= 0 else "orange"

    summary_text = (
        f"**基准表现 (上证指数)：** {bench_ret:+.2f}%\n"
        f"**组合次日平均收益：** {avg_ret:+.2f}%  （超额 Alpha: **{alpha:+.2f}%**）\n"
        f"**早盘冲高兑现均值：** {avg_max:+.2f}%\n"
        f"**次日正收益胜率：** {win_rate:.1f}%  |  **跑赢基准概率：** {beat_rate:.1f}%"
    )

    elements: list[dict] = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": summary_text,
            },
        },
        {"tag": "hr"},
    ]

    details = summary.get("details", [])
    if details:
        # 补全可能缺失的股票中文名称
        missing_names = [
            str(d["stock_code"]).strip() for d in details
            if not d.get("stock_name") or str(d.get("stock_name")).strip() == str(d.get("stock_code")).strip()
        ]
        if missing_names:
            fetched = fetch_stock_names(missing_names)
            for d in details:
                code = str(d.get("stock_code", "")).strip()
                name = str(d.get("stock_name", "")).strip()
                if not name or name == code:
                    d["stock_name"] = fetched.get(code, code)

        lines = [
            "**\U0001f4ca 标的实盘次日表现明细：**",
            f"{'代码':<8} {'名称':<6} {'评分':>4} | {'次日涨跌':>8} {'冲高幅度':>8} | {'超额Alpha':>8}",
            "-" * 50,
        ]
        for d in details:
            if "error" in d:
                lines.append(f"{d['stock_code']:<8} {d.get('stock_name', ''):<6} {d.get('score', 0):>4.0f} | {d['error']}")
            else:
                code = d["stock_code"]
                name = d.get("stock_name", code)
                score = d.get("score", 0.0)
                ret = d.get("return_pct", 0.0)
                mret = d.get("max_return_pct", 0.0)
                alp = d.get("alpha_vs_bench", 0.0)
                lines.append(
                    f"{code:<8} {name:<6} {score:>4.0f} | {ret:>+7.2f}% {mret:>+7.2f}% | {alp:>+7.2f}%"
                )

        elements.append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": "```\n" + "\n".join(lines) + "\n```",
            },
        })

    elements.append({"tag": "hr"})
    elements.append({
        "tag": "note",
        "elements": [
            {
                "tag": "plain_text",
                "content": "每日盘后由 GitHub Actions 自动回测上个交易日推荐在今日的表现并提交留存",
            }
        ],
    })

    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": f"\U0001f4ca T+1 实盘回测看板（{rep_date} 推荐 → {bt_date} 实盘）",
                },
                "template": header_template,
            },
            "elements": elements,
        },
    }


def send_to_feishu(card_data: dict, webhook_url: str = "") -> bool:
    """将卡片消息发送到飞书群聊（优先使用 requests，若未安装则自动回退至 urllib）。"""
    url = webhook_url or FEISHU_WEBHOOK_MAIN

    if not url:
        log.warning("未设置 FEISHU_WEBHOOK 环境变量，跳过实际发送")
        log.info("以下为将要发送的消息内容:\n%s", json.dumps(card_data, ensure_ascii=False, indent=2))
        return False

    try:
        if requests is not None:
            resp = requests.post(
                url,
                json=card_data,
                headers={"Content-Type": "application/json"},
                timeout=10,
            )
            resp.raise_for_status()
            result = resp.json()
        else:
            payload_bytes = json.dumps(card_data).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=payload_bytes,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = resp.read().decode("utf-8")
                result = json.loads(body) if body else {}

        if result.get("code", -1) == 0:
            log.info("飞书消息发送成功!")
            return True
        else:
            log.error("飞书返回错误: code=%d, msg=%s", result.get("code"), result.get("msg"))
            return False
    except Exception as e:
        log.error("发送飞书消息失败: %s", e)
        return False


def main():
    parser = argparse.ArgumentParser(
        description="将分析结果推送到飞书群聊",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
示例:
  # 推送评分结果
  python feishu_notifier.py

  # 主节点通知（GitHub Actions 中使用）
  python feishu_notifier.py --mode main --stocks "600519,000858" --first5 "600519,000858,000001,600036,601318" --last5 "002415,600900,000333,601888,600276"

前置条件:
  在项目根目录创建 .env 文件，填入 FEISHU_WEBHOOK 等变量。
  完整配置说明见 docs/LLM_CONFIG_GUIDE.md
        """,
    )
    parser.add_argument(
        "--result",
        default="analysis_result.json",
        help="分析结果文件路径（默认: analysis_result.json）",
    )
    parser.add_argument(
        "--mode",
        choices=["default", "main", "alert", "backtest"],
        default="default",
        help="运行模式：default=推送评分结果，main=主节点通知，alert=节点失败告警，backtest=T+1回测看板（默认: default）",
    )
    parser.add_argument("--stocks", default="", help="初选股票列表（主节点模式）")
    parser.add_argument("--first5", default="", help="前5支股票（主节点模式）")
    parser.add_argument("--last5", default="", help="后5支股票（主节点模式）")
    parser.add_argument("--trigger-code-b", default="", help="账户 B 触发 HTTP 状态码")
    parser.add_argument("--trigger-code-c", default="", help="账户 C 触发 HTTP 状态码")
    parser.add_argument(
        "--theme-summary",
        default="data/theme_summary.json",
        help="短线热点题材总结文件路径（主节点模式，默认: data/theme_summary.json）",
    )
    # 告警模式参数
    parser.add_argument("--node", default="未知节点", help="发生故障的节点名称（alert 模式）")
    parser.add_argument("--error", default="任务执行异常", help="错误摘要或异常原因（alert 模式）")
    parser.add_argument("--run-url", default="", help="GitHub Actions Run URL 链接（alert 模式）")
    parser.add_argument("--details", default="", help="补充详细错误日志（alert 模式）")
    # 回测模式参数
    parser.add_argument(
        "--backtest",
        default="data/backtest_latest.json",
        help="T+1 回测结果 JSON 文件路径（backtest 模式，默认: data/backtest_latest.json）",
    )
    args = parser.parse_args()

    if args.mode == "main":
        if not args.stocks:
            log.error("主节点模式需要提供 --stocks 参数")
            sys.exit(1)

        # 题材总结缺失或损坏都不应中断通知：降级为不带题材块的两段式卡片。
        theme_summary = None
        if os.path.isfile(args.theme_summary):
            try:
                with open(args.theme_summary, "r", encoding="utf-8") as f:
                    theme_summary = json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                log.warning("题材总结文件解析失败，跳过题材块: %s", e)
                theme_summary = None
        else:
            log.warning("题材总结文件不存在，跳过题材块: %s", args.theme_summary)

        card_data = build_main_card(
            args.stocks,
            args.first5,
            args.last5,
            args.trigger_code_b,
            args.trigger_code_c,
            theme_summary=theme_summary,
        )
        log.info("主节点飞书通知预览:\n%s", json.dumps(card_data, ensure_ascii=False, indent=2))
        success = send_to_feishu(card_data)

    elif args.mode == "alert":
        log.warning("正在生成节点失败告警卡片: %s - %s", args.node, args.error)
        card_data = build_alert_card(
            node=args.node,
            error=args.error,
            run_url=args.run_url,
            details=args.details,
        )
        log.info("失败告警卡片预览:\n%s", json.dumps(card_data, ensure_ascii=False, indent=2))
        success = send_to_feishu(card_data)

    elif args.mode == "backtest":
        if not os.path.isfile(args.backtest):
            log.error("回测结果文件不存在: %s", args.backtest)
            sys.exit(1)

        try:
            with open(args.backtest, "r", encoding="utf-8") as f:
                bt_summary = json.load(f)
            log.info("成功读取回测结果: %s", args.backtest)
        except json.JSONDecodeError as e:
            log.error("回测结果文件 JSON 解析失败: %s", e)
            sys.exit(1)

        card_data = build_backtest_card(bt_summary)
        log.info("回测看板卡片预览:\n%s", json.dumps(card_data, ensure_ascii=False, indent=2))
        success = send_to_feishu(card_data)

    else:
        if not os.path.isfile(args.result):
            log.error("结果文件不存在: %s", args.result)
            log.info("请先运行 run_skills_evaluation.py 生成结果")
            sys.exit(1)

        try:
            with open(args.result, "r", encoding="utf-8") as f:
                results = json.load(f)
            log.info("成功读取 %d 支股票的分析结果", len(results))
        except json.JSONDecodeError as e:
            log.error("结果文件 JSON 解析失败: %s", e)
            sys.exit(1)

        card_data = build_card_message(results)
        log.info("飞书消息预览:\n%s", json.dumps(card_data, ensure_ascii=False, indent=2))
        success = send_to_feishu(card_data)

    if success:
        log.info("全部完成，飞书推送成功!")
    else:
        log.info("飞书推送未完成（未设置 webhook 或发送失败），但消息已生成预览")


if __name__ == "__main__":
    main()