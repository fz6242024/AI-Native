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


def build_card_message(results: list) -> dict:
    """构建飞书富文本卡片消息。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    stock_count = len(results)

    # 卡片主体配置
    card = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": f"\U0001f916 AI投研多源流矩阵诊断报告 ({now})",
                },
                "template": "violet",
            },
            "elements": [],
        },
    }

    elements = card["card"]["elements"]

    # 纯文本摘要块（放在最前面，满足关键词校验）
    stock_codes = ", ".join(r["stock_code"] for r in results)
    elements.append({
        "tag": "div",
        "text": {
            "tag": "lark_md",
            "content": f"本次股票分析共 {stock_count} 支：{stock_codes}",
        },
    })

    for r in results:
        stock_code = r["stock_code"]
        stock_name = r["stock_name"]
        final_score = r["final_score"]
        highlights = r.get("highlights", [])
        risks = r.get("risks", [])

        if final_score >= 80:
            score_color = "green"
        elif final_score >= 70:
            score_color = "yellow"
        else:
            score_color = "red"

        elements.append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": (
                    f"**{stock_name}（{stock_code}）**\n"
                    f"综合评分：**{final_score}** 分 [{score_color}]\n"
                    f"\n"
                    f"**亮点：**\n"
                    + "\n".join(f"- {h}" for h in highlights) + "\n"
                    f"\n"
                    f"**风险：**\n"
                    + "\n".join(f"- {risk}" for risk in risks)
                ),
            },
        })
        elements.append({"tag": "hr"})

    elements.append({
        "tag": "note",
        "elements": [
            {
                "tag": "plain_text",
                "content": "数据基于 AlphaSift 初选 + 19个深度投研Skills打分流程",
            }
        ],
    })

    return card


def build_main_card(stocks: str, first5: str, last5: str, code_b: str, code_c: str) -> dict:
    """构建主节点通知卡片（初选结果 + 触发状态）。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    status_b = "成功" if code_b in ("201", "204") else f"失败 ({code_b})"
    status_c = "成功" if code_c in ("201", "204") else f"失败 ({code_c})"

    card = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": f"\U0001f4cb AlphaSift 初选完成 ({now})",
                },
                "template": "blue",
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": (
                            f"本次股票初选共 {len(stocks.split(chr(44)))} 支\n"
                            f"**初选股票:** {stocks}\n"
                            f"\n"
                            f"**前 5 支 -> 账户 B:** {first5}\n"
                            f"**后 5 支 -> 账户 C:** {last5}\n"
                            f"\n"
                            f"**触发状态:**\n"
                            f"- 账户 B: {status_b}\n"
                            f"- 账户 C: {status_c}"
                        ),
                    },
                },
                {"tag": "hr"},
                {
                    "tag": "note",
                    "elements": [
                        {"tag": "plain_text", "content": "数据基于 AlphaSift 初选 + 19个深度投研Skills打分流程"}
                    ],
                },
            ],
        },
    }
    return card


def send_to_feishu(card_data: dict, webhook_url: str = "") -> bool:
    """将卡片消息发送到飞书群聊。"""
    url = webhook_url or FEISHU_WEBHOOK_MAIN

    if not url:
        log.warning("未设置 FEISHU_WEBHOOK 环境变量，跳过实际发送")
        log.info("以下为将要发送的消息内容:\n%s", json.dumps(card_data, ensure_ascii=False, indent=2))
        return False

    try:
        resp = requests.post(
            url,
            json=card_data,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        resp.raise_for_status()
        result = resp.json()
        if result.get("code", -1) == 0:
            log.info("飞书消息发送成功!")
            return True
        else:
            log.error("飞书返回错误: code=%d, msg=%s", result.get("code"), result.get("msg"))
            return False
    except requests.exceptions.RequestException as e:
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
        choices=["default", "main"],
        default="default",
        help="运行模式：default=推送评分结果，main=主节点通知（默认: default）",
    )
    parser.add_argument("--stocks", default="", help="初选股票列表（主节点模式）")
    parser.add_argument("--first5", default="", help="前5支股票（主节点模式）")
    parser.add_argument("--last5", default="", help="后5支股票（主节点模式）")
    parser.add_argument("--trigger-code-b", default="", help="账户 B 触发 HTTP 状态码")
    parser.add_argument("--trigger-code-c", default="", help="账户 C 触发 HTTP 状态码")
    args = parser.parse_args()

    if args.mode == "main":
        if not args.stocks:
            log.error("主节点模式需要提供 --stocks 参数")
            sys.exit(1)

        card_data = build_main_card(args.stocks, args.first5, args.last5, args.trigger_code_b, args.trigger_code_c)
        log.info("主节点飞书通知预览:\n%s", json.dumps(card_data, ensure_ascii=False, indent=2))
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