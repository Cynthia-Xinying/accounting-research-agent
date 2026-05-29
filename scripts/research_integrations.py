#!/usr/bin/env python3
"""Optional integrations for the accounting research agent.

This script keeps network-heavy and provider-specific workflows separate from
the core OpenAlex collector. It still reads and writes the same paper library.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import json
import os
import re
import ssl
import textwrap
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
FIELDS_CONFIG = ROOT / "config" / "accounting_fields.yml"
JOURNAL_FEEDS_CONFIG = ROOT / "config" / "journal_feeds.json"
SSRN_QUERIES_CONFIG = ROOT / "config" / "ssrn_queries.json"
EXTRACTION_PROMPT = ROOT / "prompts" / "literature_extraction.md"
PAPERS_PATH = ROOT / "data" / "processed" / "papers.jsonl"
IDEAS_PATH = ROOT / "data" / "processed" / "ideas.jsonl"
PDF_TEXT_DIR = ROOT / "data" / "processed" / "pdf_text"
SSRN_PDF_DIR = ROOT / "data" / "raw" / "pdfs" / "ssrn"
EXPORTS_DIR = ROOT / "exports"


def ssl_context() -> ssl.SSLContext | None:
    try:
        import certifi  # type: ignore
    except ImportError:
        return None
    return ssl.create_default_context(cafile=certifi.where())


def http_get(url: str, headers: dict[str, str] | None = None, timeout: int = 30) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) accounting-research-agent/0.1",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/rss+xml;q=0.8,*/*;q=0.7",
            "Accept-Language": "en-US,en;q=0.9",
            **(headers or {}),
        },
    )
    with urllib.request.urlopen(request, timeout=timeout, context=ssl_context()) as response:
        return response.read()


def http_json(url: str, headers: dict[str, str] | None = None) -> Any:
    return json.loads(http_get(url, headers=headers).decode("utf-8"))


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


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


def load_field_rules(path: Path = FIELDS_CONFIG) -> dict[str, list[str]]:
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


def classify(text: str, rules: dict[str, list[str]]) -> list[str]:
    haystack = text.lower()
    scored = []
    for field, terms in rules.items():
        score = sum(1 for term in terms if term in haystack)
        if score:
            scored.append((score, field))
    scored.sort(reverse=True)
    return [field for _, field in scored[:3]] or ["Unclassified"]


def stable_key(row: dict[str, Any]) -> str:
    return (row.get("doi") or row.get("id") or row.get("landing_page_url") or row.get("title") or "").lower()


def merge_papers(new_rows: list[dict[str, Any]]) -> int:
    existing = read_jsonl(PAPERS_PATH)
    by_key = {stable_key(row): row for row in existing if stable_key(row)}
    added = 0
    for row in new_rows:
        key = stable_key(row)
        if not key or key in by_key:
            continue
        by_key[key] = row
        added += 1
    papers = sorted(by_key.values(), key=lambda row: row.get("publication_date") or "", reverse=True)
    write_jsonl(PAPERS_PATH, papers)
    refresh_report()
    return added


def refresh_report() -> None:
    try:
        import accounting_literature_agent as core
    except Exception:
        return
    try:
        core.build_report()
    except Exception:
        return


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    value = re.sub(r"<[^>]+>", " ", value)
    value = html.unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def slugify(value: str, fallback: str = "item") -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return slug[:90] or fallback


def parse_rss_or_atom(payload: bytes, journal: dict[str, Any], max_items: int) -> list[dict[str, Any]]:
    root = ET.fromstring(payload)
    rules = load_field_rules()
    rows: list[dict[str, Any]] = []
    rss_items = root.findall(".//item")
    atom_items = root.findall(".//{http://www.w3.org/2005/Atom}entry")

    for item in rss_items[:max_items]:
        title = clean_text(item.findtext("title"))
        abstract = clean_text(item.findtext("description"))
        link = clean_text(item.findtext("link"))
        published = clean_text(item.findtext("pubDate"))
        rows.append(feed_row(title, abstract, link, published, journal, rules))

    for item in atom_items[:max_items]:
        title = clean_text(item.findtext("{http://www.w3.org/2005/Atom}title"))
        abstract = clean_text(item.findtext("{http://www.w3.org/2005/Atom}summary"))
        link = ""
        for link_item in item.findall("{http://www.w3.org/2005/Atom}link"):
            if link_item.attrib.get("href"):
                link = link_item.attrib["href"]
                break
        published = clean_text(item.findtext("{http://www.w3.org/2005/Atom}published"))
        rows.append(feed_row(title, abstract, link, published, journal, rules))

    return [row for row in rows if row.get("title")]


def feed_row(
    title: str,
    abstract: str,
    link: str,
    published: str,
    journal: dict[str, Any],
    rules: dict[str, list[str]],
) -> dict[str, Any]:
    fields = classify(f"{title}\n{abstract}", rules)
    return {
        "id": link or f"feed:{journal.get('name')}:{title}",
        "doi": "",
        "title": title,
        "publication_date": published,
        "publication_year": None,
        "authors": [],
        "venue": journal.get("name"),
        "publisher": journal.get("publisher"),
        "source_tier": "journal_feed",
        "quality_score": 2 if abstract else 0,
        "quality_reasons": ["journal feed item"] + (["has abstract"] if abstract else []),
        "cited_by_count": 0,
        "url": link,
        "landing_page_url": link or journal.get("toc_url"),
        "fields": fields,
        "method": "not stated in available metadata",
        "data": "not stated in available metadata",
        "main_findings": "not stated in available metadata",
        "abstract": abstract,
        "referenced_works": [],
        "collected_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }


def harvest_feeds(args: argparse.Namespace) -> None:
    config = read_json(JOURNAL_FEEDS_CONFIG, default={"journals": []})
    all_rows: list[dict[str, Any]] = []
    skipped = []
    for journal in config.get("journals", []):
        feed_url = journal.get("feed_url")
        if not feed_url:
            skipped.append(journal.get("name"))
            continue
        try:
            payload = http_get(feed_url)
            all_rows.extend(parse_rss_or_atom(payload, journal, args.max_items))
        except Exception as exc:  # Feed endpoints vary by publisher.
            skipped.append(f"{journal.get('name')} ({exc})")
    added = merge_papers(all_rows)
    print(f"Imported {added} new feed items from {len(all_rows)} parsed feed records.")
    if skipped:
        print("Feeds not harvested:")
        for item in skipped:
            print(f"- {item}")


def parse_attrs(tag_attrs: str) -> dict[str, str]:
    attrs = {}
    for match in re.finditer(r"""([a-zA-Z_:.-]+)\s*=\s*(['"])(.*?)\2""", tag_attrs):
        attrs[match.group(1).lower()] = html.unescape(match.group(3))
    return attrs


def meta_values(html_text: str, key: str) -> list[str]:
    values = []
    for tag_attrs in re.findall(r"<meta\s+([^>]+)>", html_text, flags=re.IGNORECASE):
        attrs = parse_attrs(tag_attrs)
        if attrs.get("name", "").lower() == key.lower() or attrs.get("property", "").lower() == key.lower():
            if attrs.get("content"):
                values.append(clean_text(attrs["content"]))
    return values


def first_meta(html_text: str, keys: list[str]) -> str:
    for key in keys:
        values = meta_values(html_text, key)
        if values:
            return values[0]
    return ""


def ssrn_result_ids(html_text: str) -> list[str]:
    ids = re.findall(r"abstract_id=(\d+)", html_text)
    return list(dict.fromkeys(ids))


def ssrn_pdf_links(html_text: str, base_url: str) -> list[str]:
    links = []
    for href in re.findall(r"""href\s*=\s*['"]([^'"]+)['"]""", html_text, flags=re.IGNORECASE):
        if "Delivery.cfm" in href or href.lower().endswith(".pdf"):
            links.append(urllib.parse.urljoin(base_url, html.unescape(href)))
    return list(dict.fromkeys(links))


def parse_ssrn_page(abstract_id: str, html_text: str, url: str, download_pdfs: bool) -> dict[str, Any]:
    rules = load_field_rules()
    title = first_meta(html_text, ["citation_title", "og:title"]) or clean_text(
        re.search(r"<title>(.*?)</title>", html_text, flags=re.IGNORECASE | re.DOTALL).group(1)
        if re.search(r"<title>(.*?)</title>", html_text, flags=re.IGNORECASE | re.DOTALL)
        else ""
    )
    authors = meta_values(html_text, "citation_author")
    abstract = first_meta(html_text, ["description", "og:description"])
    doi = first_meta(html_text, ["citation_doi"])
    publication_date = first_meta(html_text, ["citation_publication_date", "citation_online_date"])
    pdf_links = ssrn_pdf_links(html_text, url)
    downloaded_pdf = ""

    if download_pdfs and pdf_links:
        SSRN_PDF_DIR.mkdir(parents=True, exist_ok=True)
        target = SSRN_PDF_DIR / f"ssrn-{abstract_id}.pdf"
        try:
            target.write_bytes(http_get(pdf_links[0]))
            downloaded_pdf = str(target)
        except Exception:
            downloaded_pdf = ""

    fields = classify(f"{title}\n{abstract}", rules)
    return {
        "id": f"ssrn:{abstract_id}",
        "doi": doi,
        "title": title,
        "publication_date": publication_date,
        "publication_year": int(publication_date[:4]) if publication_date[:4].isdigit() else None,
        "authors": authors,
        "venue": "SSRN",
        "publisher": "Social Science Research Network",
        "source_tier": "ssrn",
        "quality_score": 4 if abstract and fields != ["Unclassified"] else 2,
        "quality_reasons": ["SSRN result"] + (["has abstract"] if abstract else []) + (["accounting relevant"] if fields != ["Unclassified"] else []),
        "cited_by_count": 0,
        "url": url,
        "landing_page_url": url,
        "pdf_url": pdf_links[0] if pdf_links else "",
        "downloaded_pdf": downloaded_pdf,
        "fields": fields,
        "method": "not stated in available metadata",
        "data": "not stated in available metadata",
        "main_findings": "not stated in available metadata",
        "abstract": abstract,
        "referenced_works": [],
        "collected_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }


def search_ssrn(args: argparse.Namespace) -> None:
    queries = [args.query] if args.query else read_json(SSRN_QUERIES_CONFIG, default={"queries": []}).get("queries", [])
    rows: list[dict[str, Any]] = []
    for query in queries:
        search_url = "https://papers.ssrn.com/sol3/results.cfm?" + urllib.parse.urlencode(
            {"RequestTimeout": "50000000", "sort": "desc", "srcabs": query}
        )
        try:
            result_html = http_get(search_url).decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            if exc.code == 403:
                raise SystemExit(
                    "SSRN blocked the automated search request with HTTP 403. "
                    "Use BibTeX/Zotero import for SSRN papers, or run this command from a network/session SSRN allows."
                ) from exc
            raise
        for abstract_id in ssrn_result_ids(result_html)[: args.max_results]:
            paper_url = f"https://papers.ssrn.com/sol3/papers.cfm?abstract_id={abstract_id}"
            try:
                page_html = http_get(paper_url).decode("utf-8", errors="replace")
                rows.append(parse_ssrn_page(abstract_id, page_html, paper_url, args.download_pdfs))
            except Exception as exc:
                print(f"Skipped SSRN abstract {abstract_id}: {exc}")
    added = merge_papers(rows)
    print(f"Imported {added} new SSRN papers from {len(rows)} parsed SSRN records.")


def split_bibtex_entries(text: str) -> list[tuple[str, str, str]]:
    entries = []
    index = 0
    while True:
        at = text.find("@", index)
        if at == -1:
            break
        brace = text.find("{", at)
        if brace == -1:
            break
        entry_type = text[at + 1 : brace].strip().lower()
        depth = 0
        end = brace
        while end < len(text):
            if text[end] == "{":
                depth += 1
            elif text[end] == "}":
                depth -= 1
                if depth == 0:
                    break
            end += 1
        body = text[brace + 1 : end]
        citation_key, _, fields_body = body.partition(",")
        entries.append((entry_type, citation_key.strip(), fields_body))
        index = end + 1
    return entries


def parse_bibtex_fields(fields_body: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    pattern = re.compile(r"([a-zA-Z][a-zA-Z0-9_-]*)\s*=\s*")
    matches = list(pattern.finditer(fields_body))
    for i, match in enumerate(matches):
        key = match.group(1).lower()
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(fields_body)
        raw_value = fields_body[start:end].strip().rstrip(",").strip()
        fields[key] = clean_bibtex_value(raw_value)
    return fields


def clean_bibtex_value(value: str) -> str:
    value = value.strip()
    if (value.startswith("{") and value.endswith("}")) or (value.startswith('"') and value.endswith('"')):
        value = value[1:-1]
    value = value.replace("\n", " ")
    value = re.sub(r"\s+", " ", value)
    return html.unescape(value.strip())


def import_bibtex(args: argparse.Namespace) -> None:
    text = Path(args.path).read_text(encoding="utf-8")
    rules = load_field_rules()
    rows = []
    for entry_type, citation_key, fields in (
        (entry_type, key, parse_bibtex_fields(body))
        for entry_type, key, body in split_bibtex_entries(text)
    ):
        title = fields.get("title", "")
        abstract = fields.get("abstract", "")
        authors = [part.strip() for part in fields.get("author", "").split(" and ") if part.strip()]
        paper_fields = classify(f"{title}\n{abstract}", rules)
        rows.append({
            "id": f"bibtex:{citation_key}",
            "doi": fields.get("doi", ""),
            "title": title,
            "publication_date": fields.get("year", ""),
            "publication_year": int(fields["year"]) if fields.get("year", "").isdigit() else None,
            "authors": authors,
            "venue": fields.get("journal") or fields.get("booktitle") or "",
            "publisher": fields.get("publisher", ""),
            "source_tier": "bibtex_import",
            "quality_score": 3 if title else 0,
            "quality_reasons": ["BibTeX import"],
            "cited_by_count": 0,
            "url": fields.get("url", ""),
            "landing_page_url": fields.get("url", ""),
            "fields": paper_fields,
            "method": "not stated in available metadata",
            "data": "not stated in available metadata",
            "main_findings": "not stated in available metadata",
            "abstract": abstract,
            "referenced_works": [],
            "bibtex_entry_type": entry_type,
            "collected_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        })
    added = merge_papers(rows)
    print(f"Imported {added} new papers from {len(rows)} BibTeX entries.")


def sync_zotero(args: argparse.Namespace) -> None:
    api_key = args.api_key or os.environ.get("ZOTERO_API_KEY", "")
    library_path = "users" if args.library_type == "user" else "groups"
    collection_part = f"/collections/{args.collection_key}" if args.collection_key else ""
    url = (
        f"https://api.zotero.org/{library_path}/{args.library_id}{collection_part}/items?"
        + urllib.parse.urlencode({"format": "json", "limit": str(args.limit), "sort": "dateModified", "direction": "desc"})
    )
    headers = {"Zotero-API-Version": "3"}
    if api_key:
        headers["Zotero-API-Key"] = api_key
    payload = http_json(url, headers=headers)
    rules = load_field_rules()
    rows = []
    for item in payload:
        data = item.get("data", {})
        title = data.get("title", "")
        abstract = data.get("abstractNote", "")
        authors = [
            " ".join(part for part in [creator.get("firstName", ""), creator.get("lastName", "")] if part).strip()
            for creator in data.get("creators", [])
        ]
        authors = [author for author in authors if author]
        fields = classify(f"{title}\n{abstract}", rules)
        rows.append({
            "id": f"zotero:{item.get('key')}",
            "doi": data.get("DOI", ""),
            "title": title,
            "publication_date": data.get("date", ""),
            "publication_year": None,
            "authors": authors,
            "venue": data.get("publicationTitle", ""),
            "publisher": data.get("publisher", ""),
            "source_tier": "zotero",
            "quality_score": 4 if abstract else 2,
            "quality_reasons": ["Zotero sync"] + (["has abstract"] if abstract else []),
            "cited_by_count": 0,
            "url": data.get("url", ""),
            "landing_page_url": data.get("url", ""),
            "fields": fields,
            "method": "not stated in available metadata",
            "data": "not stated in available metadata",
            "main_findings": "not stated in available metadata",
            "abstract": abstract,
            "referenced_works": [],
            "zotero_key": item.get("key"),
            "collected_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        })
    added = merge_papers(rows)
    print(f"Imported {added} new papers from {len(rows)} Zotero items.")


def response_output_text(response: dict[str, Any]) -> str:
    if response.get("output_text"):
        return response["output_text"]
    chunks = []
    for item in response.get("output", []):
        for content in item.get("content", []):
            if content.get("type") in {"output_text", "text"} and content.get("text"):
                chunks.append(content["text"])
    return "\n".join(chunks)


def enrich_with_openai(args: argparse.Namespace) -> None:
    api_key = args.api_key or os.environ.get("OPENAI_API_KEY", "")
    model = args.model or os.environ.get("OPENAI_MODEL", "")
    if not api_key:
        raise SystemExit("OPENAI_API_KEY is required for enrichment.")
    if not model:
        raise SystemExit("Set OPENAI_MODEL or pass --model for enrichment.")

    prompt_template = EXTRACTION_PROMPT.read_text(encoding="utf-8")
    papers = read_jsonl(PAPERS_PATH)
    enriched = 0
    for paper in papers:
        if enriched >= args.limit:
            break
        if paper.get("llm_enrichment"):
            continue
        source_package = textwrap.dedent(
            f"""
            Title: {paper.get('title')}
            Authors: {', '.join(paper.get('authors') or [])}
            Venue: {paper.get('venue')}
            Date: {paper.get('publication_date')}
            DOI: {paper.get('doi')}
            Abstract: {paper.get('abstract')}
            """
        ).strip()
        payload = {
            "model": model,
            "instructions": prompt_template,
            "input": source_package,
            "max_output_tokens": args.max_output_tokens,
        }
        request = urllib.request.Request(
            "https://api.openai.com/v1/responses",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=120, context=ssl_context()) as response:
            result = json.loads(response.read().decode("utf-8"))
        text = response_output_text(result)
        try:
            paper["llm_enrichment"] = json.loads(text)
        except json.JSONDecodeError:
            paper["llm_enrichment_raw"] = text
        paper["llm_enriched_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        enriched += 1
    write_jsonl(PAPERS_PATH, papers)
    print(f"Enriched {enriched} papers with OpenAI.")


def extract_pdf_text(path: Path) -> str:
    try:
        from pypdf import PdfReader  # type: ignore
    except ImportError:
        try:
            from PyPDF2 import PdfReader  # type: ignore
        except ImportError as exc:
            raise SystemExit("Install pypdf or PyPDF2 to extract PDF text.") from exc
    reader = PdfReader(str(path))
    pages = []
    for page in reader.pages:
        pages.append(page.extract_text() or "")
    return "\n\n".join(pages).strip()


def extract_pdf(args: argparse.Namespace) -> None:
    path = Path(args.path).expanduser().resolve()
    text = extract_pdf_text(path)
    PDF_TEXT_DIR.mkdir(parents=True, exist_ok=True)
    output_name = slugify(args.paper_id or path.stem, "paper") + ".txt"
    output_path = PDF_TEXT_DIR / output_name
    output_path.write_text(text, encoding="utf-8")
    if args.paper_id:
        papers = read_jsonl(PAPERS_PATH)
        for paper in papers:
            if paper.get("id") == args.paper_id or paper.get("doi") == args.paper_id:
                paper["pdf_text_path"] = str(output_path)
                paper["pdf_text_extracted_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        write_jsonl(PAPERS_PATH, papers)
    print(f"Extracted {len(text)} characters to {output_path}.")


def markdown_escape(value: Any) -> str:
    return str(value or "").replace("\n", " ").strip()


def export_obsidian(_: argparse.Namespace) -> None:
    papers = read_jsonl(PAPERS_PATH)
    ideas = read_jsonl(IDEAS_PATH)
    papers_dir = EXPORTS_DIR / "obsidian" / "papers"
    ideas_dir = EXPORTS_DIR / "obsidian" / "ideas"
    papers_dir.mkdir(parents=True, exist_ok=True)
    ideas_dir.mkdir(parents=True, exist_ok=True)

    for paper in papers:
        title = paper.get("title") or paper.get("id") or "Untitled paper"
        path = papers_dir / f"{slugify(title, 'paper')}.md"
        fields = ", ".join(paper.get("fields") or [])
        authors = ", ".join(paper.get("authors") or [])
        content = f"""# {markdown_escape(title)}

## Metadata

- Authors: {markdown_escape(authors)}
- Venue: {markdown_escape(paper.get('venue'))}
- Date: {markdown_escape(paper.get('publication_date'))}
- DOI: {markdown_escape(paper.get('doi'))}
- Fields: {markdown_escape(fields)}
- Source tier: {markdown_escape(paper.get('source_tier'))}
- URL: {markdown_escape(paper.get('landing_page_url') or paper.get('url'))}

## Method

{markdown_escape(paper.get('method'))}

## Data

{markdown_escape(paper.get('data'))}

## Main Findings

{markdown_escape(paper.get('main_findings'))}

## Abstract

{paper.get('abstract') or ''}
"""
        path.write_text(content, encoding="utf-8")

    for idea in ideas:
        title = idea.get("title") or idea.get("id") or "Untitled idea"
        path = ideas_dir / f"{slugify(title, 'idea')}.md"
        content = f"""# {markdown_escape(title)}

- Field: {markdown_escape(idea.get('field'))}
- Created at: {markdown_escape(idea.get('created_at'))}
- Related papers: {markdown_escape(', '.join(idea.get('related_papers') or []))}

## Note

{idea.get('note') or ''}
"""
        path.write_text(content, encoding="utf-8")
    print(f"Exported {len(papers)} paper notes and {len(ideas)} idea notes to {EXPORTS_DIR / 'obsidian'}.")


def export_notion(_: argparse.Namespace) -> None:
    papers = read_jsonl(PAPERS_PATH)
    ideas = read_jsonl(IDEAS_PATH)
    notion_dir = EXPORTS_DIR / "notion"
    notion_dir.mkdir(parents=True, exist_ok=True)

    with (notion_dir / "papers.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["title", "authors", "venue", "publication_date", "fields", "source_tier", "doi", "url", "method", "data", "main_findings"],
        )
        writer.writeheader()
        for paper in papers:
            writer.writerow({
                "title": paper.get("title", ""),
                "authors": "; ".join(paper.get("authors") or []),
                "venue": paper.get("venue", ""),
                "publication_date": paper.get("publication_date", ""),
                "fields": "; ".join(paper.get("fields") or []),
                "source_tier": paper.get("source_tier", ""),
                "doi": paper.get("doi", ""),
                "url": paper.get("landing_page_url") or paper.get("url", ""),
                "method": paper.get("method", ""),
                "data": paper.get("data", ""),
                "main_findings": paper.get("main_findings", ""),
            })

    with (notion_dir / "ideas.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["title", "field", "note", "related_papers", "created_at"])
        writer.writeheader()
        for idea in ideas:
            writer.writerow({
                "title": idea.get("title", ""),
                "field": idea.get("field", ""),
                "note": idea.get("note", ""),
                "related_papers": "; ".join(idea.get("related_papers") or []),
                "created_at": idea.get("created_at", ""),
            })
    print(f"Exported Notion-ready CSV files to {notion_dir}.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run optional integrations for the accounting research agent.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    feeds_parser = subparsers.add_parser("feeds")
    feeds_sub = feeds_parser.add_subparsers(dest="feeds_command", required=True)
    harvest_parser = feeds_sub.add_parser("harvest")
    harvest_parser.add_argument("--max-items", type=int, default=20)
    harvest_parser.set_defaults(func=harvest_feeds)

    ssrn_parser = subparsers.add_parser("ssrn")
    ssrn_sub = ssrn_parser.add_subparsers(dest="ssrn_command", required=True)
    search_parser = ssrn_sub.add_parser("search")
    search_parser.add_argument("--query")
    search_parser.add_argument("--max-results", type=int, default=10)
    search_parser.add_argument("--download-pdfs", action="store_true")
    search_parser.set_defaults(func=search_ssrn)

    bibtex_parser = subparsers.add_parser("bibtex")
    bibtex_sub = bibtex_parser.add_subparsers(dest="bibtex_command", required=True)
    import_parser = bibtex_sub.add_parser("import")
    import_parser.add_argument("--path", required=True)
    import_parser.set_defaults(func=import_bibtex)

    zotero_parser = subparsers.add_parser("zotero")
    zotero_sub = zotero_parser.add_subparsers(dest="zotero_command", required=True)
    sync_parser = zotero_sub.add_parser("sync")
    sync_parser.add_argument("--library-id", required=True)
    sync_parser.add_argument("--library-type", choices=["user", "group"], default="user")
    sync_parser.add_argument("--collection-key")
    sync_parser.add_argument("--api-key")
    sync_parser.add_argument("--limit", type=int, default=50)
    sync_parser.set_defaults(func=sync_zotero)

    enrich_parser = subparsers.add_parser("enrich")
    enrich_parser.add_argument("--limit", type=int, default=5)
    enrich_parser.add_argument("--model")
    enrich_parser.add_argument("--api-key")
    enrich_parser.add_argument("--max-output-tokens", type=int, default=1200)
    enrich_parser.set_defaults(func=enrich_with_openai)

    pdf_parser = subparsers.add_parser("pdf")
    pdf_sub = pdf_parser.add_subparsers(dest="pdf_command", required=True)
    extract_parser = pdf_sub.add_parser("extract")
    extract_parser.add_argument("--path", required=True)
    extract_parser.add_argument("--paper-id")
    extract_parser.set_defaults(func=extract_pdf)

    export_parser = subparsers.add_parser("export")
    export_sub = export_parser.add_subparsers(dest="export_command", required=True)
    obsidian_parser = export_sub.add_parser("obsidian")
    obsidian_parser.set_defaults(func=export_obsidian)
    notion_parser = export_sub.add_parser("notion")
    notion_parser.set_defaults(func=export_notion)

    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        args.func(args)
    except urllib.error.URLError as exc:
        raise SystemExit(f"Network request failed: {exc}") from exc


if __name__ == "__main__":
    main()
