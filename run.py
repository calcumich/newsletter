#!/usr/bin/env python
import argparse
import base64
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from typing import Iterable, List, Optional, Tuple
from urllib.parse import parse_qsl, quote, urlsplit, urlunsplit

import requests

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

TRACKING_PARAMS = {
    "fbclid",
    "gclid",
    "igshid",
    "mc_cid",
    "mc_eid",
    "mkt_tok",
    "ref",
}

SKIP_SUBSTRINGS = [
    "unsubscribe",
    "optout",
    "manage-preferences",
    "manage_preferences",
    "preferences",
    "viewinbrowser",
    "view-in-browser",
]

SKIP_DOMAINS = {
    "facebook.com",
    "twitter.com",
    "x.com",
    "linkedin.com",
    "instagram.com",
}

SKIP_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"}


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
            content_hash TEXT
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
            content_hash TEXT
        )
        """
    )
    conn.commit()
    return conn


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


def extract_links(html: Optional[str], text: Optional[str]) -> List[Tuple[str, Optional[str]]]:
    links: List[Tuple[str, Optional[str]]] = []
    if html and BeautifulSoup is not None:
        soup = BeautifulSoup(html, "lxml" if "lxml" in sys.modules else "html.parser")
        for tag in soup.find_all("a", href=True):
            href = tag["href"].strip()
            anchor = tag.get_text(" ", strip=True) or None
            links.append((href, anchor))
    if text:
        for match in re.findall(r"https?://[^\s<>()\"']+", text):
            links.append((match, None))
    return links


def canonicalize_url(url: str) -> Optional[str]:
    url = url.strip()
    if not url:
        return None
    if url.startswith("//"):
        url = "https:" + url
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return None
    netloc = parts.netloc.lower()
    if netloc.endswith(":80") and parts.scheme == "http":
        netloc = netloc[:-3]
    if netloc.endswith(":443") and parts.scheme == "https":
        netloc = netloc[:-4]
    path = parts.path or "/"
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    query_pairs = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key.startswith("utm_"):
            continue
        if key in TRACKING_PARAMS:
            continue
        query_pairs.append((key, value))
    query_pairs.sort()
    query = "&".join([f"{quote(k)}={quote(v)}" if v else quote(k) for k, v in query_pairs])
    return urlunsplit((parts.scheme, netloc, path, query, ""))


def should_skip_url(url: str) -> bool:
    lower = url.lower()
    if lower.startswith("mailto:"):
        return True
    for fragment in SKIP_SUBSTRINGS:
        if fragment in lower:
            return True
    parts = urlsplit(lower)
    if parts.netloc in SKIP_DOMAINS:
        return True
    for ext in SKIP_EXTENSIONS:
        if parts.path.endswith(ext):
            return True
    return False


def hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def store_message(conn: sqlite3.Connection, msg: GmailMessage) -> None:
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


def store_link(
    conn: sqlite3.Connection,
    url: str,
    message_id: str,
    discovered_at: int,
) -> None:
    domain = urlsplit(url).netloc
    conn.execute(
        """
        INSERT OR IGNORE INTO links
        (url_canonical, first_seen_message_id, domain, discovered_at)
        VALUES (?, ?, ?, ?)
        """,
        (url, message_id, domain, discovered_at),
    )


def extract_and_store_links(conn, msg: GmailMessage) -> List[Tuple[str, Optional[str]]]:
    raw_links = extract_links(msg.html, msg.text)
    seen = set()
    canonical_links: List[Tuple[str, Optional[str]]] = []
    for href, _anchor in raw_links:
        if should_skip_url(href):
            continue
        canon = canonicalize_url(href)
        if not canon or canon in seen:
            continue
        seen.add(canon)
        canonical_links.append((canon, _anchor))
        store_link(conn, canon, msg.message_id, int(time.time()))
    return canonical_links


def slugify_filename(text: str) -> str:
    safe = re.sub(r"[^\w\s.-]", "", text, flags=re.UNICODE)
    safe = re.sub(r"\s+", " ", safe).strip()
    if not safe:
        safe = "Newsletter"
    return safe[:120]


def write_issue_note(
    vault_path: str,
    issues_subdir: str,
    msg: GmailMessage,
    links: List[Tuple[str, Optional[str]]],
) -> Optional[str]:
    if not vault_path:
        return None
    issue_date = time.strftime("%Y-%m-%d", time.localtime(msg.internal_date / 1000))
    year = issue_date[:4]
    month = issue_date[5:7]
    folder = os.path.join(vault_path, issues_subdir, year, month)
    os.makedirs(folder, exist_ok=True)
    subject = msg.subject or "Newsletter"
    filename = f"{issue_date} - {slugify_filename(subject)}.md"
    path = os.path.join(folder, filename)
    if os.path.exists(path):
        return path
    domain_map: dict[str, List[Tuple[str, Optional[str]]]] = {}
    for url, anchor in links:
        domain = urlsplit(url).netloc
        domain_map.setdefault(domain, []).append((url, anchor))
    domains_sorted = sorted(domain_map.keys())
    frontmatter = [
        "---",
        "type: newsletter-issue",
        f'source: "{subject}"',
        f"date: {issue_date}",
        f'gmail_message_id: "{msg.message_id}"',
        f'from: "{msg.from_email}"',
        "tags: [newsletters]",
        "---",
        "",
        "# Summary",
        f"- Total links: {len(links)}",
        f"- Domains: {len(domains_sorted)}",
        "",
        "# Links",
        "",
    ]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(frontmatter))
        for domain in domains_sorted:
            f.write(f"## {domain}\n\n")
            for url, anchor in domain_map[domain]:
                label = anchor.strip() if anchor else url
                f.write(f"- [{label}]({url})\n")
            f.write("\n")
    return path


def get_unprocessed_links(conn: sqlite3.Connection, limit: int) -> List[str]:
    rows = conn.execute(
        "SELECT url_canonical FROM links WHERE processed_at IS NULL LIMIT ?",
        (limit,),
    ).fetchall()
    return [row[0] for row in rows]


def fetch_article(url: str, timeout: int = 15) -> Tuple[str, Optional[str], Optional[str]]:
    headers = {"User-Agent": "newsletter-ingest/0.1"}
    try:
        resp = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
    except requests.RequestException as exc:
        return "fail", None, str(exc)
    if resp.status_code >= 400:
        return f"http_{resp.status_code}", None, None
    content_type = resp.headers.get("content-type", "").lower()
    if "text/html" not in content_type:
        return "non_html", None, None
    title = None
    text_content = None
    if BeautifulSoup is not None:
        soup = BeautifulSoup(resp.text, "lxml" if "lxml" in sys.modules else "html.parser")
        if soup.title and soup.title.text:
            title = soup.title.text.strip()
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        text_content = soup.get_text(" ", strip=True)
    return "ok", title, text_content


def mark_link_processed(
    conn: sqlite3.Connection,
    url: str,
    status: str,
    title: Optional[str],
    content_hash: Optional[str],
) -> None:
    conn.execute(
        """
        UPDATE links
        SET processed_at = ?, fetch_status = ?, title = ?, content_hash = ?
        WHERE url_canonical = ?
        """,
        (int(time.time()), status, title, content_hash, url),
    )


def ingest(
    label: str,
    max_results: int,
    since_days: Optional[int],
    db_path: str,
    credentials_path: str,
    token_path: str,
    fetch_articles_flag: bool,
    max_articles: int,
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
        links = extract_and_store_links(conn, msg)
        write_issue_note(vault_path or "", issues_subdir, msg, links)
    conn.commit()

    if fetch_articles_flag:
        urls = get_unprocessed_links(conn, max_articles)
        for url in urls:
            status, title, text = fetch_article(url)
            content_hash = hash_text(text) if text else None
            mark_link_processed(conn, url, status, title, content_hash)
        conn.commit()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Newsletter Gmail ingestion.")
    parser.add_argument("--label", default="Newsletters", help="Gmail label name")
    parser.add_argument("--max", type=int, default=20, help="Max messages to fetch")
    parser.add_argument("--since-days", type=int, default=7, help="Only fetch recent messages")
    parser.add_argument("--db", default=DEFAULT_DB, help="SQLite DB path")
    parser.add_argument("--credentials", default="credentials.json", help="OAuth credentials")
    parser.add_argument("--token", default="token.json", help="OAuth token")
    parser.add_argument(
        "--vault",
        default="",
        help="Path to Obsidian vault (issue notes written under this folder)",
    )
    parser.add_argument(
        "--issues-dir",
        default=os.path.join("Newsletters", "Issues"),
        help="Subdirectory inside the vault for issue notes",
    )
    parser.add_argument(
        "--fetch-articles",
        action="store_true",
        help="Fetch article pages and store title/hash",
    )
    parser.add_argument("--max-articles", type=int, default=50, help="Max links to fetch")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    ingest(
        label=args.label,
        max_results=args.max,
        since_days=args.since_days,
        db_path=args.db,
        credentials_path=args.credentials,
        token_path=args.token,
        fetch_articles_flag=args.fetch_articles,
        max_articles=args.max_articles,
        vault_path=args.vault,
        issues_subdir=args.issues_dir,
    )


if __name__ == "__main__":
    main()
