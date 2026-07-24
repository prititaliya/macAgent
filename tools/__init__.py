"""Search and other tools for MacAgent."""

from tools.duckduckgo import (
    build_grounded_context,
    fetch_next_pages,
    fetch_page_text,
    format_results_for_llm,
    search_duckduckgo,
)

__all__ = [
    "search_duckduckgo",
    "format_results_for_llm",
    "fetch_page_text",
    "fetch_next_pages",
    "build_grounded_context",
]
