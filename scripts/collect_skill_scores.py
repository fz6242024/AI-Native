# ============================================================
# collect_skill_scores.py
# 主节点收集 B/C 账户的 Skills 深度评分结果
# 轮询 B/C 的 skills_runner workflow 直到完成，下载评分历史并合并到主仓库
# ============================================================

import argparse
import json
import logging
import os
import time
import urllib.request
import urllib.error
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
HISTORY_PATH = "data/skill_scores_history.json"
# 候选分支：先探测仓库默认分支，再依次兜底这些。
# 此前硬编码 master，B/C 若用 main 就会永远 404 且无从察觉。
DEFAULT_BRANCH_CANDIDATES = ("master", "main")


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
    """等待 repo 最近一次 workflow_dispatch 的 run 完成，返回 run id。

    只有 conclusion 为 success 才返回 run id。此前只判 status=="completed"，
    导致 B/C 的工作流**跑挂**也被当成"已完成"，随后下载评分历史 404 被
    merge_and_save 静默跳过 —— 整条历史链路失效却没有任何告警。
    """
    url = f"{API}/repos/{repo}/actions/runs?event=workflow_dispatch&per_page=20"
    deadline = time.time() + timeout_minutes * 60
    while time.time() < deadline:
        try:
            _, data = _request(url, token)
            runs = data.get("workflow_runs", []) if data else []
            for run in runs:
                if run.get("path") == f".github/workflows/{WORKFLOW_NAME}" or WORKFLOW_NAME in run.get("path", ""):
                    status = run.get("status")
                    conclusion = run.get("conclusion")
                    log.info("  %s run %s 状态: %s / conclusion: %s", repo, run["id"], status, conclusion)
                    if status == "completed":
                        if conclusion == "success":
                            return run["id"]
                        log.error(
                            "  %s run %s 已结束但 conclusion=%s，不会产出评分历史",
                            repo, run["id"], conclusion,
                        )
                        print(
                            f"::warning::{repo} 的 {WORKFLOW_NAME} 最近一次运行结论为 "
                            f"{conclusion}，评分历史缺失，该账户本次不参与历史加权"
                        )
                        return None
                    if status in ("queued", "in_progress"):
                        break
            # 没有进行中的 run 时稍等再查
        except Exception as e:
            log.warning("  查询 %s workflow 状态失败: %s", repo, e)
        time.sleep(15)
    log.warning("%s workflow 超时未完成", repo)
    print(f"::warning::{repo} 的 {WORKFLOW_NAME} 等待 {timeout_minutes} 分钟仍未完成")
    return None


def _repo_default_branch(repo: str, token: str) -> str | None:
    """查询仓库默认分支，失败返回 None（不致命，还有候选分支兜底）。"""
    try:
        _, data = _request(f"{API}/repos/{repo}", token)
        branch = (data or {}).get("default_branch")
        if branch:
            log.info("  %s 默认分支: %s", repo, branch)
            return branch
    except Exception as e:
        log.warning("  查询 %s 默认分支失败: %s", repo, e)
    return None


def _fetch_history_from_branch(repo: str, token: str, branch: str) -> list | None:
    """从指定分支取评分历史。返回 None 表示该分支没有/读不到（继续试下一个分支）。"""
    api_url = f"{API}/repos/{repo}/contents/{HISTORY_PATH}?ref={branch}"
    if token:
        try:
            status, data = _request(api_url, token)
            if status == 200 and data and data.get("content"):
                import base64
                log.info("  已从 %s@%s 取到评分历史（API）", repo, branch)
                return json.loads(base64.b64decode(data["content"]).decode("utf-8"))
        except urllib.error.HTTPError as e:
            log.info("  %s@%s 无评分历史（API HTTP %s）", repo, branch, e.code)
        except Exception as e:
            log.warning("  %s@%s API 下载失败: %s", repo, branch, e)

    raw_url = f"{RAW}/{repo}/{branch}/{HISTORY_PATH}"
    try:
        req = urllib.request.Request(raw_url, headers={"User-Agent": "AlphaSift-collector"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            log.info("  已从 %s@%s 取到评分历史（raw）", repo, branch)
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        log.info("  %s@%s 无评分历史（raw HTTP %s）", repo, branch, e.code)
    except Exception as e:
        log.warning("  %s@%s raw 下载失败: %s", repo, branch, e)
    return None


def download_history_file(repo: str, token: str = "", branches: tuple[str, ...] = ()) -> list | None:
    """按候选分支顺序下载评分历史，返回首个成功的结果。

    此前硬编码 branch="master"，B/C 若用 main 就永远 404 且无从察觉。
    现在顺序为：显式指定分支 -> 仓库 API 探测的默认分支 -> master -> main，
    全部失败时打印尝试过的分支与最终 URL，便于定位。
    """
    candidates: list[str] = [b for b in branches if b]
    default_branch = _repo_default_branch(repo, token)
    if default_branch:
        candidates.append(default_branch)
    candidates.extend(DEFAULT_BRANCH_CANDIDATES)

    ordered: list[str] = []
    for b in candidates:
        if b not in ordered:
            ordered.append(b)

    for branch in ordered:
        history = _fetch_history_from_branch(repo, token, branch)
        if history is not None:
            return history

    log.error(
        "  %s 所有候选分支均未取到 %s（尝试过: %s）",
        repo, HISTORY_PATH, ", ".join(ordered),
    )
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
    parser.add_argument(
        "--branch",
        default=os.environ.get("SKILL_HISTORY_BRANCH", ""),
        help="B/C 评分历史所在分支（留空则自动探测默认分支并回退 master/main）",
    )
    args = parser.parse_args()

    out_dir = Path(__file__).resolve().parent.parent / "data" / "skill_history"
    log.info("开始收集 B/C 账户评分结果 -> %s", out_dir)

    branches = (args.branch,) if args.branch else ()
    if branches:
        log.info("指定分支: %s", args.branch)

    collected = 0
    for account, repo, pat in (("B", args.repo_b, args.pat_b), ("C", args.repo_c, args.pat_c)):
        log.info("等待 %s (%s) 评分完成...", account, repo)
        run_id = wait_for_workflow(repo, pat)
        if run_id is None:
            continue
        log.info("  %s 已完成 (run %s)，下载评分历史", account, run_id)
        history = download_history_file(repo, pat, branches=branches)
        if history is None:
            print(f"::warning::{account} ({repo}) 未取到评分历史，该账户本次不参与历史加权")
            continue
        merge_and_save(account, history, out_dir)
        collected += 1

    has_files = out_dir.is_dir() and any(out_dir.glob("*.json"))
    if not has_files:
        print(
            "::warning::未收集到任何评分历史，data/skill_history 为空，本次初选不会应用历史加权"
            f"（请检查 B/C 的 {WORKFLOW_NAME} 是否成功、{HISTORY_PATH} 是否已提交）"
        )
    log.info("收集完成，成功账户 %d 个，目录: %s", collected, out_dir)


if __name__ == "__main__":
    main()
