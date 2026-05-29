#!/usr/bin/env python3
"""Optional integrations for the accounting research agent.

This script keeps network-heavy and provider-specific workflows separate from
the core OpenAlex collector. It still reads and writes the same paper library.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
import html
import json
import os
import re
import smtplib
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
CROSSREF_MAILTO = "accounting-research-agent@example.com"


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


def normalize_doi(value: str | None) -> str:
    if not value:
        return ""
    value = value.strip()
    value = re.sub(r"^https?://(dx\.)?doi\.org/", "", value, flags=re.IGNORECASE)
    value = re.sub(r"^doi:\s*", "", value, flags=re.IGNORECASE)
    return value.strip()


def doi_from_url(value: str | None) -> str:
    if not value:
        return ""
    match = re.search(r"(10\.\d{4,9}/[^\s?#]+)", urllib.parse.unquote(value), flags=re.IGNORECASE)
    return normalize_doi(match.group(1)) if match else ""


def crossref_get(path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
    query = {"mailto": CROSSREF_MAILTO, **(params or {})}
    url = "https://api.crossref.org/" + path.lstrip("/")
    if query:
        url += "?" + urllib.parse.urlencode(query)
    return http_json(url, headers={"Accept": "application/json"})


def crossref_lookup_by_doi(doi: str) -> dict[str, Any] | None:
    doi = normalize_doi(doi)
    if not doi:
        return None
    try:
        payload = crossref_get(f"works/{urllib.parse.quote(doi, safe='/')}")
    except Exception:
        return None
    return (payload.get("message") or {}) if isinstance(payload, dict) else None


def crossref_lookup_by_title(title: str, venue: str | None = None) -> dict[str, Any] | None:
    if not title:
        return None
    params = {"query.title": title, "rows": "3"}
    if venue:
        params["query.container-title"] = venue
    try:
        payload = crossref_get("works", params)
    except Exception:
        return None
    items = ((payload.get("message") or {}).get("items") or []) if isinstance(payload, dict) else []
    normalized_title = re.sub(r"\s+", " ", title).strip().lower()
    for item in items:
        item_title = " ".join(item.get("title") or [])
        if re.sub(r"\s+", " ", item_title).strip().lower() == normalized_title:
            return item
    return items[0] if items else None


def crossref_authors(item: dict[str, Any]) -> list[str]:
    authors = []
    for author in item.get("author") or []:
        name = " ".join(part for part in [author.get("given", ""), author.get("family", "")] if part).strip()
        if name:
            authors.append(name)
    return authors


def crossref_date(item: dict[str, Any]) -> str:
    for key in ["published-online", "published-print", "published", "issued"]:
        date_parts = (item.get(key) or {}).get("date-parts") or []
        if date_parts and date_parts[0]:
            parts = [str(part) for part in date_parts[0]]
            if len(parts) == 1:
                return parts[0]
            if len(parts) == 2:
                return f"{parts[0]}-{int(parts[1]):02d}"
            return f"{parts[0]}-{int(parts[1]):02d}-{int(parts[2]):02d}"
    return ""


def apply_crossref_metadata(row: dict[str, Any], item: dict[str, Any]) -> bool:
    changed = False
    title = " ".join(item.get("title") or []).strip()
    abstract = clean_text(item.get("abstract"))
    authors = crossref_authors(item)
    doi = normalize_doi(item.get("DOI"))
    container = " ".join(item.get("container-title") or []).strip()
    publisher = item.get("publisher") or ""
    publication_date = crossref_date(item)
    url = item.get("URL") or ""

    updates = {
        "doi": doi,
        "title": title,
        "abstract": abstract,
        "publication_date": publication_date,
        "venue": container,
        "publisher": publisher,
        "landing_page_url": url,
    }
    for key, value in updates.items():
        if value and (not row.get(key) or row.get(key) == "not available"):
            row[key] = value
            changed = True
    if authors and not row.get("authors"):
        row["authors"] = authors
        changed = True
    if doi and not row.get("url", "").startswith("https://openalex.org"):
        row["url"] = row.get("url") or f"https://doi.org/{doi}"

    if changed:
        try:
            import accounting_literature_agent as core
            text = f"{row.get('title')}\n{row.get('abstract')}"
            rules = core.load_field_rules()
            row["fields"] = core.classify(text, rules)
            row["method"] = core.infer_method(text)
            row["data"] = core.infer_data(text)
            row["main_findings"] = core.short_finding(row.get("abstract") or "")
        except Exception:
            pass
        reasons = row.get("quality_reasons") or []
        if "Crossref metadata" not in reasons:
            row["quality_reasons"] = reasons + ["Crossref metadata"]
        row["crossref_enriched_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    return changed


def slugify(value: str, fallback: str = "item") -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return slug[:90] or fallback


def unique_markdown_path(directory: Path, slug: str) -> Path:
    path = directory / f"{slug}.md"
    if not path.exists():
        return path
    index = 2
    while True:
        candidate = directory / f"{slug}-{index}.md"
        if not candidate.exists():
            return candidate
        index += 1


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
    row = {
        "id": link or f"feed:{journal.get('name')}:{title}",
        "doi": doi_from_url(link),
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
    item = crossref_lookup_by_doi(row["doi"]) if row["doi"] else crossref_lookup_by_title(title, journal.get("name"))
    if item:
        apply_crossref_metadata(row, item)
    return row


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


def enrich_crossref_metadata(args: argparse.Namespace) -> None:
    papers = read_jsonl(PAPERS_PATH)
    changed = 0
    checked = 0
    eligible_tiers = set(args.source_tier or ["priority_journal", "journal_feed"])

    for paper in papers:
        if checked >= args.limit:
            break
        if paper.get("source_tier") not in eligible_tiers:
            continue
        if paper.get("crossref_enriched_at") and not args.refresh:
            continue

        checked += 1
        doi = normalize_doi(paper.get("doi")) or doi_from_url(paper.get("landing_page_url") or paper.get("url"))
        item = crossref_lookup_by_doi(doi) if doi else None
        if not item and args.allow_title_lookup:
            item = crossref_lookup_by_title(paper.get("title") or "", paper.get("venue"))
        if item and apply_crossref_metadata(paper, item):
            changed += 1

    write_jsonl(PAPERS_PATH, papers)
    refresh_report()
    print(f"Checked {checked} papers and enriched {changed} records with Crossref metadata.")


def response_output_text(response: dict[str, Any]) -> str:
    if response.get("output_text"):
        return response["output_text"]
    chunks = []
    for item in response.get("output", []):
        for content in item.get("content", []):
            if content.get("type") in {"output_text", "text"} and content.get("text"):
                chunks.append(content["text"])
    return "\n".join(chunks)


def read_pdf_text_for_enrichment(paper: dict[str, Any], max_chars: int) -> str:
    path_value = paper.get("pdf_text_path")
    if not path_value:
        return ""
    path = Path(path_value)
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")[:max_chars]


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
        full_text = read_pdf_text_for_enrichment(paper, args.max_full_text_chars)
        source_package = textwrap.dedent(
            f"""
            Title: {paper.get('title')}
            Authors: {', '.join(paper.get('authors') or [])}
            Venue: {paper.get('venue')}
            Date: {paper.get('publication_date')}
            DOI: {paper.get('doi')}
            Abstract: {paper.get('abstract')}
            Full text excerpt: {full_text or 'not available'}
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


def analysis_status(paper: dict[str, Any]) -> str:
    if paper.get("llm_enrichment") and paper.get("pdf_text_path"):
        return "enriched and full-text analyzed"
    if paper.get("llm_enrichment"):
        return "enriched with LLM"
    if paper.get("pdf_text_path"):
        return "full-text extracted"
    return "metadata only"


def first_sentence(value: str, fallback: str = "The available metadata is too thin to summarize this paper confidently.") -> str:
    text = re.sub(r"\s+", " ", value or "").strip()
    text = re.sub(r"^ABSTRACT\s+", "", text, flags=re.IGNORECASE)
    if not text or text == "not stated in available metadata":
        return fallback
    sentences = re.split(r"(?<=[.!?])\s+", text)
    return (sentences[0] if sentences else text)[:460]


def has_substantive_metadata(paper: dict[str, Any]) -> bool:
    abstract = re.sub(r"\s+", " ", paper.get("abstract") or "").strip().lower()
    if len(abstract) < 80:
        return False
    return not any(marker in abstract for marker in ["earlyview", "early view", "current issue"])


def radar_brief(paper: dict[str, Any]) -> str:
    fields = ", ".join(paper.get("fields") or ["Unclassified"])
    venue = paper.get("venue") or "unknown venue"
    if has_substantive_metadata(paper):
        source_text = paper.get("main_findings") if paper.get("main_findings") != "not stated in available metadata" else paper.get("abstract")
        signal = first_sentence(source_text)
        innovation = first_sentence(paper.get("abstract") or paper.get("main_findings") or "")
    else:
        signal = "The available metadata is too thin for a confident content summary."
        innovation = "The possible innovation is not visible yet; treat this as a discovery lead until publisher metadata or PDF text is available."

    if paper.get("source_tier") == "priority_journal":
        quality = f"Its quality potential comes from venue fit: it appears in {venue}, a tracked priority accounting venue, and connects to {fields}."
    elif paper.get("doi") and paper.get("referenced_works"):
        quality = f"Its quality potential is preliminary but stronger than a generic web hit because it has a DOI, references, and a visible connection to {fields}."
    else:
        quality = f"It is worth a quick screen because the metadata connects it to {fields}, but venue fit and design still need checking."

    return (
        f"Metadata signal: {signal} "
        f"Possible innovation: {innovation} "
        f"{quality} Method, data, and identification are intentionally left unclaimed until PDF/full-text enrichment."
    )


def count_by(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        values = row.get(key)
        if isinstance(values, list):
            for value in values or ["Unclassified"]:
                counts[str(value)] = counts.get(str(value), 0) + 1
        else:
            value = str(values or "unknown")
            counts[value] = counts.get(value, 0) + 1
    return counts


def bar_chart(title: str, counts: dict[str, int]) -> str:
    if not counts:
        return ""
    max_count = max(counts.values()) or 1
    rows = []
    for label, count in sorted(counts.items(), key=lambda item: item[1], reverse=True):
        width = max(4, round((count / max_count) * 100))
        rows.append(
            f"""
            <div class="bar-row">
              <span class="bar-label">{html.escape(label)}</span>
              <span class="bar-track"><span class="bar-fill" style="width: {width}%"></span></span>
              <span class="bar-count">{count}</span>
            </div>
            """
        )
    return f"""
    <section class="panel">
      <h2>{html.escape(title)}</h2>
      <div class="bars">{''.join(rows)}</div>
    </section>
    """


def option_tags(values: list[str]) -> str:
    return "".join(f'<option value="{html.escape(value)}">{html.escape(value)}</option>' for value in values)


def export_radar(_: argparse.Namespace) -> None:
    papers = read_jsonl(PAPERS_PATH)
    radar_dir = EXPORTS_DIR / "radar"
    radar_dir.mkdir(parents=True, exist_ok=True)
    output_path = radar_dir / "index.html"

    total = len(papers)
    enriched_count = sum(1 for paper in papers if analysis_status(paper) != "metadata only")
    metadata_count = total - enriched_count
    priority_count = sum(1 for paper in papers if paper.get("source_tier") == "priority_journal")
    supplemental_count = sum(1 for paper in papers if paper.get("source_tier") == "supplemental")
    feed_count = sum(1 for paper in papers if paper.get("source_tier") == "journal_feed")
    field_counts = count_by(papers, "fields")
    tier_counts = count_by(papers, "source_tier")
    status_counts = {}
    for paper in papers:
        status = analysis_status(paper)
        status_counts[status] = status_counts.get(status, 0) + 1

    top_fields = sorted(field_counts.items(), key=lambda item: item[1], reverse=True)[:3]
    top_field_text = ", ".join(f"{field} ({count})" for field, count in top_fields) or "not enough field data yet"
    screen_candidates = sorted(
        papers,
        key=lambda row: (
            int(row.get("quality_score") or 0),
            1 if row.get("source_tier") == "priority_journal" else 0,
            1 if has_substantive_metadata(row) else 0,
        ),
        reverse=True,
    )[:5]

    fields = sorted(field_counts)
    tiers = sorted(tier_counts)
    statuses = sorted(status_counts)
    cards = []
    for index, paper in enumerate(papers, start=1):
        title = paper.get("title") or "Untitled paper"
        fields_text = ", ".join(paper.get("fields") or ["Unclassified"])
        authors = ", ".join((paper.get("authors") or [])[:3])
        if len(paper.get("authors") or []) > 3:
            authors += " et al."
        url = paper.get("landing_page_url") or paper.get("url") or "#"
        status = analysis_status(paper)
        source_tier = paper.get("source_tier") or "unknown"
        brief = radar_brief(paper)
        card_fields = "|".join(paper.get("fields") or ["Unclassified"])
        searchable = " ".join([
            title,
            fields_text,
            paper.get("venue") or "",
            authors,
            brief,
        ]).lower()
        cards.append(f"""
        <article class="paper-card" data-field="{html.escape(card_fields)}" data-tier="{html.escape(source_tier)}" data-status="{html.escape(status)}" data-search="{html.escape(searchable)}">
          <div class="card-topline">
            <span class="rank">#{index}</span>
            <span class="badge">{html.escape(source_tier.replace('_', ' '))}</span>
            <span class="badge muted">{html.escape(status)}</span>
          </div>
          <h3>{html.escape(title)}</h3>
          <p class="meta">{html.escape(paper.get('venue') or 'unknown venue')} · {html.escape(paper.get('publication_date') or 'unknown date')}</p>
          <p class="meta">{html.escape(authors or 'authors not available')}</p>
          <p class="fields">{html.escape(fields_text)}</p>
          <p class="brief">{html.escape(brief)}</p>
          <a class="paper-link" href="{html.escape(url)}" target="_blank" rel="noopener">Open source</a>
        </article>
        """)

    candidate_items = "".join(
        f"<li><strong>{html.escape(paper.get('title') or 'Untitled paper')}</strong><span>{html.escape(paper.get('venue') or 'unknown venue')} · {html.escape(', '.join(paper.get('fields') or []))}</span></li>"
        for paper in screen_candidates
    )

    generated_at = dt.datetime.now().isoformat(timespec="seconds")
    html_text = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Accounting Research Radar</title>
  <style>
    :root {{
      --ink: #17201a;
      --muted: #5d685f;
      --line: #d9dfd8;
      --paper: #fbfcf8;
      --panel: #ffffff;
      --accent: #2f7d66;
      --accent-2: #9b5f2f;
      --soft: #edf5f1;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--paper);
      color: var(--ink);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      line-height: 1.5;
    }}
    header {{
      padding: 36px clamp(18px, 4vw, 56px) 24px;
      border-bottom: 1px solid var(--line);
      background: linear-gradient(180deg, #ffffff, #f6faf5);
    }}
    main {{ padding: 24px clamp(18px, 4vw, 56px) 48px; }}
    h1 {{ margin: 0 0 10px; font-size: clamp(32px, 5vw, 56px); letter-spacing: 0; }}
    h2 {{ margin: 0 0 16px; font-size: 18px; }}
    h3 {{ margin: 10px 0 8px; font-size: 20px; line-height: 1.25; }}
    p {{ margin: 0; }}
    .subtitle {{ max-width: 900px; color: var(--muted); font-size: 17px; }}
    .timestamp {{ margin-top: 14px; color: var(--muted); font-size: 13px; }}
    .metrics {{
      display: grid;
      grid-template-columns: repeat(5, minmax(120px, 1fr));
      gap: 12px;
      margin-top: 24px;
    }}
    .metric {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
    }}
    .metric strong {{ display: block; font-size: 28px; line-height: 1; margin-bottom: 6px; }}
    .metric span {{ color: var(--muted); font-size: 13px; }}
    .grid {{
      display: grid;
      grid-template-columns: minmax(0, 1.4fr) minmax(280px, .8fr);
      gap: 18px;
      align-items: start;
    }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 18px;
      margin-bottom: 18px;
    }}
    .summary {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(280px, .75fr);
      gap: 18px;
    }}
    .summary p {{ color: var(--muted); }}
    .priority-list {{ margin: 0; padding-left: 20px; }}
    .priority-list li {{ margin: 0 0 12px; }}
    .priority-list span {{ display: block; color: var(--muted); font-size: 13px; margin-top: 2px; }}
    .bar-row {{
      display: grid;
      grid-template-columns: minmax(150px, 240px) minmax(120px, 1fr) 42px;
      gap: 10px;
      align-items: center;
      margin: 10px 0;
      font-size: 13px;
    }}
    .bar-label {{ color: var(--ink); }}
    .bar-track {{ height: 10px; background: #edf0eb; border-radius: 999px; overflow: hidden; }}
    .bar-fill {{ display: block; height: 100%; background: var(--accent); border-radius: inherit; }}
    .bar-count {{ text-align: right; color: var(--muted); }}
    .controls {{
      position: sticky;
      top: 0;
      z-index: 2;
      display: grid;
      grid-template-columns: minmax(220px, 1fr) repeat(3, minmax(150px, 220px));
      gap: 10px;
      background: rgba(251, 252, 248, .95);
      backdrop-filter: blur(12px);
      padding: 12px 0;
      margin-bottom: 12px;
    }}
    input, select {{
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      color: var(--ink);
      padding: 10px 12px;
      font: inherit;
    }}
    .count-line {{ color: var(--muted); margin: 0 0 12px; }}
    .paper-card {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 18px;
      margin-bottom: 14px;
    }}
    .card-topline {{ display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }}
    .rank {{ color: var(--muted); font-size: 13px; }}
    .badge {{
      display: inline-flex;
      align-items: center;
      min-height: 24px;
      padding: 3px 8px;
      border-radius: 999px;
      background: var(--soft);
      color: #245b4c;
      font-size: 12px;
      text-transform: capitalize;
    }}
    .badge.muted {{ background: #f4efe8; color: #79512e; }}
    .meta {{ color: var(--muted); font-size: 13px; margin-top: 3px; }}
    .fields {{ color: var(--accent); font-weight: 650; margin-top: 8px; }}
    .brief {{ margin-top: 10px; color: #2e3932; }}
    .paper-link {{
      display: inline-flex;
      margin-top: 12px;
      color: var(--accent);
      font-weight: 700;
      text-decoration: none;
    }}
    .paper-link:hover {{ text-decoration: underline; }}
    .empty {{ display: none; padding: 28px; text-align: center; color: var(--muted); }}
    @media (max-width: 900px) {{
      .metrics, .grid, .summary, .controls {{ grid-template-columns: 1fr; }}
      .bar-row {{ grid-template-columns: 1fr; gap: 5px; }}
      .bar-count {{ text-align: left; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>Accounting Research Radar</h1>
    <p class="subtitle">A rapid, metadata-based view of new accounting research. This first layer is for discovery and prioritization; method, data, and identification are reserved for the deep-reading layer after PDF extraction and OpenAI enrichment.</p>
    <p class="timestamp">Generated at {html.escape(generated_at)}</p>
    <section class="metrics">
      <div class="metric"><strong>{total}</strong><span>Total papers</span></div>
      <div class="metric"><strong>{priority_count}</strong><span>Priority journal papers</span></div>
      <div class="metric"><strong>{supplemental_count}</strong><span>Supplemental papers</span></div>
      <div class="metric"><strong>{feed_count}</strong><span>Feed-only alerts</span></div>
      <div class="metric"><strong>{enriched_count}</strong><span>Deep enriched</span></div>
    </section>
  </header>
  <main>
    <section class="summary">
      <div class="panel">
        <h2>Macro Read</h2>
        <p>The current radar is concentrated in {html.escape(top_field_text)}. There are {metadata_count} metadata-only papers waiting for screening and {enriched_count} papers with deep-reading evidence. Use the cards below to decide which papers deserve PDF collection and full enrichment.</p>
      </div>
      <div class="panel">
        <h2>Priority Screening Queue</h2>
        <ol class="priority-list">{candidate_items}</ol>
      </div>
    </section>
    <section class="grid">
      <div>
        {bar_chart("Field Distribution", field_counts)}
      </div>
      <div>
        {bar_chart("Source Mix", tier_counts)}
        {bar_chart("Analysis Status", status_counts)}
      </div>
    </section>
    <section class="panel">
      <h2>Rapid Radar Cards</h2>
      <div class="controls">
        <input id="search" type="search" placeholder="Search title, venue, field, or brief">
        <select id="field"><option value="">All fields</option>{option_tags(fields)}</select>
        <select id="tier"><option value="">All sources</option>{option_tags(tiers)}</select>
        <select id="status"><option value="">All statuses</option>{option_tags(statuses)}</select>
      </div>
      <p class="count-line"><span id="visibleCount">{total}</span> papers visible</p>
      <div id="cards">{''.join(cards)}</div>
      <div id="empty" class="empty">No papers match the current filters.</div>
    </section>
  </main>
  <script>
    const cards = Array.from(document.querySelectorAll('.paper-card'));
    const search = document.getElementById('search');
    const field = document.getElementById('field');
    const tier = document.getElementById('tier');
    const status = document.getElementById('status');
    const visibleCount = document.getElementById('visibleCount');
    const empty = document.getElementById('empty');

    function applyFilters() {{
      const query = search.value.trim().toLowerCase();
      const selectedField = field.value;
      const selectedTier = tier.value;
      const selectedStatus = status.value;
      let visible = 0;
      for (const card of cards) {{
        const matchesSearch = !query || card.dataset.search.includes(query);
        const matchesField = !selectedField || card.dataset.field.split('|').includes(selectedField);
        const matchesTier = !selectedTier || card.dataset.tier === selectedTier;
        const matchesStatus = !selectedStatus || card.dataset.status === selectedStatus;
        const show = matchesSearch && matchesField && matchesTier && matchesStatus;
        card.style.display = show ? '' : 'none';
        if (show) visible += 1;
      }}
      visibleCount.textContent = visible;
      empty.style.display = visible ? 'none' : 'block';
    }}

    [search, field, tier, status].forEach(control => control.addEventListener('input', applyFilters));
  </script>
</body>
</html>
"""
    output_path.write_text(html_text, encoding="utf-8")
    print(f"Exported radar dashboard for {len(papers)} papers to {output_path}.")


def parse_paper_date(value: str | None) -> dt.date | None:
    if not value:
        return None
    text = str(value).strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}", text):
        try:
            return dt.date.fromisoformat(text[:10])
        except ValueError:
            return None
    if re.match(r"^\d{4}$", text):
        return dt.date(int(text), 1, 1)
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None
    return parsed.date() if parsed else None


def papers_since(papers: list[dict[str, Any]], from_date: str | None) -> list[dict[str, Any]]:
    if not from_date:
        return papers
    try:
        start = dt.date.fromisoformat(from_date)
    except ValueError:
        return papers
    today = dt.date.today()
    selected = []
    for paper in papers:
        paper_date = parse_paper_date(paper.get("publication_date"))
        if paper_date and start <= paper_date <= today:
            selected.append(paper)
    return selected or papers


def recommendation_score(paper: dict[str, Any]) -> int:
    score = int(paper.get("quality_score") or 0) * 10
    if paper.get("source_tier") == "priority_journal":
        score += 30
    if has_substantive_metadata(paper):
        score += 15
    if paper.get("doi"):
        score += 5
    if paper.get("referenced_works"):
        score += 5
    if "Unclassified" not in (paper.get("fields") or []):
        score += 5
    if analysis_status(paper) != "metadata only":
        score += 8
    return score


def select_recommended_papers(papers: list[dict[str, Any]], limit: int, deep_queue: bool = False) -> list[dict[str, Any]]:
    candidates = papers
    if deep_queue:
        candidates = [paper for paper in papers if analysis_status(paper) == "metadata only"]
    return sorted(candidates, key=recommendation_score, reverse=True)[:limit]


def digest_macro_summary(papers: list[dict[str, Any]]) -> str:
    field_counts = count_by(papers, "fields")
    tier_counts = count_by(papers, "source_tier")
    top_fields = sorted(field_counts.items(), key=lambda item: item[1], reverse=True)[:3]
    top_field_text = ", ".join(f"{field} ({count})" for field, count in top_fields) or "not enough field data"
    priority = tier_counts.get("priority_journal", 0)
    supplemental = tier_counts.get("supplemental", 0)
    enriched = sum(1 for paper in papers if analysis_status(paper) != "metadata only")
    return (
        f"This period contains {len(papers)} radar papers. The most active fields are {top_field_text}. "
        f"The source mix is {priority} priority-journal papers and {supplemental} supplemental papers. "
        f"{enriched} papers have deep-reading evidence; the rest should be treated as metadata-only leads."
    )


def digest_paper_entry(paper: dict[str, Any], index: int) -> str:
    title = paper.get("title") or "Untitled paper"
    fields = ", ".join(paper.get("fields") or ["Unclassified"])
    venue = paper.get("venue") or "unknown venue"
    date = paper.get("publication_date") or "unknown date"
    url = paper.get("landing_page_url") or paper.get("url") or ""
    return textwrap.dedent(
        f"""
        {index}. **{title}**
           - Venue/date: {venue}, {date}
           - Field: {fields}
           - Why it is worth screening: {radar_brief(paper)}
           - URL: {url}
        """
    ).strip()


def write_digest(path: Path, title: str, papers: list[dict[str, Any]], recommended: list[dict[str, Any]], from_date: str | None) -> str:
    lines = [
        f"# {title}",
        "",
        f"Generated at: {dt.datetime.now().isoformat(timespec='seconds')}",
        f"Coverage start: {from_date or 'all available records'}",
        "",
        "## Macro Summary",
        "",
        digest_macro_summary(papers),
        "",
        "## Recommended Papers",
        "",
    ]
    if not recommended:
        lines.append("No papers matched the recommendation criteria.")
    else:
        for index, paper in enumerate(recommended, start=1):
            lines.extend([digest_paper_entry(paper, index), ""])
    content = "\n".join(lines).strip() + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return content


def smtp_config(recipient: str | None = None) -> dict[str, Any]:
    port_text = os.environ.get("SMTP_PORT") or "587"
    return {
        "host": os.environ.get("SMTP_HOST", ""),
        "port": int(port_text),
        "username": os.environ.get("SMTP_USERNAME", ""),
        "password": os.environ.get("SMTP_PASSWORD", ""),
        "sender": os.environ.get("SMTP_FROM", os.environ.get("SMTP_USERNAME", "")),
        "recipient": recipient or os.environ.get("EMAIL_RECIPIENT", ""),
        "use_tls": os.environ.get("SMTP_USE_TLS", "true").lower() not in {"0", "false", "no"},
    }


def send_email(subject: str, body: str, recipient: str | None = None) -> None:
    config = smtp_config(recipient)
    missing = [key for key in ["host", "sender", "recipient"] if not config[key]]
    if missing:
        raise SystemExit(f"Missing email configuration: {', '.join(missing)}. Set SMTP_HOST, SMTP_FROM, and EMAIL_RECIPIENT.")

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = config["sender"]
    message["To"] = config["recipient"]
    message.set_content(body)

    with smtplib.SMTP(config["host"], config["port"], timeout=30) as smtp:
        if config["use_tls"]:
            smtp.starttls(context=ssl_context())
        if config["username"] and config["password"]:
            smtp.login(config["username"], config["password"])
        smtp.send_message(message)
    print(f"Sent email digest to {config['recipient']}.")


def collect_for_digest(from_date: str, max_results: int, supplemental_max_results: int) -> None:
    try:
        import accounting_literature_agent as core
    except Exception as exc:
        raise SystemExit(f"Could not import core collector: {exc}") from exc
    args = argparse.Namespace(
        from_date=from_date,
        max_results=max_results,
        supplemental_max_results=supplemental_max_results,
        include_supplemental=True,
    )
    core.collect(args)


def clean_for_digest() -> None:
    try:
        import accounting_literature_agent as core
    except Exception:
        return
    args = argparse.Namespace(dry_run=False)
    core.clean_library(args)


def build_latest_report() -> None:
    try:
        import accounting_literature_agent as core
    except Exception:
        return
    core.build_report()


def generate_monthly_digest(args: argparse.Namespace) -> None:
    from_date = args.from_date or (dt.date.today() - dt.timedelta(days=31)).isoformat()
    if args.collect:
        collect_for_digest(from_date, args.max_results, args.supplemental_max_results)
    if args.clean:
        clean_for_digest()
    build_latest_report()
    export_radar(argparse.Namespace())

    papers = papers_since(read_jsonl(PAPERS_PATH), from_date)
    recommended = select_recommended_papers(papers, args.limit)
    output_path = ROOT / "data" / "processed" / "monthly_digest.md"
    content = write_digest(output_path, "Monthly Accounting Research Digest", papers, recommended, from_date)
    print(f"Wrote monthly digest with {len(recommended)} recommended papers to {output_path}.")
    if args.send_email:
        send_email(args.subject or "Monthly Accounting Research Digest", content, args.recipient)


def generate_weekly_deep_queue(args: argparse.Namespace) -> None:
    from_date = args.from_date or (dt.date.today() - dt.timedelta(days=7)).isoformat()
    papers = papers_since(read_jsonl(PAPERS_PATH), from_date)
    recommended = select_recommended_papers(papers, args.limit, deep_queue=True)
    output_path = ROOT / "data" / "processed" / "weekly_deep_reading_queue.md"
    content = write_digest(output_path, "Weekly Deep Reading Recommendation Queue", papers, recommended, from_date)
    print(f"Wrote weekly deep reading queue with {len(recommended)} papers to {output_path}.")
    if args.send_email:
        send_email(args.subject or "Weekly Accounting Deep Reading Queue", content, args.recipient)


def send_digest_file(args: argparse.Namespace) -> None:
    path = Path(args.path).expanduser().resolve()
    if not path.exists():
        raise SystemExit(f"Digest file not found: {path}")
    send_email(args.subject, path.read_text(encoding="utf-8"), args.recipient)


def export_obsidian(_: argparse.Namespace) -> None:
    papers = read_jsonl(PAPERS_PATH)
    ideas = read_jsonl(IDEAS_PATH)
    papers_dir = EXPORTS_DIR / "obsidian" / "papers"
    ideas_dir = EXPORTS_DIR / "obsidian" / "ideas"
    papers_dir.mkdir(parents=True, exist_ok=True)
    ideas_dir.mkdir(parents=True, exist_ok=True)
    for old_note in list(papers_dir.glob("*.md")) + list(ideas_dir.glob("*.md")):
        old_note.unlink()

    for paper in papers:
        title = paper.get("title") or paper.get("id") or "Untitled paper"
        path = unique_markdown_path(papers_dir, slugify(title, "paper"))
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
        path = unique_markdown_path(ideas_dir, slugify(title, "idea"))
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

    metadata_parser = subparsers.add_parser("metadata")
    metadata_sub = metadata_parser.add_subparsers(dest="metadata_command", required=True)
    crossref_parser = metadata_sub.add_parser("enrich-crossref")
    crossref_parser.add_argument("--limit", type=int, default=50)
    crossref_parser.add_argument("--source-tier", nargs="*")
    crossref_parser.add_argument("--allow-title-lookup", action=argparse.BooleanOptionalAction, default=True)
    crossref_parser.add_argument("--refresh", action="store_true")
    crossref_parser.set_defaults(func=enrich_crossref_metadata)

    enrich_parser = subparsers.add_parser("enrich")
    enrich_parser.add_argument("--limit", type=int, default=5)
    enrich_parser.add_argument("--model")
    enrich_parser.add_argument("--api-key")
    enrich_parser.add_argument("--max-output-tokens", type=int, default=1200)
    enrich_parser.add_argument("--max-full-text-chars", type=int, default=60000)
    enrich_parser.set_defaults(func=enrich_with_openai)

    pdf_parser = subparsers.add_parser("pdf")
    pdf_sub = pdf_parser.add_subparsers(dest="pdf_command", required=True)
    extract_parser = pdf_sub.add_parser("extract")
    extract_parser.add_argument("--path", required=True)
    extract_parser.add_argument("--paper-id")
    extract_parser.set_defaults(func=extract_pdf)

    export_parser = subparsers.add_parser("export")
    export_sub = export_parser.add_subparsers(dest="export_command", required=True)
    radar_parser = export_sub.add_parser("radar")
    radar_parser.set_defaults(func=export_radar)
    obsidian_parser = export_sub.add_parser("obsidian")
    obsidian_parser.set_defaults(func=export_obsidian)
    notion_parser = export_sub.add_parser("notion")
    notion_parser.set_defaults(func=export_notion)

    digest_parser = subparsers.add_parser("digest")
    digest_sub = digest_parser.add_subparsers(dest="digest_command", required=True)
    monthly_parser = digest_sub.add_parser("monthly")
    monthly_parser.add_argument("--from-date")
    monthly_parser.add_argument("--limit", type=int, default=30)
    monthly_parser.add_argument("--collect", action=argparse.BooleanOptionalAction, default=False)
    monthly_parser.add_argument("--clean", action=argparse.BooleanOptionalAction, default=True)
    monthly_parser.add_argument("--max-results", type=int, default=120)
    monthly_parser.add_argument("--supplemental-max-results", type=int, default=60)
    monthly_parser.add_argument("--send-email", action="store_true")
    monthly_parser.add_argument("--recipient")
    monthly_parser.add_argument("--subject")
    monthly_parser.set_defaults(func=generate_monthly_digest)

    weekly_parser = digest_sub.add_parser("weekly-deep")
    weekly_parser.add_argument("--from-date")
    weekly_parser.add_argument("--limit", type=int, default=8)
    weekly_parser.add_argument("--send-email", action="store_true")
    weekly_parser.add_argument("--recipient")
    weekly_parser.add_argument("--subject")
    weekly_parser.set_defaults(func=generate_weekly_deep_queue)

    email_parser = digest_sub.add_parser("email")
    email_parser.add_argument("--path", required=True)
    email_parser.add_argument("--subject", required=True)
    email_parser.add_argument("--recipient")
    email_parser.set_defaults(func=send_digest_file)

    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        args.func(args)
    except urllib.error.URLError as exc:
        raise SystemExit(f"Network request failed: {exc}") from exc


if __name__ == "__main__":
    main()
