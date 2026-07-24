"""DuckDuckGo search + page fetch — no API key.

Search with `ddgs`, then read page text so answers are grounded in real
content. Pages are scraped one at a time; callers can request the next
unread URL only when the first page isn't enough.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_WS_RE = re.compile(r"\s+")

# Cap how many full pages we ever scrape for one query session.
_MAX_PAGES_TOTAL = 3


def search_duckduckgo(query: str, max_results: int = 5) -> List[Dict[str, str]]:
    """Return [{title, href, body}, ...] for a text query."""
    q = (query or "").strip()
    if not q:
        return []

    try:
        from ddgs import DDGS
    except ImportError:
        logger.warning("ddgs not installed; pip install ddgs")
        return []

    try:
        raw = DDGS().text(q, max_results=max_results, backend="duckduckgo")
    except Exception as exc:  # noqa: BLE001
        logger.warning("DuckDuckGo search failed: %s", exc)
        try:
            raw = DDGS().text(q, max_results=max_results)
        except Exception as exc2:  # noqa: BLE001
            logger.warning("Search fallback failed: %s", exc2)
            return []

    results: List[Dict[str, str]] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        href = str(item.get("href") or item.get("link") or "").strip()
        body = str(item.get("body") or item.get("snippet") or "").strip()
        if not (title or body or href):
            continue
        results.append({"title": title, "href": href, "body": body})
    return results


def fetch_page_text(url: str, max_chars: int = 4000) -> str:
    """Fetch and extract plain-ish text from a URL via ddgs.extract."""
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        return ""
    try:
        from ddgs import DDGS
    except ImportError:
        return ""

    try:
        data = DDGS().extract(url, fmt="text_plain")
    except Exception:
        try:
            data = DDGS().extract(url, fmt="text_markdown")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Page extract failed for %s: %s", url, exc)
            return ""

    if not isinstance(data, dict):
        return ""
    content = data.get("content")
    if isinstance(content, bytes):
        content = content.decode("utf-8", errors="ignore")
    text = _WS_RE.sub(" ", str(content or "")).strip()
    if len(text) > max_chars:
        text = text[: max_chars - 1].rsplit(" ", 1)[0] + "…"
    return text


def _scrape_one(
    url: str, *, max_chars: int
) -> Tuple[str, str]:
    """Return (page_block, url) or ("", "") if too thin."""
    href = (url or "").strip()
    if not href:
        return "", ""
    page = fetch_page_text(href, max_chars=max_chars)
    if len(page) < 80:
        return "", ""
    return f"Page content from {href}:\n{page}", href


def fetch_next_pages(
    urls: List[str],
    *,
    pages_to_read: int = 1,
    max_chars_per_page: int = 2800,
    already_read: int = 0,
) -> Tuple[str, List[str], List[str], int]:
    """Scrape the next N unread URLs.

    Returns (context_block, fetched_urls, still_unread, pages_read_total).
    """
    remaining = [u for u in (urls or []) if isinstance(u, str) and u.strip()]
    if pages_to_read < 1 or already_read >= _MAX_PAGES_TOTAL:
        return "", [], remaining, already_read

    budget = min(pages_to_read, _MAX_PAGES_TOTAL - already_read)
    blocks: List[str] = []
    fetched: List[str] = []
    unread = list(remaining)
    read = already_read

    while unread and len(fetched) < budget:
        href = unread.pop(0)
        block, used = _scrape_one(href, max_chars=max_chars_per_page)
        if not block:
            continue
        blocks.append(block)
        fetched.append(used)
        read += 1

    return "\n\n".join(blocks), fetched, unread, read


def build_grounded_context(
    query: str,
    *,
    max_results: int = 4,
    pages_to_read: int = 1,
    max_chars_per_page: int = 2800,
    unread_urls: Optional[List[str]] = None,
    pages_already_read: int = 0,
) -> Dict[str, Any]:
    """Search (or continue), scrape up to pages_to_read pages.

    Returns dict with context, sources, unread_urls, pages_read, scraped.
    Default scrapes **one** page; pass unread_urls to read the next one.
    """
    # Continue mode: only fetch the next unread page(s).
    if unread_urls:
        block, fetched, still, read = fetch_next_pages(
            list(unread_urls),
            pages_to_read=pages_to_read,
            max_chars_per_page=max_chars_per_page,
            already_read=max(0, int(pages_already_read or 0)),
        )
        return {
            "context": block,
            "sources": fetched,
            "unread_urls": still,
            "pages_read": read,
            "scraped": bool(fetched),
            "continued": True,
        }

    results = search_duckduckgo(query, max_results=max_results)
    if not results:
        return {
            "context": "",
            "sources": [],
            "unread_urls": [],
            "pages_read": 0,
            "scraped": False,
            "continued": False,
        }

    sources: List[str] = []
    blocks: List[str] = []
    candidate_urls: List[str] = []

    snippet_lines = []
    for i, r in enumerate(results[:max_results], start=1):
        href = r.get("href") or ""
        if href and href not in sources:
            sources.append(href)
        if href and href not in candidate_urls:
            candidate_urls.append(href)
        snippet_lines.append(
            f"{i}. {r.get('title') or '(no title)'}\n"
            f"   {r.get('body') or ''}\n"
            f"   URL: {href}"
        )
    blocks.append("Search hits:\n" + "\n".join(snippet_lines))

    page_block, fetched, still, read = fetch_next_pages(
        candidate_urls,
        pages_to_read=max(1, int(pages_to_read or 1)),
        max_chars_per_page=max_chars_per_page,
        already_read=0,
    )
    if page_block:
        blocks.append(page_block)
        for href in fetched:
            if href not in sources:
                sources.append(href)

    return {
        "context": "\n\n".join(blocks),
        "sources": sources,
        "unread_urls": still,
        "pages_read": read,
        "scraped": bool(fetched),
        "continued": False,
    }


def format_results_for_llm(results: List[Dict[str, str]], limit: int = 5) -> str:
    lines: List[str] = []
    for i, r in enumerate(results[:limit], start=1):
        lines.append(
            f"{i}. {r.get('title') or '(no title)'}\n"
            f"   {r.get('body') or ''}\n"
            f"   URL: {r.get('href') or ''}"
        )
    return "\n".join(lines)
