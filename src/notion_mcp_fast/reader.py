"""
Notion Local Data Reader with TTL-based caching.

Reads Notion's local SQLite cache to provide fast access to pages, databases,
and blocks without API calls.
"""

import os
import platform
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any

from .block_parser import (
    get_title,
    parse_rich_text,
    render_block,
    safe_json_loads,
)


def _get_default_notion_db_path() -> str:
    """Get the default Notion database path based on OS."""
    # Allow override via environment variable
    if env_path := os.environ.get("NOTION_DB_PATH"):
        return env_path

    system = platform.system()
    if system == "Darwin":  # macOS
        return os.path.expanduser("~/Library/Application Support/Notion/notion.db")
    elif system == "Windows":
        return os.path.join(os.environ.get("APPDATA", ""), "Notion", "notion.db")
    else:  # Linux
        return os.path.expanduser("~/.config/Notion/notion.db")


NOTION_DB_PATH = _get_default_notion_db_path()

CACHE_TTL_SECONDS = 300  # 5 minutes


@dataclass
class CachedData:
    """Container for cached Notion metadata."""

    pages: dict[str, dict[str, Any]] = field(default_factory=dict)
    databases: dict[str, dict[str, Any]] = field(default_factory=dict)
    workspaces: dict[str, dict[str, Any]] = field(default_factory=dict)
    users: dict[str, dict[str, Any]] = field(default_factory=dict)
    meta_user_id: str | None = None
    loaded_at: float = 0.0

    def is_expired(self) -> bool:
        """Check if the cache has expired."""
        return time.time() - self.loaded_at > CACHE_TTL_SECONDS


class NotionLocalReader:
    """
    Reader for Notion's local SQLite cache.

    Provides fast, local-only access to Notion data without API calls.
    Metadata is cached in memory with a 5-minute TTL.
    Block content is loaded on-demand to conserve memory.
    """

    def __init__(self, db_path: str = NOTION_DB_PATH):
        self._db_path = db_path
        self._cache = CachedData()

    def _check_db_exists(self) -> None:
        """Verify the Notion database exists."""
        if not os.path.exists(self._db_path):
            raise FileNotFoundError(
                f"Notion database not found at {self._db_path}. "
                "Please ensure Notion.app is installed and has been opened at least once."
            )

    def _get_connection(self) -> sqlite3.Connection:
        """Get a read-only SQLite connection."""
        self._check_db_exists()
        conn = sqlite3.connect(f"file:{self._db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def _detect_meta_user_id(self, conn: sqlite3.Connection) -> str | None:
        """
        Detect the primary user's meta_user_id.

        Finds the user with the most blocks (typically the logged-in user).
        """
        cursor = conn.execute("""
            SELECT meta_user_id, COUNT(*) as count
            FROM block
            WHERE meta_user_id IS NOT NULL
            GROUP BY meta_user_id
            ORDER BY count DESC
            LIMIT 1
        """)
        row = cursor.fetchone()
        return row["meta_user_id"] if row else None

    def _reload_cache(self) -> None:
        """Reload metadata from the SQLite database."""
        conn = self._get_connection()
        try:
            # Detect primary user
            meta_user_id = self._detect_meta_user_id(conn)

            cache = CachedData(
                meta_user_id=meta_user_id,
                loaded_at=time.time()
            )

            # Load users first (needed for resolving creator/editor names)
            cursor = conn.execute("""
                SELECT id, name, email, given_name, family_name, profile_photo
                FROM notion_user
            """)

            for row in cursor:
                user_id = row["id"]
                name = row["name"] or ""
                if not name and (row["given_name"] or row["family_name"]):
                    name = f"{row['given_name'] or ''} {row['family_name'] or ''}".strip()

                cache.users[user_id] = {
                    "id": user_id,
                    "name": name,
                    "email": row["email"] or "",
                    "profile_photo": row["profile_photo"],
                }

            # Build user filter clause
            user_filter = ""
            params: tuple = ()
            if meta_user_id:
                user_filter = "AND meta_user_id = ?"
                params = (meta_user_id,)

            # Load pages (type = 'page')
            cursor = conn.execute(f"""
                SELECT id, type, properties, parent_id, parent_table,
                       format, created_time, last_edited_time,
                       created_by_id, last_edited_by_id, alive
                FROM block
                WHERE type = 'page' AND alive = 1
                {user_filter}
            """, params)

            for row in cursor:
                block_id = row["id"]
                properties = safe_json_loads(row["properties"])
                format_data = safe_json_loads(row["format"])

                # Resolve creator/editor names from users cache
                created_by_id = row["created_by_id"]
                last_edited_by_id = row["last_edited_by_id"]
                created_by_name = cache.users.get(created_by_id, {}).get("name") if created_by_id else None
                last_edited_by_name = cache.users.get(last_edited_by_id, {}).get("name") if last_edited_by_id else None

                cache.pages[block_id] = {
                    "id": block_id,
                    "title": get_title(properties),
                    "parent_id": row["parent_id"],
                    "parent_table": row["parent_table"],
                    "icon": format_data.get("page_icon") if format_data else None,
                    "cover": format_data.get("page_cover") if format_data else None,
                    "created_time": row["created_time"],
                    "last_edited_time": row["last_edited_time"],
                    "created_by_id": created_by_id,
                    "created_by_name": created_by_name,
                    "last_edited_by_id": last_edited_by_id,
                    "last_edited_by_name": last_edited_by_name,
                }

            # Load databases (collection_view_page and collection_view)
            cursor = conn.execute(f"""
                SELECT id, type, collection_id, view_ids, parent_id, parent_table,
                       created_time, last_edited_time, alive
                FROM block
                WHERE (type = 'collection_view_page' OR type = 'collection_view')
                  AND alive = 1
                {user_filter}
            """, params)

            for row in cursor:
                block_id = row["id"]

                cache.databases[block_id] = {
                    "id": block_id,
                    "type": row["type"],
                    "collection_id": row["collection_id"],
                    "parent_id": row["parent_id"],
                    "parent_table": row["parent_table"],
                    "view_ids": safe_json_loads(row["view_ids"]),
                    "created_time": row["created_time"],
                    "last_edited_time": row["last_edited_time"],
                }

            # Load workspaces (spaces)
            cursor = conn.execute("""
                SELECT id, name, icon, plan_type, settings
                FROM space
            """)

            for row in cursor:
                space_id = row["id"]
                settings = safe_json_loads(row["settings"])

                cache.workspaces[space_id] = {
                    "id": space_id,
                    "name": row["name"] or "",
                    "icon": row["icon"],
                    "plan_type": row["plan_type"],
                    "domain": settings.get("domain") if settings else None,
                }

            self._cache = cache
        finally:
            conn.close()

    def _ensure_cache(self) -> CachedData:
        """Ensure the cache is loaded and not expired."""
        if self._cache.is_expired() or not self._cache.loaded_at:
            self._reload_cache()
        return self._cache

    @property
    def pages(self) -> dict[str, dict[str, Any]]:
        """Get all pages."""
        return self._ensure_cache().pages

    @property
    def databases(self) -> dict[str, dict[str, Any]]:
        """Get all databases."""
        return self._ensure_cache().databases

    @property
    def workspaces(self) -> dict[str, dict[str, Any]]:
        """Get all workspaces."""
        return self._ensure_cache().workspaces

    @property
    def users(self) -> dict[str, dict[str, Any]]:
        """Get all users."""
        return self._ensure_cache().users

    def get_page_content(
        self, page_id: str, max_depth: int = 2
    ) -> dict[str, Any] | None:
        """
        Get page with its content blocks.

        Args:
            page_id: The page ID
            max_depth: How deep to traverse child blocks (default: 2)

        Returns:
            Page with content or None if not found
        """
        page = self.pages.get(page_id)
        if not page:
            return None

        # Load blocks on-demand
        conn = self._get_connection()
        try:
            blocks = self._load_page_blocks(conn, page_id, max_depth)
            return {
                **page,
                "content": blocks,
            }
        finally:
            conn.close()

    def _load_page_blocks(
        self, conn: sqlite3.Connection, parent_id: str, depth: int
    ) -> list[dict[str, Any]]:
        """Load blocks for a page recursively."""
        if depth <= 0:
            return []

        cache = self._ensure_cache()
        user_filter = ""
        params: list = [parent_id]
        if cache.meta_user_id:
            user_filter = "AND meta_user_id = ?"
            params.append(cache.meta_user_id)

        cursor = conn.execute(f"""
            SELECT id, type, properties, content, format, created_time
            FROM block
            WHERE parent_id = ? AND alive = 1
            {user_filter}
            ORDER BY created_time
        """, params)

        blocks = []
        for row in cursor:
            block_id = row["id"]
            block_type = row["type"] or "text"

            # Skip page blocks (they're separate entities)
            if block_type == "page":
                continue

            properties = safe_json_loads(row["properties"])
            content_ids = safe_json_loads(row["content"])
            has_children = bool(content_ids)

            # Render block content (tables need their row children assembled)
            if block_type == "table":
                text_content = self._render_table(conn, content_ids, row["format"])
                has_children = False  # rows consumed into the table; skip recursion
            else:
                text_content = render_block(block_type, properties)

            block = {
                "id": block_id,
                "type": block_type,
                "content": text_content,
            }

            # Recursively load children
            if has_children and depth > 1:
                children = self._load_page_blocks(conn, block_id, depth - 1)
                if children:
                    block["children"] = children

            blocks.append(block)

        return blocks

    def _render_table(
        self, conn: sqlite3.Connection, row_ids: Any, format_raw: Any
    ) -> str:
        """Assemble a Notion table block into a Markdown table.

        Cell text lives in child table_row blocks (properties keyed by column
        id). Column order and the header flag come from the table block's
        format; row order follows the table block's content (row id list).
        """
        fmt = safe_json_loads(format_raw)
        col_order = fmt.get("table_block_column_order") or []
        has_header = bool(fmt.get("table_block_column_header", False))

        row_ids = row_ids if isinstance(row_ids, list) else []
        if not row_ids:
            return "[Table]"

        def esc(text: str) -> str:
            return text.replace("|", "\\|").replace("\n", " ").strip()

        rows: list[list[str]] = []
        for rid in row_ids:
            r = conn.execute(
                "SELECT properties FROM block WHERE id = ? AND alive = 1",
                (rid,),
            ).fetchone()
            props = safe_json_loads(r["properties"]) if r else {}
            cols = col_order or list(props.keys())
            rows.append([esc(parse_rich_text(props.get(c, ""))) for c in cols])

        if not rows:
            return "[Table]"

        ncol = max(len(r) for r in rows)
        rows = [r + [""] * (ncol - len(r)) for r in rows]  # pad ragged rows

        lines = []
        if has_header:
            header, body = rows[0], rows[1:]
        else:
            header, body = [""] * ncol, rows
        lines.append("| " + " | ".join(header) + " |")
        lines.append("| " + " | ".join(["---"] * ncol) + " |")
        for r in body:
            lines.append("| " + " | ".join(r) + " |")
        return "\n".join(lines)

    def search_pages(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        """
        Search pages by title.

        Args:
            query: Search query (case-insensitive)
            limit: Maximum results

        Returns:
            List of matching pages
        """
        query_lower = query.lower()
        results = []

        for page in self.pages.values():
            title = page.get("title", "")
            if query_lower in title.lower():
                results.append(page)
                if len(results) >= limit:
                    break

        return results

    def full_text_search(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        """
        Search page content (not just titles).

        Uses SQLite LIKE for on-demand search without caching all content.

        Args:
            query: Search query
            limit: Maximum results

        Returns:
            List of pages with matching content
        """
        conn = self._get_connection()
        try:
            cache = self._ensure_cache()
            user_filter = ""
            params: list = [f"%{query}%"]
            if cache.meta_user_id:
                user_filter = "AND meta_user_id = ?"
                params.append(cache.meta_user_id)

            # Search in block properties (contains text content)
            cursor = conn.execute(f"""
                SELECT DISTINCT parent_id
                FROM block
                WHERE properties LIKE ?
                  AND alive = 1
                {user_filter}
                LIMIT ?
            """, params + [limit * 3])

            page_ids = set()
            for row in cursor:
                page_id = row["parent_id"]
                if page_id:
                    page_ids.add(page_id)

            # Find root pages for these blocks
            results = []
            for page_id in page_ids:
                page = self._find_root_page(conn, page_id)
                if page and page["id"] not in [r["id"] for r in results]:
                    results.append(page)
                    if len(results) >= limit:
                        break

            return results
        finally:
            conn.close()

    def _find_root_page(
        self, conn: sqlite3.Connection, block_id: str
    ) -> dict[str, Any] | None:
        """Find the root page containing a block."""
        # Check if it's already a page
        if block_id in self.pages:
            return self.pages[block_id]

        # Walk up the parent chain
        visited = set()
        current_id = block_id

        while current_id and current_id not in visited:
            visited.add(current_id)

            # Check if current is a page
            if current_id in self.pages:
                return self.pages[current_id]

            # Get parent
            cursor = conn.execute("""
                SELECT parent_id
                FROM block
                WHERE id = ?
            """, (current_id,))
            row = cursor.fetchone()
            if row and row["parent_id"]:
                current_id = row["parent_id"]
            else:
                break

        return None

    def get_database_records(
        self, database_id: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        """
        Get records from a database.

        Args:
            database_id: Database (collection_view_page) ID
            limit: Maximum records

        Returns:
            List of database records
        """
        db = self.databases.get(database_id)
        if not db:
            return []

        collection_id = db.get("collection_id")
        if not collection_id:
            return []

        # Get collection schema
        conn = self._get_connection()
        try:
            schema = self._get_collection_schema(conn, collection_id)

            cache = self._ensure_cache()
            user_filter = ""
            params: list = [collection_id]
            if cache.meta_user_id:
                user_filter = "AND meta_user_id = ?"
                params.append(cache.meta_user_id)

            # Get pages in this collection
            cursor = conn.execute(f"""
                SELECT id, properties, created_time, last_edited_time
                FROM block
                WHERE parent_id = ?
                  AND type = 'page'
                  AND alive = 1
                {user_filter}
                ORDER BY last_edited_time DESC
                LIMIT ?
            """, params + [limit])

            records = []
            for row in cursor:
                record_id = row["id"]
                properties = safe_json_loads(row["properties"])
                if not properties:
                    continue

                record = {
                    "id": record_id,
                    "created_time": row["created_time"],
                    "last_edited_time": row["last_edited_time"],
                }

                # Map schema properties
                for prop_id, prop_info in schema.items():
                    prop_name = prop_info.get("name", prop_id)
                    prop_value = properties.get(prop_id)
                    if prop_value:
                        record[prop_name] = parse_rich_text(prop_value)

                records.append(record)

            return records
        finally:
            conn.close()

    def _get_collection_schema(
        self, conn: sqlite3.Connection, collection_id: str
    ) -> dict[str, Any]:
        """Get schema for a collection."""
        cursor = conn.execute("""
            SELECT schema
            FROM collection
            WHERE id = ?
        """, (collection_id,))
        row = cursor.fetchone()
        if not row:
            return {}

        return safe_json_loads(row["schema"])

    def get_database_schema(self, database_id: str) -> dict[str, Any] | None:
        """
        Get database schema.

        Args:
            database_id: Database ID

        Returns:
            Schema with property definitions
        """
        db = self.databases.get(database_id)
        if not db:
            return None

        collection_id = db.get("collection_id")
        if not collection_id:
            return None

        conn = self._get_connection()
        try:
            cursor = conn.execute("""
                SELECT name, schema, icon, description
                FROM collection
                WHERE id = ?
            """, (collection_id,))
            row = cursor.fetchone()
            if not row:
                return None

            schema = safe_json_loads(row["schema"])
            name = safe_json_loads(row["name"]) if row["name"] else []

            return {
                "name": name,
                "description": row["description"],
                "icon": row["icon"],
                "schema": schema,
            }
        finally:
            conn.close()

    def get_summary(self) -> dict[str, Any]:
        """Get a summary of loaded data."""
        cache = self._ensure_cache()
        return {
            "pages": len(cache.pages),
            "databases": len(cache.databases),
            "workspaces": len(cache.workspaces),
            "users": len(cache.users),
            "meta_user_id": cache.meta_user_id,
        }

    def get_user_name(self, user_id: str | None) -> str:
        """Get user name from user ID."""
        if not user_id:
            return "Unknown"
        user = self.users.get(user_id, {})
        return user.get("name", "Unknown")
