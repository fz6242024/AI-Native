# AI-Native

> A 股智能投研流水线：AlphaSift 全市场初选 + 19 个投研 Skills 深度打分 + 飞书卡片推送，基于 GitHub Actions 主从架构，三节点并行运行。

## 架构一览

本项目采用「主从架构 + 状态文件共享」，拆分运行时间并并行利用 3 个 GitHub 账户的免费算力：

| 节点 | 仓库 | 角色 |
| --- | --- | --- |
| 主节点 A | `fz6242024/AI-Native` | AlphaSift 初选、任务分流、评分回收、结果合并推送 |
| 从节点 B | `zf6242206/AI-Native` | 负责前 5 支股票的深度打分 |
| 从节点 C | `fz6242025/AI-Native` | 负责后 5 支股票的深度打分 |

流程：

1. **主节点**每个工作日 09:00 / 12:30（北京时间）定时触发：AlphaSift 从全市场筛出约 10 支初选股票（`top_10_stocks.json`）。
2. **分流**：前 5 支派发给从节点 B，后 5 支派发给从节点 C（通过 `workflow_dispatch` 跨仓库触发）。
3. **深度评分**：从节点并行调用 19 个投研 Skills 打分（A股深度研究、财报分析、行业筛选、持仓管理、思维工具五大类）。
4. **评分回收**：主节点通过 `scripts/collect_skill_scores.py` 回收 B/C 的评分结果，生成题材总结。
5. **推送**：汇总结果通过飞书富文本卡片推送到群聊。

## 核心工作流

| 文件 | 说明 |
| --- | --- |
| `.github/workflows/alphasift_main.yml` | 主节点：定时初选、向 B/C 分流、评分回收、题材总结、飞书主推送 |
| `.github/workflows/skills_runner.yml` | 从节点：接收股票列表、执行 19 Skills 评分、飞书推送本节点结果并提交评分历史 |
| `.github/workflows/daily_snapshot.yml` | 每日快照维护 `data/snapshot_latest.json` |
| `.github/workflows/llm_diag.yml` | LLM 配置/连通性诊断 |

## 关键脚本

| 文件 | 说明 |
| --- | --- |
| `scripts/alphasift_discovery.py` | AlphaSift 初选主脚本 |
| `scripts/run_skills_evaluation.py` | 19 个 Skills 的深度打分执行 |
| `scripts/collect_skill_scores.py` | 回收 B/C 从节点的评分 |
| `scripts/feishu_notifier.py` | 飞书富文本卡片推送 |
| `scripts/theme_summary.py` | 短线热点题材总结 |
| `scripts/snapshot_collector.py` | 快照采集 |
| `scripts/llm_config.py` | LLM 配置管理 |
| `scripts/apply_alphasift_patch.py` | 对已安装的 alphasift 包打 JSON 容错补丁（本地源码改动在 CI 中不生效，故用此覆盖方式） |

## 关键设计与注意事项

- **19 个 Skills 分工**：A 股深度研究（investment-research、investment-team、management-deep-dive、private-company-research、deep-company-series）、财报分析（earnings-review、earnings-team）、行业筛选（industry-research、industry-funnel、quality-screen、bottleneck-hunter、investment-checklist）、持仓管理（portfolio-review、thesis-tracker、thesis-drift、news-pulse）、思维工具（dyp-ask、financial-data、wechat-article、investment-memo-craft）。
- **评分纪律**：先一票否决校验，再百分制打分；输出严格 JSON（`stock_code` / `skill_executed` / `score` / `judgment`（30 字内）/ `core_risk`）。
- **LLM 输出预算**：`LLM_MAX_CANDIDATES=8`、`LLM_MAX_TOKENS=4096`、`LLM_TIMEOUT_SEC=120`。经验：30 候选 × 15 字段输出需求超过 8192 tokens，2048 tokens 必然被 `finish_reason=length` 截断导致 JSON 不完整；8 候选 + 4096 tokens 可稳定产出合法 JSON；单次响应实测 33–46s，超时放宽到 120s。具体事故与参数依据见 `alphasift_main.yml` 中的注释说明。
- **alphasift 补丁**：上游 `ranker.py` 无法容错 LLM 返回的畸形 JSON（截断/缺冒号/裸换行），每次 CI 安装后由 `apply_alphasift_patch.py` 覆盖已安装文件；上游合入同等修复后可删除该步骤与 `vendor/alphasift/`。
- **提交评分历史只提交 `data/skill_scores_history.json`**：不要把 `analysis_result.json` 加入 `git add`（运行时产物，在 `.gitignore` 中），否则整条命令 exit 1 并连带评分历史漏提交。
- **飞书通知不阻塞**：评分历史提交步骤设为 `continue-on-error`，推送失败不影响后续步骤。

## 本地开发

```bash
pip install -r requirements.txt
pip install git+https://github.com/ZhuLinsen/alphasift.git@main

# 主节点初选
python scripts/alphasift_discovery.py --output top_10_stocks.json

# 从节点深度评分（示例）
python scripts/run_skills_evaluation.py --stocks '["600519","000858"]'

# 回收 B/C 评分
python scripts/collect_skill_scores.py --repo-b zf6242206/AI-Native --repo-c fz6242025/AI-Native \
  --pat-b <PAT_B> --pat-c <PAT_C>

# 测试
python -m pytest tests/
```

依赖见 `requirements.txt`：`requests`、`python-dotenv`、`openai`、`akshare`（Python 3.10）。

## 配置（仓库 Secrets）

| 变量 | 说明 |
| --- | --- |
| `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | LLM 接口与模型（优先；没有时回退到 `OPENAI_API_KEY`） |
| `OPENAI_API_KEY` | OpenAI 兼容接口密钥（AlphaSift 与 LLM 排序） |
| `FEISHU_WEBHOOK` / `FEISHU_WEBHOOK_MAIN` | 飞书群机器人 Webhook |
| `GH_PAT_ACCOUNT_B` / `GH_PAT_ACCOUNT_C` | 用于跨仓库触发与回收 B/C 结果的 Personal Access Token |

---

设计与架构细节见 [docs/design.md](docs/design.md)。
