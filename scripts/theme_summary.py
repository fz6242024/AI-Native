# ============================================================
# theme_summary.py
# 短线热点题材总结：从行业板块榜 + 当日全市场快照提炼热点题材与原因逻辑
#
# 数据来源与降级
#   主源 —— 新浪行业板块接口（1 个请求，约 6.5KB），一次拿到全部行业的
#           涨跌幅与领涨股，据此排出"今日行业涨幅榜"；
#   降级 —— 接口不可用（CI 代理拦截 / 超时）时退回"快照涨幅榜"，由 LLM
#           从个股名称识别题材。降级只改变喂给 LLM 的输入，不中断流程。
#
# 为什么不用 akshare / 东方财富：CI 代理环境会拦截东财接口（实测 ProxyError），
# 而新浪与腾讯行情源在 CI 里是通的（快照采集用的就是腾讯 qt.gtimg.cn）。
#
# 输出：一个 JSON 文件，供 feishu_notifier.py 的主卡片消费。
# ============================================================

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import requests

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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)
log.info(log_debug_msg)

def resolve_llm_config(env=None) -> tuple:
    """解析 LLM 配置，返回 (base_url, api_key, model)。

    env 默认取 os.environ；显式传入映射便于测试。

    解析链与 scripts/run_skills_evaluation.py:38-79 保持一致，顺序为：
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
    model = "agnes-3.0-flash"
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
            base_url = env.get("LLM_BASE_URL") or env.get("OPENAI_BASE_URL", "https://api.openai.com/v1")

        model = env.get("LITELLM_MODEL", model)
        _env_model = env.get("LLM_MODEL", "").strip()
        if _env_model:
            model = _env_model

    return base_url, api_key, model


LLM_BASE_URL, LLM_API_KEY, LLM_MODEL = resolve_llm_config()
log.info("LLM 配置: BASE_URL=%s, MODEL=%s, API_KEY=%s",
         LLM_BASE_URL, LLM_MODEL, "已配置" if LLM_API_KEY else "未配置")

SINA_BOARD_URL = "https://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php"
SINA_HEADERS = {
    "Referer": "https://finance.sina.com.cn",
    "User-Agent": "Mozilla/5.0 (compatible; AlphaSift-theme-summary)",
}

# 新浪板块字段位置（已用 [5] = [4]/([3]-[4])*100 反推验证过）
_B_NODE, _B_NAME, _B_COUNT = 0, 1, 2
_B_AVG_PRICE, _B_AVG_CHANGE, _B_CHANGE_PCT = 3, 4, 5
_B_AMOUNT = 7
_B_LEADER_CODE, _B_LEADER_PCT, _B_LEADER_PRICE, _B_LEADER_NAME = 8, 9, 10, 12

# 涨停判定阈值：主板涨停 10%，留一点浮点余量
LIMIT_UP_PCT = 9.8
TOP_BOARDS = 8      # 喂给 LLM 的行业榜条数
TOP_GAINERS = 25    # 降级模式下喂给 LLM 的个股涨幅榜条数


# ------------------------------------------------------------
# 数据获取
# ------------------------------------------------------------

def fetch_industry_boards(timeout: int = 10) -> list:
    """拉取新浪行业板块榜。失败返回空列表（调用方据此降级，不抛异常）。

    返回 [{"node","name","count","change_pct","amount","leader_code",
           "leader_name","leader_change_pct"}, ...]，按涨跌幅降序。
    """
    try:
        resp = requests.get(SINA_BOARD_URL, headers=SINA_HEADERS, timeout=timeout)
        resp.raise_for_status()
        resp.encoding = "gbk"
        body = resp.text
    except Exception as e:  # noqa: BLE001 - 网络问题必须降级而非中断
        log.warning("新浪行业板块接口不可用（%s: %s），将降级为快照涨幅榜", type(e).__name__, e)
        return []

    # 形如：var S_Finance_bankuai_sinaindustry = {"new_blhy":"new_blhy,玻璃行业,19,...", ...};
    if "=" not in body:
        log.warning("新浪行业板块返回格式异常，将降级为快照涨幅榜")
        return []
    payload = body.split("=", 1)[1].strip().rstrip(";")
    items = re.findall(r'"[^"]+":"([^"]+)"', payload)

    boards = []
    for item in items:
        parts = item.split(",")
        if len(parts) <= _B_LEADER_NAME:
            continue
        try:
            boards.append({
                "node": parts[_B_NODE],
                "name": parts[_B_NAME],
                "count": int(parts[_B_COUNT] or 0),
                "change_pct": float(parts[_B_CHANGE_PCT]),
                "amount": float(parts[_B_AMOUNT] or 0),
                "leader_code": parts[_B_LEADER_CODE],
                "leader_name": parts[_B_LEADER_NAME],
                "leader_change_pct": float(parts[_B_LEADER_PCT] or 0),
            })
        except (ValueError, IndexError):
            continue

    boards.sort(key=lambda b: b["change_pct"], reverse=True)
    log.info("新浪行业板块获取成功，共 %d 个板块，最高涨幅 %.2f%%（%s）",
             len(boards), boards[0]["change_pct"] if boards else 0.0,
             boards[0]["name"] if boards else "-")
    return boards


def compute_market_pulse(stocks: list) -> dict:
    """从快照算市场温度：涨跌家数、涨停家数、成交额、涨跌幅中位数。"""
    if not stocks:
        return {}
    changes = []
    amount_total = 0.0
    up = down = flat = limit_up = 0
    for s in stocks:
        try:
            chg = float(s.get("change_pct") or 0.0)
        except (TypeError, ValueError):
            chg = 0.0
        changes.append(chg)
        if chg > 0:
            up += 1
        elif chg < 0:
            down += 1
        else:
            flat += 1
        if chg >= LIMIT_UP_PCT:
            limit_up += 1
        try:
            amount_total += float(s.get("amount") or 0.0)
        except (TypeError, ValueError):
            pass

    changes.sort()
    median = changes[len(changes) // 2] if changes else 0.0
    return {
        "total": len(stocks),
        "up": up,
        "down": down,
        "flat": flat,
        "limit_up": limit_up,
        "median_change": round(median, 2),
        "amount_yi": round(amount_total / 1e8, 0),   # 成交额（亿元）
    }


def top_gainers(stocks: list, limit: int = TOP_GAINERS) -> list:
    """按涨幅取前 N 支（降级模式下给 LLM 识别题材用）。"""
    ranked = sorted(stocks, key=lambda s: float(s.get("change_pct") or 0.0), reverse=True)
    return [
        {
            "code": s.get("code", ""),
            "name": s.get("name", ""),
            "change_pct": round(float(s.get("change_pct") or 0.0), 2),
            "turnover_rate": round(float(s.get("turnover_rate") or 0.0), 2),
        }
        for s in ranked[:limit]
    ]


# ------------------------------------------------------------
# LLM 总结
# ------------------------------------------------------------

SYSTEM_PROMPT = (
    "你是一位资深 A 股短线交易员，擅长从盘面数据中识别当日热点题材，"
    "并用简明的中文解释背后的资金逻辑。"
    "严格按要求输出 JSON，不要输出任何其他内容。"
)


def _build_prompt(boards: list, pulse: dict, gainers: list, picks: list) -> str:
    lines = ["【今日市场温度】"]
    if pulse:
        lines.append(
            f"全市场 {pulse.get('total')} 支：上涨 {pulse.get('up')} 家、"
            f"下跌 {pulse.get('down')} 家、平盘 {pulse.get('flat')} 家；"
            f"涨停(>=9.8%) {pulse.get('limit_up')} 家；"
            f"涨跌幅中位数 {pulse.get('median_change')}%；"
            f"成交额约 {pulse.get('amount_yi')} 亿元。"
        )

    if boards:
        lines.append("")
        lines.append("【今日行业板块涨幅榜】（按板块涨跌幅降序）")
        for b in boards[:TOP_BOARDS]:
            lines.append(
                f"- {b['name']}：板块涨跌 {b['change_pct']:.2f}%，"
                f"领涨股 {b['leader_name']}({b['leader_code']}) {b['leader_change_pct']:.2f}%"
            )
    else:
        lines.append("")
        lines.append("【今日个股涨幅榜】（行业分类数据不可用，请据个股名称自行归纳题材）")
        for g in gainers:
            lines.append(f"- {g['name']}({g['code']}) 涨 {g['change_pct']:.2f}%")

    if picks:
        lines.append("")
        lines.append("【本次初选选出的股票】")
        lines.append("、".join(f"{p.get('name', '')}({p.get('code', '')})" for p in picks))

    lines.append("")
    lines.append(
        "请完成两件事，输出严格 JSON：\n"
        '{"summary": "一句话概括今天的市场风格（不超过 40 字）",\n'
        ' "themes": [{"name": "题材名（不超过 8 字）",\n'
        '             "change_pct": 该题材代表板块的涨跌幅数字,\n'
        '             "leaders": ["代表股名1", "代表股名2"],\n'
        '             "logic": "为什么涨、资金逻辑是什么（不超过 60 字）"}]}\n'
        f"themes 给出 3 个最值得关注的短线题材，按重要性降序。\n"
        "leaders 请给出该题材 2-3 支代表股（可结合你对 A 股该板块的了解，"
        "不必局限在给定数据里的领涨股）。但只选主板股票，即 600/601/603/605/000/002 开头；"
        "不要出现 300/301/688/689/8/4 开头的股票。"
        "若确实只能确定 1 支，就只给 1 支；不确定的股票不要写。\n"
        "若某题材无法从给定数据判断，宁可不写，也不要编造。"
    )
    return "\n".join(lines)


def summarize_with_llm(boards: list, pulse: dict, gainers: list, picks: list) -> dict | None:
    """调 LLM 总结题材。任何失败都返回 None（调用方回退到纯数据版）。"""
    if not LLM_API_KEY:
        log.warning("未配置 LLM_API_KEY，跳过题材总结")
        return None

    prompt = _build_prompt(boards, pulse, gainers, picks)
    try:
        from openai import OpenAI

        client = OpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY)
        response = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            temperature=0.4,
            max_tokens=1200,
        )
        raw = (response.choices[0].message.content or "").strip()
        data = _extract_json(raw)
        if data is None:
            log.warning("LLM 返回内容无法解析为 JSON: %s", raw[:200])
            return None
        return data
    except Exception as e:  # noqa: BLE001 - LLM 失败不应中断主流程
        log.warning("LLM 题材总结失败: %s: %s", type(e).__name__, e)
        return None


def _extract_json(raw: str) -> dict | None:
    """从 LLM 回复里抠出第一个完整 JSON 对象。容忍 ```json 围栏与前后废话。"""
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass
    # 退一步：取第一个 { 到最后一个 } 之间
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(text[start:end + 1])
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


def fallback_themes(boards: list, pulse: dict) -> dict:
    """无 LLM 时的纯数据版：直接给行业涨幅榜前三，不做逻辑推断。"""
    themes = []
    for b in boards[:3]:
        themes.append({
            "name": b["name"],
            "change_pct": round(b["change_pct"], 2),
            "leaders": [b["leader_name"]] if b.get("leader_name") else [],
            "logic": f"板块涨跌 {b['change_pct']:.2f}%，领涨股 {b['leader_name']}"
                     f"({b['leader_change_pct']:.2f}%)",
        })
    summary = ""
    if pulse:
        summary = (f"全市场上涨 {pulse.get('up')} 家、下跌 {pulse.get('down')} 家，"
                   f"涨停 {pulse.get('limit_up')} 家")
    return {"summary": summary, "themes": themes}


# ------------------------------------------------------------
# 主流程
# ------------------------------------------------------------

def build_theme_summary(snapshot_path: Path, picks_path: Path | None = None) -> dict:
    """组装题材总结。**保证不抛异常** —— 任何环节失败都退化为可用输出。"""
    stocks = []
    try:
        payload = json.loads(Path(snapshot_path).read_text(encoding="utf-8"))
        stocks = payload.get("stocks", []) if isinstance(payload, dict) else payload
    except Exception as e:  # noqa: BLE001
        log.warning("读取快照失败: %s: %s", type(e).__name__, e)

    picks = []
    if picks_path and Path(picks_path).exists():
        try:
            picks = json.loads(Path(picks_path).read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            log.warning("读取初选结果失败: %s: %s", type(e).__name__, e)

    pulse = compute_market_pulse(stocks)
    boards = fetch_industry_boards()
    gainers = top_gainers(stocks) if not boards else []

    result = summarize_with_llm(boards, pulse, gainers, picks)
    llm_used = result is not None
    source = "sina_industry" if boards else "snapshot_only"
    if result is None:
        log.info("回退到纯数据版题材总结")
        result = fallback_themes(boards, pulse)

    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": source,
        "llm_used": llm_used,
        "market_pulse": pulse,
        "themes": result.get("themes", []),
        "summary": result.get("summary", ""),
    }


def main():
    parser = argparse.ArgumentParser(description="生成短线热点题材总结")
    parser.add_argument("--snapshot", default="data/snapshot_latest.json", help="全市场快照路径")
    parser.add_argument("--picks", default="top_10_stocks.json", help="初选结果路径（可选）")
    parser.add_argument("--output", default="data/theme_summary.json", help="输出路径")
    args = parser.parse_args()

    summary = build_theme_summary(Path(args.snapshot), Path(args.picks))

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    log.info("题材总结已保存到: %s", out)
    log.info("数据源: %s | 题材数: %d", summary["source"], len(summary["themes"]))
    if summary.get("summary"):
        log.info("市场总述: %s", summary["summary"])
    for t in summary["themes"]:
        log.info("  · %s（%s%%）代表股: %s —— %s",
                 t.get("name", ""), t.get("change_pct", "-"),
                 "、".join(t.get("leaders", [])) or "-", t.get("logic", ""))


if __name__ == "__main__":
    main()
