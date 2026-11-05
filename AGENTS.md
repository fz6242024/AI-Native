# AlphaSift 投研自动化项目 — Agent 指南

## 项目概述
这是 AlphaSift 的调度与测试仓库。主流程在 GitHub Actions 中运行：
- **账户 A（主节点）**：定时扫描全市场，初选约 10 支股票，写入 `top_10_stocks.json`
- **账户 B & C（从节点）**：各接 5 支股票，并行调用 19 个投研 Skills 打分
- **飞书推送**：结果汇总后通过 Webhook 发到群聊

本仓库存放的是调度脚本、测试工具和系统提示词模板。实际 Skills 代码在 `C:\Users\zf\.codex\skills`。

## 文件结构速览
| 文件 | 用途 |
|------|------|
| `README.md` | 完整架构说明，包含工作流 YAML 和飞书推送脚本 |
| `scripts/` | 辅助脚本目录 |
| `*.py` | 测试、调试、验证脚本（见下方） |
| `.env` / `.env.example` | 环境变量配置，敏感信息放 `.env` |
| `requirements.txt` | Python 依赖清单 |

## 测试脚本分类
开发时按场景挑选脚本，不要一次性全跑：

**API 与密钥检查**
- `check_pat.py` — 检查 GitHub PAT 是否有效
- `check_runner.py` — 检查 Actions Runner 状态
- `check_secret_meta.py` — 检查 Secrets 元数据
- `inspect_token.py` — 查看 Token 详情
- `test_secrets.py` — 测试 Secrets 读取

**Workflow 触发测试**
- `test_pat.py` / `test_pat2.py` / `test_pat_trigger.py` — 用 PAT 触发 workflow
- `test_b.py` / `verify_b.py` / `verify_new_b.py` — 账户 B 相关测试
- `test_b2.py` — 账户 B 的补充测试
- `check_wf_b.py` — 检查账户 B 的 workflow

**其他**
- `analyze.py` — 分析已有结果
- `explain.py` — 解释错误信息
- `cleanup.py` — 清理临时文件
- `final_verify.py` — 最终验证

## 安全规范（重要）
- 所有密钥（OpenAI API Key、GitHub PAT、飞书 Webhook）必须放在 `.env` 文件中，**绝不能**提交到 Git
- `.gitignore` 已排除 `.env`，但请自行确认没有硬编码密钥
- 分享代码或日志前，手动删除或脱敏以下字段：
  - `ghp_xxxx`（GitHub PAT）
  - `sk-xxxx`（OpenAI Key）
  - 飞书 Webhook URL
- 不要向任何人发送你的密钥截图或明文

## 开发建议
1. 改代码前先读 `README.md` 了解整体架构
2. 每次只测一个脚本，确认通过后再继续
3. 修改 `.github/workflows/` 下的 YAML 后，用 `workflow_dispatch` 手动触发测试
4. Skills 的实际逻辑不在本仓库，在 Codex skills 目录中
5. 输出格式必须是纯 JSON，方便下游解析

## 常见问题
- **问：怎么跑整个流程？**
  答：本仓库是调度和测试工具，完整流程需要在三个 GitHub 账户上配置好 Actions 和 Secrets。
- **问：本地调试用什么？**
  答：可以用 `python test_pat.py` 等脚本单独测试某个环节。
- **问：Skills 在哪里？**
  答：在 `C:\Users\zf\.codex\skills` 目录下，不在本仓库。
