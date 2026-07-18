"""Harvest Chrome browsing history into MacAgent SQLite cache."""

from __future__ import annotations

import logging
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import List

from memory.sqlite import ContextMemory

logger = logging.getLogger(__name__)

CHROME_ROOT = Path.home() / "Library/Application Support/Google/Chrome"
DEFAULT_LIMIT = 5000


def _discover_history_paths() -> List[Path]:
    if not CHROME_ROOT.exists():
        return []
    paths: List[Path] = []
    for candidate in sorted(CHROME_ROOT.glob("*/History")):
        # Skip system dirs that aren't profiles
        name = candidate.parent.name
        if name in {"System Profile", "Guest Profile"}:
            continue
        if candidate.is_file():
            paths.append(candidate)
    return paths


def _read_urls_from_copy(history_path: Path, limit: int) -> List[tuple]:
    with tempfile.TemporaryDirectory(prefix="macagent-hist-") as tmp:
        dest = Path(tmp) / "History"
        shutil.copy2(history_path, dest)
        conn = sqlite3.connect(f"file:{dest}?mode=ro", uri=True)
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT url, COALESCE(title, ''), visit_count
                FROM urls
                WHERE url IS NOT NULL AND url != ''
                ORDER BY visit_count DESC
                LIMIT ?
                """,
                (int(limit),),
            )
            return cursor.fetchall()
        finally:
            conn.close()


def harvest_chrome_history(limit: int = DEFAULT_LIMIT) -> int:
    """Copy Chrome History DB(s) and upsert top URLs into browser_history_cache.

    Supports Default and Profile N directories. Returns number of rows upserted.
    """
    paths = _discover_history_paths()
    if not paths:
        raise FileNotFoundError(
            f"No Chrome History found under {CHROME_ROOT}. "
            "Is Google Chrome installed?"
        )

    memory = ContextMemory()
    # Merge profiles; keep highest visit_count via upsert overwrite when larger.
    merged: dict[str, tuple[str, int]] = {}
    per_profile = max(100, limit // max(1, len(paths)))
    for path in paths:
        try:
            rows = _read_urls_from_copy(path, per_profile)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Skip history at %s: %s", path, exc)
            continue
        for url, title, visit_count in rows:
            prev = merged.get(url)
            vc = int(visit_count or 1)
            if prev is None or vc > prev[1]:
                merged[url] = (title or "", vc)

    # Cap to limit by visit_count
    ranked = sorted(merged.items(), key=lambda kv: kv[1][1], reverse=True)[:limit]
    count = 0
    for url, (title, visit_count) in ranked:
        memory.upsert_history(url, title, visit_count)
        count += 1
    logger.info(
        "Harvested %d Chrome history rows from %d profile(s) (limit=%d)",
        count,
        len(paths),
        limit,
    )
    return count


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    try:
        n = harvest_chrome_history()
    except FileNotFoundError as exc:
        print(exc)
        return 1
    print(f"Upserted {n} history rows into MacAgent SQLite.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
