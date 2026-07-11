# ============================================================
# skills/prompts/ — AlphaSift Skills Prompt 配置
# ============================================================
# 
# 本目录包含 19 个投研 Skills 的研究框架 prompt。
# 每个 Skill 一个 YAML 文件，命名格式: {skill_key}.yaml
#
# 文件结构:
#   - key:     唯一标识符（对应 SKILLS_CONFIG 的键）
#   - name:    中文名称
#   - prompt:  研究框架模板，支持 {code} 和 {name} 两个变量
#
# 使用方式:
#   run_skills_evaluation.py 启动时自动从本目录加载所有 .yaml 文件
#
# 新增 Skill 的方法:
#   1. 在下方列表中添加一行
#   2. 新建 skills/prompts/{new_skill_key}.yaml 文件
#   3. 确保 YAML 中的 key 与文件名一致
#
# 19 个 Skills 列表:
#   1.  investment-research      投资研究（四大师框架）
#   2.  investment-team          投研团队（机构持仓分析）
#   3.  management-deep-dive     管理层纵深
#   4.  private-company-research 未上市公司影子对手
#   5.  deep-company-series      商业模式演变追踪
#   6.  earnings-review          财报精读
#   7.  earnings-team            财报团队并行解读
#   8.  industry-research        行业研究
#   9.  industry-funnel          行业漏斗筛选
#   10. quality-screen           去劣筛选
#   11. bottleneck-hunter        供应链瓶颈猎手
#   12. investment-checklist     投资买入前 Checklist
#   13. portfolio-review         组合管理
#   14. thesis-tracker           投资论文追踪
#   15. news-pulse               公司新闻脉搏
#   16. dyp-ask                段永平问答
#   17. financial-data           财务数据获取
#   18. wechat-article           公众号文章
#   19. investment-memo-craft    投资论文写作
