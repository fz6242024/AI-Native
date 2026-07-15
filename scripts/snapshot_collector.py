#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
snapshot_collector.py — A股主板快照采集器（精简版）
从东方财富拉取实时行情，保存为 JSON 文件
支持增量对比和历史存档
运行环境：GitHub Actions（Ubuntu）
"""

import json
import logging
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# 数据目录
BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"
SNAPSHOT_FILE = DATA_DIR / "snapshot_latest.json"
DIFF_FILE = DATA_DIR / "snapshot_diff.json"
WATCHLIST_FILE = DATA_DIR / "watchlist.json"
ARCHIVE_DIR = DATA_DIR / "archive"

# 东方财富行情接口
EM_HQ_URL = "https://push2.eastmoney.com/api/qt/ulist.np/get"
EM_HEADERS = {"Referer": "https://quote.eastmoney.com"}

# 银行股黑名单（股票代码）
KNOWN_BANK_CODES = {
    "000001", "000002", "002142", "002807", "001289", "002966",
    "001339", "002936", "001267", "600908", "600919", "600926",
    "601002", "601009", "601128", "601166", "601216", "601229",
    "601328", "601336", "601377", "601456", "601566", "601577",
    "601588", "601601", "601658", "601665", "601688", "601766",
    "601816", "601838", "601939", "601988", "601997", "601998",
}

BANK_NAME_KEYWORDS = ["银行", "农商行", "农信社", "农村商业"]


def _code_to_secid(code):
    """将股票代码转换为东财 secid 格式（1.600519 / 0.000001）"""
    return f"1.{code}" if code.startswith("6") else f"0.{code}"


def fetch_main_board_stocks():
    """
    拉取主板股票（深市000+沪市600），约2000支。
    分批请求，每批200支。
    """
    all_stocks = []
    log.info("正在获取A股主板股票列表...")
    
    # 构建主板代码列表
    codes = []
    for i in range(1, 1000):
        codes.append(f"{i:06d}")
    for i in range(600000, 602000):
        codes.append(str(i))
    for i in range(603000, 604000):
        codes.append(str(i))
    for i in range(605000, 606000):
        codes.append(str(i))
    
    log.info("主板候选代码总数: %d", len(codes))
    
    batch_size = 200
    total_fetched = 0
    
    for batch_start in range(0, len(codes), batch_size):
        batch_codes = codes[batch_start:batch_start + batch_size]
        secids = [_code_to_secid(c) for c in batch_codes]
        
        params = {
            "fields": "f1,f2,f3,f4,f12,f13,f14,f15,f16,f17,f18,f20,f21",
            "secids": ",".join(secids),
        }
        
        try:
            r = requests.get(EM_HQ_URL, params=params, headers=EM_HEADERS, timeout=30)
            data = r.json()
            
            if data.get("data") and data["data"].get("diff"):
                for item in data["data"]["diff"]:
                    code = item.get("f12", "")
                    if not code:
                        continue
                    
                    # 精简字段：只保留核心数据
                    price = item.get("f2", 0) / 100.0 if item.get("f2") else 0
                    prev_close = item.get("f20", 0) / 100.0 if item.get("f20") else 0
                    
                    stock = {
                        "code": code,
                        "name": item.get("f14", ""),
                        "price": price,
                        "change_pct": round((price - prev_close) / prev_close * 100, 2) if prev_close > 0 else 0.0,
                        "volume": item.get("f4", 0),
                        "is_bank": (code in KNOWN_BANK_CODES or 
                                    any(kw in item.get("f14", "") for kw in BANK_NAME_KEYWORDS)),
                        "timestamp": datetime.now().isoformat(),
                    }
                    
                    all_stocks.append(stock)
                    total_fetched += 1
                
                log.info("批次完成: %d/%d", total_fetched, len(codes))
            else:
                log.warning("批次 %d-%d 无数据", batch_start, batch_start + batch_size)
            
            time.sleep(0.2)
            
        except Exception as e:
            log.warning("批次请求失败 (%d-%d): %s", batch_start, batch_start + batch_size, e)
            continue
    
    log.info("主板快照采集完成，共 %d 支股票", total_fetched)
    return all_stocks


def save_snapshot(stocks, filepath=None):
    """保存快照为 JSON 文件"""
    if filepath is None:
        filepath = SNAPSHOT_FILE
    
    filepath.parent.mkdir(parents=True, exist_ok=True)
    
    data = {
        "snapshot_time": datetime.now().isoformat(),
        "total_stocks": len(stocks),
        "stocks": stocks,
    }
    
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    
    log.info("快照已保存到: %s (%d 支)", filepath.name, len(stocks))
    return data


def load_previous_snapshot():
    """加载上一份快照用于增量对比"""
    if not SNAPSHOT_FILE.exists():
        log.info("没有历史快照，跳过增量对比")
        return None
    
    with open(SNAPSHOT_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def compute_diff(current, previous):
    """计算两份快照的差异"""
    if not previous:
        return {"diff_time": datetime.now().isoformat(), "changed": [], "stats": {"note": "首次快照"}}
    
    prev_map = {s["code"]: s for s in previous.get("stocks", [])}
    curr_map = {s["code"]: s for s in current.get("stocks", [])}
    
    changed = []
    for code, curr_stock in curr_map.items():
        if code in prev_map:
            prev_stock = prev_map[code]
            prev_price = prev_stock.get("price", 0)
            curr_price = curr_stock.get("price", 0)
            
            if prev_price > 0 and abs(curr_price - prev_price) > 0.001:
                price_change = round(curr_price - prev_price, 2)
                pct_change = round(price_change / prev_price * 100, 2)
                
                changed.append({
                    "code": code,
                    "name": curr_stock["name"],
                    "prev_price": prev_price,
                    "curr_price": curr_price,
                    "price_change": price_change,
                    "change_pct": pct_change,
                })
    
    diff_data = {
        "diff_time": datetime.now().isoformat(),
        "prev_snapshot_time": previous.get("snapshot_time", ""),
        "curr_snapshot_time": current.get("snapshot_time", ""),
        "total_changed": len(changed),
        "changed": changed[:200],
        "stats": {
            "total_stocks_current": current.get("total_stocks", 0),
            "total_stocks_previous": previous.get("total_stocks", 0),
            "changed_count": len(changed),
            "bank_stocks": sum(1 for s in curr_map.values() if s.get("is_bank")),
        },
    }
    
    log.info("增量对比完成: %d 支股票有价格变化", len(changed))
    return diff_data


def archive_snapshot(data):
    """将快照归档到日期子目录"""
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now().strftime("%Y%m%d")
    archive_file = ARCHIVE_DIR / f"snapshot_{date_str}.json"
    
    if not archive_file.exists():
        with open(archive_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        log.info("快照已归档: %s", archive_file.name)
    else:
        log.info("快照已存在，跳过归档: %s", archive_file.name)


def update_watchlist(high_score_stocks):
    """更新重点跟踪列表"""
    if not WATCHLIST_FILE.exists():
        watchlist = []
    else:
        with open(WATCHLIST_FILE, "r", encoding="utf-8") as f:
            watchlist_data = json.load(f)
            watchlist = watchlist_data.get("stocks", [])
    
    for stock in high_score_stocks:
        code = stock.get("code")
        if code and code not in [s["code"] for s in watchlist]:
            watchlist.append({
                "code": code,
                "name": stock.get("name", ""),
                "score": stock.get("score", 0),
                "date_added": datetime.now().isoformat(),
            })
    
    cutoff = (datetime.now() - timedelta(days=30)).isoformat()
    watchlist = [s for s in watchlist if s.get("date_added", "") >= cutoff]
    
    watchlist_data = {
        "updated_time": datetime.now().isoformat(),
        "total_tracked": len(watchlist),
        "stocks": watchlist,
    }
    
    with open(WATCHLIST_FILE, "w", encoding="utf-8") as f:
        json.dump(watchlist_data, f, ensure_ascii=False, indent=2)
    
    log.info("重点跟踪列表已更新: %d 支", len(watchlist))
    return watchlist_data


def main():
    """主入口"""
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
        with open(DIFF_FILE, "w", encoding="utf-8") as f:
            json.dump(diff_data, f, ensure_ascii=False, indent=2)
        log.info("差异分析已保存到: %s", DIFF_FILE.name)
    else:
        diff_data = {
            "diff_time": datetime.now().isoformat(),
            "changed": [],
            "stats": {"note": "首次快照"},
        }
        with open(DIFF_FILE, "w", encoding="utf-8") as f:
            json.dump(diff_data, f, ensure_ascii=False, indent=2)
    
    archive_snapshot(current_data)
    
    bank_count = sum(1 for s in stocks if s.get("is_bank"))
    log.info("=" * 50)
    log.info("采集摘要:")
    log.info("  总股票数: %d", len(stocks))
    log.info("  银行股数: %d", bank_count)
    log.info("  采集时间: %s", current_data["snapshot_time"])
    if previous_data:
        log.info("  价格变化: %d 支", diff_data.get("total_changed", 0))
    log.info("=" * 50)


if __name__ == "__main__":
    main()
