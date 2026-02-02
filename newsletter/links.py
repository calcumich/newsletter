import re
import sys
import time
from typing import List, Optional, Tuple
from urllib.parse import parse_qsl, quote, urlsplit, urlunsplit

import requests

try:
    from bs4 import BeautifulSoup
except Exception:  # pragma: no cover - optional import
    BeautifulSoup = None

from newsletter.db import store_link

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
    msg,
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
