#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""backtest_daily_report.py -- 每日投研报告次日 (T+1) 真实盘面回测与绩效追踪器。

设计理念：
1. 采用 qt.gtimg.cn 极速行情接口，批量获取各标的与大盘指数（sh000001）的昨收、今开、最高、最低、现价与涨跌幅；
2. 自动匹配前一交易日（如昨日）推荐股票与评分；
3. 输出完整的 T+1 回测分析：
   - 次日收盘涨跌幅 vs 大盘基准超额 (Alpha)
   - 盘中最大冲高幅度（兑现机会）
   - 胜率统计与跑赢大盘概率
4. 结果持久化为 JSON (data/backtest_latest.json)，支持一键飞书卡片推送。
"""

from __future__ import annotations

import argparse
import json
import logging
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
TENCENT_QUOTE_URL = "https://qt.gtimg.cn/q="


def _format_symbol(code: str) -> str:
    """转换为腾讯行情代码格式（sh600519 / sz000858）。"""
    code_str = str(code).strip()
    prefix = "sz" if code_str.startswith(("0", "3")) else "sh"
    return f"{prefix}{code_str}"


def fetch_realtime_quotes(symbols: list[str], timeout: int = 10) -> dict[str, dict[str, Any]]:
    """批量从腾讯极速行情接口获取行情（含昨收、今开、最高、现价、涨跌幅）。"""
    if not symbols:
        return {}
    
    url = TENCENT_QUOTE_URL + ",".join(symbols)
    req = urllib.request.Request(
        url,
        headers={"Referer": "https://stockapp.finance.qq.com"},
    )
    
    quotes: dict[str, dict[str, Any]] = {}
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("gbk", errors="ignore")
            for line in text.strip().split(";"):
                line = line.strip()
                if not line or "~" not in line:
                    continue
                parts = line.split("~")
                if len(parts) > 35:
                    name = parts[1]
                    code = parts[2]
                    curr_price = float(parts[3]) if parts[3] else 0.0
                    prev_close = float(parts[4]) if parts[4] else 0.0
                    open_price = float(parts[5]) if parts[5] else 0.0
                    change_pct = float(parts[32]) if parts[32] else 0.0
                    high_price = float(parts[33]) if parts[33] else 0.0
                    low_price = float(parts[34]) if parts[34] else 0.0
                    
                    quotes[code] = {
                        "name": name,
                        "code": code,
                        "curr_price": curr_price,
                        "prev_close": prev_close,
                        "open_price": open_price,
                        "change_pct": change_pct,
                        "high_price": high_price,
                        "low_price": low_price,
                    }
    except Exception as e:
        log.warning("批量获取行情数据失败: %s", e)

    return quotes


def load_recommended_stocks(date_str: str | None = None, base_dir: Path | None = None) -> list[dict[str, Any]]:
    """加载指定日期的推荐股票列表。
    
    若 date_str 为空，则自动查找最近一次有打分记录的日期。
    """
    base = Path(base_dir) if base_dir is not None else DATA_DIR
    hist_dir = base / "skill_history"
    all_records: list[dict[str, Any]] = []

    # 1. 扫描 skill_history 目录
    if hist_dir.is_dir():
        for json_file in sorted(hist_dir.glob("*.json")):
            try:
                with open(json_file, "r", encoding="utf-8") as f:
                    content = json.load(f)
                    if isinstance(content, dict) and "results" in content:
                        all_records.append(content)
                    elif isinstance(content, list):
                        all_records.extend(content)
            except Exception:
                continue

    # 2. 扫描 skill_scores_history.json
    scores_hist = base / "skill_scores_history.json"
    if scores_hist.exists():
        try:
            with open(scores_hist, "r", encoding="utf-8") as f:
                content = json.load(f)
                if isinstance(content, list):
                    all_records.extend(content)
        except Exception:
            pass

    if not all_records:
        return []

    # 按日期分组
    date_map: dict[str, list[dict[str, Any]]] = {}
    for entry in all_records:
        d = entry.get("date")
        if not d:
            continue
        res_list = entry.get("results", [])
        if res_list:
            date_map.setdefault(d, []).extend(res_list)

    if not date_map:
        return []

    target_date = date_str
    if not target_date or target_date not in date_map:
        sorted_dates = sorted(date_map.keys())
        target_date = sorted_dates[-1]

    seen_codes = set()
    unique_stocks = []
    for item in date_map[target_date]:
        code = str(item.get("stock_code") or item.get("code", "")).strip()
        if code and code not in seen_codes:
            seen_codes.add(code)
            unique_stocks.append({
                "date": target_date,
                "stock_code": code,
                "stock_name": item.get("stock_name", code),
                "final_score": float(item.get("final_score", 0)),
                "reliable": item.get("reliable", True),
            })

    return unique_stocks


def run_t1_backtest(
    stocks: list[dict[str, Any]],
    quote_fetcher=fetch_realtime_quotes,
) -> dict[str, Any]:
    """对推荐股票执行 T+1 实盘行情回测。"""
    if not stocks:
        return {"error": "无有效候选股票"}

    report_date = stocks[0]["date"]
    
    # 构造标的代码列表（包含基准指数 sh000001 和 sh000300）
    symbols = ["sh000001", "sh000300"]
    for s in stocks:
        symbols.append(_format_symbol(s["stock_code"]))

    quotes = quote_fetcher(symbols)
    bench_quote = quotes.get("000001", {})
    bench_return = bench_quote.get("change_pct", 0.0)

    results_details = []
    returns = []
    max_returns = []

    for s in stocks:
        code = s["stock_code"]
        name = s["stock_name"]
        score = s["final_score"]
        q = quotes.get(code)

        if q and q.get("prev_close", 0) > 0:
            tc = q["prev_close"]
            t1c = q["curr_price"]
            t1o = q["open_price"]
            t1h = q["high_price"]
            t1l = q["low_price"]
            ret_pct = q["change_pct"]
            
            # 盘中冲高幅度
            max_ret_pct = round((t1h - tc) / tc * 100, 2) if tc > 0 else 0.0
            max_drawdown = round((t1l - tc) / tc * 100, 2) if tc > 0 else 0.0
            alpha = round(ret_pct - bench_return, 2)

            returns.append(ret_pct)
            max_returns.append(max_ret_pct)

            results_details.append({
                "stock_code": code,
                "stock_name": q.get("name") or name,
                "score": score,
                "t_close": tc,
                "t1_open": t1o,
                "t1_high": t1h,
                "t1_low": t1l,
                "t1_close": t1c,
                "return_pct": ret_pct,
                "max_return_pct": max_ret_pct,
                "max_drawdown_pct": max_drawdown,
                "alpha_vs_bench": alpha,
            })
        else:
            results_details.append({
                "stock_code": code,
                "stock_name": name,
                "score": score,
                "error": "行情缺失",
            })

    valid_returns = [r for r in returns]
    avg_return = round(sum(valid_returns) / len(valid_returns), 2) if valid_returns else 0.0
    avg_max_return = round(sum(max_returns) / len(max_returns), 2) if max_returns else 0.0
    win_rate = round(len([r for r in valid_returns if r > 0]) / len(valid_returns) * 100, 1) if valid_returns else 0.0
    beat_bench_rate = round(len([r for r in valid_returns if r > bench_return]) / len(valid_returns) * 100, 1) if valid_returns else 0.0

    today_str = datetime.now().strftime("%Y-%m-%d")
    return {
        "report_date": report_date,
        "backtest_date": today_str,
        "benchmark": "上证指数 (000001)",
        "benchmark_return_pct": bench_return,
        "portfolio_avg_return_pct": avg_return,
        "portfolio_avg_max_return_pct": avg_max_return,
        "portfolio_alpha_pct": round(avg_return - bench_return, 2),
        "win_rate_pct": win_rate,
        "beat_bench_rate_pct": beat_bench_rate,
        "details": results_details,
    }


def format_backtest_console(summary: dict[str, Any]) -> str:
    """生成整洁易读的控制台回测结果看板。"""
    if "error" in summary:
        return f"回测异常: {summary['error']}"

    lines = [
        "=" * 72,
        f"📊 每日投研报告 T+1 真实盘面回测看板 (报告日:{summary['report_date']} → 回测日:{summary['backtest_date']})",
        "=" * 72,
        f"基准表现 (上证指数): {summary['benchmark_return_pct']:+.2f}%",
        f"组合平均次日收益:   {summary['portfolio_avg_return_pct']:+.2f}%  (超额 Alpha: {summary['portfolio_alpha_pct']:+.2f}%)",
        f"盘中最大冲高均值:   {summary['portfolio_avg_max_return_pct']:+.2f}%  (早盘冲高兑现窗口)",
        f"次日正收益胜率:     {summary['win_rate_pct']:.1f}%",
        f"跑赢基准大盘概率:   {summary['beat_bench_rate_pct']:.1f}%",
        "-" * 72,
        f"{'代码':<8} {'名称':<6} {'评分':>4} | {'昨收(买入)':>8} {'今现价':>8} | {'次日涨跌':>8} {'盘中最高':>8} | {'超额Alpha':>8}",
        "-" * 72,
    ]

    for d in summary.get("details", []):
        if "error" in d:
            lines.append(f"{d['stock_code']:<8} {d['stock_name']:<6} {d['score']:>4.0f} | {d['error']}")
        else:
            code = d["stock_code"]
            name = d["stock_name"]
            score = d["score"]
            tc = d["t_close"]
            t1c = d["t1_close"]
            ret = d["return_pct"]
            mret = d["max_return_pct"]
            alp = d["alpha_vs_bench"]
            lines.append(
                f"{code:<8} {name:<6} {score:>4.0f} | {tc:>8.2f} {t1c:>8.2f} | "
                f"{ret:>+7.2f}% {mret:>+7.2f}% | {alp:>+7.2f}%"
            )

    lines.append("=" * 72)
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="每日投研报告次日 T+1 回测工具")
    parser.add_argument("--date", help="指定报告日期（YYYY-MM-DD），缺省自动选最近一次")
    parser.add_argument("--output", help="回测结果输出 JSON 路径", default=str(DATA_DIR / "backtest_latest.json"))
    args = parser.parse_args()

    log.info("开始执行投研报告回测...")
    stocks = load_recommended_stocks(args.date)
    if not stocks:
        log.warning("未找到可供回测的历史推荐股票记录")
        return

    summary = run_t1_backtest(stocks)
    output_text = format_backtest_console(summary)
    print(output_text)

    try:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        log.info("回测数据已保存到: %s", out_path)
    except Exception as e:
        log.warning("保存回测结果失败: %s", e)


if __name__ == "__main__":
    main()
