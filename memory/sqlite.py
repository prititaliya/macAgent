import json
import re
import sqlite3
from pathlib import Path
from typing import Any, List, Optional, Tuple

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SETTINGS_PATH = _PROJECT_ROOT / "config" / "settings.json"
_DEFAULT_DB = _PROJECT_ROOT / "database" / "assistant.db"

_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "to",
        "for",
        "of",
        "and",
        "or",
        "in",
        "on",
        "at",
        "is",
        "it",
        "my",
        "me",
        "i",
        "we",
        "you",
        "please",
        "open",
        "go",
        "launch",
        "show",
        "get",
        "want",
        "wanna",
        "lets",
        "let's",
        "watch",
        "see",
        "current",
        "game",
        "website",
        "site",
        "page",
        "app",
        "code",
        "visual",
        "studio",
        "vs",
        "open",
        "launch",
    }
)


def _resolve_db_path() -> Path:
    if _SETTINGS_PATH.exists():
        with open(_SETTINGS_PATH, "r", encoding="utf-8") as f:
            settings = json.load(f)
        configured = (settings.get("db_path") or "").strip()
        if configured:
            return Path(configured).expanduser()
    return _DEFAULT_DB


def normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return url
    if not url.startswith(("http://", "https://")):
        return f"https://{url}"
    return url


class ContextMemory:
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = Path(db_path).expanduser() if db_path else _resolve_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._bootstrap_db()

    def _bootstrap_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS entity_mappings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    alias TEXT UNIQUE COLLATE NOCASE,
                    resolved_target TEXT NOT NULL,
                    target_type TEXT CHECK(target_type IN ('url', 'app', 'workflow')),
                    hit_count INTEGER DEFAULT 1,
                    last_used TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS browser_history_cache (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    url TEXT UNIQUE,
                    title TEXT,
                    visit_count INTEGER DEFAULT 1
                );
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS purpose_sites (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    url TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    hit_count INTEGER DEFAULT 0
                );
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS activity_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    utterance TEXT NOT NULL,
                    action TEXT NOT NULL,
                    detail TEXT,
                    result TEXT
                );
                """
            )
            conn.commit()
            self._seed_default_aliases(cursor)
            conn.commit()

    def _seed_default_aliases(self, cursor: sqlite3.Cursor) -> None:
        defaults = [
            ("github", "https://github.com", "url"),
            ("google", "https://www.google.com", "url"),
            ("gmail", "https://mail.google.com", "url"),
            ("twitter", "https://x.com", "url"),
            ("x", "https://x.com", "url"),
            ("linkedin", "https://www.linkedin.com", "url"),
            ("reddit", "https://www.reddit.com", "url"),
            ("amazon", "https://www.amazon.com", "url"),
            ("netflix", "https://www.netflix.com", "url"),
            ("chatgpt", "https://chatgpt.com", "url"),
            ("football", "https://www.espn.com/soccer/", "url"),
            ("soccer", "https://www.espn.com/soccer/", "url"),
            ("footy", "https://www.espn.com/soccer/", "url"),
            # Native apps (resolved_target = macOS app name for `open -a`)
            ("vscode", "Visual Studio Code", "app"),
            ("vs code", "Visual Studio Code", "app"),
            ("visual studio code", "Visual Studio Code", "app"),
            ("visual code", "Visual Studio Code", "app"),
            ("cursor", "Cursor", "app"),
            ("safari", "Safari", "app"),
            ("chrome", "Google Chrome", "app"),
            ("terminal", "Terminal", "app"),
            ("calculator", "Calculator", "app"),
            ("notes", "Notes", "app"),
            ("slack", "Slack", "app"),
            ("spotify", "Spotify", "app"),
        ]
        for alias, target, target_type in defaults:
            cursor.execute(
                """
                INSERT OR IGNORE INTO entity_mappings (alias, resolved_target, target_type)
                VALUES (?, ?, ?)
                """,
                (alias, target, target_type),
            )

    def add_purpose_site(self, url: str, purpose: str) -> dict[str, Any]:
        url = normalize_url(url)
        purpose = (purpose or "").strip()
        if not url or not purpose:
            raise ValueError("url and purpose are required")
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO purpose_sites (url, purpose) VALUES (?, ?)",
                (url, purpose),
            )
            conn.commit()
            site_id = cursor.lastrowid
        return {"id": site_id, "url": url, "purpose": purpose}

    def list_purpose_sites(self) -> List[dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, url, purpose, hit_count, created_at
                FROM purpose_sites
                ORDER BY id ASC
                """
            )
            rows = cursor.fetchall()
        return [
            {
                "id": r[0],
                "url": r[1],
                "purpose": r[2],
                "hit_count": r[3],
                "created_at": r[4],
            }
            for r in rows
        ]

    def get_purpose_site(self, site_id: int) -> Optional[dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, url, purpose, hit_count, created_at
                FROM purpose_sites WHERE id = ?
                """,
                (site_id,),
            )
            row = cursor.fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "url": row[1],
            "purpose": row[2],
            "hit_count": row[3],
            "created_at": row[4],
        }

    def bump_purpose_site(self, site_id: int) -> None:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE purpose_sites SET hit_count = hit_count + 1 WHERE id = ?",
                (site_id,),
            )
            conn.commit()

    def update_purpose_site(
        self, site_id: int, url: str, purpose: str
    ) -> Optional[dict[str, Any]]:
        url = normalize_url(url)
        purpose = (purpose or "").strip()
        if not url or not purpose:
            raise ValueError("url and purpose are required")
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE purpose_sites
                SET url = ?, purpose = ?
                WHERE id = ?
                """,
                (url, purpose, site_id),
            )
            if cursor.rowcount == 0:
                return None
            conn.commit()
        return self.get_purpose_site(site_id)

    def delete_purpose_site(self, site_id: int) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM purpose_sites WHERE id = ?", (site_id,))
            deleted = cursor.rowcount > 0
            conn.commit()
        return deleted

    def list_app_aliases(self) -> List[dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, alias, resolved_target, hit_count, last_used
                FROM entity_mappings
                WHERE target_type = 'app'
                ORDER BY alias COLLATE NOCASE ASC
                """
            )
            rows = cursor.fetchall()
        return [
            {
                "id": r[0],
                "alias": r[1],
                "app_name": r[2],
                "hit_count": r[3],
                "last_used": r[4],
            }
            for r in rows
        ]

    def add_app_alias(self, alias: str, app_name: str) -> dict[str, Any]:
        alias = (alias or "").strip().lower()
        app_name = (app_name or "").strip()
        if not alias or not app_name:
            raise ValueError("alias and app_name are required")
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, target_type FROM entity_mappings WHERE alias = ?",
                (alias,),
            )
            existing = cursor.fetchone()
            if existing:
                alias_id = int(existing[0])
                cursor.execute(
                    """
                    UPDATE entity_mappings
                    SET resolved_target = ?, target_type = 'app'
                    WHERE id = ?
                    """,
                    (app_name, alias_id),
                )
                conn.commit()
                return {
                    "id": alias_id,
                    "alias": alias,
                    "app_name": app_name,
                    "updated": True,
                }
            cursor.execute(
                """
                INSERT INTO entity_mappings (alias, resolved_target, target_type)
                VALUES (?, ?, 'app')
                """,
                (alias, app_name),
            )
            conn.commit()
            row_id = cursor.lastrowid
        return {"id": row_id, "alias": alias, "app_name": app_name, "updated": False}

    def update_app_alias(
        self, alias_id: int, alias: str, app_name: str
    ) -> Optional[dict[str, Any]]:
        alias = (alias or "").strip().lower()
        app_name = (app_name or "").strip()
        if not alias or not app_name:
            raise ValueError("alias and app_name are required")
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            try:
                cursor.execute(
                    """
                    UPDATE entity_mappings
                    SET alias = ?, resolved_target = ?, target_type = 'app'
                    WHERE id = ? AND target_type = 'app'
                    """,
                    (alias, app_name, alias_id),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"alias already exists: {alias}") from exc
            if cursor.rowcount == 0:
                return None
            conn.commit()
        return {"id": alias_id, "alias": alias, "app_name": app_name}

    def delete_app_alias(self, alias_id: int) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM entity_mappings WHERE id = ? AND target_type = 'app'",
                (alias_id,),
            )
            deleted = cursor.rowcount > 0
            conn.commit()
        return deleted

    def log_activity(
        self,
        utterance: str,
        action: str,
        detail: str = "",
        result: str = "",
    ) -> None:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO activity_log (utterance, action, detail, result)
                VALUES (?, ?, ?, ?)
                """,
                (
                    (utterance or "")[:500],
                    (action or "")[:80],
                    (detail or "")[:500],
                    (result or "")[:500],
                ),
            )
            # Keep last 500 entries
            cursor.execute(
                """
                DELETE FROM activity_log WHERE id NOT IN (
                    SELECT id FROM activity_log ORDER BY id DESC LIMIT 500
                )
                """
            )
            conn.commit()

    def list_activity(self, limit: int = 100) -> List[dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, created_at, utterance, action, detail, result
                FROM activity_log
                ORDER BY id DESC
                LIMIT ?
                """,
                (int(limit),),
            )
            rows = cursor.fetchall()
        return [
            {
                "id": r[0],
                "created_at": r[1],
                "utterance": r[2],
                "action": r[3],
                "detail": r[4],
                "result": r[5],
            }
            for r in rows
        ]

    def recent_interactions(self, limit: int = 8) -> List[dict[str, Any]]:
        """Recent agent Q&A turns for prompt context."""
        rows = self.list_activity(limit=max(int(limit), 1) * 3)
        out: list[dict[str, Any]] = []
        for row in rows:
            if row.get("action") != "agent":
                continue
            out.append(
                {
                    "created_at": row.get("created_at"),
                    "utterance": row.get("utterance") or "",
                    "answer": row.get("result") or "",
                    "detail": row.get("detail") or "",
                }
            )
            if len(out) >= int(limit):
                break
        return out

    def search_interactions(self, query: str, limit: int = 5) -> List[dict[str, Any]]:
        """Keyword search over past utterances and answers."""
        q = (query or "").strip()
        if not q:
            return self.recent_interactions(limit=limit)
        tokens = [
            t
            for t in re.split(r"[^\w]+", q.lower())
            if len(t) >= 3 and t not in _STOPWORDS
        ]
        rows = self.list_activity(limit=500)
        scored: list[tuple[int, dict[str, Any]]] = []
        for row in rows:
            if row.get("action") != "agent":
                continue
            utter = (row.get("utterance") or "").lower()
            ans = (row.get("result") or "").lower()
            blob = f"{utter} {ans}"
            if not tokens:
                score = 1 if q.lower() in blob else 0
            else:
                score = sum(1 for t in tokens if t in blob)
            if score <= 0 and q:
                continue
            scored.append(
                (
                    score,
                    {
                        "created_at": row.get("created_at"),
                        "utterance": row.get("utterance") or "",
                        "answer": row.get("result") or "",
                        "detail": row.get("detail") or "",
                    },
                )
            )
        scored.sort(key=lambda x: (-x[0], str(x[1].get("created_at") or "")))
        if not tokens and not q:
            return [item for _, item in scored[: int(limit)]]
        return [item for _, item in scored[: int(limit)]]

    def upsert_history(self, url: str, title: str, visit_count: int) -> None:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO browser_history_cache (url, title, visit_count)
                VALUES (?, ?, ?)
                ON CONFLICT(url) DO UPDATE SET
                    title = excluded.title,
                    visit_count = excluded.visit_count
                """,
                (url, title or "", int(visit_count or 1)),
            )
            conn.commit()

    def resolve_history(self, query: str) -> Optional[str]:
        """Soft-match browser history by title/url LIKE tokens."""
        tokens = [
            t
            for t in re.findall(r"[a-z0-9]{3,}", (query or "").lower())
            if t not in _STOPWORDS
        ]
        if not tokens:
            return None
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            for token in sorted(tokens, key=len, reverse=True):
                pattern = f"%{token}%"
                cursor.execute(
                    """
                    SELECT url FROM browser_history_cache
                    WHERE title LIKE ? OR url LIKE ?
                    ORDER BY visit_count DESC
                    LIMIT 1
                    """,
                    (pattern, pattern),
                )
                row = cursor.fetchone()
                if row:
                    return row[0]
        return None

    def resolve_alias(self, alias_str: str) -> Tuple[Optional[str], Optional[str]]:
        alias = alias_str.strip()
        if not alias:
            return None, None
        candidates = [alias, alias.lower()]
        if alias.lower().endswith(".com"):
            candidates.append(alias.lower()[: -len(".com")])
        else:
            candidates.append(f"{alias.lower()}.com")

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            for candidate in candidates:
                cursor.execute(
                    "SELECT resolved_target, target_type FROM entity_mappings WHERE alias = ?",
                    (candidate,),
                )
                row = cursor.fetchone()
                if row:
                    cursor.execute(
                        """
                        UPDATE entity_mappings
                        SET hit_count = hit_count + 1, last_used = CURRENT_TIMESTAMP
                        WHERE alias = ?
                        """,
                        (candidate,),
                    )
                    conn.commit()
                    return row[0], row[1]
        return None, None

    def resolve_from_utterance(
        self, text: str
    ) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """Soft-match: find the best alias mentioned inside free-form speech."""
        haystack = (text or "").strip().lower()
        if not haystack:
            return None, None, None

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT alias, resolved_target, target_type FROM entity_mappings"
            )
            rows = cursor.fetchall()

        best = None
        for alias, resolved, target_type in rows:
            token = (alias or "").strip().lower()
            if len(token) < 3:
                continue
            if (
                f" {token} " in f" {haystack} "
                or haystack == token
                or haystack.startswith(token + " ")
                or haystack.endswith(" " + token)
            ):
                score = len(token)
                if best is None or score > best[0]:
                    best = (score, alias, resolved, target_type)

        if not best:
            return None, None, None

        _, alias, resolved, target_type = best
        self.resolve_alias(alias)
        return resolved, target_type, alias
