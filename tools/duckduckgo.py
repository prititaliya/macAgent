"""DuckDuckGo search + page fetch — no API key.

Search with `ddgs`, then read page text so the small local model answers from
real content instead of hallucinating. Does not open a browser window.
"""

from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_WS_RE = re.compile(r"\s+")


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


def build_grounded_context(
    query: str,
    *,
    max_results: int = 4,
    pages_to_read: int = 2,
    max_chars_per_page: int = 2800,
) -> Tuple[str, List[str]]:
    """Search, read top pages, return (context_for_llm, source_urls)."""
    results = search_duckduckgo(query, max_results=max_results)
    if not results:
        return "", []

    sources: List[str] = []
    blocks: List[str] = []

    # Always include search snippets as a thin fallback layer.
    snippet_lines = []
    for i, r in enumerate(results[:max_results], start=1):
        href = r.get("href") or ""
        if href and href not in sources:
            sources.append(href)
        snippet_lines.append(
            f"{i}. {r.get('title') or '(no title)'}\n"
            f"   {r.get('body') or ''}\n"
            f"   URL: {href}"
        )
    blocks.append("Search hits:\n" + "\n".join(snippet_lines))

    read = 0
    for r in results:
        if read >= pages_to_read:
            break
        href = r.get("href") or ""
        if not href:
            continue
        page = fetch_page_text(href, max_chars=max_chars_per_page)
        if len(page) < 80:
            continue
        read += 1
        blocks.append(
            f"Page content from {href}:\n{page}"
        )
        if href not in sources:
            sources.append(href)

    return "\n\n".join(blocks), sources


def format_results_for_llm(results: List[Dict[str, str]], limit: int = 5) -> str:
    lines: List[str] = []
    for i, r in enumerate(results[:limit], start=1):
        lines.append(
            f"{i}. {r.get('title') or '(no title)'}\n"
            f"   {r.get('body') or ''}\n"
            f"   URL: {r.get('href') or ''}"
        )
    return "\n".join(lines)
