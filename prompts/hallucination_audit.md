# Hallucination Audit Prompt

Use this prompt after a research/write/review/revise/finalize workflow produces a draft.

Check the draft against the supplied source package. Return:

```json
{
  "verdict": "pass | revise | fail",
  "citation_errors": [],
  "unsupported_claims": [],
  "method_or_data_misstatements": [],
  "overgeneralized_conclusions": [],
  "missing_counterevidence": [],
  "recommended_fixes": []
}
```

Rules:

- Every empirical claim must map to a source.
- Every citation must exist in the bibliography or source package.
- Flag vague claims such as "many studies show" unless evidence is supplied.
- Flag causal language when the paper only supports association.
- Separate missing evidence from false evidence.

