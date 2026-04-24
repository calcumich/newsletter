import argparse
import json
import os
import time
from typing import Optional
from urllib.parse import urlsplit

from newsletter.db import (
    get_links_for_refresh,
    get_unprocessed_links,
    init_db,
    mark_link_processed,
    store_message,
    update_issue_note_path,
)
from newsletter.fetch import fetch_article_detailed
from newsletter.gmail import get_gmail_service, resolve_label_id, list_messages, get_message
from newsletter.links import canonicalize_url, extract_and_store_links, resolve_redirect_url
from newsletter.obsidian import update_issue_note_with_article_link, write_article_note, write_issue_note
from newsletter.summarize import summarize_text

DEFAULT_DB = "newsletter.db"


def hash_text(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write_jsonl_event(log_jsonl: str, event: dict) -> None:
    if not log_jsonl:
        return
    folder = os.path.dirname(log_jsonl)
    if folder:
        os.makedirs(folder, exist_ok=True)
    payload = dict(event)
    payload.setdefault("timestamp", int(time.time()))
    with open(log_jsonl, "a", encoding="utf-8") as f:
        f.write(json.dumps(payload) + "\n")


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
    log_jsonl: str,
    vault_path: Optional[str],
    issues_subdir: str,
    beautiful_soup_available: bool,
) -> None:
    write_jsonl_event(
        log_jsonl,
        {
            "event": "run_start",
            "command": "ingest",
            "label": label,
            "max_results": max_results,
            "since_days": since_days,
        },
    )
    service = get_gmail_service(
        credentials_path,
        token_path,
        beautiful_soup_available=beautiful_soup_available,
    )
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
        write_jsonl_event(
            log_jsonl,
            {"event": "run_summary", "command": "ingest", "messages_found": 0},
        )
        return

    processed_messages = 0
    skipped_messages = 0
    for ref in message_refs:
        message_id = ref["id"]
        exists = conn.execute(
            "SELECT 1 FROM gmail_messages WHERE message_id = ?",
            (message_id,),
        ).fetchone()
        if exists:
            skipped_messages += 1
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
        processed_messages += 1
        write_jsonl_event(
            log_jsonl,
            {
                "event": "message_processed",
                "command": "ingest",
                "message_id": msg.message_id,
                "subject": msg.subject,
                "links_found": len(links),
                "issue_note_path": issue_path,
            },
        )
    conn.commit()
    write_jsonl_event(
        log_jsonl,
        {
            "event": "ingest_summary",
            "command": "ingest",
            "messages_found": len(message_refs),
            "messages_processed": processed_messages,
            "messages_skipped_existing": skipped_messages,
        },
    )

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
            status, title, text, fetch_meta = fetch_article_detailed(
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
            write_jsonl_event(
                log_jsonl,
                {
                    "event": "url_processed",
                    "command": "ingest",
                    "url": url,
                    "domain": domain,
                    "status": status,
                    "title": title,
                    "error_class": fetch_meta.get("error_class"),
                    "http_status": fetch_meta.get("http_status"),
                    "retry_count": fetch_meta.get("retry_count"),
                },
            )
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
        write_jsonl_event(
            log_jsonl,
            {
                "event": "run_summary",
                "command": "ingest",
                "fetch_total": total,
                "status_counts": status_counts,
                "domain_counts_top10": dict(
                    sorted(domain_counts.items(), key=lambda x: x[1], reverse=True)[:10]
                ),
                "failure_domain_counts_top10": dict(top_failures) if failure_domain_counts else {},
            },
        )


def process_links(
    db_path: str,
    vault_path: str,
    articles_subdir: str,
    max_links: int,
    dry_run: bool,
    log_jsonl: str,
    fetch_timeout: int,
    fetch_retries: int,
    fetch_rate_limit: float,
    fetch_summary_json: str,
) -> None:
    write_jsonl_event(
        log_jsonl,
        {"event": "run_start", "command": "process-links", "max_links": max_links, "dry_run": dry_run},
    )
    if not dry_run and not vault_path:
        raise RuntimeError("Vault path is required for process-links.")
    conn = init_db(db_path)
    urls = get_unprocessed_links(conn, max_links)
    if not urls:
        print("No unprocessed links.")
        write_jsonl_event(
            log_jsonl,
            {"event": "run_summary", "command": "process-links", "total_candidates": 0},
        )
        return
    if dry_run:
        print(f"[process-dry-run] unprocessed links: {len(urls)}")
        for url in urls:
            print(f"[process-dry-run] {url}")
            write_jsonl_event(
                log_jsonl,
                {"event": "candidate", "command": "process-links", "url": url},
            )
        write_jsonl_event(
            log_jsonl,
            {"event": "run_summary", "command": "process-links", "total_candidates": len(urls), "dry_run": True},
        )
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
        status, title, text, fetch_meta = fetch_article_detailed(
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
            llm_meta = summary_data.get("_meta", {})
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
                    source_url=url,
                )
        else:
            mark_link_processed(conn, url, status, title, content_hash)
            note_path = None
        write_jsonl_event(
            log_jsonl,
            {
                "event": "url_processed",
                "command": "process-links",
                "url": url,
                "domain": domain,
                "status": status,
                "title": title,
                "error_class": fetch_meta.get("error_class"),
                "http_status": fetch_meta.get("http_status"),
                "retry_count": fetch_meta.get("retry_count"),
                "note_path": note_path,
                "llm_mode": llm_meta.get("llm_mode") if status == "ok" and text else None,
                "fallback_used": llm_meta.get("fallback_used") if status == "ok" and text else None,
                "model": llm_meta.get("model") if status == "ok" and text else None,
                "prompt_version": llm_meta.get("prompt_version") if status == "ok" and text else None,
                "llm_latency_ms": llm_meta.get("llm_latency_ms") if status == "ok" and text else None,
            },
        )
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
    write_jsonl_event(
        log_jsonl,
        {
            "event": "run_summary",
            "command": "process-links",
            "total": total,
            "status_counts": status_counts,
            "domain_counts_top10": dict(
                sorted(domain_counts.items(), key=lambda x: x[1], reverse=True)[:10]
            ),
            "failure_domain_counts_top10": dict(top_failures) if failure_domain_counts else {},
        },
    )


def refresh_links(
    db_path: str,
    vault_path: str,
    articles_subdir: str,
    max_links: int,
    older_than_days: int,
    statuses: Optional[list[str]],
    domains: Optional[list[str]],
    categories: Optional[list[str]],
    status_mode: str,
    dry_run: bool,
    log_jsonl: str,
    fetch_timeout: int,
    fetch_retries: int,
    fetch_rate_limit: float,
    fetch_summary_json: str,
) -> None:
    write_jsonl_event(
        log_jsonl,
        {
            "event": "run_start",
            "command": "refresh",
            "max_links": max_links,
            "older_than_days": older_than_days,
            "dry_run": dry_run,
            "statuses": statuses or [],
            "domains": domains or [],
            "categories": categories or [],
            "status_mode": status_mode,
        },
    )
    if not vault_path:
        raise RuntimeError("Vault path is required for refresh.")
    conn = init_db(db_path)
    urls = get_links_for_refresh(
        conn,
        limit=max_links,
        older_than_days=older_than_days,
        statuses=statuses,
        domains=domains,
        categories=categories,
        status_mode=status_mode,
    )
    if not urls:
        print("No links eligible for refresh.")
        write_jsonl_event(
            log_jsonl,
            {"event": "run_summary", "command": "refresh", "total_candidates": 0},
        )
        return
    if dry_run:
        print(f"[refresh-dry-run] eligible links: {len(urls)}")
        for url in urls:
            print(f"[refresh-dry-run] {url}")
            write_jsonl_event(
                log_jsonl,
                {"event": "candidate", "command": "refresh", "url": url},
            )
        write_jsonl_event(
            log_jsonl,
            {"event": "run_summary", "command": "refresh", "total_candidates": len(urls), "dry_run": True},
        )
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
        print(f"[refresh] {url}")
        status, title, text, fetch_meta = fetch_article_detailed(
            url,
            timeout=fetch_timeout,
            retries=fetch_retries,
        )
        print(f"[refresh] status={status} title={title or ''}")
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
            llm_meta = summary_data.get("_meta", {})
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
                    source_url=url,
                )
        else:
            mark_link_processed(conn, url, status, title, content_hash)
            note_path = None
        write_jsonl_event(
            log_jsonl,
            {
                "event": "url_processed",
                "command": "refresh",
                "url": url,
                "domain": domain,
                "status": status,
                "title": title,
                "error_class": fetch_meta.get("error_class"),
                "http_status": fetch_meta.get("http_status"),
                "retry_count": fetch_meta.get("retry_count"),
                "note_path": note_path,
                "llm_mode": llm_meta.get("llm_mode") if status == "ok" and text else None,
                "fallback_used": llm_meta.get("fallback_used") if status == "ok" and text else None,
                "model": llm_meta.get("model") if status == "ok" and text else None,
                "prompt_version": llm_meta.get("prompt_version") if status == "ok" and text else None,
                "llm_latency_ms": llm_meta.get("llm_latency_ms") if status == "ok" and text else None,
            },
        )
        if fetch_rate_limit > 0:
            time.sleep(fetch_rate_limit)
    conn.commit()
    print("\n[refresh-summary] total:", total)
    if status_counts:
        print("[refresh-summary] status counts:")
        for key in sorted(status_counts):
            print(f"  {key}: {status_counts[key]}")
    if domain_counts:
        top_domains = sorted(domain_counts.items(), key=lambda x: x[1], reverse=True)[:10]
        print("[refresh-summary] top domains:")
        for domain, count in top_domains:
            print(f"  {domain}: {count}")
    if failure_domain_counts:
        top_failures = sorted(
            failure_domain_counts.items(), key=lambda x: x[1], reverse=True
        )[:10]
        print("[refresh-summary] top failing domains:")
        for domain, count in top_failures:
            print(f"  {domain}: {count}")
    if fetch_summary_json:
        summary = {
            "type": "refresh_summary",
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
    write_jsonl_event(
        log_jsonl,
        {
            "event": "run_summary",
            "command": "refresh",
            "total": total,
            "status_counts": status_counts,
            "domain_counts_top10": dict(
                sorted(domain_counts.items(), key=lambda x: x[1], reverse=True)[:10]
            ),
            "failure_domain_counts_top10": dict(top_failures) if failure_domain_counts else {},
        },
    )


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
    ingest_parser.add_argument(
        "--log-jsonl",
        default="",
        help="Append structured run events to this JSONL file (optional)",
    )
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
    process_parser.add_argument(
        "--log-jsonl",
        default="",
        help="Append structured run events to this JSONL file (optional)",
    )
    process_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show unprocessed links without fetching or writing",
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

    refresh_parser = subparsers.add_parser(
        "refresh",
        help="Reprocess previously processed links older than a threshold",
    )
    refresh_parser.add_argument("--db", default=DEFAULT_DB, help="SQLite DB path")
    refresh_parser.add_argument(
        "--vault",
        default="",
        help="Path to Obsidian vault (article notes written under this folder)",
    )
    refresh_parser.add_argument(
        "--articles-dir",
        default=os.path.join("Newsletters", "Articles"),
        help="Subdirectory inside the vault for article notes",
    )
    refresh_parser.add_argument("--max-links", type=int, default=25, help="Max links to refresh")
    refresh_parser.add_argument(
        "--older-than-days",
        type=int,
        default=30,
        help="Refresh links last processed at least this many days ago",
    )
    refresh_parser.add_argument(
        "--statuses",
        default="",
        help="Optional comma-separated fetch statuses to include (e.g. ok,fail,http_403)",
    )
    refresh_mode = refresh_parser.add_mutually_exclusive_group()
    refresh_mode.add_argument(
        "--failed-only",
        action="store_true",
        help="Only include links whose previous fetch status was not ok",
    )
    refresh_mode.add_argument(
        "--ok-only",
        action="store_true",
        help="Only include links whose previous fetch status was ok",
    )
    refresh_mode.add_argument(
        "--stale-ok",
        action="store_true",
        help="Shortcut for --ok-only with older-than filtering",
    )
    refresh_parser.add_argument(
        "--domains",
        default="",
        help="Optional comma-separated domains to include (e.g. example.com,news.ycombinator.com)",
    )
    refresh_parser.add_argument(
        "--categories",
        default="",
        help="Optional comma-separated categories to include (e.g. Dev Tools,Security)",
    )
    refresh_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show links eligible for refresh without fetching or writing",
    )
    refresh_parser.add_argument(
        "--fetch-timeout",
        type=int,
        default=15,
        help="Timeout in seconds for article fetches",
    )
    refresh_parser.add_argument(
        "--fetch-retries",
        type=int,
        default=2,
        help="Number of retries for transient fetch failures",
    )
    refresh_parser.add_argument(
        "--fetch-rate-limit",
        type=float,
        default=0.0,
        help="Seconds to sleep between fetches (0 disables)",
    )
    refresh_parser.add_argument(
        "--fetch-summary-json",
        default="",
        help="Write refresh summary JSON to this path (optional)",
    )
    refresh_parser.add_argument(
        "--log-jsonl",
        default="",
        help="Append structured run events to this JSONL file (optional)",
    )

    return parser


def main(beautiful_soup_available: bool) -> None:
    args = build_parser().parse_args()
    if args.command is None:
        args = build_parser().parse_args(["ingest", *os.sys.argv[1:]])
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
            log_jsonl=args.log_jsonl,
            resolve_redirects=args.resolve_redirects,
            redirect_timeout=args.redirect_timeout,
            redirect_retries=args.redirect_retries,
            redirect_rate_limit=args.redirect_rate_limit,
            vault_path=args.vault,
            issues_subdir=args.issues_dir,
            beautiful_soup_available=beautiful_soup_available,
        )
        return
    if args.command == "process-links":
        process_links(
            db_path=args.db,
            vault_path=args.vault,
            articles_subdir=args.articles_dir,
            max_links=args.max_links,
            dry_run=args.dry_run,
            log_jsonl=args.log_jsonl,
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
    if args.command == "refresh":
        statuses = [s.strip() for s in args.statuses.split(",") if s.strip()]
        domains = [d.strip().lower() for d in args.domains.split(",") if d.strip()]
        categories = [c.strip() for c in args.categories.split(",") if c.strip()]
        status_mode = "any"
        if args.failed_only:
            status_mode = "failed_only"
        elif args.ok_only or args.stale_ok:
            status_mode = "ok_only"
        if status_mode != "any" and statuses:
            raise SystemExit("Do not combine --statuses with --failed-only/--ok-only/--stale-ok.")
        refresh_links(
            db_path=args.db,
            vault_path=args.vault,
            articles_subdir=args.articles_dir,
            max_links=args.max_links,
            older_than_days=args.older_than_days,
            statuses=statuses or None,
            domains=domains or None,
            categories=categories or None,
            status_mode=status_mode,
            dry_run=args.dry_run,
            log_jsonl=args.log_jsonl,
            fetch_timeout=args.fetch_timeout,
            fetch_retries=args.fetch_retries,
            fetch_rate_limit=args.fetch_rate_limit,
            fetch_summary_json=args.fetch_summary_json,
        )
        return
    raise SystemExit("Unknown command")
