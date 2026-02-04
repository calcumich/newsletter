import json
import sqlite3
import time
from typing import Iterable, Optional
from urllib.parse import urlsplit


def init_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS gmail_messages (
            message_id TEXT PRIMARY KEY,
            internal_date INTEGER,
            subject TEXT,
            from_email TEXT,
            label_ids TEXT,
            processed_at INTEGER,
            content_hash TEXT,
            issue_note_path TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS links (
            url_canonical TEXT PRIMARY KEY,
            first_seen_message_id TEXT,
            domain TEXT,
            title TEXT,
            discovered_at INTEGER,
            processed_at INTEGER,
            fetch_status TEXT,
            content_hash TEXT,
            summary TEXT,
            category TEXT,
            tags TEXT,
            note_path TEXT,
            original_url TEXT
        )
        """
    )
    ensure_link_columns(conn)
    ensure_gmail_columns(conn)
    conn.commit()
    return conn


def ensure_gmail_columns(conn: sqlite3.Connection) -> None:
    required = {
        "issue_note_path": "TEXT",
    }
    existing = {
        row[1] for row in conn.execute("PRAGMA table_info(gmail_messages)").fetchall()
    }
    for column, col_type in required.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE gmail_messages ADD COLUMN {column} {col_type}")


def ensure_link_columns(conn: sqlite3.Connection) -> None:
    required = {
        "summary": "TEXT",
        "category": "TEXT",
        "tags": "TEXT",
        "note_path": "TEXT",
        "original_url": "TEXT",
    }
    existing = {row[1] for row in conn.execute("PRAGMA table_info(links)").fetchall()}
    for column, col_type in required.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE links ADD COLUMN {column} {col_type}")


def store_message(conn: sqlite3.Connection, msg) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO gmail_messages
        (message_id, internal_date, subject, from_email, label_ids, processed_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            msg.message_id,
            msg.internal_date,
            msg.subject,
            msg.from_email,
            msg.label_ids,
            int(time.time()),
        ),
    )


def update_issue_note_path(
    conn: sqlite3.Connection,
    message_id: str,
    issue_note_path: Optional[str],
) -> None:
    if not issue_note_path:
        return
    conn.execute(
        "UPDATE gmail_messages SET issue_note_path = ? WHERE message_id = ?",
        (issue_note_path, message_id),
    )


def store_link(
    conn: sqlite3.Connection,
    url: str,
    original_url: Optional[str],
    message_id: str,
    discovered_at: int,
) -> None:
    domain = urlsplit(url).netloc
    conn.execute(
        """
        INSERT OR IGNORE INTO links
        (url_canonical, first_seen_message_id, domain, discovered_at, original_url)
        VALUES (?, ?, ?, ?, ?)
        """,
        (url, message_id, domain, discovered_at, original_url),
    )


def get_unprocessed_links(conn: sqlite3.Connection, limit: int) -> list[str]:
    rows = conn.execute(
        "SELECT url_canonical FROM links WHERE processed_at IS NULL LIMIT ?",
        (limit,),
    ).fetchall()
    return [row[0] for row in rows]


def get_links_for_refresh(
    conn: sqlite3.Connection,
    *,
    limit: int,
    older_than_days: int,
    statuses: Optional[Iterable[str]] = None,
    domains: Optional[Iterable[str]] = None,
    categories: Optional[Iterable[str]] = None,
    status_mode: str = "any",
) -> list[str]:
    cutoff = int(time.time()) - max(0, older_than_days) * 86400
    query = """
        SELECT url_canonical
        FROM links
        WHERE processed_at IS NOT NULL
          AND processed_at <= ?
    """
    params: list[object] = [cutoff]
    if status_mode == "ok_only":
        query += " AND fetch_status = ?"
        params.append("ok")
    elif status_mode == "failed_only":
        query += " AND fetch_status IS NOT NULL AND fetch_status != ?"
        params.append("ok")
    status_list = [s for s in (statuses or []) if s]
    if status_list:
        placeholders = ",".join(["?"] * len(status_list))
        query += f" AND fetch_status IN ({placeholders})"
        params.extend(status_list)
    domain_list = [d.lower() for d in (domains or []) if d]
    if domain_list:
        placeholders = ",".join(["?"] * len(domain_list))
        query += f" AND lower(domain) IN ({placeholders})"
        params.extend(domain_list)
    category_list = [c.lower() for c in (categories or []) if c]
    if category_list:
        placeholders = ",".join(["?"] * len(category_list))
        query += f" AND lower(COALESCE(category, '')) IN ({placeholders})"
        params.extend(category_list)
    query += " ORDER BY processed_at ASC LIMIT ?"
    params.append(limit)
    rows = conn.execute(query, params).fetchall()
    return [row[0] for row in rows]


def mark_link_processed(
    conn: sqlite3.Connection,
    url: str,
    status: str,
    title: Optional[str],
    content_hash: Optional[str],
    summary: Optional[str] = None,
    category: Optional[str] = None,
    tags: Optional[Iterable[str]] = None,
    note_path: Optional[str] = None,
) -> None:
    tags_json = json.dumps(sorted(set(tags))) if tags is not None else None
    conn.execute(
        """
        UPDATE links
        SET processed_at = ?, fetch_status = ?, title = ?, content_hash = ?
            , summary = COALESCE(?, summary)
            , category = COALESCE(?, category)
            , tags = COALESCE(?, tags)
            , note_path = COALESCE(?, note_path)
        WHERE url_canonical = ?
        """,
        (
            int(time.time()),
            status,
            title,
            content_hash,
            summary,
            category,
            tags_json,
            note_path,
            url,
        ),
    )
