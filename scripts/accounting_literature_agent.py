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
import textwrap
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "accounting_fields.yml"
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
        "compustat",
        "crsp",
        "audit analytics",
        "ibes",
        "sec",
        "edgar",
        "wrds",
        "boardex",
        "iss",
        "ravenpack",
        "sustainalytics",
        "refinitiv",
    ]
    hits = [name for name in candidates if name in lower]
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


def openalex_query(search: str, from_date: str, per_page: int) -> list[dict[str, Any]]:
    params = {
        "search": search,
        "filter": f"from_publication_date:{from_date},type:article",
        "sort": "publication_date:desc",
        "per-page": str(per_page),
    }
    url = "https://api.openalex.org/works?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={"User-Agent": "accounting-research-agent/0.1"})
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return payload.get("results", [])


def normalize_work(work: dict[str, Any], rules: dict[str, list[str]]) -> dict[str, Any]:
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
    return {
        "id": work.get("id"),
        "doi": doi,
        "title": title,
        "publication_date": work.get("publication_date"),
        "publication_year": work.get("publication_year"),
        "authors": authors,
        "venue": source.get("display_name"),
        "url": work.get("id"),
        "landing_page_url": primary_location.get("landing_page_url"),
        "fields": classify(text, rules),
        "method": infer_method(text),
        "data": infer_data(text),
        "main_findings": short_finding(abstract),
        "abstract": abstract,
        "referenced_works": work.get("referenced_works") or [],
        "collected_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }


def stable_key(row: dict[str, Any]) -> str:
    return (row.get("doi") or row.get("id") or row.get("title") or "").lower()


def collect(args: argparse.Namespace) -> None:
    rules = load_field_rules()
    existing = read_jsonl(PAPERS_PATH)
    by_key = {stable_key(row): row for row in existing if stable_key(row)}
    reference_rows = read_jsonl(REFERENCES_PATH)
    reference_ids = {row.get("id") for row in reference_rows}

    max_per_term = max(1, args.max_results // len(DEFAULT_SEARCH_TERMS))
    new_count = 0
    for term in DEFAULT_SEARCH_TERMS:
        for work in openalex_query(term, args.from_date, max_per_term):
            row = normalize_work(work, rules)
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
    print(f"Collected {new_count} new papers. Library now has {len(papers)} papers.")


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
    for paper in papers:
        for field in paper.get("fields", []):
            field_counts[field] = field_counts.get(field, 0) + 1

    lines = [
        "# Latest Accounting Research Report",
        "",
        f"Generated at: {dt.datetime.now().isoformat(timespec='seconds')}",
        f"Total papers: {len(papers)}",
        "",
        "## Field distribution",
        "",
    ]
    for field, count in sorted(field_counts.items(), key=lambda item: item[1], reverse=True):
        lines.append(f"- {field}: {count}")

    lines.extend(["", "## Recent papers", ""])
    for paper in papers[:30]:
        fields = ", ".join(paper.get("fields", []))
        authors = ", ".join((paper.get("authors") or [])[:3])
        if len(paper.get("authors") or []) > 3:
            authors += " et al."
        lines.extend([
            f"### {paper.get('title')}",
            "",
            f"- Date: {paper.get('publication_date')}",
            f"- Field: {fields}",
            f"- Authors: {authors or 'not available'}",
            f"- Venue: {paper.get('venue') or 'not available'}",
            f"- Method: {paper.get('method')}",
            f"- Data: {paper.get('data')}",
            f"- Main finding: {paper.get('main_findings')}",
            f"- URL: {paper.get('landing_page_url') or paper.get('url')}",
            "",
        ])

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
            Try again when network access is available.
            """
        ).strip()
        raise SystemExit(message) from exc


if __name__ == "__main__":
    main()

