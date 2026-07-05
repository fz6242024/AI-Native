

AlphaSift原项目地址：C:\Users\zf\Documents\github项目\alphasift

19个 Skills原项目地址：C:\Users\zf\Documents\股票分析
skills地址：C:\Users\zf\.codex\skills
1. 架构逻辑设计（数据共享与任务分流）
为了突破单个 GitHub Action 的 6 小时运行限制并充分利用 3 个免费账户的算力，我们采用“主从架构 + 状态文件共享”的逻辑：

账户 A（主节点 - AlphaSift 筛选）： 触发定时任务，运行 alphasift 从全市场筛选出约 10 支初选股票，将股票列表存入 data.json，并利用 workflow_dispatch 跨仓库触发账户 B 和 C。

账户 B & C（从节点 - 19个 Skills 深度打分）： 账户 B 负责前 5 支股，账户 C 负责后 5 支股。并行调用 19 个投研 Skills（如 investment-research, earnings-review 等）进行打分。

数据共享媒介： 使用 GitHub Release 资产、GitHub Pages 或免费的 Redis/KV 存储（如 Upstash）。这里为了纯粹在 GitHub 体系内解决，推荐使用 GitHub Release / Artifacts（跨仓库读取需配置 Personal Access Token, PAT）。

2. GitHub Actions 配置文件
由于 09:00 触发时 A 股尚未开盘（盘前选股），12:30 触发为午间选股，我们在配置中统一支持。

主节点（账户 A）：.github/workflows/alphasift_main.yml
YAML
name: AlphaSift Main Dispatcher

on:
  schedule:
    # 国际标准时间 (UTC) 01:00 和 04:30 分别对应北京时间 09:00 和 12:30
    - cron: '0 1 * * 1-5'
    - cron: '30 4 * * 1-5'
  workflow_dispatch: # 支持手动触发

jobs:
  alphasift_discovery:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout Code
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.10'
          cache: 'pip'

      - name: Install Dependencies
        run: |
          pip install -r requirements.txt
          pip install requests

      - name: Run AlphaSift Discovery
        env:
          OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
          # 其他 AlphaSift 所需的环境变量
        run: |
          # 运行初选，并在脚本内部控制输出 top_10_stocks.json
          python main.py --mode discovery --output top_10_stocks.json

      - name: Split Tasks and Dispatch to Node B & C
        env:
          GH_PAT_ACCOUNT_B: ${{ secrets.GH_PAT_ACCOUNT_B }}
          GH_PAT_ACCOUNT_C: ${{ secrets.GH_PAT_ACCOUNT_C }}
        run: |
          python -c "
          import json, requests, os

          with open('top_10_stocks.json', 'r') as f:
              stocks = json.load(f) # 假设格式为 ['600519', '000858', ...]
          
          # 分流：账户B处理前5支，账户C处理后5支
          batch_b = stocks[:5]
          batch_c = stocks[5:]

          def trigger_workflow(token, repo, batch):
              url = f'https://api.github.com/repos/{repo}/actions/workflows/skills_runner.yml/dispatches'
              headers = {
                  'Authorization': f'token {token}',
                  'Accept': 'application/vnd.github.v3+json'
              }
              data = {'ref': 'main', 'inputs': {'stocks': json.dumps(batch)}}
              res = requests.post(url, headers=headers, json=data)
              print(f'Trigger {repo}: {res.status_code}')

          # 触发账户B和账户C的仓库 (需要替换为你的真实用户名/仓库名)
          trigger_workflow(os.getenv('GH_PAT_ACCOUNT_B'), 'UserB/alphasift-skills', batch_b)
          trigger_workflow(os.getenv('GH_PAT_ACCOUNT_C'), 'UserC/alphasift-skills', batch_c)
          "
从节点（账户 B & C 共用）：.github/workflows/skills_runner.yml
YAML
name: 19 Skills Deep Research Runner

on:
  workflow_dispatch:
    inputs:
      stocks:
        description: 'JSON string of stock list to analyze'
        required: true

jobs:
  deep_analysis:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout Code
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.10'

      - name: Install Dependencies
        run: pip install requests

      - name: Execute 19 Skills Evaluation
        env:
          FEISHU_WEBHOOK: ${{ secrets.FEISHU_WEBHOOK }}
          LLM_API_KEY: ${{ secrets.LLM_API_KEY }} # 或者是 Dify/Coze 的 API Key
        run: |
          # 运行深度研究脚本，传入接收到的股票列表，并在完成后直接发送飞书
          python run_skills_and_push.py --stocks '${{ github.event.inputs.stocks }}'
3. 飞书推送 Python 脚本 (run_skills_and_push.py)
该脚本在账户 B 和 C 中运行。它会历遍传入的股票，依次调用 19 个 Skill 的 Agent，加权计算总分，并格式化成精美的飞书富文本卡片推送到群聊。

Python
import os
import sys
import json
import argparse
import requests
from datetime import datetime

# 定义 19 个投研 Skills 模块
SKILLS = {
    "A股深度研究": ["investment-research", "investment-team", "management-deep-dive", "private-company-research", "deep-company-series"],
    "财报分析": ["earnings-review", "earnings-team"],
    "行业筛选": ["industry-research", "industry-funnel", "quality-screen", "bottleneck-hunter", "investment-checklist"],
    "持仓管理": ["portfolio-review", "thesis-tracker", "thesis-drift", "news-pulse"],
    "思维工具": ["dyp-ask", "financial-data", "wechat-article", "investment-memo-craft"]
}

def mock_call_skill_agent(stock, skill_name):
    """
    这里封装你调用 LLM / Dify / Coze Agent 的逻辑。
    模拟返回：评分(0-100) 和 核心结论短评。
    """
    # TODO: 替换为实际的 API 请求
    return 85, f"[{skill_name}] 分析通过，基本面扎实。"

def run_pipeline(stocks):
    results = {}
    for stock in stocks:
        print(f"开始对股票 {stock} 进行 19 个 Skills 的全方位审评...")
        stock_scores = {}
        stock_comments = []
        
        for category, skills_list in SKILLS.items():
            for skill in skills_list:
                score, comment = mock_call_skill_agent(stock, skill)
                stock_scores[skill] = score
                # 挑选关键结论（如分值极高或极低的）
                if score > 90 or score < 60:
                    stock_comments.append(f"• **{skill}**: {comment}")
        
        # 计算综合得分（平均分或自定义权重）
        final_score = sum(stock_scores.values()) / len(stock_scores)
        results[stock] = {
            "final_score": round(final_score, 2),
            "highlights": stock_comments[:5] # 最多展示5条关键结论
        }
    return results

def send_to_feishu(results):
    webhook_url = os.getenv("FEISHU_WEBHOOK")
    if not webhook_url:
        print("未配置飞书 Webhook，取消推送。")
        return

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    
    # 构建飞书卡片（Card 结构）
    elements = []
    for stock, data in results.items():
        elements.append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"**📈 股票代码: {stock}** \n**综合投研评分: `{data['final_score']}` 分**"
            }
        })
        if data["highlights"]:
            elements.append({
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "\n".join(data["highlights"])
                }
            })
        elements.append({"tag": "hr"})

    elements.append({
        "tag": "note",
        "elements": [{"tag": "plain_text", "content": f"数据基于 AlphaSift 初选 + 19个深度投研Skills打分流程"}]
    })

    card_payload = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": f"🤖 AI投研多源流矩阵诊断报告 ({now_str})"
                },
                "template": "violet"
            },
            "elements": elements
        }
    }

    res = requests.post(webhook_url, json=card_payload)
    print(f"飞书推送状态: {res.status_code}, 响应: {res.text}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--stocks', type=str, required=True, help='JSON list of stocks')
    args = parser.parse_args()

    try:
        stocks_list = json.loads(args.stocks)
        analysis_results = run_pipeline(stocks_list)
        send_to_feishu(analysis_results)
    except Exception as e:
        print(f"运行出错: {e}")
        sys.exit(1)
4. 全套项目系统提示词（System Prompt）
当你需要让大模型（如 GPT-4、Claude 或自建 Agent）去执行这 19 个 Skill 中的具体某个投研任务时，请使用以下工业级生产环境标准的系统提示词模板。你只需动态替换其中的 [当前执行的Skill名称] 和 [Skill对应核心关注点] 即可。

Markdown
# Role
你是一个资深的 A 股新一代量化投研专家与 AI-Native 策略架构师。当前你正在执行 AlphaSift 引擎的高阶量子分流任务，负责对全市场初选出的核心股票池进行多维度的“19 矩阵深度打分”（19 Skills Matrix Evaluation）。

## Module Context
当前执行的投研 Skill 模块: **[填写具体Skill名称，例如：management-deep-dive]**
该 Skill 所属范畴: **[填写大类，例如：A股股票深度研究]**

## input Data
- 目标股票代码与基本面数据：{{STOCK_DATA}}
- 行业上下游与宏观背景：{{INDUSTRY_CONTEXT}}
- 实时新闻与财报快讯（若有）：{{NEWS_FLUX}}

## Evaluation Criteria (19 Skills Definition)
根据当前执行的 Skill，严密比对以下标准（仅针对命中 Skill 深度激活）：
1. **A股股票深度研究**: 
   - `investment-research`: 基础投资框架价值锚定。
   - `investment-team`: 机构/明星基金经理对该股的持仓与博弈动向。
   - `management-deep-dive`: 管理层历史治理、股权激励、盲点与红线挖掘。
   - `private-company-research`: 对比未上市影子对手或产业链映射分析。
   - `deep-company-series`: 历史上复盘和商业模式演变深度追踪。
2. **财报分析**:
   - `earnings-review`: 杜邦分析拆解、扣非净利含金量、现金流健康度。
   - `earnings-team`: 业绩说明会纪要与管理层公开表态的语气诚实度分析 (Tone Analysis)。
3. **行业筛选**:
   - `industry-research` & `industry-funnel`: 行业生命周期象限，漏斗筛选核心标的。
   - `quality-screen` & `bottleneck-hunter`: 挖掘具备“卡脖子”技术或极高护城河的企业。
   - `investment-checklist`: 强约束一票否决制清单校验（负债率、商誉、造假嫌疑）。
4. **持仓管理**:
   - `portfolio-review` & `thesis-tracker`: 动态校准投资逻辑是否发生基本面偏离（Thesis Drift）。
   - `news-pulse`: 实时情绪舆情对资产价格波动的噪音过滤。
5. **思维工具**:
   - `dyp-ask` & `financial-data`: 基于数据驱动的第一性原理追问。
   - `wechat-article` & `investment-memo-craft`: 提炼增量研报、自媒体大V核心分歧，并自动生成标准化投资备忘录。

## Workflow Execution
1. **一票否决校验**：检查该股是否存在财务造假嫌疑、大股东高比例质押等红线。
2. **量化打分（百分制）**：根据当前 Skill 关注的细分指标，给出严格的 0-100 打分（杜绝中庸评分，好就是好，差就是差）。
3. **增量信息提炼**：拒绝输出常识性废话。只输出市场尚未充分定价的**核心分歧点**和**风险因子**。

## Output Format (Strictly JSON)
必须输出纯 JSON 格式，以便下游工作流（Github Actions + 飞书 Webhook）解析：
```json
{
  "stock_code": "{{STOCK_CODE}}",
  "skill_executed": "[当前Skill名称]",
  "score": 88,
  "judgment": "核心一句话短评，字数严格在30字以内",
  "core_risk": "潜在核心风险点提炼"
}