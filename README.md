# Accounting Research Agent

An accounting research literature radar and idea-management scaffold. The first version is designed to:

- Collect recent papers across accounting research domains
- Prioritize high-quality accounting journals
- Classify papers by accounting field
- Extract structured metadata about method, data, main findings, and references
- Incrementally merge new papers into the existing library
- Store reading insights and research ideas for later writing
- Leave room for `academic-research-skills`, `gstock`, and `superpower` style review workflows

## Quick Start

```bash
python3 scripts/accounting_literature_agent.py collect --from-date 2026-01-01 --max-results 50
python3 scripts/accounting_literature_agent.py collect --from-date 2026-01-01 --max-results 50 --no-include-supplemental
python3 scripts/accounting_literature_agent.py ideas add --title "AI disclosures and audit risk assessment" --note "Compare how Big 4 and non-Big 4 auditors respond to AI-related risk disclosures."
python3 scripts/accounting_literature_agent.py report
```

Output files:

- `data/processed/papers.jsonl`: deduplicated paper library
- `data/processed/references.jsonl`: referenced-work library
- `data/processed/ideas.jsonl`: reading insights and research ideas
- `data/processed/latest_report.md`: latest trend report

## Suggested Workflow

1. Run `collect` weekly to gather recent accounting papers.
2. Read `latest_report.md` to identify field-level trends.
3. Select important papers for deep reading.
4. Save reading insights with `ideas add`.
5. Use a research/write/review/revise/finalize workflow for mature ideas.
6. Run an independent hallucination audit before trusting generated drafts.

## Collection Scope

The collection policy lives in `config/source_policy.json`. By default, the agent prioritizes these journals:

- Accounting and Business Research
- Accounting and Finance
- Accounting Horizons
- Behavioral Research in Accounting
- International Journal of Accounting, Auditing and Performance Evaluation
- Journal of Accounting & Organizational Change
- Journal of Accounting, Auditing and Finance
- Journal of International Accounting Research
- Qualitative Research in Accounting and Management
- Contemporary Accounting Research
- European Accounting Review
- Auditing: A Journal of Practice and Theory

The agent can also collect supplemental papers from SSRN, AAA-related sources, and broader accounting searches. Supplemental papers must pass a minimum quality score. Current quality signals include:

- Abstract available
- DOI available
- Referenced works available
- Accounting-field relevance
- Recognized supplemental source
- Early citation signal for recent papers

To collect only priority-journal papers:

```bash
python3 scripts/accounting_literature_agent.py collect --no-include-supplemental
```

## Accounting Fields

Field classification rules live in `config/accounting_fields.yml`. Current fields:

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

## Next Steps

Version 0.1 uses OpenAlex public metadata, which is useful for building a trend radar. Future versions can add:

- SSRN search and download
- Journal website RSS or table-of-contents feeds
- Manual Google Scholar import
- Zotero or BibTeX sync
- OpenAI API enrichment for deeper structured summaries
- PDF full-text parsing
- Obsidian or Notion knowledge-base export
