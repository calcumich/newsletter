#!/usr/bin/env python
import argparse
import base64
import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple
from urllib.parse import urlsplit

from newsletter.db import (
    get_unprocessed_links,
    init_db,
    mark_link_processed,
    store_message,
    update_issue_note_path,
)
from newsletter.links import canonicalize_url, extract_and_store_links, resolve_redirect_url
from newsletter.obsidian import (
    update_issue_note_with_article_link,
    write_article_note,
    write_issue_note,
)
from newsletter.fetch import fetch_article
from newsletter.summarize import summarize_text

try:
    from bs4 import BeautifulSoup
except Exception:  # pragma: no cover - optional import
    BeautifulSoup = None

try:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
except Exception:
    Credentials = None
    InstalledAppFlow = None
    Request = None
    build = None


SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
DEFAULT_DB = "newsletter.db"

@dataclass
class GmailMessage:
    message_id: str
    internal_date: int
    subject: str
    from_email: str
    label_ids: str
    html: Optional[str]
    text: Optional[str]


def ensure_dependencies():
    if Credentials is None or InstalledAppFlow is None or build is None:
        raise RuntimeError(
            "Missing Google API dependencies. Install requirements.txt first."
        )
    if BeautifulSoup is None:
        raise RuntimeError("Missing BeautifulSoup. Install requirements.txt first.")


def get_gmail_service(credentials_path: str, token_path: str):
    ensure_dependencies()
    creds = None
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(credentials_path, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_path, "w", encoding="utf-8") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def resolve_label_id(service, label_name: str) -> Optional[str]:
    labels = service.users().labels().list(userId="me").execute().get("labels", [])
    for label in labels:
        if label.get("name", "").lower() == label_name.lower():
            return label.get("id")
    return None


def list_messages(service, label_id: str, max_results: int, since_query: Optional[str]):
    q = since_query or ""
    response = (
        service.users()
        .messages()
        .list(userId="me", labelIds=[label_id], maxResults=max_results, q=q)
        .execute()
    )
    return response.get("messages", [])


def decode_part(data: str) -> str:
    return base64.urlsafe_b64decode(data.encode("utf-8")).decode("utf-8", errors="replace")


def extract_parts(payload) -> Tuple[Optional[str], Optional[str]]:
    html = None
    text = None

    def walk(part):
        nonlocal html, text
        mime = part.get("mimeType", "")
        body = part.get("body", {})
        data = body.get("data")
        if data and mime == "text/html":
            html = decode_part(data)
        elif data and mime == "text/plain" and text is None:
            text = decode_part(data)
        for child in part.get("parts", []) or []:
            walk(child)

    walk(payload)
    return html, text


def get_message(service, message_id: str) -> GmailMessage:
    message = (
        service.users().messages().get(userId="me", id=message_id, format="full").execute()
    )
    headers = {h["name"].lower(): h["value"] for h in message["payload"].get("headers", [])}
    subject = headers.get("subject", "")
    from_email = headers.get("from", "")
    label_ids = json.dumps(message.get("labelIds", []))
    internal_date = int(message.get("internalDate", 0))
    html, text = extract_parts(message["payload"])
    return GmailMessage(
        message_id=message_id,
        internal_date=internal_date,
        subject=subject,
        from_email=from_email,
        label_ids=label_ids,
        html=html,
        text=text,
    )


def hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def ingest(
    label: str,
    max_results: int,
    since_days: Optional[int],
    db_path: str,
    credentials_path: str,
    token_path: str,
    fetch_articles_flag: bool,
    max_articles: int,
    fetch_timeout: int,
    fetch_retries: int,
    fetch_rate_limit: float,
    fetch_summary_json: str,
    resolve_redirects: bool,
    redirect_timeout: int,
    redirect_retries: int,
    redirect_rate_limit: float,
    vault_path: Optional[str],
    issues_subdir: str,
) -> None:
    service = get_gmail_service(credentials_path, token_path)
    label_id = resolve_label_id(service, label)
    if not label_id:
        raise RuntimeError(f"Label not found: {label}")

    since_query = None
    if since_days is not None:
        since_query = f"newer_than:{since_days}d"

    conn = init_db(db_path)
    message_refs = list_messages(service, label_id, max_results, since_query)
    if not message_refs:
        print("No messages found.")
        return

    for ref in message_refs:
        message_id = ref["id"]
        exists = conn.execute(
            "SELECT 1 FROM gmail_messages WHERE message_id = ?",
            (message_id,),
        ).fetchone()
        if exists:
            continue
        msg = get_message(service, message_id)
        store_message(conn, msg)
        links = extract_and_store_links(
            conn,
            msg,
            resolve_redirects=resolve_redirects,
            redirect_timeout=redirect_timeout,
            redirect_retries=redirect_retries,
            redirect_rate_limit=redirect_rate_limit,
        )
        issue_path = write_issue_note(vault_path or "", issues_subdir, msg, links)
        update_issue_note_path(conn, msg.message_id, issue_path)
    conn.commit()

    if fetch_articles_flag:
        urls = get_unprocessed_links(conn, max_articles)
        total = 0
        status_counts: dict[str, int] = {}
        domain_counts: dict[str, int] = {}
        failure_domain_counts: dict[str, int] = {}
        for url in urls:
            total += 1
            domain = urlsplit(url).netloc
            domain_counts[domain] = domain_counts.get(domain, 0) + 1
            print(f"[fetch] {url}")
            status, title, text = fetch_article(
                url,
                timeout=fetch_timeout,
                retries=fetch_retries,
            )
            print(f"[fetch] status={status} title={title or ''}")
            content_hash = hash_text(text) if text else None
            mark_link_processed(conn, url, status, title, content_hash)
            status_counts[status] = status_counts.get(status, 0) + 1
            if status != "ok":
                failure_domain_counts[domain] = failure_domain_counts.get(domain, 0) + 1
            if fetch_rate_limit > 0:
                time.sleep(fetch_rate_limit)
        conn.commit()
        print("\n[fetch-summary] total:", total)
        if status_counts:
            print("[fetch-summary] status counts:")
            for key in sorted(status_counts):
                print(f"  {key}: {status_counts[key]}")
        if domain_counts:
            top_domains = sorted(domain_counts.items(), key=lambda x: x[1], reverse=True)[:10]
            print("[fetch-summary] top domains:")
            for domain, count in top_domains:
                print(f"  {domain}: {count}")
        if failure_domain_counts:
            top_failures = sorted(
                failure_domain_counts.items(), key=lambda x: x[1], reverse=True
            )[:10]
            print("[fetch-summary] top failing domains:")
            for domain, count in top_failures:
                print(f"  {domain}: {count}")
        if fetch_summary_json:
            summary = {
                "type": "fetch_summary",
                "total": total,
                "status_counts": status_counts,
                "domain_counts_top10": dict(
                    sorted(domain_counts.items(), key=lambda x: x[1], reverse=True)[:10]
                ),
                "failure_domain_counts_top10": dict(top_failures) if failure_domain_counts else {},
                "timestamp": int(time.time()),
            }
            with open(fetch_summary_json, "w", encoding="utf-8") as f:
                json.dump(summary, f, indent=2)


def process_links(
    db_path: str,
    vault_path: str,
    articles_subdir: str,
    max_links: int,
    fetch_timeout: int,
    fetch_retries: int,
    fetch_rate_limit: float,
    fetch_summary_json: str,
) -> None:
    if not vault_path:
        raise RuntimeError("Vault path is required for process-links.")
    conn = init_db(db_path)
    urls = get_unprocessed_links(conn, max_links)
    if not urls:
        print("No unprocessed links.")
        return
    today = time.strftime("%Y-%m-%d")
    total = 0
    status_counts: dict[str, int] = {}
    domain_counts: dict[str, int] = {}
    failure_domain_counts: dict[str, int] = {}
    for url in urls:
        total += 1
        domain = urlsplit(url).netloc
        domain_counts[domain] = domain_counts.get(domain, 0) + 1
        print(f"[fetch] {url}")
        status, title, text = fetch_article(
            url,
            timeout=fetch_timeout,
            retries=fetch_retries,
        )
        print(f"[fetch] status={status} title={title or ''}")
        content_hash = hash_text(text) if text else None
        status_counts[status] = status_counts.get(status, 0) + 1
        if status != "ok":
            failure_domain_counts[domain] = failure_domain_counts.get(domain, 0) + 1
        if status == "ok" and text:
            summary_data = summarize_text(
                text,
                title=title,
                url=url,
                domain=domain,
            )
            summary = summary_data.get("summary", "")
            bullets = summary_data.get("bullets", [])
            category = summary_data.get("category", "Other")
            tags = summary_data.get("tags", [])
            note_title = title or domain or "Article"
            note_path = write_article_note(
                vault_path,
                articles_subdir,
                title=note_title,
                url=url,
                date_iso=today,
                source=domain or "unknown",
                category=category,
                tags=tags,
                summary=summary,
                bullets=bullets,
            )
            mark_link_processed(
                conn,
                url,
                status,
                title,
                content_hash,
                summary=summary,
                category=category,
                tags=tags,
                note_path=note_path,
            )
            row = conn.execute(
                """
                SELECT gm.issue_note_path
                FROM links l
                JOIN gmail_messages gm
                  ON gm.message_id = l.first_seen_message_id
                WHERE l.url_canonical = ?
                """,
                (url,),
            ).fetchone()
            if row and row[0]:
                update_issue_note_with_article_link(
                    row[0],
                    note_path,
                    vault_path,
                    note_title,
                )
        else:
            mark_link_processed(conn, url, status, title, content_hash)
        if fetch_rate_limit > 0:
            time.sleep(fetch_rate_limit)
    conn.commit()
    print("\n[fetch-summary] total:", total)
    if status_counts:
        print("[fetch-summary] status counts:")
        for key in sorted(status_counts):
            print(f"  {key}: {status_counts[key]}")
    if domain_counts:
        top_domains = sorted(domain_counts.items(), key=lambda x: x[1], reverse=True)[:10]
        print("[fetch-summary] top domains:")
        for domain, count in top_domains:
            print(f"  {domain}: {count}")
    if failure_domain_counts:
        top_failures = sorted(
            failure_domain_counts.items(), key=lambda x: x[1], reverse=True
        )[:10]
        print("[fetch-summary] top failing domains:")
        for domain, count in top_failures:
            print(f"  {domain}: {count}")
    if fetch_summary_json:
        summary = {
            "type": "fetch_summary",
            "total": total,
            "status_counts": status_counts,
            "domain_counts_top10": dict(
                sorted(domain_counts.items(), key=lambda x: x[1], reverse=True)[:10]
            ),
            "failure_domain_counts_top10": dict(top_failures) if failure_domain_counts else {},
            "timestamp": int(time.time()),
        }
        with open(fetch_summary_json, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)


def backfill_redirects(
    db_path: str,
    *,
    max_links: int,
    redirect_timeout: int,
    redirect_retries: int,
    redirect_rate_limit: float,
) -> None:
    conn = init_db(db_path)
    rows = conn.execute(
        "SELECT url_canonical, original_url FROM links ORDER BY discovered_at DESC LIMIT ?",
        (max_links,),
    ).fetchall()
    updated = 0
    for url_canonical, original_url in rows:
        resolved = resolve_redirect_url(
            url_canonical,
            timeout=redirect_timeout,
            retries=redirect_retries,
        )
        if not resolved:
            continue
        canon = canonicalize_url(resolved)
        if not canon or canon == url_canonical:
            continue
        exists = conn.execute(
            "SELECT 1 FROM links WHERE url_canonical = ?",
            (canon,),
        ).fetchone()
        if exists:
            print(f"[backfill] skip (conflict): {url_canonical} -> {canon}")
            continue
        domain = urlsplit(canon).netloc
        conn.execute(
            """
            UPDATE links
            SET url_canonical = ?, domain = ?, original_url = COALESCE(?, original_url)
            WHERE url_canonical = ?
            """,
            (
                canon,
                domain,
                original_url or url_canonical,
                url_canonical,
            ),
        )
        updated += 1
        if redirect_rate_limit > 0:
            time.sleep(redirect_rate_limit)
    conn.commit()
    print(f"[backfill] updated {updated} links")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Newsletter Gmail ingestion.")
    subparsers = parser.add_subparsers(dest="command")

    ingest_parser = subparsers.add_parser("ingest", help="Ingest Gmail messages")
    ingest_parser.add_argument("--label", default="Newsletters", help="Gmail label name")
    ingest_parser.add_argument("--max", type=int, default=20, help="Max messages to fetch")
    ingest_parser.add_argument(
        "--since-days", type=int, default=7, help="Only fetch recent messages"
    )
    ingest_parser.add_argument("--db", default=DEFAULT_DB, help="SQLite DB path")
    ingest_parser.add_argument("--credentials", default="credentials.json", help="OAuth credentials")
    ingest_parser.add_argument("--token", default="token.json", help="OAuth token")
    ingest_parser.add_argument(
        "--vault",
        default="",
        help="Path to Obsidian vault (issue notes written under this folder)",
    )
    ingest_parser.add_argument(
        "--issues-dir",
        default=os.path.join("Newsletters", "Issues"),
        help="Subdirectory inside the vault for issue notes",
    )
    ingest_parser.add_argument(
        "--fetch-articles",
        action="store_true",
        help="Fetch article pages and store title/hash",
    )
    ingest_parser.add_argument("--max-articles", type=int, default=50, help="Max links to fetch")
    ingest_parser.add_argument(
        "--resolve-redirects",
        action="store_true",
        help="Resolve tracking/redirect URLs before storing",
    )
    ingest_parser.add_argument(
        "--redirect-timeout",
        type=int,
        default=10,
        help="Timeout in seconds for redirect resolution",
    )
    ingest_parser.add_argument(
        "--redirect-retries",
        type=int,
        default=1,
        help="Number of retries for redirect resolution",
    )
    ingest_parser.add_argument(
        "--redirect-rate-limit",
        type=float,
        default=0.0,
        help="Seconds to sleep between redirect checks (0 disables)",
    )
    ingest_parser.add_argument(
        "--fetch-timeout",
        type=int,
        default=15,
        help="Timeout in seconds for article fetches",
    )
    ingest_parser.add_argument(
        "--fetch-retries",
        type=int,
        default=2,
        help="Number of retries for transient fetch failures",
    )
    ingest_parser.add_argument(
        "--fetch-rate-limit",
        type=float,
        default=0.0,
        help="Seconds to sleep between fetches (0 disables)",
    )
    ingest_parser.add_argument(
        "--fetch-summary-json",
        default="",
        help="Write fetch summary JSON to this path (optional)",
    )

    process_parser = subparsers.add_parser(
        "process-links", help="Fetch and process unprocessed article links"
    )
    process_parser.add_argument("--db", default=DEFAULT_DB, help="SQLite DB path")
    process_parser.add_argument(
        "--vault",
        default="",
        help="Path to Obsidian vault (article notes written under this folder)",
    )
    process_parser.add_argument(
        "--articles-dir",
        default=os.path.join("Newsletters", "Articles"),
        help="Subdirectory inside the vault for article notes",
    )
    process_parser.add_argument("--max-links", type=int, default=25, help="Max links to fetch")
    process_parser.add_argument(
        "--fetch-timeout",
        type=int,
        default=15,
        help="Timeout in seconds for article fetches",
    )
    process_parser.add_argument(
        "--fetch-retries",
        type=int,
        default=2,
        help="Number of retries for transient fetch failures",
    )
    process_parser.add_argument(
        "--fetch-rate-limit",
        type=float,
        default=0.0,
        help="Seconds to sleep between fetches (0 disables)",
    )
    process_parser.add_argument(
        "--fetch-summary-json",
        default="",
        help="Write fetch summary JSON to this path (optional)",
    )

    backfill_parser = subparsers.add_parser(
        "backfill-redirects",
        help="Resolve and update redirect URLs for existing links",
    )
    backfill_parser.add_argument("--db", default=DEFAULT_DB, help="SQLite DB path")
    backfill_parser.add_argument("--max-links", type=int, default=200, help="Max links to scan")
    backfill_parser.add_argument(
        "--redirect-timeout",
        type=int,
        default=10,
        help="Timeout in seconds for redirect resolution",
    )
    backfill_parser.add_argument(
        "--redirect-retries",
        type=int,
        default=1,
        help="Number of retries for redirect resolution",
    )
    backfill_parser.add_argument(
        "--redirect-rate-limit",
        type=float,
        default=0.0,
        help="Seconds to sleep between redirect checks (0 disables)",
    )

    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.command is None:
        # Default to ingest subcommand when none is provided.
        args = build_parser().parse_args(["ingest", *sys.argv[1:]])
    if args.command == "ingest":
        ingest(
            label=args.label,
            max_results=args.max,
            since_days=args.since_days,
            db_path=args.db,
            credentials_path=args.credentials,
            token_path=args.token,
            fetch_articles_flag=args.fetch_articles,
            max_articles=args.max_articles,
            fetch_timeout=args.fetch_timeout,
            fetch_retries=args.fetch_retries,
            fetch_rate_limit=args.fetch_rate_limit,
            fetch_summary_json=args.fetch_summary_json,
            resolve_redirects=args.resolve_redirects,
            redirect_timeout=args.redirect_timeout,
            redirect_retries=args.redirect_retries,
            redirect_rate_limit=args.redirect_rate_limit,
            vault_path=args.vault,
            issues_subdir=args.issues_dir,
        )
        return
    if args.command == "process-links":
        process_links(
            db_path=args.db,
            vault_path=args.vault,
            articles_subdir=args.articles_dir,
            max_links=args.max_links,
            fetch_timeout=args.fetch_timeout,
            fetch_retries=args.fetch_retries,
            fetch_rate_limit=args.fetch_rate_limit,
            fetch_summary_json=args.fetch_summary_json,
        )
        return
    if args.command == "backfill-redirects":
        backfill_redirects(
            db_path=args.db,
            max_links=args.max_links,
            redirect_timeout=args.redirect_timeout,
            redirect_retries=args.redirect_retries,
            redirect_rate_limit=args.redirect_rate_limit,
        )
        return
    raise SystemExit("Unknown command")


if __name__ == "__main__":
    main()
