# Literature Extraction Prompt

Use this prompt when an LLM is available to enrich one paper record.

You are helping build an accounting research knowledge base. Read the title, abstract, metadata, and if available the full text. Return strict JSON with these fields:

```json
{
  "research_question": "",
  "accounting_field": "",
  "method": "",
  "data": "",
  "main_findings": "",
  "theory_or_mechanism": "",
  "identification_strategy": "",
  "limitations": "",
  "future_research_ideas": [],
  "cited_papers_to_collect": []
}
```

Rules:

- Do not invent methods, data, findings, or citations.
- If the abstract does not say, write `"not stated in available metadata"`.
- Keep each field concise but specific.
- Put cited papers in `cited_papers_to_collect` only when the citation is visible in the provided text.

