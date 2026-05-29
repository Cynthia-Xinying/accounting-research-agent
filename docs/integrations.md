# Integrations

## academic-research-skills

The GitHub project that appears to match your description is `Imbad0202/academic-research-skills`, described as an academic research workflow with stages such as research, write, review, revise, and finalize.

Recommended use:

1. Feed it only a curated source package from `data/processed/papers.jsonl` and your selected PDFs or notes.
2. Require every output draft to include a claim-to-source map.
3. After `finalize`, run the hallucination audit prompt in `prompts/hallucination_audit.md`.

## gstock and superpower

Treat these as independent supervisors rather than co-authors:

- gstock: check whether empirical claims, citations, and data descriptions are grounded in supplied sources.
- superpower: stress-test logic, causal language, missing counterevidence, and unsupported theoretical jumps.

If either tool cannot receive the same source package, do not use it as a factual auditor. A checker that sees only the draft can detect suspicious writing, but it cannot prove source faithfulness.

## Minimal anti-hallucination protocol

Every generated review or manuscript section should ship with:

- Source package ID
- List of papers used
- Claim table
- Citation table
- Unsupported claim list
- Revision log

Keep the source package immutable for each writing run so later audits can reproduce the result.

