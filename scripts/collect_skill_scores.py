# ============================================================
# collect_skill_scores.py
# 主节点收集 B/C 账户的 Skills 深度评分结果
# 轮询 B/C 的 skills_runner workflow 直到完成，下载评分历史并合并到主仓库
# ============================================================

import argparse
import json
import logging
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com"
WORKFLOW_NAME = "skills_runner.yml"


def _request(url: str, token: str, method: str = "GET", payload: dict | None = None):
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "AlphaSift-collector",
    }
    if token:
        headers["Authorization"] = f"token {token}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read().decode("utf-8")
        return resp.status, json.loads(body) if body else None


def wait_for_workflow(repo: str, token: str, timeout_minutes: int = 25) -> int | None:
    """等待 repo 最近一次 workflow_dispatch 的 run 完成，返回 run id。"""
    url = f"{API}/repos/{repo}/actions/runs?event=workflow_dispatch&per_page=20"
    deadline = time.time() + timeout_minutes * 60
    while time.time() < deadline:
        try:
            _, data = _request(url, token)
            runs = data.get("workflow_runs", []) if data else []
            for run in runs:
                if run.get("path") == f".github/workflows/{WORKFLOW_NAME}" or WORKFLOW_NAME in run.get("path", ""):
                    status = run.get("status")
                    log.info("  %s run %s 状态: %s", repo, run["id"], status)
                    if status == "completed":
                        return run["id"]
                    if status in ("queued", "in_progress"):
                        break
            # 没有进行中的 run 时稍等再查
        except Exception as e:
            log.warning("  查询 %s workflow 状态失败: %s", repo, e)
        time.sleep(15)
    log.warning("%s workflow 超时未完成", repo)
    return None


def download_history_file(repo: str, token: str = "", branch: str = "master") -> list | None:
    """从仓库下载评分历史文件：优先 API 带 token（私有仓库），再试 raw（公开仓库）。"""
    if token:
        url = f"{API}/repos/{repo}/contents/data/skill_scores_history.json?ref={branch}"
        try:
            status, data = _request(url, token)
            if status == 200 and data and data.get("content"):
                import base64
                content = base64.b64decode(data["content"]).decode("utf-8")
                return json.loads(content)
        except Exception as e:
            log.warning("  API 下载 %s 评分历史失败: %s", repo, e)
    url = f"{RAW}/{repo}/{branch}/data/skill_scores_history.json"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "AlphaSift-collector"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        log.warning("  下载 %s 评分历史失败: %s", repo, e)
        return None


def merge_and_save(account: str, history: list | None, out_dir: Path) -> None:
    """把某个账户的评分历史合并写入主仓库 data/skill_history/。"""
    if not history:
        log.info("  %s 无评分历史，跳过", account)
        return
    if not isinstance(history, list):
        log.warning("  %s 评分历史格式异常，跳过", account)
        return
    out_dir.mkdir(parents=True, exist_ok=True)
    for entry in history:
        date_str = entry.get("date", "unknown")
        results = entry.get("results", [])
        if not results:
            continue
        out_file = out_dir / f"{date_str}_{account}.json"
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump({"date": date_str, "account": account, "results": results}, f, ensure_ascii=False, indent=2)
        log.info("  已保存 %s 的 %s 评分: %d 支股票", account, date_str, len(results))


def main() -> None:
    parser = argparse.ArgumentParser(description="收集 B/C 账户的 Skills 深度评分结果")
    parser.add_argument("--repo-b", required=True, help="账户 B 仓库，如 zf6242206/AI-Native")
    parser.add_argument("--repo-c", required=True, help="账户 C 仓库，如 fz6242025/AI-Native")
    parser.add_argument("--pat-b", default="", help="账户 B 的 PAT（可选，公开仓库可读）")
    parser.add_argument("--pat-c", default="", help="账户 C 的 PAT（可选，公开仓库可读）")
    args = parser.parse_args()

    out_dir = Path(__file__).resolve().parent.parent / "data" / "skill_history"
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    log.info("开始收集 B/C 账户评分结果 -> %s", out_dir)

    for account, repo, pat in (("B", args.repo_b, args.pat_b), ("C", args.repo_c, args.pat_c)):
        log.info("等待 %s (%s) 评分完成...", account, repo)
        run_id = wait_for_workflow(repo, pat)
        if run_id is None:
            continue
        log.info("  %s 已完成 (run %s)，下载评分历史", account, run_id)
        history = download_history_file(repo, pat)
        merge_and_save(account, history, out_dir)

    log.info("收集完成，目录: %s", out_dir)


if __name__ == "__main__":
    main()
