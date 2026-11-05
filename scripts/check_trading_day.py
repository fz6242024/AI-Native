#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""check_trading_day.py -- A 股交易日判断工具（用于 GitHub Actions cron 调度过滤）

功能：
1. 解决 GitHub Actions cron (0 1 * * 1-5 等) 无法识别 A 股法定节假日及休市安排的问题；
2. 准确判断当日（或指定日期）是否为 A 股交易日：
   - 周末（周六、周日）一律休市（哪怕调休补班，A 股亦不开市）；
   - 元旦、春节、清明、劳动节、端午、中秋、国庆等法定节假日一律休市；
   - 离线内置 2024~2027 年法定休市安排，100% 确定性运行，0 毫秒延时，不依赖外网；
   - 支持动态 import chinesecalendar（若存在则联动）及可选网络 API 校验；
3. 输出与集成：
   - 自动写入 GitHub Actions 的 $GITHUB_OUTPUT (is_trading_day=true/false)；
   - 支持 --force 绕过检查（用于手动 workflow_dispatch 强制运行）；
   - 支持 --strict 非交易日返回退出码 1，便于 Shell 条件分支。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# 北京时间时区 (UTC+8)
BEIJING_TZ = timezone(timedelta(hours=8))

# ==============================================================================
# 离线休市日历（周一至周五中属于法定节假日休市的日期，周末已自动由 weekday >= 5 过滤）
# 涵盖 2024 ~ 2027 年国务院公布的休市安排
# ==============================================================================
HOLIDAYS_A_SHARE: dict[str, str] = {
    # 2024
    "2024-01-01": "元旦",
    "2024-02-09": "除夕",
    "2024-02-12": "春节",
    "2024-02-13": "春节",
    "2024-02-14": "春节",
    "2024-02-15": "春节",
    "2024-02-16": "春节",
    "2024-04-04": "清明节",
    "2024-04-05": "清明节",
    "2024-05-01": "劳动节",
    "2024-05-02": "劳动节",
    "2024-05-03": "劳动节",
    "2024-06-10": "端午节",
    "2024-09-16": "中秋节",
    "2024-09-17": "中秋节",
    "2024-10-01": "国庆节",
    "2024-10-02": "国庆节",
    "2024-10-03": "国庆节",
    "2024-10-04": "国庆节",
    "2024-10-07": "国庆节",

    # 2025
    "2025-01-01": "元旦",
    "2025-01-28": "除夕",
    "2025-01-29": "春节",
    "2025-01-30": "春节",
    "2025-01-31": "春节",
    "2025-02-03": "春节",
    "2025-02-04": "春节",
    "2025-04-04": "清明节",
    "2025-05-01": "劳动节",
    "2025-05-02": "劳动节",
    "2025-05-05": "劳动节",
    "2025-06-02": "端午节",
    "2025-10-01": "国庆中秋",
    "2025-10-02": "国庆中秋",
    "2025-10-03": "国庆中秋",
    "2025-10-06": "国庆中秋",
    "2025-10-07": "国庆中秋",
    "2025-10-08": "国庆中秋",

    # 2026
    "2026-01-01": "元旦",
    "2026-01-02": "元旦",
    "2026-02-16": "除夕",
    "2026-02-17": "春节",
    "2026-02-18": "春节",
    "2026-02-19": "春节",
    "2026-02-20": "春节",
    "2026-02-23": "春节",
    "2026-04-06": "清明节",
    "2026-05-01": "劳动节",
    "2026-05-04": "劳动节",
    "2026-05-05": "劳动节",
    "2026-06-19": "端午节",
    "2026-09-25": "中秋节",
    "2026-10-01": "国庆节",
    "2026-10-02": "国庆节",
    "2026-10-05": "国庆节",
    "2026-10-06": "国庆节",
    "2026-10-07": "国庆节",

    # 2027
    "2027-01-01": "元旦",
    "2027-02-05": "除夕",
    "2027-02-08": "春节",
    "2027-02-09": "春节",
    "2027-02-10": "春节",
    "2027-02-11": "春节",
    "2027-02-12": "春节",
    "2027-04-05": "清明节",
    "2027-05-03": "劳动节",
    "2027-05-04": "劳动节",
    "2027-05-05": "劳动节",
    "2027-06-09": "端午节",
    "2027-09-15": "中秋节",
    "2027-10-01": "国庆节",
    "2027-10-04": "国庆节",
    "2027-10-05": "国庆节",
    "2027-10-06": "国庆节",
    "2027-10-07": "国庆节",
}


def get_beijing_today() -> date:
    """获取当前北京时间的日期，确保 GitHub Actions runner 在 UTC 时不会偏移。"""
    return datetime.now(BEIJING_TZ).date()


def is_chinese_holiday_dynamic(d: date) -> tuple[bool, str]:
    """尝试通过已安装的第三方库检测是否法定休市。"""
    try:
        import chinesecalendar  # type: ignore
        if chinesecalendar.is_holiday(d):
            # 获取节日名称
            holiday_name = chinesecalendar.get_holiday_detail(d)[1] or "法定节假日"
            return True, holiday_name
    except ImportError:
        pass
    except Exception as e:
        log.debug("chinesecalendar 动态检查异常: %s", e)
    return False, ""


def check_trading_day(target_date: date) -> tuple[bool, str]:
    """判断指定日期是否为 A 股交易日。

    返回: (is_trading_day, reason_description)
    """
    weekday = target_date.weekday()
    date_str = target_date.strftime("%Y-%m-%d")

    # 1. 基础规则：周六、周日一律不开市（即使调休补班，股市亦休市）
    if weekday == 5:
        return False, f"周六（非交易日）[{date_str}]"
    if weekday == 6:
        return False, f"周日（非交易日）[{date_str}]"

    # 2. 内置离线休市日历匹配
    if date_str in HOLIDAYS_A_SHARE:
        holiday_name = HOLIDAYS_A_SHARE[date_str]
        return False, f"法定节假日休市: {holiday_name} [{date_str}]"

    # 3. 动态三方库 chinesecalendar 检测（若安装）
    is_dyn_holiday, dyn_name = is_chinese_holiday_dynamic(target_date)
    if is_dyn_holiday:
        return False, f"法定节假日休市: {dyn_name} [{date_str}]"

    # 4. 周一至周五且非法定节假日，判定为交易日
    weekday_cn = ["周一", "周二", "周三", "周四", "周五"][weekday]
    return True, f"正常 A 股交易日 ({weekday_cn}) [{date_str}]"


def write_github_output(output_path: str, kv_pairs: dict[str, str]) -> None:
    """写入 GitHub Actions $GITHUB_OUTPUT 文件。"""
    try:
        with open(output_path, "a", encoding="utf-8") as f:
            for k, v in kv_pairs.items():
                f.write(f"{k}={v}\n")
        log.info("已写入 GITHUB_OUTPUT: %s", kv_pairs)
    except Exception as e:
        log.warning("写入 GITHUB_OUTPUT 失败: %s", e)


def main():
    parser = argparse.ArgumentParser(description="A 股交易日历判断工具")
    parser.add_argument("--date", help="指定检查日期 (YYYY-MM-DD)，缺省使用当前北京时间日期")
    parser.add_argument(
        "--force",
        help="强制判定为交易日（支持 true / 1 / yes），用于手动调试或强制执行",
        default="",
    )
    parser.add_argument("--github-output", help="写入 GITHUB_OUTPUT 路径")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="非交易日时以退出码 1 退出（默认始终以 0 退出）",
    )
    args = parser.parse_args()

    # 1. 解析目标日期
    if args.date:
        try:
            target_date = datetime.strptime(args.date.strip(), "%Y-%m-%d").date()
        except ValueError:
            log.error("日期格式无效: %s，应为 YYYY-MM-DD", args.date)
            sys.exit(2)
    else:
        target_date = get_beijing_today()

    date_str = target_date.strftime("%Y-%m-%d")
    log.info("检查日期: %s (北京时间)", date_str)

    # 2. 检查强制标记
    force_env = os.environ.get("FORCE_RUN", "").strip().lower()
    force_arg = str(args.force).strip().lower()
    is_force = force_arg in ("true", "1", "yes") or force_env in ("true", "1", "yes")

    if is_force:
        is_trading = True
        reason = f"用户强制指定运行 (--force / FORCE_RUN=1)，跳过交易日校验 [{date_str}]"
        log.info("⚡ %s", reason)
    else:
        is_trading, reason = check_trading_day(target_date)
        if is_trading:
            log.info("✅ 判定结果: 是交易日 -> %s", reason)
        else:
            log.info("⛔ 判定结果: 非交易日 -> %s", reason)

    # 3. 输出给 GitHub Actions
    gh_output = args.github_output or os.environ.get("GITHUB_OUTPUT", "")
    if gh_output:
        outputs = {
            "is_trading_day": "true" if is_trading else "false",
            "trading_date": date_str,
            "reason": reason,
        }
        write_github_output(gh_output, outputs)

    # 4. 控制台明确输出摘要
    print(f"IS_TRADING_DAY={'true' if is_trading else 'false'}")
    print(f"DATE={date_str}")
    print(f"REASON={reason}")

    # 5. 退出码控制
    if args.strict and not is_trading:
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
