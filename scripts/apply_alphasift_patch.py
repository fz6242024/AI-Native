#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 AI-Native 内置的 alphasift 补丁覆盖到 pip 安装的包上。

## 为什么需要这个脚本

生产工作流 `.github/workflows/alphasift_main.yml` 通过

    pip install git+https://github.com/ZhuLinsen/alphasift.git@main

安装上游 alphasift。而 `scripts/alphasift_discovery.py` 的
`_ensure_alphasift_available()` **优先 import 已安装的包**，本地目录只是
fallback。因此直接改本地 alphasift 源码在 CI 里不生效，必须覆盖已安装文件。

## 补了什么

上游 `alphasift/ranker.py` 无法容错 LLM 返回的三种畸形 JSON，导致线上
10 个策略里 9 个 LLM 排序失败、回落 screen_score、白烧 850s：

1. **截断** —— `max_tokens` 打满后在 `ranked[]` 中间切断，外层 `{` 永不闭合；
   `_balanced_json_values` 只回收顶层平衡片段，此时返回空列表，导致
   `_extract_partial_ranking_array` 拿不到任何 item，抛 `no_json_found`。
2. **缺冒号** —— `"catalysts":[...]` 被写成 `"catalysts [...]`，一次响应 121 处。
   注意它同时丢了闭合引号，会让引号奇偶翻转、吞掉对象的闭合 `}`，所以
   冒号归一化必须在**扫描之前**做。
3. **裸换行** —— 字符串值内含字面换行，严格 JSON 报 `Invalid control character`。

诊断报告：`deliverables/gstack/debug-llm-ranking-failure-2026-09-23.md`
补丁来源：alphasift 本地仓库提交 `f78f3e7`
（`alphasift/ranker.py` + `tests/test_ranker_json_repair.py` 33 例）

## 何时可以删掉

上游合入同等修复后，删除 `vendor/alphasift/`、本脚本，以及工作流里的
"应用 alphasift JSON 容错补丁" 步骤即可。

## 用法

    python scripts/apply_alphasift_patch.py          # 覆盖并自检
    python scripts/apply_alphasift_patch.py --check  # 只检查，不改动
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VENDORED_RANKER = REPO_ROOT / "vendor" / "alphasift" / "ranker.py"

# 补丁必须包含的标记，缺任一即说明 vendor 文件被改坏或退化
REQUIRED_MARKERS = (
    "def _repair_missing_colons",
    "include_nested",
    "strict=False",
)

# 上游若新增顶层定义而我们的副本没有，说明副本已过期、继续覆盖会回退上游功能
ALLOW_DRIFT_ENV = "ALPHASIFT_PATCH_ALLOW_DRIFT"


def _fail(message: str) -> int:
    print(f"::error::{message}")
    return 1


def _top_level_names(source: str) -> set[str]:
    return set(re.findall(r"^(?:def|class)\s+(\w+)", source, re.MULTILINE))


def _locate_installed_ranker() -> Path | None:
    try:
        import alphasift
    except ImportError as exc:  # pragma: no cover - 取决于 CI 环境
        print(f"::error::alphasift 未安装: {exc}")
        return None

    package_dir = Path(alphasift.__file__).resolve().parent
    target = package_dir / "ranker.py"
    if not target.is_file():
        print(f"::error::已安装的 alphasift 中未找到 ranker.py: {target}")
        return None
    return target


def _self_check(allow_drift: bool) -> int:
    """在子进程里重新导入，确认补丁真的生效（避免路径/缓存导致静默失效）。"""
    probe = (
        "import inspect\n"
        "from alphasift.ranker import _balanced_json_values, _repair_missing_colons\n"
        "params = inspect.signature(_balanced_json_values).parameters\n"
        "assert 'include_nested' in params, params\n"
        "assert callable(_repair_missing_colons)\n"
        "print('patch-ok')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    if result.returncode != 0 or "patch-ok" not in result.stdout:
        print("::error::补丁自检失败，覆盖后仍无法使用新接口")
        print(f"  stdout: {result.stdout.strip()}")
        print(f"  stderr: {result.stderr.strip()}")
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="应用内置的 alphasift JSON 容错补丁")
    parser.add_argument("--check", action="store_true", help="只检查，不修改文件")
    args = parser.parse_args()

    if not VENDORED_RANKER.is_file():
        return _fail(f"补丁文件不存在: {VENDORED_RANKER}")

    vendored_source = VENDORED_RANKER.read_text(encoding="utf-8")
    missing = [m for m in REQUIRED_MARKERS if m not in vendored_source]
    if missing:
        return _fail(f"补丁文件缺少预期标记 {missing}，拒绝覆盖")

    target = _locate_installed_ranker()
    if target is None:
        return 1

    upstream_source = target.read_text(encoding="utf-8")

    # 上游漂移检测：上游新增了我们副本没有的顶层定义 -> 覆盖会回退上游功能
    allow_drift = os.environ.get(ALLOW_DRIFT_ENV, "").strip() in {"1", "true", "yes"}
    added_upstream = _top_level_names(upstream_source) - _top_level_names(vendored_source)
    if added_upstream and not allow_drift:
        return _fail(
            "检测到上游 ranker.py 已新增以下定义，内置副本已过期，"
            f"继续覆盖会回退上游功能: {sorted(added_upstream)}。"
            f"请重新生成 vendor/alphasift/ranker.py（见本脚本顶部说明）；"
            f"确需强行覆盖可设 {ALLOW_DRIFT_ENV}=1"
        )

    if vendored_source == upstream_source:
        print("ℹ️ 上游已包含同等修复，无需覆盖")
        return _self_check(allow_drift)

    if args.check:
        print(f"ℹ️ 需要覆盖（--check 模式，未改动）: {target}")
        return 0

    shutil.copyfile(VENDORED_RANKER, target)

    applied = target.read_text(encoding="utf-8")
    still_missing = [m for m in REQUIRED_MARKERS if m not in applied]
    if still_missing:
        return _fail(f"覆盖后目标文件仍缺少标记 {still_missing}")

    print(f"✅ 已应用 alphasift JSON 容错补丁 -> {target}")
    return _self_check(allow_drift)


if __name__ == "__main__":
    sys.exit(main())
