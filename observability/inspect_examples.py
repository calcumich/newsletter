import argparse
import os
import sqlite3
from textwrap import indent


def open_db(db_path: str) -> sqlite3.Connection:
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"Database not found: {db_path}")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def first_row(conn: sqlite3.Connection, query: str, params=()):
    row = conn.execute(query, params).fetchone()
    return dict(row) if row else None


def rows(conn: sqlite3.Connection, query: str, params=()):
    return [dict(r) for r in conn.execute(query, params).fetchall()]


def file_preview(path: str, max_lines: int = 40) -> str:
    if not path or not os.path.exists(path):
        return "<missing>"
    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    return "".join(lines[:max_lines]).rstrip()


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect newsletter pipeline outputs")
    parser.add_argument("--db", default="newsletter.db", help="Path to SQLite DB")
    parser.add_argument("--vault", default="", help="Optional path to Obsidian vault")
    parser.add_argument("--max-samples", type=int, default=3, help="Max samples to show")
    args = parser.parse_args()

    conn = open_db(args.db)

    print("== Gmail examples ==")
    gmail_count = first_row(conn, "SELECT COUNT(*) AS n FROM gmail_messages")
    print(f"Total messages: {gmail_count['n'] if gmail_count else 0}")
    gmail_samples = rows(
        conn,
        """
        SELECT message_id, internal_date, subject, from_email, issue_note_path
        FROM gmail_messages
        ORDER BY internal_date DESC
        LIMIT ?
        """,
        (args.max_samples,),
    )
    if not gmail_samples:
        print("(no messages found)")
    for msg in gmail_samples:
        print("- Message")
        for key in ["message_id", "internal_date", "subject", "from_email", "issue_note_path"]:
            print(f"  {key}: {msg.get(key)}")
        if msg.get("issue_note_path"):
            preview = file_preview(msg["issue_note_path"])
            print("  issue note preview:")
            print(indent(preview or "<empty>", "    "))

    print("\n== Link examples ==")
    link_count = first_row(conn, "SELECT COUNT(*) AS n FROM links")
    print(f"Total links: {link_count['n'] if link_count else 0}")
    status_counts = rows(
        conn,
        """
        SELECT COALESCE(fetch_status, 'unprocessed') AS status, COUNT(*) AS n
        FROM links
        GROUP BY status
        ORDER BY n DESC
        """,
    )
    if status_counts:
        print("Status breakdown:")
        for row in status_counts:
            print(f"  {row['status']}: {row['n']}")

    top_domains = rows(
        conn,
        """
        SELECT domain, COUNT(*) AS n
        FROM links
        GROUP BY domain
        ORDER BY n DESC
        LIMIT ?
        """,
        (args.max_samples,),
    )
    if top_domains:
        print("Top domains:")
        for row in top_domains:
            print(f"  {row['domain']}: {row['n']}")

    link_samples = rows(
        conn,
        """
        SELECT url_canonical, domain, fetch_status, title, summary, note_path
        FROM links
        ORDER BY discovered_at DESC
        LIMIT ?
        """,
        (args.max_samples,),
    )
    if not link_samples:
        print("(no links found)")
    for link in link_samples:
        print("- Link")
        for key in ["url_canonical", "domain", "fetch_status", "title", "note_path"]:
            print(f"  {key}: {link.get(key)}")
        if link.get("summary"):
            print("  summary:")
            print(indent(link["summary"].strip(), "    "))
        if link.get("note_path"):
            preview = file_preview(link["note_path"])
            print("  article note preview:")
            print(indent(preview or "<empty>", "    "))

    if args.vault:
        print("\n== Vault sample paths ==")
        issues_root = os.path.join(args.vault, "Newsletters", "Issues")
        articles_root = os.path.join(args.vault, "Newsletters", "Articles")
        print(f"Issues root exists: {issues_root} -> {os.path.exists(issues_root)}")
        print(f"Articles root exists: {articles_root} -> {os.path.exists(articles_root)}")


if __name__ == "__main__":
    main()
