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
    import trafilatura
except Exception:  # pragma: no cover - optional import
    trafilatura = None

try:
    from readability import Document
except Exception:  # pragma: no cover - optional import
    Document = None

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
            note_path TEXT
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
    existing = {
        row[1] for row in conn.execute("PRAGMA table_info(links)").fetchall()
    }
    for column, col_type in required.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE links ADD COLUMN {column} {col_type}")


def normalize_tags(tags: Optional[Iterable[str]]) -> List[str]:
    if not tags:
        return []
    cleaned = [tag.strip() for tag in tags if tag and tag.strip()]
    return sorted(set(cleaned))


def build_article_note_content(
    *,
    title: str,
    url: str,
    date_iso: str,
    source: str,
    category: str,
    tags: Optional[Iterable[str]],
    summary: str,
    bullets: Optional[Iterable[str]] = None,
    why_it_matters: Optional[str] = None,
) -> str:
    tag_list = normalize_tags(tags)
    yaml_tags = "[" + ", ".join([f'"{tag}"' for tag in tag_list]) + "]"
    frontmatter = [
        "---",
        "type: article",
        f'source: "{source}"',
        f'title: "{title}"',
        f"date: {date_iso}",
        f'url: "{url}"',
        f'category: "{category}"',
        f"tags: {yaml_tags}",
        "---",
        "",
        "# Summary",
        summary.strip() if summary else "",
        "",
        "# Key takeaways",
    ]
    lines = frontmatter
    bullets = [b.strip() for b in (bullets or []) if b and b.strip()]
    if bullets:
        for bullet in bullets:
            lines.append(f"- {bullet}")
    else:
        lines.append("-")
    if why_it_matters and why_it_matters.strip():
        lines.extend(["", "# Why it matters", why_it_matters.strip()])
    return "\n".join(lines).rstrip() + "\n"


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


def resolve_redirect_url(
    url: str,
    *,
    timeout: int = 10,
    retries: int = 1,
    backoff_base: float = 0.5,
) -> Optional[str]:
    headers = {"User-Agent": "newsletter-ingest/0.1"}
    attempt = 0
    while True:
        try:
            resp = requests.head(
                url, headers=headers, timeout=timeout, allow_redirects=True
            )
            if resp.status_code in (405, 403) or resp.status_code >= 500:
                resp = requests.get(
                    url, headers=headers, timeout=timeout, allow_redirects=True
                )
        except requests.RequestException:
            if attempt < retries:
                time.sleep(backoff_base * (2**attempt))
                attempt += 1
                continue
            return None
        if resp.status_code >= 400:
            return None
        return resp.url or url


def extract_and_store_links(
    conn,
    msg: GmailMessage,
    *,
    resolve_redirects: bool = False,
    redirect_timeout: int = 10,
    redirect_retries: int = 1,
    redirect_rate_limit: float = 0.0,
) -> List[Tuple[str, Optional[str]]]:
    raw_links = extract_links(msg.html, msg.text)
    seen = set()
    canonical_links: List[Tuple[str, Optional[str]]] = []
    for href, _anchor in raw_links:
        if should_skip_url(href):
            continue
        resolved_href = href
        if resolve_redirects:
            resolved = resolve_redirect_url(
                href,
                timeout=redirect_timeout,
                retries=redirect_retries,
            )
            if resolved:
                resolved_href = resolved
            if redirect_rate_limit > 0:
                time.sleep(redirect_rate_limit)
        canon = canonicalize_url(resolved_href)
        if not canon or canon in seen:
            continue
        seen.add(canon)
        canonical_links.append((canon, _anchor))
        store_link(conn, canon, href, msg.message_id, int(time.time()))
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


def make_obsidian_link(article_path: str, vault_path: str, title: str) -> str:
    rel = os.path.relpath(article_path, vault_path).replace("\\", "/")
    if rel.lower().endswith(".md"):
        rel = rel[:-3]
    return f"[[{rel}|{title}]]"


def update_issue_note_with_article_link(
    issue_note_path: str,
    article_path: str,
    vault_path: str,
    title: str,
) -> None:
    if not os.path.exists(issue_note_path):
        return
    link = make_obsidian_link(article_path, vault_path, title)
    with open(issue_note_path, "r", encoding="utf-8") as f:
        content = f.read()
    if link in content:
        return
    section_header = "\n## Articles\n"
    if "## Articles" not in content:
        content = content.rstrip() + section_header
    content = content.rstrip() + f"\n- {link}\n"
    with open(issue_note_path, "w", encoding="utf-8") as f:
        f.write(content)


def get_unprocessed_links(conn: sqlite3.Connection, limit: int) -> List[str]:
    rows = conn.execute(
        "SELECT url_canonical FROM links WHERE processed_at IS NULL LIMIT ?",
        (limit,),
    ).fetchall()
    return [row[0] for row in rows]


def fetch_article(
    url: str,
    *,
    timeout: int = 15,
    retries: int = 2,
    backoff_base: float = 1.0,
) -> Tuple[str, Optional[str], Optional[str]]:
    headers = {"User-Agent": "newsletter-ingest/0.1"}
    attempt = 0
    while True:
        try:
            resp = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        except requests.RequestException as exc:
            if attempt < retries:
                delay = backoff_base * (2**attempt)
                print(f"[fetch] error: {exc} (retrying in {delay:.1f}s)")
                time.sleep(delay)
                attempt += 1
                continue
            return "fail", None, str(exc)
        if resp.status_code >= 500 and attempt < retries:
            delay = backoff_base * (2**attempt)
            print(
                f"[fetch] http_{resp.status_code} for {url} (retrying in {delay:.1f}s)"
            )
            time.sleep(delay)
            attempt += 1
            continue
        if resp.status_code >= 400:
            return f"http_{resp.status_code}", None, None
        if resp.url and resp.url != url:
            print(f"[fetch] redirect: {url} -> {resp.url}")
        content_type = resp.headers.get("content-type", "").lower()
        if "text/html" not in content_type:
            return "non_html", None, None
        title, text_content = extract_main_text(resp.text)
        return "ok", title, text_content


def extract_main_text(html: str) -> Tuple[Optional[str], Optional[str]]:
    title = None
    text_content = None
    if trafilatura is not None:
        extracted = trafilatura.extract(html)
        if extracted:
            text_content = extracted.strip()
    if text_content is None and Document is not None:
        doc = Document(html)
        title = doc.short_title()
        content_html = doc.summary()
        if BeautifulSoup is not None:
            soup = BeautifulSoup(content_html, "lxml" if "lxml" in sys.modules else "html.parser")
            text_content = soup.get_text(" ", strip=True)
    if BeautifulSoup is not None:
        soup = BeautifulSoup(html, "lxml" if "lxml" in sys.modules else "html.parser")
        if not title and soup.title and soup.title.text:
            title = soup.title.text.strip()
        if text_content is None:
            for tag in soup(["script", "style", "noscript"]):
                tag.decompose()
            text_content = soup.get_text(" ", strip=True)
    return title, text_content


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
    tags_json = json.dumps(normalize_tags(tags)) if tags is not None else None
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


def summarize_text_stub(text: str, max_sentences: int = 2) -> Tuple[str, List[str]]:
    if not text:
        return "", []
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    summary = " ".join(sentences[:max_sentences]).strip()
    bullets = [s.strip() for s in sentences[: min(5, len(sentences))] if s.strip()]
    return summary, bullets


def extract_output_text(response_json: dict) -> str:
    output_items = response_json.get("output", [])
    parts: List[str] = []
    for item in output_items:
        if item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if content.get("type") == "output_text":
                text = content.get("text")
                if text:
                    parts.append(text)
    return "\n".join(parts).strip()


def summarize_text_openai(
    text: str,
    *,
    title: Optional[str],
    url: str,
    domain: str,
    model: str,
) -> Optional[dict]:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "summary": {"type": "string"},
            "bullets": {"type": "array", "items": {"type": "string"}},
            "category": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "confidence": {"type": "number"},
            "paywall_or_blocked": {"type": "boolean"},
        },
        "required": [
            "summary",
            "bullets",
            "category",
            "tags",
            "confidence",
            "paywall_or_blocked",
        ],
    }
    prompt = (
        "Summarize the following article content. "
        "Return concise summary, 3-6 bullet takeaways, category from a small tech set, "
        "3-8 tags, confidence 0-1, and whether paywalled/blocked."
    )
    payload = {
        "model": model,
        "input": [
            {"role": "system", "content": prompt},
            {
                "role": "user",
                "content": f"Title: {title or ''}\nURL: {url}\nDomain: {domain}\n\n{text}",
            },
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "article_summary",
                "strict": True,
                "schema": schema,
            }
        },
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    try:
        resp = requests.post(
            "https://api.openai.com/v1/responses",
            headers=headers,
            data=json.dumps(payload),
            timeout=30,
        )
    except requests.RequestException:
        return None
    if resp.status_code >= 400:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    raw_text = extract_output_text(data)
    if not raw_text:
        return None
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        return None


def summarize_text(
    text: str,
    *,
    title: Optional[str],
    url: str,
    domain: str,
) -> dict:
    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    result = summarize_text_openai(text, title=title, url=url, domain=domain, model=model)
    if result:
        return result
    summary, bullets = summarize_text_stub(text)
    return {
        "summary": summary,
        "bullets": bullets,
        "category": "Other",
        "tags": [],
        "confidence": 0.3,
        "paywall_or_blocked": False,
    }


def write_article_note(
    vault_path: str,
    articles_subdir: str,
    *,
    title: str,
    url: str,
    date_iso: str,
    source: str,
    category: str,
    tags: Optional[Iterable[str]],
    summary: str,
    bullets: Optional[Iterable[str]],
    why_it_matters: Optional[str] = None,
) -> str:
    year = date_iso[:4]
    folder = os.path.join(vault_path, articles_subdir, category, year)
    os.makedirs(folder, exist_ok=True)
    filename = f"{slugify_filename(title)}.md"
    path = os.path.join(folder, filename)
    if not os.path.exists(path):
        content = build_article_note_content(
            title=title,
            url=url,
            date_iso=date_iso,
            source=source,
            category=category,
            tags=tags,
            summary=summary,
            bullets=bullets,
            why_it_matters=why_it_matters,
        )
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
    return path


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
        for url in urls:
            print(f"[fetch] {url}")
            status, title, text = fetch_article(
                url,
                timeout=fetch_timeout,
                retries=fetch_retries,
            )
            print(f"[fetch] status={status} title={title or ''}")
            content_hash = hash_text(text) if text else None
            mark_link_processed(conn, url, status, title, content_hash)
            if fetch_rate_limit > 0:
                time.sleep(fetch_rate_limit)
        conn.commit()


def process_links(
    db_path: str,
    vault_path: str,
    articles_subdir: str,
    max_links: int,
    fetch_timeout: int,
    fetch_retries: int,
    fetch_rate_limit: float,
) -> None:
    if not vault_path:
        raise RuntimeError("Vault path is required for process-links.")
    conn = init_db(db_path)
    urls = get_unprocessed_links(conn, max_links)
    if not urls:
        print("No unprocessed links.")
        return
    today = time.strftime("%Y-%m-%d")
    for url in urls:
        print(f"[fetch] {url}")
        status, title, text = fetch_article(
            url,
            timeout=fetch_timeout,
            retries=fetch_retries,
        )
        print(f"[fetch] status={status} title={title or ''}")
        content_hash = hash_text(text) if text else None
        domain = urlsplit(url).netloc
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
        )
        return
    raise SystemExit("Unknown command")


if __name__ == "__main__":
    main()
