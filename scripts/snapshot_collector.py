#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""snapshot_collector.py -- A股主板快照采集器（云端版）"""
import json, logging, sys, time
from datetime import datetime, timedelta
from pathlib import Path
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"
SNAPSHOT_FILE = DATA_DIR / "snapshot_latest.json"
DIFF_FILE = DATA_DIR / "snapshot_diff.json"
WATCHLIST_FILE = DATA_DIR / "watchlist.json"
ARCHIVE_DIR = DATA_DIR / "archive"

TENCENT_URL = "https://qt.gtimg.cn/q="
TENCENT_REF = {"Referer": "https://stockapp.finance.qq.com"}

KNOWN_BANK_CODES = {
    "000001", "002142", "002807", "002839", "002936", "002948", "002966", "002958",
    "001227", "001289",
    "600000", "600015", "600016", "600036", "600908", "600919", "600926", "600928",
    "601009", "601128", "601166", "601169", "601229", "601288", "601328", "601398",
    "601528", "601577", "601658", "601665", "601818", "601838", "601860", "601916",
    "601939", "601963", "601988", "601997", "601998",
}
BANK_KW = ["银行", "农商行", "农信社", "农村商业"]

def _tcc(code):
    return ("sz" if code.startswith(("0","3")) else "sh") + code

def _f(parts, idx):
    """安全取腾讯行情字段为浮点数（缺失/空返回 0）。"""
    try:
        v = parts[idx].strip()
        return float(v) if v else 0.0
    except (IndexError, ValueError):
        return 0.0

def _is_bank(code, name):
    """银行判断：优先代码白名单，其次剥掉 XD/XR/DR 前缀后按名称关键字判断。"""
    if code in KNOWN_BANK_CODES:
        return True
    nm = name.replace("XD", "").replace("XR", "").replace("DR", "").replace("*ST", "")
    return any(kw in nm for kw in BANK_KW)

def fetch_main_board_stocks():
    all_stocks = []
    log.info("正在获取A股主板股票列表...")
    codes = []
    for i in range(1, 1000): codes.append(f"{i:06d}")
    for i in range(600000, 602000): codes.append(str(i))
    for i in range(603000, 604000): codes.append(str(i))
    for i in range(605000, 606000): codes.append(str(i))
    log.info("主板候选代码总数: %d", len(codes))
    batch_size = 50
    total_fetched = 0
    for bs in range(0, len(codes), batch_size):
        tcodes = [_tcc(c) for c in codes[bs:bs+batch_size]]
        url = TENCENT_URL + ",".join(tcodes)
        try:
            r = requests.get(url, headers=TENCENT_REF, timeout=30)
            r.encoding = "gbk"
            for line in r.text.strip().split("\n"):
                if "~" not in line: continue
                p = line.split("~")
                if len(p) < 50: continue
                code = p[2]
                if not code: continue
                if not (code.startswith("000") or code.startswith("600") or code.startswith("601") or code.startswith("603") or code.startswith("605")): continue
                price = _f(p, 3)
                # 腾讯接口字段位置：3价格 32涨跌幅 36成交量(手) 37成交额(万)
                # 38换手率 39市盈率 44流通市值(亿) 45总市值(亿) 46市净率 49量比
                all_stocks.append({
                    "code": code,
                    "name": p[1],
                    "price": price,
                    "change_pct": _f(p, 32),
                    "volume": int(_f(p, 36)),
                    "amount": _f(p, 37) * 10000.0,      # 万元 -> 元
                    "turnover_rate": _f(p, 38),
                    "pe_ratio": _f(p, 39) or 0.0,
                    "circ_mv": _f(p, 44) * 1e8,          # 亿元 -> 元
                    "total_mv": _f(p, 45) * 1e8,         # 亿元 -> 元
                    "pb_ratio": _f(p, 46) or 0.0,
                    "volume_ratio": _f(p, 49) or 1.0,    # 量比缺失按 1 处理
                    "is_bank": _is_bank(code, p[1]),
                    "timestamp": datetime.now().isoformat(),
                })
                total_fetched += 1
            log.info("批次完成: %d/%d", total_fetched, len(codes))
            time.sleep(0.5)
        except Exception as e:
            log.warning("批次请求失败 (%d-%d): %s", bs, bs+batch_size, e)
            continue
    log.info("主板快照采集完成，共 %d 支股票", total_fetched)
    return all_stocks

def save_snapshot(stocks, filepath=None):
    if filepath is None: filepath = SNAPSHOT_FILE
    filepath.parent.mkdir(parents=True, exist_ok=True)
    data = {"snapshot_time": datetime.now().isoformat(), "total_stocks": len(stocks), "stocks": stocks}
    with open(filepath, "w", encoding="utf-8") as f: json.dump(data, f, ensure_ascii=False, indent=2)
    log.info("快照已保存到: %s (%d 支)", filepath.name, len(stocks))
    return data

def load_previous_snapshot():
    if not SNAPSHOT_FILE.exists(): return None
    with open(SNAPSHOT_FILE, "r", encoding="utf-8") as f: return json.load(f)

def compute_diff(current, previous):
    if not previous: return {"diff_time": datetime.now().isoformat(), "changed": [], "stats": {"note": "首次快照"}}
    pm = {s["code"]: s for s in previous.get("stocks", [])}
    cm = {s["code"]: s for s in current.get("stocks", [])}
    changed = []
    for code, cs in cm.items():
        if code in pm:
            pp, cp = pm[code].get("price", 0), cs.get("price", 0)
            if pp > 0 and abs(cp - pp) > 0.001:
                pc = round(cp - pp, 2)
                changed.append({"code": code, "name": cs["name"], "prev_price": pp, "curr_price": cp, "price_change": pc, "change_pct": round(pc/pp*100, 2)})
    dd = {"diff_time": datetime.now().isoformat(), "prev_snapshot_time": previous.get("snapshot_time",""), "curr_snapshot_time": current.get("snapshot_time",""), "total_changed": len(changed), "changed": changed[:200], "stats": {"total_stocks_current": current.get("total_stocks",0), "total_stocks_previous": previous.get("total_stocks",0), "changed_count": len(changed), "bank_stocks": sum(1 for s in cm.values() if s.get("is_bank"))}}
    log.info("增量对比完成: %d 支股票有价格变化", len(changed))
    return dd

def archive_snapshot(data):
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    af = ARCHIVE_DIR / f"snapshot_{datetime.now().strftime('%Y%m%d')}.json"
    if not af.exists():
        with open(af, "w", encoding="utf-8") as f: json.dump(data, f, ensure_ascii=False, indent=2)
        log.info("快照已归档: %s", af.name)

def main():
    log.info("=" * 50)
    log.info("A股主板快照采集开始")
    log.info("=" * 50)
    stocks = fetch_main_board_stocks()
    if not stocks:
        log.error("未采集到任何数据，退出")
        sys.exit(1)
    current_data = save_snapshot(stocks)
    previous_data = load_previous_snapshot()
    if previous_data:
        diff_data = compute_diff(current_data, previous_data)
        with open(DIFF_FILE, "w", encoding="utf-8") as f: json.dump(diff_data, f, ensure_ascii=False, indent=2)
        log.info("差异分析已保存到: %s", DIFF_FILE.name)
    else:
        diff_data = {"diff_time": datetime.now().isoformat(), "changed": [], "stats": {"note": "首次快照"}}
        with open(DIFF_FILE, "w", encoding="utf-8") as f: json.dump(diff_data, f, ensure_ascii=False, indent=2)
    archive_snapshot(current_data)
    bank_count = sum(1 for s in stocks if s.get("is_bank"))
    log.info("=" * 50)
    log.info("采集摘要:")
    log.info("  总股票数: %d", len(stocks))
    log.info("  银行股数: %d", bank_count)
    log.info("  采集时间: %s", current_data["snapshot_time"])
    if previous_data: log.info("  价格变化: %d 支", diff_data.get("total_changed", 0))
    log.info("=" * 50)

if __name__ == "__main__":
    main()
