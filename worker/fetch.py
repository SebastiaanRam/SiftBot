"""Paper fetching from arXiv, Semantic Scholar, and PubMed."""

from __future__ import annotations

import hashlib
import os
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING

import feedparser
import httpx

if TYPE_CHECKING:
    from db.client import DBClient


@dataclass
class Paper:
    id: str               # "arxiv:2403.12345" | "s2:abc123" | "pm:38012345"
    title: str
    abstract: str
    authors: list[str]
    published: date
    source: str           # "arxiv" | "semantic_scholar" | "pubmed"
    url: str
    pdf_url: str | None = None
    venue: str | None = None


def normalize_title(title: str) -> str:
    """Lowercase, collapse whitespace, strip non-alphanumeric for dedup."""
    t = title.lower()
    t = re.sub(r"[^a-z0-9\s]", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def title_hash(title: str) -> str:
    return hashlib.sha256(normalize_title(title).encode()).hexdigest()


# ── arXiv ─────────────────────────────────────────────────────────────────────

def fetch_arxiv(categories: list[str], days_back: int = 1) -> list[Paper]:
    papers: list[Paper] = []
    cutoff = date.today() - timedelta(days=days_back)

    for cat in categories:
        query = f"cat:{cat}"
        url = (
            "http://export.arxiv.org/api/query"
            f"?search_query={query}&sortBy=submittedDate&sortOrder=descending&max_results=100"
        )
        try:
            feed = feedparser.parse(url)
        except Exception as exc:
            print(f"[fetch] arXiv error for {cat}: {exc}")
            continue

        for entry in feed.entries:
            if not getattr(entry, "published_parsed", None):
                continue
            try:
                pub_dt = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
                pub_date = pub_dt.date()
            except Exception:
                continue

            if pub_date < cutoff:
                continue

            arxiv_id = entry.id.split("/abs/")[-1].split("v")[0]
            paper_id = f"arxiv:{arxiv_id}"

            authors = [a.name for a in getattr(entry, "authors", [])][:5]
            abstract = getattr(entry, "summary", "").replace("\n", " ").strip()

            pdf_url = None
            for link in getattr(entry, "links", []):
                if link.get("type") == "application/pdf":
                    pdf_url = link["href"]
                    break

            papers.append(
                Paper(
                    id=paper_id,
                    title=entry.title.replace("\n", " ").strip(),
                    abstract=abstract,
                    authors=authors,
                    published=pub_date,
                    source="arxiv",
                    url=f"https://arxiv.org/abs/{arxiv_id}",
                    pdf_url=pdf_url or f"https://arxiv.org/pdf/{arxiv_id}",
                )
            )

        # Small sleep to be polite to the API
        time.sleep(0.5)

    return _dedup_within_list(papers)


# ── Semantic Scholar ──────────────────────────────────────────────────────────

def fetch_semantic_scholar(query: str, days_back: int = 1) -> list[Paper]:
    api_key = os.environ.get("SEMANTIC_SCHOLAR_API_KEY", "")
    cutoff = date.today() - timedelta(days=days_back)

    headers = {}
    if api_key:
        headers["x-api-key"] = api_key

    fields = "title,abstract,authors,year,externalIds,openAccessPdf,publicationDate,venue,journal"
    params = {
        "query": query,
        "fields": fields,
        "limit": 100,
        "publicationDateOrYear": f"{cutoff.isoformat()}:",
    }

    papers: list[Paper] = []
    try:
        with httpx.Client(timeout=30) as client:
            resp = client.get(
                "https://api.semanticscholar.org/graph/v1/paper/search",
                params=params,
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as exc:
        print(f"[fetch] Semantic Scholar HTTP error {exc.response.status_code}: {exc}")
        return []
    except Exception as exc:
        print(f"[fetch] Semantic Scholar error: {exc}")
        return []

    for item in data.get("data", []):
        pub_date_str = item.get("publicationDate")
        if not pub_date_str:
            year = item.get("year")
            if not year:
                continue
            pub_date = date(int(year), 1, 1)
        else:
            try:
                pub_date = date.fromisoformat(pub_date_str)
            except ValueError:
                continue

        if pub_date < cutoff:
            continue

        s2_id = item.get("paperId", "")
        if not s2_id:
            continue

        external = item.get("externalIds", {}) or {}
        arxiv_id = external.get("ArXiv")
        if arxiv_id:
            # Prefer arxiv ID for papers available there
            paper_id = f"arxiv:{arxiv_id}"
            url = f"https://arxiv.org/abs/{arxiv_id}"
            pdf_url = f"https://arxiv.org/pdf/{arxiv_id}"
        else:
            paper_id = f"s2:{s2_id}"
            url = f"https://www.semanticscholar.org/paper/{s2_id}"
            oa_pdf = item.get("openAccessPdf") or {}
            pdf_url = oa_pdf.get("url")

        authors = [a["name"] for a in (item.get("authors") or [])[:5]]
        venue = (item.get("venue") or "").strip() or None
        if not venue and item.get("journal"):
            venue = (item["journal"].get("name") or "").strip() or None

        papers.append(
            Paper(
                id=paper_id,
                title=(item.get("title") or "").strip(),
                abstract=(item.get("abstract") or "").strip(),
                authors=authors,
                published=pub_date,
                source="semantic_scholar",
                url=url,
                pdf_url=pdf_url,
                venue=venue,
            )
        )

    return _dedup_within_list(papers)


# ── PubMed ────────────────────────────────────────────────────────────────────

_NCBI_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"


def fetch_pubmed(query: str, days_back: int = 1) -> list[Paper]:
    api_key = _env_key("NCBI_API_KEY")
    cutoff = date.today() - timedelta(days=days_back)

    # esearch
    search_params: dict = {
        "db": "pubmed",
        "term": query,
        "retmax": 100,
        "sort": "date",
        "retmode": "json",
        "datetype": "pdat",
        "reldate": days_back + 1,
    }
    if api_key:
        search_params["api_key"] = api_key

    try:
        with httpx.Client(timeout=30) as client:
            search_resp = client.get(f"{_NCBI_BASE}/esearch.fcgi", params=search_params)
            search_resp.raise_for_status()
            search_data = search_resp.json()
            ids = search_data.get("esearchresult", {}).get("idlist", [])

            if not ids:
                return []

            # efetch
            fetch_params: dict = {
                "db": "pubmed",
                "id": ",".join(ids),
                "retmode": "xml",
                "rettype": "abstract",
            }
            if api_key:
                fetch_params["api_key"] = api_key

            fetch_resp = client.get(f"{_NCBI_BASE}/efetch.fcgi", params=fetch_params)
            fetch_resp.raise_for_status()
            xml_text = fetch_resp.text
    except Exception as exc:
        print(f"[fetch] PubMed error: {exc}")
        return []

    papers: list[Paper] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        print(f"[fetch] PubMed XML parse error: {exc}")
        return []

    for article in root.findall(".//PubmedArticle"):
        medline = article.find("MedlineCitation")
        if medline is None:
            continue

        pmid_el = medline.find("PMID")
        pmid = pmid_el.text if pmid_el is not None else None
        if not pmid:
            continue

        art = medline.find("Article")
        if art is None:
            continue

        title_el = art.find("ArticleTitle")
        title = (title_el.text or "").strip() if title_el is not None else ""
        if not title:
            continue

        # Abstract
        abstract_parts = []
        for text_el in art.findall(".//AbstractText"):
            label = text_el.get("Label", "")
            text = text_el.text or ""
            if label:
                abstract_parts.append(f"{label}: {text}")
            else:
                abstract_parts.append(text)
        abstract = " ".join(abstract_parts).strip()

        # Authors
        authors: list[str] = []
        for author in art.findall(".//Author")[:5]:
            last = (author.findtext("LastName") or "").strip()
            fore = (author.findtext("ForeName") or "").strip()
            if last:
                authors.append(f"{fore} {last}".strip())

        # Publication date
        pub_date_el = art.find(".//PubDate")
        pub_date: date | None = None
        if pub_date_el is not None:
            year_el = pub_date_el.find("Year")
            month_el = pub_date_el.find("Month")
            day_el = pub_date_el.find("Day")
            try:
                year = int(year_el.text) if year_el is not None else 0
                month_raw = (month_el.text or "1") if month_el is not None else "1"
                # Month can be abbreviated text like "Jan"
                try:
                    month = int(month_raw)
                except ValueError:
                    month = datetime.strptime(month_raw[:3], "%b").month
                day = int(day_el.text) if day_el is not None else 1
                pub_date = date(year, month, day)
            except (ValueError, TypeError):
                pass

        if pub_date is None or pub_date < cutoff:
            continue

        # Journal/venue
        journal_el = art.find(".//Journal/Title")
        venue = journal_el.text.strip() if journal_el is not None else None

        papers.append(
            Paper(
                id=f"pm:{pmid}",
                title=title,
                abstract=abstract,
                authors=authors,
                published=pub_date,
                source="pubmed",
                url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                pdf_url=None,
                venue=venue,
            )
        )

    return _dedup_within_list(papers)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _dedup_within_list(papers: list[Paper]) -> list[Paper]:
    """Remove duplicates within a single batch by title hash."""
    seen: set[str] = set()
    result: list[Paper] = []
    for p in papers:
        h = title_hash(p.title)
        if h not in seen:
            seen.add(h)
            result.append(p)
    return result


def deduplicate_new(papers: list[Paper], db: "DBClient") -> list[Paper]:
    """Return only papers whose title_hash is not already in the database."""
    new_papers: list[Paper] = []
    for p in papers:
        if not db.paper_exists(title_hash(p.title)):
            new_papers.append(p)
    return new_papers


def _env_key(name: str) -> str:
    """Return the env var value, treating comment-only values as empty."""
    val = os.environ.get(name, "").strip()
    # dotenv may read un-filled lines like: KEY=   # Optional — ...
    if val.startswith("#") or not val:
        return ""
    return val


def fetch_all(
    categories: list[str],
    s2_query: str,
    pubmed_query: str,
    days_back: int = 1,
) -> list[Paper]:
    """Fetch from all three sources, combining and deduplicating within the list."""
    all_papers: list[Paper] = []

    print(f"[fetch] Fetching arXiv categories: {categories}")
    arxiv_papers = fetch_arxiv(categories, days_back)
    print(f"[fetch] arXiv: {len(arxiv_papers)} papers")
    all_papers.extend(arxiv_papers)

    s2_key = _env_key("SEMANTIC_SCHOLAR_API_KEY")
    if s2_key and s2_query:
        print(f"[fetch] Fetching Semantic Scholar: {s2_query!r}")
        s2_papers = fetch_semantic_scholar(s2_query, days_back)
        print(f"[fetch] Semantic Scholar: {len(s2_papers)} papers")
        all_papers.extend(s2_papers)
    else:
        print("[fetch] Skipping Semantic Scholar (no API key or query)")

    pubmed_query = pubmed_query.strip()
    if pubmed_query:
        print(f"[fetch] Fetching PubMed: {pubmed_query!r}")
        pm_papers = fetch_pubmed(pubmed_query, days_back)
        print(f"[fetch] PubMed: {len(pm_papers)} papers")
        all_papers.extend(pm_papers)
    else:
        print("[fetch] Skipping PubMed (no PUBMED_QUERY set)")

    return _dedup_within_list(all_papers)
