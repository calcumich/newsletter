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
    extract_main_text,
    init_db,
    mark_link_processed,
    normalize_tags,
    process_links,
    ensure_link_columns,
    should_skip_url,
    slugify_filename,
    summarize_text_stub,
    write_article_note,
    write_issue_note,
)


def test_canonicalize_url_strips_tracking_and_normalizes():
    url = "https://Example.com/path/?utm_source=a&ref=b&x=1"
    canon = canonicalize_url(url)
    assert canon == "https://example.com/path?x=1"


def test_canonicalize_url_edge_cases():
    assert canonicalize_url("https://example.com/path/") == "https://example.com/path"
    assert canonicalize_url("https://example.com:443/path") == "https://example.com/path"
    assert canonicalize_url("http://example.com:80/path") == "http://example.com/path"
    canon = canonicalize_url("https://example.com/path?b=2&a=1")
    assert canon == "https://example.com/path?a=1&b=2"


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


def test_normalize_tags_edge_cases():
    assert normalize_tags(None) == []
    assert normalize_tags(["", "  ", "alpha", "Alpha", "alpha"]) == ["Alpha", "alpha"]


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


def test_write_issue_note_groups_by_domain():
    msg = GmailMessage(
        message_id="msg-3",
        internal_date=1700000000000,
        subject="Domain Test",
        from_email="sender@example.com",
        label_ids="[]",
        html=None,
        text=None,
    )
    links = [
        ("https://b.com/x", "B"),
        ("https://a.com/y", "A"),
    ]
    base = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".test_tmp"))
    try:
        os.makedirs(base, exist_ok=True)
        vault = os.path.join(base, "vault")
        path = write_issue_note(vault, os.path.join("Newsletters", "Issues"), msg, links)
        assert path is not None
        content = open(path, "r", encoding="utf-8").read()
        assert "## a.com" in content
        assert "## b.com" in content
        assert content.index("## a.com") < content.index("## b.com")
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


def test_build_article_note_content_no_optional_sections():
    content = build_article_note_content(
        title="Test Article",
        url="https://example.com/a",
        date_iso="2026-01-30",
        source="example.com",
        category="Dev Tools",
        tags=[],
        summary="Short summary.",
        bullets=[],
        why_it_matters=None,
    )
    assert "# Key takeaways" in content
    assert "\n-\n" in content
    assert "# Why it matters" not in content


def test_ensure_link_columns_idempotent():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE links (
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
    ensure_link_columns(conn)
    ensure_link_columns(conn)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(links)").fetchall()}
    assert "summary" in cols
    assert "category" in cols
    assert "tags" in cols
    assert "note_path" in cols


def test_summarize_text_stub():
    text = "First sentence. Second sentence? Third sentence!"
    summary, bullets = summarize_text_stub(text, max_sentences=2)
    assert summary == "First sentence. Second sentence?"
    assert bullets[0] == "First sentence."
    assert bullets[1] == "Second sentence?"


def test_extract_main_text_basic():
    html = "<html><head><title>My Title</title></head><body><p>Hello world.</p></body></html>"
    title, text = extract_main_text(html)
    assert title == "My Title"
    assert "Hello world." in (text or "")


def test_write_article_note_creates_file():
    base = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".test_tmp"))
    try:
        os.makedirs(base, exist_ok=True)
        vault = os.path.join(base, "vault")
        path = write_article_note(
            vault,
            os.path.join("Newsletters", "Articles"),
            title="A Test Article",
            url="https://example.com/a",
            date_iso="2026-01-30",
            source="example.com",
            category="Other",
            tags=["alpha", "beta"],
            summary="Short summary.",
            bullets=["One", "Two"],
            why_it_matters=None,
        )
        assert os.path.exists(path)
        content = open(path, "r", encoding="utf-8").read()
        assert 'type: article' in content
        assert 'title: "A Test Article"' in content
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_mark_link_processed_updates_optional_fields():
    conn = init_db(":memory:")
    conn.execute(
        """
        INSERT INTO links (url_canonical, first_seen_message_id, domain, discovered_at)
        VALUES ('https://example.com/a', 'msg', 'example.com', 1)
        """
    )
    mark_link_processed(
        conn,
        "https://example.com/a",
        "ok",
        "Title",
        "hash",
        summary="Summary",
        category="Other",
        tags=["b", "a"],
        note_path="vault/path.md",
    )
    row = conn.execute(
        "SELECT summary, category, tags, note_path FROM links WHERE url_canonical = ?",
        ("https://example.com/a",),
    ).fetchone()
    assert row == ("Summary", "Other", '["a", "b"]', "vault/path.md")


def test_process_links_writes_notes_and_updates_db(monkeypatch):
    base = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".test_tmp"))
    try:
        os.makedirs(base, exist_ok=True)
        vault = os.path.join(base, "vault")
        db_path = os.path.join(base, "test.db")
        conn = init_db(db_path)
        conn.execute(
            """
            INSERT INTO links (url_canonical, first_seen_message_id, domain, discovered_at)
            VALUES ('https://example.com/a', 'msg', 'example.com', 1)
            """
        )
        conn.commit()

        def fake_fetch_article(_url):
            return "ok", "Example Title", "First sentence. Second sentence."

        monkeypatch.setattr("run.fetch_article", fake_fetch_article)
        process_links(
            db_path=db_path,
            vault_path=vault,
            articles_subdir=os.path.join("Newsletters", "Articles"),
            max_links=10,
        )
        row = conn.execute(
            "SELECT fetch_status, summary, note_path FROM links WHERE url_canonical = ?",
            ("https://example.com/a",),
        ).fetchone()
        assert row[0] == "ok"
        assert row[1]
        assert row[2]
        assert os.path.exists(row[2])
    finally:
        shutil.rmtree(base, ignore_errors=True)
