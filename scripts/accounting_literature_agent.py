#!/usr/bin/env python3
"""A lightweight accounting literature scout.

The first version intentionally uses only the Python standard library so the
project can run before you choose a database, LLM provider, or PDF parser.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import ssl
import textwrap
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "accounting_fields.yml"
SOURCE_POLICY = ROOT / "config" / "source_policy.json"
SOURCE_CACHE = ROOT / "data" / "raw" / "openalex_source_ids.json"
PAPERS_PATH = ROOT / "data" / "processed" / "papers.jsonl"
REFERENCES_PATH = ROOT / "data" / "processed" / "references.jsonl"
IDEAS_PATH = ROOT / "data" / "processed" / "ideas.jsonl"
REPORT_PATH = ROOT / "data" / "processed" / "latest_report.md"


DEFAULT_SEARCH_TERMS = [
    "accounting",
    "financial reporting",
    "auditing",
    "tax avoidance",
    "management accounting",
    "corporate governance accounting",
    "ESG disclosure accounting",
    "capital markets accounting",
]

OPENALEX_MAILTO = "accounting-research-agent@example.com"


def ssl_context() -> ssl.SSLContext | None:
    try:
        import certifi  # type: ignore
    except ImportError:
        return None
    return ssl.create_default_context(cafile=certifi.where())


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def load_source_policy(path: Path = SOURCE_POLICY) -> dict[str, Any]:
    return read_json(path, default={})


def load_field_rules(path: Path = CONFIG) -> dict[str, list[str]]:
    rules: dict[str, list[str]] = {}
    current: str | None = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if not raw.startswith(" ") and line.endswith(":"):
            current = line[:-1]
            rules[current] = []
        elif current and line.startswith("- "):
            rules[current].append(line[2:].strip().lower())
    return rules


def inverted_abstract(index: dict[str, list[int]] | None) -> str:
    if not index:
        return ""
    positions: list[tuple[int, str]] = []
    for word, offsets in index.items():
        for offset in offsets:
            positions.append((offset, word))
    return " ".join(word for _, word in sorted(positions))


def classify(text: str, rules: dict[str, list[str]]) -> list[str]:
    haystack = text.lower()
    scored = []
    for field, terms in rules.items():
        score = sum(1 for term in terms if term in haystack)
        if score:
            scored.append((score, field))
    scored.sort(reverse=True)
    return [field for _, field in scored[:3]] or ["Unclassified"]


def infer_method(text: str) -> str:
    patterns = [
        ("experiment", "experiment"),
        ("survey", "survey"),
        ("difference-in-differences", "difference-in-differences"),
        ("regression discontinuity", "regression discontinuity"),
        ("textual analysis", "textual analysis"),
        ("machine learning", "machine learning"),
        ("archival", "archival empirical study"),
        ("analytical model", "analytical modeling"),
        ("case study", "case study"),
    ]
    lower = text.lower()
    hits = [label for key, label in patterns if key in lower]
    return ", ".join(dict.fromkeys(hits)) if hits else "not stated in available metadata"


def infer_data(text: str) -> str:
    lower = text.lower()
    candidates = [
        ("compustat", r"\bcompustat\b"),
        ("crsp", r"\bcrsp\b"),
        ("audit analytics", r"\baudit analytics\b"),
        ("ibes", r"\bibes\b|\bi/b/e/s\b"),
        ("sec", r"\bsec\b|securities and exchange commission"),
        ("edgar", r"\bedgar\b"),
        ("wrds", r"\bwrds\b"),
        ("boardex", r"\bboardex\b"),
        ("iss", r"\biss\b|institutional shareholder services"),
        ("ravenpack", r"\bravenpack\b"),
        ("sustainalytics", r"\bsustainalytics\b"),
        ("refinitiv", r"\brefinitiv\b"),
    ]
    hits = [name for name, pattern in candidates if re.search(pattern, lower)]
    return ", ".join(hits) if hits else "not stated in available metadata"


def short_finding(abstract: str) -> str:
    if not abstract:
        return "not stated in available metadata"
    sentences = re.split(r"(?<=[.!?])\s+", abstract.strip())
    finding_markers = ("find", "show", "document", "evidence", "result", "suggest")
    for sentence in sentences:
        if any(marker in sentence.lower() for marker in finding_markers):
            return sentence[:500]
    return sentences[0][:500] if sentences else "not stated in available metadata"


def openalex_get(path: str, params: dict[str, str]) -> dict[str, Any]:
    params = {**params, "mailto": OPENALEX_MAILTO}
    url = "https://api.openalex.org/" + path + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={"User-Agent": "accounting-research-agent/0.1"})
    with urllib.request.urlopen(request, timeout=30, context=ssl_context()) as response:
        return json.loads(response.read().decode("utf-8"))


def openalex_query(
    search: str | None,
    from_date: str,
    per_page: int,
    extra_filter: str | None = None,
) -> list[dict[str, Any]]:
    filters = [f"from_publication_date:{from_date}", "type:article"]
    if extra_filter:
        filters.append(extra_filter)
    params = {
        "filter": ",".join(filters),
        "sort": "publication_date:desc",
        "per-page": str(per_page),
    }
    if search:
        params["search"] = search
    payload = openalex_get("works", params)
    return payload.get("results", [])


def source_display_name(work: dict[str, Any]) -> str:
    primary_location = work.get("primary_location") or {}
    source = primary_location.get("source") or {}
    return source.get("display_name") or ""


def source_publisher(work: dict[str, Any]) -> str:
    primary_location = work.get("primary_location") or {}
    source = primary_location.get("source") or {}
    return source.get("publisher") or ""


def normalize_name(value: str | None) -> str:
    tokens = re.sub(r"[^a-z0-9]+", " ", value or "").strip().lower().split()
    stopwords = {"a", "an", "and", "the", "of"}
    return " ".join(token for token in tokens if token not in stopwords)


def source_matches(value: str, candidates: list[str]) -> bool:
    normalized = normalize_name(value)
    return any(normalize_name(candidate) in normalized for candidate in candidates)


def has_phrase(text: str, phrase: str) -> bool:
    pattern = r"\b" + re.escape(phrase.lower()).replace(r"\ ", r"\s+") + r"\b"
    return bool(re.search(pattern, text.lower()))


def is_high_signal_supplemental(row: dict[str, Any], work: dict[str, Any], policy: dict[str, Any]) -> bool:
    """Keep supplemental searches focused on accounting research, not broad keyword neighbors."""
    fields = set(row.get("fields") or [])
    if not fields or fields == {"Unclassified"}:
        return False

    text = "\n".join([
        str(row.get("title") or ""),
        str(row.get("abstract") or ""),
        str(row.get("venue") or ""),
    ])
    core_accounting_terms = [
        "accounting",
        "accountant",
        "audit",
        "auditor",
        "assurance",
        "financial reporting",
        "financial statement",
        "earnings management",
        "earnings quality",
        "accrual",
        "book-tax",
        "tax",
        "income tax",
        "value-added tax",
        "tax avoidance",
        "tax aggressiveness",
        "tax compliance",
        "tax practice",
        "tax rate",
        "taxpayer",
        "management accounting",
        "cost accounting",
        "internal control",
        "disclosure",
        "xbrl",
        "gaap",
        "ifrs",
        "fasb",
    ]
    if any(has_phrase(text, term) for term in core_accounting_terms):
        return True

    supplemental_sources = policy.get("supplemental_sources", [])
    recognized_source = (
        source_matches(source_display_name(work), supplemental_sources)
        or source_matches(source_publisher(work), supplemental_sources)
    )
    accounting_specific_fields = {
        "Financial Accounting",
        "Auditing",
        "Tax",
        "Management Accounting",
        "Accounting Information Systems",
    }
    if recognized_source and fields.intersection(accounting_specific_fields):
        return True

    return False


def resolve_openalex_source_ids(journal_names: list[str]) -> dict[str, str]:
    cache = read_json(SOURCE_CACHE, default={})
    changed = False
    for journal in journal_names:
        if cache.get(journal):
            continue
        payload = openalex_get("sources", {"search": journal, "per-page": "5"})
        if not payload.get("results"):
            payload = openalex_get("sources", {"search": normalize_name(journal), "per-page": "5"})
        source_id = ""
        normalized_journal = normalize_name(journal)
        for result in payload.get("results", []):
            display_name = result.get("display_name") or ""
            if normalize_name(display_name) == normalized_journal:
                source_id = result.get("id") or ""
                break
        if not source_id and payload.get("results"):
            source_id = payload["results"][0].get("id") or ""
        cache[journal] = source_id
        changed = True
    if changed:
        write_json(SOURCE_CACHE, cache)
    return {journal: source_id for journal, source_id in cache.items() if source_id}


def quality_score(work: dict[str, Any], abstract: str, fields: list[str], policy: dict[str, Any]) -> tuple[int, list[str]]:
    rules = policy.get("quality_rules", {})
    score = 0
    reasons: list[str] = []
    if abstract:
        score += int(rules.get("has_abstract", 0))
        reasons.append("has abstract")
    if work.get("doi"):
        score += int(rules.get("has_doi", 0))
        reasons.append("has DOI")
    if work.get("referenced_works"):
        score += int(rules.get("has_referenced_works", 0))
        reasons.append("has references")
    if fields != ["Unclassified"]:
        score += int(rules.get("accounting_relevant", 0))
        reasons.append("accounting relevant")
    supplemental_sources = policy.get("supplemental_sources", [])
    if source_matches(source_display_name(work), supplemental_sources) or source_matches(source_publisher(work), supplemental_sources):
        score += int(rules.get("source_is_supplemental", 0))
        reasons.append("recognized supplemental source")
    cited_by_count = int(work.get("cited_by_count") or 0)
    publication_year = int(work.get("publication_year") or 0)
    current_year = dt.date.today().year
    if publication_year >= current_year - 2 and cited_by_count > 0:
        score += int(rules.get("recent_citation_signal", 0))
        reasons.append("early citation signal")
    return score, reasons


def normalize_work(
    work: dict[str, Any],
    rules: dict[str, list[str]],
    policy: dict[str, Any],
    source_tier: str,
) -> dict[str, Any]:
    abstract = inverted_abstract(work.get("abstract_inverted_index"))
    title = work.get("title") or ""
    text = f"{title}\n{abstract}"
    doi = work.get("doi") or ""
    primary_location = work.get("primary_location") or {}
    source = primary_location.get("source") or {}
    authorships = work.get("authorships") or []
    authors = [
        item.get("author", {}).get("display_name")
        for item in authorships
        if item.get("author", {}).get("display_name")
    ]
    fields = classify(text, rules)
    score, reasons = quality_score(work, abstract, fields, policy)
    return {
        "id": work.get("id"),
        "doi": doi,
        "title": title,
        "publication_date": work.get("publication_date"),
        "publication_year": work.get("publication_year"),
        "authors": authors,
        "venue": source.get("display_name"),
        "publisher": source.get("publisher"),
        "source_tier": source_tier,
        "quality_score": score,
        "quality_reasons": reasons,
        "cited_by_count": work.get("cited_by_count") or 0,
        "url": work.get("id"),
        "landing_page_url": primary_location.get("landing_page_url"),
        "fields": fields,
        "method": infer_method(text),
        "data": infer_data(text),
        "main_findings": short_finding(abstract),
        "abstract": abstract,
        "referenced_works": work.get("referenced_works") or [],
        "collected_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }


def stable_key(row: dict[str, Any]) -> str:
    return (row.get("doi") or row.get("id") or row.get("title") or "").lower()


def paper_text(row: dict[str, Any]) -> str:
    return "\n".join([
        str(row.get("title") or ""),
        str(row.get("abstract") or ""),
        str(row.get("main_findings") or ""),
    ])


def has_deep_analysis(row: dict[str, Any]) -> bool:
    return bool(row.get("llm_enrichment") or row.get("pdf_text_path"))


def enriched_value(row: dict[str, Any], key: str, fallback: str = "not stated in available metadata") -> str:
    enrichment = row.get("llm_enrichment") or {}
    value = enrichment.get(key) if isinstance(enrichment, dict) else None
    if value:
        if isinstance(value, list):
            return "; ".join(str(item) for item in value if item) or fallback
        return str(value)
    return str(row.get(key) or fallback)


def analysis_status(row: dict[str, Any]) -> str:
    if row.get("llm_enrichment") and row.get("pdf_text_path"):
        return "enriched and full-text analyzed"
    if row.get("llm_enrichment"):
        return "enriched with LLM"
    if row.get("pdf_text_path"):
        return "full-text extracted"
    return "metadata only"


def first_sentence(value: str, fallback: str = "The available metadata is too thin to summarize the paper confidently.") -> str:
    text = re.sub(r"\s+", " ", value or "").strip()
    text = re.sub(r"^ABSTRACT\s+", "", text, flags=re.IGNORECASE)
    if not text or text == "not stated in available metadata":
        return fallback
    sentences = re.split(r"(?<=[.!?])\s+", text)
    return (sentences[0] if sentences else text)[:450]


def has_substantive_metadata(row: dict[str, Any]) -> bool:
    abstract = re.sub(r"\s+", " ", row.get("abstract") or "").strip().lower()
    if len(abstract) < 80:
        return False
    thin_markers = ["earlyview", "early view", "current issue"]
    return not any(marker in abstract for marker in thin_markers)


def quality_reason_sentence(row: dict[str, Any]) -> str:
    reasons = set(row.get("quality_reasons") or [])
    venue = row.get("venue") or "the listed venue"
    fields = ", ".join(row.get("fields") or ["accounting research"])
    if row.get("source_tier") == "priority_journal":
        return f"Its quality potential comes from venue fit: it appears in {venue}, one of the tracked priority venues, and connects to {fields}."
    if "has DOI" in reasons and "has references" in reasons:
        return f"Its quality potential is preliminary, but it has stronger bibliographic signals than a generic web hit because it has a DOI, references, and a clear connection to {fields}."
    if "has abstract" in reasons:
        return f"It is worth a quick look because the abstract gives enough signal to connect it to {fields}, though venue fit and design still need checking."
    return f"It is a radar item only; confirm relevance before investing reading time."


def radar_paragraph(row: dict[str, Any]) -> str:
    title = row.get("title") or "Untitled paper"
    fields = ", ".join(row.get("fields") or ["Unclassified"])
    venue = row.get("venue") or "unknown venue"
    date = row.get("publication_date") or "unknown date"
    if has_substantive_metadata(row):
        signal = first_sentence(row.get("main_findings") if row.get("main_findings") != "not stated in available metadata" else row.get("abstract"))
        innovation = first_sentence(row.get("abstract") or row.get("main_findings") or "")
    else:
        signal = "The available metadata is too thin for a confident content summary."
        innovation = "The possible innovation is not visible yet; treat this as a discovery lead until publisher metadata or PDF text is available."
    reason = quality_reason_sentence(row)
    return (
        f"**{title}** ({venue}, {date}) is a rapid-radar item in {fields}. "
        f"Metadata signal: {signal} "
        f"Possible innovation: {innovation} "
        f"{reason} Method, data, and identification are not asserted until PDF/full-text enrichment is available."
    )


def clean_library(args: argparse.Namespace) -> None:
    policy = load_source_policy()
    minimum_supplemental_score = int(policy.get("minimum_supplemental_quality_score", 4))
    papers = read_jsonl(PAPERS_PATH)
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []

    for paper in papers:
        paper["data"] = infer_data(paper_text(paper))
        if paper.get("source_tier") == "supplemental":
            high_quality = int(paper.get("quality_score") or 0) >= minimum_supplemental_score
            high_signal = is_high_signal_supplemental(paper, {}, policy)
            if not high_quality or not high_signal:
                dropped.append(paper)
                continue
        kept.append(paper)

    kept_ids = {paper.get("id") for paper in kept if paper.get("id")}
    references = [
        row for row in read_jsonl(REFERENCES_PATH)
        if not row.get("cited_by_paper_id") or row.get("cited_by_paper_id") in kept_ids
    ]

    if not args.dry_run:
        write_jsonl(PAPERS_PATH, kept)
        write_jsonl(REFERENCES_PATH, references)
        build_report()

    print(
        f"{'Would keep' if args.dry_run else 'Kept'} {len(kept)} papers "
        f"and {'would drop' if args.dry_run else 'dropped'} {len(dropped)} low-signal supplemental papers."
    )
    if dropped:
        print("Dropped supplemental papers:")
        for paper in dropped:
            print(f"- {paper.get('title')}")


def collect(args: argparse.Namespace) -> None:
    rules = load_field_rules()
    policy = load_source_policy()
    existing = read_jsonl(PAPERS_PATH)
    by_key = {stable_key(row): row for row in existing if stable_key(row)}
    reference_rows = read_jsonl(REFERENCES_PATH)
    reference_ids = {row.get("id") for row in reference_rows}

    priority_journals = policy.get("priority_journals", [])
    source_ids = resolve_openalex_source_ids(priority_journals)
    minimum_supplemental_score = int(policy.get("minimum_supplemental_quality_score", 4))
    per_priority_source = max(1, args.max_results // max(1, len(priority_journals)))
    per_supplemental_term = max(1, args.supplemental_max_results // max(1, len(policy.get("supplemental_search_terms", []))))
    new_count = 0
    skipped_supplemental = 0

    work_batches: list[tuple[str, list[dict[str, Any]]]] = []
    for journal in priority_journals:
        source_id = source_ids.get(journal)
        if source_id:
            works = openalex_query(None, args.from_date, per_priority_source, f"primary_location.source.id:{source_id}")
        else:
            works = openalex_query(journal, args.from_date, per_priority_source)
        work_batches.append(("priority_journal", works))

    if args.include_supplemental:
        terms = policy.get("supplemental_search_terms") or DEFAULT_SEARCH_TERMS
        for term in terms:
            work_batches.append(("supplemental", openalex_query(term, args.from_date, per_supplemental_term)))

    for source_tier, works in work_batches:
        for work in works:
            if source_tier == "priority_journal" and not source_matches(source_display_name(work), priority_journals):
                continue
            row = normalize_work(work, rules, policy, source_tier)
            if source_tier == "supplemental":
                if row["quality_score"] < minimum_supplemental_score:
                    skipped_supplemental += 1
                    continue
                if not is_high_signal_supplemental(row, work, policy):
                    skipped_supplemental += 1
                    continue
            key = stable_key(row)
            if not key or key in by_key:
                continue
            by_key[key] = row
            new_count += 1
            for ref_id in row["referenced_works"]:
                if ref_id not in reference_ids:
                    reference_rows.append({
                        "id": ref_id,
                        "cited_by_paper_id": row["id"],
                        "cited_by_paper_title": row["title"],
                        "collected_at": row["collected_at"],
                    })
                    reference_ids.add(ref_id)

    papers = sorted(by_key.values(), key=lambda row: row.get("publication_date") or "", reverse=True)
    write_jsonl(PAPERS_PATH, papers)
    write_jsonl(REFERENCES_PATH, reference_rows)
    build_report()
    print(
        f"Collected {new_count} new papers. "
        f"Skipped {skipped_supplemental} low-signal supplemental papers. "
        f"Library now has {len(papers)} papers."
    )


def add_idea(args: argparse.Namespace) -> None:
    row = {
        "id": dt.datetime.now(dt.timezone.utc).strftime("idea-%Y%m%d%H%M%S"),
        "title": args.title,
        "note": args.note,
        "field": args.field,
        "related_papers": args.related_papers or [],
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    append_jsonl(IDEAS_PATH, row)
    print(f"Added idea: {row['id']}")


def build_report() -> None:
    papers = read_jsonl(PAPERS_PATH)
    field_counts: dict[str, int] = {}
    tier_counts: dict[str, int] = {}
    deep_papers = [paper for paper in papers if has_deep_analysis(paper)]
    metadata_papers = [paper for paper in papers if not has_deep_analysis(paper)]
    for paper in papers:
        for field in paper.get("fields", []):
            field_counts[field] = field_counts.get(field, 0) + 1
        tier = paper.get("source_tier") or "unknown"
        tier_counts[tier] = tier_counts.get(tier, 0) + 1

    lines = [
        "# Latest Accounting Research Report",
        "",
        f"Generated at: {dt.datetime.now().isoformat(timespec='seconds')}",
        f"Total papers: {len(papers)}",
        f"Enriched / full-text analyzed papers: {len(deep_papers)}",
        f"Metadata-only radar papers: {len(metadata_papers)}",
        "",
        "## Field distribution",
        "",
    ]
    for field, count in sorted(field_counts.items(), key=lambda item: item[1], reverse=True):
        lines.append(f"- {field}: {count}")

    lines.extend(["", "## Source tiers", ""])
    for tier, count in sorted(tier_counts.items(), key=lambda item: item[1], reverse=True):
        lines.append(f"- {tier}: {count}")

    lines.extend([
        "",
        "## Two-Layer Workflow",
        "",
        "Layer 1 is a rapid radar based only on OpenAlex, feeds, and bibliographic metadata. It flags potentially relevant papers in plain language but does not claim to know the method, data, or identification strategy. Layer 2 is the deep-reading layer: after a PDF is extracted and enriched, the report records the research question, method, data, identification strategy, findings, contribution, limitations, and future research ideas.",
    ])

    def append_deep_section(selected: list[dict[str, Any]], limit: int) -> None:
        lines.extend(["", "## Layer 2: Deep Reading Enhanced Papers", ""])
        if not selected:
            lines.append("No papers have PDF/full-text or LLM enrichment yet. Use `pdf extract` plus `enrich` for papers selected from the radar.")
            return
        for paper in selected[:limit]:
            fields = ", ".join(paper.get("fields", []))
            authors = ", ".join((paper.get("authors") or [])[:3])
            if len(paper.get("authors") or []) > 3:
                authors += " et al."
            lines.extend([
                f"### {paper.get('title')}",
                "",
                f"- Date: {paper.get('publication_date')}",
                f"- Field: {fields}",
                f"- Source tier: {paper.get('source_tier') or 'unknown'}",
                f"- Analysis status: {analysis_status(paper)}",
                f"- Authors: {authors or 'not available'}",
                f"- Venue: {paper.get('venue') or 'not available'}",
                f"- Research question: {enriched_value(paper, 'research_question')}",
                f"- Method: {enriched_value(paper, 'method')}",
                f"- Data: {enriched_value(paper, 'data')}",
                f"- Identification strategy: {enriched_value(paper, 'identification_strategy')}",
                f"- Main findings: {enriched_value(paper, 'main_findings')}",
                f"- Contribution: {enriched_value(paper, 'contribution')}",
                f"- Limitations: {enriched_value(paper, 'limitations')}",
                f"- Future research ideas: {enriched_value(paper, 'future_research_ideas')}",
                f"- URL: {paper.get('landing_page_url') or paper.get('url')}",
                "",
            ])

    def append_radar_section(selected: list[dict[str, Any]], limit: int) -> None:
        lines.extend(["", "## Layer 1: Rapid Radar", ""])
        if not selected:
            lines.append("No metadata-only radar papers are waiting for screening.")
            return
        for paper in selected[:limit]:
            lines.extend([
                radar_paragraph(paper),
                "",
                f"URL: {paper.get('landing_page_url') or paper.get('url')}",
                "",
            ])

    append_radar_section(metadata_papers, 30)
    append_deep_section(deep_papers, 30)

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")


def report(_: argparse.Namespace | None = None) -> None:
    build_report()
    print(REPORT_PATH.read_text(encoding="utf-8"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect, classify, and summarize accounting research papers."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    collect_parser = subparsers.add_parser("collect")
    collect_parser.add_argument("--from-date", default=(dt.date.today() - dt.timedelta(days=30)).isoformat())
    collect_parser.add_argument("--max-results", type=int, default=80)
    collect_parser.add_argument("--supplemental-max-results", type=int, default=40)
    collect_parser.add_argument(
        "--include-supplemental",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Also collect high-signal papers from SSRN/AAA-style supplemental searches.",
    )
    collect_parser.set_defaults(func=collect)

    ideas_parser = subparsers.add_parser("ideas")
    ideas_sub = ideas_parser.add_subparsers(dest="ideas_command", required=True)
    add_parser = ideas_sub.add_parser("add")
    add_parser.add_argument("--title", required=True)
    add_parser.add_argument("--note", required=True)
    add_parser.add_argument("--field", default="Unclassified")
    add_parser.add_argument("--related-papers", nargs="*")
    add_parser.set_defaults(func=add_idea)

    report_parser = subparsers.add_parser("report")
    report_parser.set_defaults(func=report)

    maintain_parser = subparsers.add_parser("maintain")
    maintain_sub = maintain_parser.add_subparsers(dest="maintain_command", required=True)
    clean_parser = maintain_sub.add_parser("clean-library")
    clean_parser.add_argument("--dry-run", action="store_true")
    clean_parser.set_defaults(func=clean_library)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        args.func(args)
    except urllib.error.URLError as exc:
        message = textwrap.dedent(
            f"""
            Network request failed: {exc}

            The script is ready, but collection needs network access to OpenAlex.
            If this is an SSL certificate error on macOS, run Python's
            "Install Certificates.command" or install a Python distribution
            with a working certificate bundle, then try again.
            """
        ).strip()
        raise SystemExit(message) from exc


if __name__ == "__main__":
    main()
