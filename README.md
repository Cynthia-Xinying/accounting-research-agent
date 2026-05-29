# Accounting Research Agent

这是一个面向会计研究的文献雷达与研究想法整理脚手架。第一版目标是：

- 持续收集会计各领域最新论文
- 自动按研究板块分类
- 提取方法、数据、主要结论、引用文献等结构化信息
- 将新文献与旧文献库增量合并
- 保存阅读灵感和论文 idea，方便后续写作
- 为 `academic-research-skills`、`gstock`、`superpower` 这类研究写作与核查工具预留监督流程

## 快速开始

```bash
python3 scripts/accounting_literature_agent.py collect --from-date 2026-01-01 --max-results 50
python3 scripts/accounting_literature_agent.py ideas add --title "审计 AI 披露与风险评估" --note "可以比较 Big 4 与非 Big 4 审计师对 AI 风险披露的反应。"
python3 scripts/accounting_literature_agent.py report
```

输出文件：

- `data/processed/papers.jsonl`: 去重后的论文库
- `data/processed/references.jsonl`: 被引用论文库
- `data/processed/ideas.jsonl`: 阅读灵感与研究 idea
- `data/processed/latest_report.md`: 最近趋势报告

## 建议工作流

1. 每周运行一次 `collect`，收集最新会计论文。
2. 先读 `latest_report.md`，了解各板块趋势。
3. 选择重点论文精读，把灵感用 `ideas add` 记录下来。
4. 对成熟 idea 使用 research/write/review/revise/finalize 写作流水线。
5. 使用独立核查流程检查引用、数据、方法描述和结论是否幻觉。

## 会计研究板块

分类规则在 `config/accounting_fields.yml` 中。当前覆盖：

- Financial Accounting
- Auditing
- Tax
- Management Accounting
- Corporate Governance
- ESG and Sustainability
- Capital Markets
- Accounting Information Systems
- Disclosure
- Regulation and Standard Setting

## 下一步

第一版使用 OpenAlex 公共元数据，适合建立趋势雷达。更强版本可以继续接入：

- SSRN 搜索与下载
- 期刊官网 RSS 或 TOC
- Google Scholar 手动导入
- Zotero / BibTeX 同步
- OpenAI API 深度摘要与表格化抽取
- PDF 全文解析
- 自动生成 Obsidian 或 Notion 知识库

