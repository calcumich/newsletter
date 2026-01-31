import os
import sqlite3
import time
import shutil

from run import (
    GmailMessage,
    build_article_note_content,
    canonicalize_url,
    extract_and_store_links,
    extract_links,
    init_db,
    should_skip_url,
    slugify_filename,
    write_issue_note,
)


def test_canonicalize_url_strips_tracking_and_normalizes():
    url = "https://Example.com/path/?utm_source=a&ref=b&x=1"
    canon = canonicalize_url(url)
    assert canon == "https://example.com/path?x=1"


def test_should_skip_url_rules():
    assert should_skip_url("mailto:test@example.com") is True
    assert should_skip_url("https://example.com/unsubscribe") is True
    assert should_skip_url("https://twitter.com/somepath") is True
    assert should_skip_url("https://example.com/image.png") is True
    assert should_skip_url("https://example.com/article") is False


def test_extract_links_html_and_text():
    html = '<a href="https://example.com/a">Alpha</a>'
    text = "See https://example.com/b for more."
    links = extract_links(html, text)
    assert ("https://example.com/a", "Alpha") in links
    assert ("https://example.com/b", None) in links


def test_slugify_filename_basic():
    assert slugify_filename("Hello, World!") == "Hello World"
    assert slugify_filename("") == "Newsletter"


def test_extract_and_store_links_dedupes_and_inserts():
    conn = init_db(":memory:")
    msg = GmailMessage(
        message_id="msg-1",
        internal_date=int(time.time() * 1000),
        subject="Test",
        from_email="sender@example.com",
        label_ids="[]",
        html='<a href="https://example.com/a?utm_source=x">A</a>'
        '<a href="https://example.com/a">A2</a>',
        text=None,
    )
    links = extract_and_store_links(conn, msg)
    assert links == [("https://example.com/a", "A")]
    rows = conn.execute("SELECT url_canonical FROM links").fetchall()
    assert rows == [("https://example.com/a",)]


def test_write_issue_note_creates_file_and_contents():
    msg = GmailMessage(
        message_id="msg-2",
        internal_date=1700000000000,  # fixed timestamp
        subject="Weekly Update",
        from_email="sender@example.com",
        label_ids="[]",
        html=None,
        text=None,
    )
    links = [("https://example.com/a", "Alpha")]
    base = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".test_tmp"))
    try:
        os.makedirs(base, exist_ok=True)
        vault = os.path.join(base, "vault")
        path = write_issue_note(vault, os.path.join("Newsletters", "Issues"), msg, links)
        assert path is not None
        assert os.path.exists(path)
        content = []
        for root, _dirs, files in os.walk(os.path.join(vault, "Newsletters", "Issues")):
            for name in files:
                if name.endswith(".md"):
                    content.append(os.path.join(root, name))
        assert content
        text = open(content[0], "r", encoding="utf-8").read()
        assert "type: newsletter-issue" in text
        assert "- [Alpha](https://example.com/a)" in text
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_build_article_note_content_is_deterministic():
    content = build_article_note_content(
        title="Test Article",
        url="https://example.com/a",
        date_iso="2026-01-30",
        source="example.com",
        category="Dev Tools",
        tags=["beta", "alpha", "alpha"],
        summary="Short summary.",
        bullets=["Second", "First"],
        why_it_matters="Because it is useful.",
    )
    assert 'type: article' in content
    assert 'title: "Test Article"' in content
    assert 'url: "https://example.com/a"' in content
    assert "tags: [\"alpha\", \"beta\"]" in content
    assert content.index("- Second") < content.index("- First")
    assert "# Why it matters" in content
