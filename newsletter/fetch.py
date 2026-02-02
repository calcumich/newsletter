import sys
import time
from typing import Optional, Tuple

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
