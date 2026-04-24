import os
import json
import sqlite3
import time
import shutil

from newsletter.db import ensure_link_columns, get_links_for_refresh, init_db, mark_link_processed
from newsletter.links import (
    canonicalize_url,
    extract_and_store_links,
    extract_links,
    resolve_redirect_url,
    should_skip_url,
)
from newsletter.obsidian import (
    build_article_note_content,
    make_obsidian_link,
    normalize_tags,
    slugify_filename,
    update_issue_note_with_article_link,
    write_article_note,
    write_issue_note,
)
from newsletter.fetch import extract_main_text, fetch_article, fetch_article_detailed
from newsletter.summarize import (
    extract_output_text,
    normalize_summary_output,
    summarize_text,
    summarize_text_stub,
)
from newsletter.gmail import GmailMessage
from newsletter.cli import backfill_redirects, main, process_links, refresh_links
from observability.log_report import main as log_report_main, run as log_report_run
from observability.logs import filter_events, load_events
from observability.log_stats import (
    error_class_breakdown,
    latency_summary,
    llm_mode_breakdown,
    success_rate,
    top_failing_domains,
    trend_by_day,
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
    links = extract_and_store_links(conn, msg, resolve_redirects=False)
    assert links == [("https://example.com/a", "A")]
    rows = conn.execute("SELECT url_canonical, original_url FROM links").fetchall()
    assert rows == [("https://example.com/a", "https://example.com/a?utm_source=x")]


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


def test_make_obsidian_link():
    link = make_obsidian_link(
        "C:\\vault\\Newsletters\\Articles\\Other\\2026\\Article.md",
        "C:\\vault",
        "Title",
    )
    assert link == "[[Newsletters/Articles/Other/2026/Article|Title]]"


def test_update_issue_note_with_article_link():
    base = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".test_tmp"))
    try:
        os.makedirs(base, exist_ok=True)
        vault = os.path.join(base, "vault")
        issue_path = os.path.join(vault, "Newsletters", "Issues", "2026", "01")
        os.makedirs(issue_path, exist_ok=True)
        issue_file = os.path.join(issue_path, "issue.md")
        with open(issue_file, "w", encoding="utf-8") as f:
            f.write("# Links\n")
        article_path = os.path.join(
            vault, "Newsletters", "Articles", "Other", "2026", "article.md"
        )
        os.makedirs(os.path.dirname(article_path), exist_ok=True)
        with open(article_path, "w", encoding="utf-8") as f:
            f.write("content")
        update_issue_note_with_article_link(
            issue_file,
            article_path,
            vault,
            "Article Title",
        )
        content = open(issue_file, "r", encoding="utf-8").read()
        assert "## Articles" in content
        assert "Article Title" in content
        update_issue_note_with_article_link(
            issue_file,
            article_path,
            vault,
            "Article Title",
        )
        content_again = open(issue_file, "r", encoding="utf-8").read()
        assert content_again.count("Article Title") == 1
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_update_issue_note_replaces_external_markdown_link():
    base = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".test_tmp"))
    try:
        os.makedirs(base, exist_ok=True)
        vault = os.path.join(base, "vault")
        issue_path = os.path.join(vault, "Newsletters", "Issues", "2026", "01")
        os.makedirs(issue_path, exist_ok=True)
        issue_file = os.path.join(issue_path, "issue.md")
        with open(issue_file, "w", encoding="utf-8") as f:
            f.write("# Links\n\n- [Alpha](https://example.com/a)\n")
        article_path = os.path.join(
            vault, "Newsletters", "Articles", "Other", "2026", "article.md"
        )
        os.makedirs(os.path.dirname(article_path), exist_ok=True)
        with open(article_path, "w", encoding="utf-8") as f:
            f.write("content")
        update_issue_note_with_article_link(
            issue_file,
            article_path,
            vault,
            "Article Title",
            source_url="https://example.com/a",
        )
        content = open(issue_file, "r", encoding="utf-8").read()
        assert "[Alpha](https://example.com/a)" not in content
        assert "[[Newsletters/Articles/Other/2026/article|Alpha]]" in content
        assert "## Articles" not in content
    finally:
        shutil.rmtree(base, ignore_errors=True)


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
    assert "original_url" in cols


def test_summarize_text_stub():
    text = "First sentence. Second sentence? Third sentence!"
    summary, bullets = summarize_text_stub(text, max_sentences=2)
    assert summary == "First sentence. Second sentence?"
    assert bullets[0] == "First sentence."
    assert bullets[1] == "Second sentence?"


def test_extract_output_text():
    response = {
        "output": [
            {
                "type": "message",
                "content": [
                    {"type": "output_text", "text": "{\"summary\":\"x\"}"},
                ],
            }
        ]
    }
    assert extract_output_text(response) == '{"summary":"x"}'


def test_summarize_text_falls_back_without_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    result = summarize_text(
        "First sentence. Second sentence.",
        title="Title",
        url="https://example.com",
        domain="example.com",
    )
    assert result["summary"]
    assert result["category"] == "Other"
    assert "needs-review" in result["tags"]
    assert result["_meta"]["llm_mode"] == "stub"
    assert result["_meta"]["fallback_used"] is True


def test_normalize_summary_output_validates_category_and_tags():
    result = normalize_summary_output(
        {
            "summary": "  Summary. ",
            "bullets": [" One ", "", "Two"],
            "category": "NotARealCategory",
            "tags": ["AI", "ai", " ", "news"],
            "confidence": 0.9,
            "paywall_or_blocked": False,
        }
    )
    assert result["summary"] == "Summary."
    assert result["bullets"] == ["One", "Two"]
    assert result["category"] == "Other"
    assert result["tags"] == ["AI", "news", "needs-review"]


def test_normalize_summary_output_low_confidence_marks_review():
    result = normalize_summary_output(
        {
            "summary": "Summary.",
            "bullets": ["One"],
            "category": "Dev Tools",
            "tags": ["automation"],
            "confidence": 0.2,
            "paywall_or_blocked": False,
        }
    )
    assert result["category"] == "Dev Tools"
    assert "needs-review" in result["tags"]


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


def test_get_links_for_refresh_filters_by_age_and_status():
    conn = init_db(":memory:")
    now = int(time.time())
    old = now - 40 * 86400
    recent = now - 5 * 86400
    conn.execute(
        """
        INSERT INTO links (url_canonical, first_seen_message_id, domain, discovered_at, processed_at, fetch_status)
        VALUES ('https://example.com/old-ok', 'msg', 'example.com', 1, ?, 'ok')
        """,
        (old,),
    )
    conn.execute(
        """
        INSERT INTO links (url_canonical, first_seen_message_id, domain, discovered_at, processed_at, fetch_status)
        VALUES ('https://example.com/recent-ok', 'msg', 'example.com', 1, ?, 'ok')
        """,
        (recent,),
    )
    conn.execute(
        """
        INSERT INTO links (url_canonical, first_seen_message_id, domain, discovered_at, processed_at, fetch_status)
        VALUES ('https://example.com/old-fail', 'msg', 'example.com', 1, ?, 'fail')
        """,
        (old,),
    )
    conn.execute(
        """
        INSERT INTO links (url_canonical, first_seen_message_id, domain, discovered_at)
        VALUES ('https://example.com/unprocessed', 'msg', 'example.com', 1)
        """
    )
    conn.commit()
    urls = get_links_for_refresh(
        conn,
        limit=10,
        older_than_days=30,
        statuses=["ok"],
    )
    assert urls == ["https://example.com/old-ok"]


def test_get_links_for_refresh_filters_by_domain_and_category():
    conn = init_db(":memory:")
    old = int(time.time()) - 40 * 86400
    conn.execute(
        """
        INSERT INTO links (url_canonical, first_seen_message_id, domain, category, discovered_at, processed_at, fetch_status)
        VALUES ('https://a.com/one', 'msg', 'a.com', 'Dev Tools', 1, ?, 'ok')
        """,
        (old,),
    )
    conn.execute(
        """
        INSERT INTO links (url_canonical, first_seen_message_id, domain, category, discovered_at, processed_at, fetch_status)
        VALUES ('https://b.com/two', 'msg', 'b.com', 'Security', 1, ?, 'ok')
        """,
        (old,),
    )
    conn.commit()
    urls = get_links_for_refresh(
        conn,
        limit=10,
        older_than_days=30,
        domains=["A.COM"],
        categories=["dev tools"],
    )
    assert urls == ["https://a.com/one"]


def test_get_links_for_refresh_status_mode_failed_only():
    conn = init_db(":memory:")
    old = int(time.time()) - 40 * 86400
    conn.execute(
        """
        INSERT INTO links (url_canonical, first_seen_message_id, domain, discovered_at, processed_at, fetch_status)
        VALUES ('https://example.com/ok', 'msg', 'example.com', 1, ?, 'ok')
        """,
        (old,),
    )
    conn.execute(
        """
        INSERT INTO links (url_canonical, first_seen_message_id, domain, discovered_at, processed_at, fetch_status)
        VALUES ('https://example.com/fail', 'msg', 'example.com', 1, ?, 'fail')
        """,
        (old,),
    )
    conn.commit()
    urls = get_links_for_refresh(
        conn,
        limit=10,
        older_than_days=30,
        status_mode="failed_only",
    )
    assert urls == ["https://example.com/fail"]


def test_get_links_for_refresh_status_mode_ok_only():
    conn = init_db(":memory:")
    old = int(time.time()) - 40 * 86400
    conn.execute(
        """
        INSERT INTO links (url_canonical, first_seen_message_id, domain, discovered_at, processed_at, fetch_status)
        VALUES ('https://example.com/ok', 'msg', 'example.com', 1, ?, 'ok')
        """,
        (old,),
    )
    conn.execute(
        """
        INSERT INTO links (url_canonical, first_seen_message_id, domain, discovered_at, processed_at, fetch_status)
        VALUES ('https://example.com/fail', 'msg', 'example.com', 1, ?, 'fail')
        """,
        (old,),
    )
    conn.commit()
    urls = get_links_for_refresh(
        conn,
        limit=10,
        older_than_days=30,
        status_mode="ok_only",
    )
    assert urls == ["https://example.com/ok"]


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

        def fake_fetch_article(_url, **_kwargs):
            return "ok", "Example Title", "First sentence. Second sentence.", {
                "error_class": None,
                "http_status": None,
                "retry_count": 0,
            }

        monkeypatch.setattr("newsletter.cli.fetch_article_detailed", fake_fetch_article)
        process_links(
            db_path=db_path,
            vault_path=vault,
            articles_subdir=os.path.join("Newsletters", "Articles"),
            max_links=10,
            dry_run=False,
            log_jsonl="",
            fetch_timeout=5,
            fetch_retries=0,
            fetch_rate_limit=0.0,
            fetch_summary_json="",
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


def test_process_links_dry_run_does_not_fetch(monkeypatch):
    base = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".test_tmp"))
    try:
        os.makedirs(base, exist_ok=True)
        db_path = os.path.join(base, "process_dry_run.db")
        conn = init_db(db_path)
        conn.execute(
            """
            INSERT INTO links (url_canonical, first_seen_message_id, domain, discovered_at)
            VALUES ('https://example.com/a', 'msg', 'example.com', 1)
            """
        )
        conn.commit()

        def fake_fetch_article(_url, **_kwargs):
            raise AssertionError("fetch_article should not be called in dry-run mode")

        monkeypatch.setattr("newsletter.cli.fetch_article_detailed", fake_fetch_article)
        process_links(
            db_path=db_path,
            vault_path="",
            articles_subdir=os.path.join("Newsletters", "Articles"),
            max_links=10,
            dry_run=True,
            log_jsonl="",
            fetch_timeout=5,
            fetch_retries=0,
            fetch_rate_limit=0.0,
            fetch_summary_json="",
        )
        row = conn.execute(
            "SELECT processed_at, summary, note_path FROM links WHERE url_canonical = ?",
            ("https://example.com/a",),
        ).fetchone()
        assert row == (None, None, None)
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_process_links_writes_jsonl_events(monkeypatch):
    base = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".test_tmp"))
    try:
        os.makedirs(base, exist_ok=True)
        vault = os.path.join(base, "vault")
        db_path = os.path.join(base, "process_jsonl.db")
        log_path = os.path.join(base, "run.jsonl")
        conn = init_db(db_path)
        conn.execute(
            """
            INSERT INTO links (url_canonical, first_seen_message_id, domain, discovered_at)
            VALUES ('https://example.com/a', 'msg', 'example.com', 1)
            """
        )
        conn.commit()

        def fake_fetch_article(_url, **_kwargs):
            return "ok", "Logged Title", "First sentence. Second sentence.", {
                "error_class": None,
                "http_status": None,
                "retry_count": 0,
            }

        monkeypatch.setattr("newsletter.cli.fetch_article_detailed", fake_fetch_article)
        process_links(
            db_path=db_path,
            vault_path=vault,
            articles_subdir=os.path.join("Newsletters", "Articles"),
            max_links=10,
            dry_run=False,
            log_jsonl=log_path,
            fetch_timeout=5,
            fetch_retries=0,
            fetch_rate_limit=0.0,
            fetch_summary_json="",
        )
        with open(log_path, "r", encoding="utf-8") as f:
            events = [json.loads(line) for line in f if line.strip()]
        event_names = [e["event"] for e in events]
        assert event_names[0] == "run_start"
        assert "url_processed" in event_names
        assert event_names[-1] == "run_summary"
        processed = [e for e in events if e["event"] == "url_processed"][0]
        assert processed["command"] == "process-links"
        assert processed["url"] == "https://example.com/a"
        assert processed["status"] == "ok"
        assert processed["error_class"] is None
        assert processed["http_status"] is None
        assert processed["retry_count"] == 0
        assert processed["note_path"]
        assert processed["llm_mode"] in {"openai", "stub"}
        assert processed["fallback_used"] in {True, False}
        assert processed["model"]
        assert processed["prompt_version"]
        assert isinstance(processed["llm_latency_ms"], int)
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_refresh_links_reprocesses_old_items(monkeypatch):
    base = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".test_tmp"))
    try:
        os.makedirs(base, exist_ok=True)
        vault = os.path.join(base, "vault")
        db_path = os.path.join(base, "refresh.db")
        conn = init_db(db_path)
        old = int(time.time()) - 40 * 86400
        conn.execute(
            """
            INSERT INTO links (url_canonical, first_seen_message_id, domain, discovered_at, processed_at, fetch_status)
            VALUES ('https://example.com/a', 'msg', 'example.com', 1, ?, 'ok')
            """,
            (old,),
        )
        conn.commit()

        def fake_fetch_article(_url, **_kwargs):
            return "ok", "Refreshed Title", "First sentence. Second sentence.", {
                "error_class": None,
                "http_status": None,
                "retry_count": 0,
            }

        monkeypatch.setattr("newsletter.cli.fetch_article_detailed", fake_fetch_article)
        refresh_links(
            db_path=db_path,
            vault_path=vault,
            articles_subdir=os.path.join("Newsletters", "Articles"),
            max_links=10,
            older_than_days=30,
            statuses=["ok"],
            domains=None,
            categories=None,
            status_mode="any",
            dry_run=False,
            log_jsonl="",
            fetch_timeout=5,
            fetch_retries=0,
            fetch_rate_limit=0.0,
            fetch_summary_json="",
        )
        row = conn.execute(
            "SELECT title, summary, note_path, processed_at FROM links WHERE url_canonical = ?",
            ("https://example.com/a",),
        ).fetchone()
        assert row[0] == "Refreshed Title"
        assert row[1]
        assert row[2]
        assert os.path.exists(row[2])
        assert row[3] > old
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_refresh_links_dry_run_does_not_fetch(monkeypatch):
    base = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".test_tmp"))
    try:
        os.makedirs(base, exist_ok=True)
        vault = os.path.join(base, "vault")
        db_path = os.path.join(base, "refresh_dry_run.db")
        conn = init_db(db_path)
        old = int(time.time()) - 40 * 86400
        conn.execute(
            """
            INSERT INTO links (url_canonical, first_seen_message_id, domain, discovered_at, processed_at, fetch_status)
            VALUES ('https://example.com/a', 'msg', 'example.com', 1, ?, 'ok')
            """,
            (old,),
        )
        conn.commit()

        def fake_fetch_article(_url, **_kwargs):
            raise AssertionError("fetch_article should not be called in dry-run mode")

        monkeypatch.setattr("newsletter.cli.fetch_article_detailed", fake_fetch_article)
        refresh_links(
            db_path=db_path,
            vault_path=vault,
            articles_subdir=os.path.join("Newsletters", "Articles"),
            max_links=10,
            older_than_days=30,
            statuses=["ok"],
            domains=None,
            categories=None,
            status_mode="any",
            dry_run=True,
            log_jsonl="",
            fetch_timeout=5,
            fetch_retries=0,
            fetch_rate_limit=0.0,
            fetch_summary_json="",
        )
        row = conn.execute(
            "SELECT title, summary, note_path, processed_at FROM links WHERE url_canonical = ?",
            ("https://example.com/a",),
        ).fetchone()
        assert row[0] is None
        assert row[1] is None
        assert row[2] is None
        assert row[3] == old
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_refresh_rejects_statuses_with_preset(monkeypatch):
    monkeypatch.setattr(
        "os.sys.argv",
        [
            "run.py",
            "refresh",
            "--statuses",
            "ok",
            "--failed-only",
        ],
    )
    try:
        main(beautiful_soup_available=True)
        assert False, "Expected SystemExit"
    except SystemExit as exc:
        assert "Do not combine --statuses" in str(exc)


def test_resolve_redirect_url_returns_final(monkeypatch):
    class FakeResponse:
        status_code = 200
        url = "https://final.example.com/post"

    def fake_head(_url, **_kwargs):
        return FakeResponse()

    import requests

    monkeypatch.setattr(requests, "head", fake_head)
    resolved = resolve_redirect_url("https://tldrtracking.example.com/abc")
    assert resolved == "https://final.example.com/post"


def test_backfill_redirects_updates_canonical(monkeypatch):
    base = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".test_tmp"))
    try:
        os.makedirs(base, exist_ok=True)
        db_path = os.path.join(base, "backfill.db")
        conn = init_db(db_path)
        conn.execute(
            """
            INSERT INTO links (url_canonical, first_seen_message_id, domain, discovered_at)
            VALUES ('https://tldrtracking.example.com/abc', 'msg', 'tldrtracking.example.com', 1)
            """
        )
        conn.commit()

        def fake_resolve(url, **_kwargs):
            assert url == "https://tldrtracking.example.com/abc"
            return "https://final.example.com/post?utm_source=x"

        monkeypatch.setattr("newsletter.cli.resolve_redirect_url", fake_resolve)
        backfill_redirects(
            db_path=db_path,
            max_links=10,
            redirect_timeout=1,
            redirect_retries=0,
            redirect_rate_limit=0.0,
        )
        row = conn.execute(
            "SELECT url_canonical, domain, original_url FROM links"
        ).fetchone()
        assert row == (
            "https://final.example.com/post",
            "final.example.com",
            "https://tldrtracking.example.com/abc",
        )
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_fetch_article_retries_then_succeeds(monkeypatch):
    calls = {"count": 0}

    class FakeResponse:
        status_code = 200
        headers = {"content-type": "text/html; charset=utf-8"}
        text = "<html><head><title>Ok</title></head><body>Hi</body></html>"
        url = "https://example.com/final"

    def fake_get(_url, **_kwargs):
        calls["count"] += 1
        if calls["count"] < 3:
            raise requests.RequestException("boom")
        return FakeResponse()

    import requests

    monkeypatch.setattr(requests, "get", fake_get)
    status, title, text = fetch_article(
        "https://example.com/start",
        timeout=1,
        retries=2,
        backoff_base=0,
    )
    assert calls["count"] == 3
    assert status == "ok"
    assert title == "Ok"
    assert "Hi" in (text or "")


def test_fetch_article_detailed_http_error_metadata(monkeypatch):
    class FakeResponse:
        status_code = 403
        headers = {"content-type": "text/html; charset=utf-8"}
        text = "<html></html>"
        url = "https://example.com/blocked"

    def fake_get(_url, **_kwargs):
        return FakeResponse()

    import requests

    monkeypatch.setattr(requests, "get", fake_get)
    status, title, text, meta = fetch_article_detailed(
        "https://example.com/blocked",
        timeout=1,
        retries=0,
        backoff_base=0,
    )
    assert status == "http_403"
    assert title is None
    assert text is None
    assert meta["error_class"] == "blocked"
    assert meta["http_status"] == 403
    assert meta["retry_count"] == 0


def test_load_events_reads_jsonl_and_tolerates_malformed_lines():
    base = os.path.join(".test_tmp", f"logs_load_{int(time.time() * 1000)}")
    os.makedirs(base, exist_ok=True)
    try:
        path = os.path.join(base, "run.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"event": "run_start", "command": "process-links"}) + "\n")
            f.write("\n")
            f.write("not json\n")
            f.write(json.dumps(["not", "a", "dict"]) + "\n")
            f.write(json.dumps({"event": "url_processed", "status": "ok"}) + "\n")
        events = load_events([path])
        assert len(events) == 2
        assert events[0]["event"] == "run_start"
        assert events[1]["event"] == "url_processed"
        assert all(ev["_source_file"] == path for ev in events)
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_load_events_expands_globs_and_missing_paths():
    base = os.path.join(".test_tmp", f"logs_glob_{int(time.time() * 1000)}")
    os.makedirs(base, exist_ok=True)
    try:
        for name in ("a.jsonl", "b.jsonl"):
            with open(os.path.join(base, name), "w", encoding="utf-8") as f:
                f.write(json.dumps({"event": "url_processed", "file": name}) + "\n")
        events = load_events([os.path.join(base, "*.jsonl"), os.path.join(base, "missing.jsonl")])
        files = sorted(ev["file"] for ev in events)
        assert files == ["a.jsonl", "b.jsonl"]
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_filter_events_by_since_days_command_and_event():
    now = 1_800_000_000.0
    one_day = 86400
    events = [
        {"event": "url_processed", "command": "process-links", "timestamp": now - 2 * one_day, "url": "fresh"},
        {"event": "url_processed", "command": "process-links", "timestamp": now - 10 * one_day, "url": "old"},
        {"event": "run_start", "command": "process-links", "timestamp": now - 1 * one_day},
        {"event": "url_processed", "command": "refresh", "timestamp": now - 1 * one_day, "url": "other_cmd"},
        {"event": "url_processed", "command": "process-links", "url": "no_timestamp"},
    ]

    recent = filter_events(events, since_days=7, now=now)
    assert {ev.get("url") for ev in recent if "url" in ev} == {"fresh", "other_cmd"}
    assert any(ev["event"] == "run_start" for ev in recent)

    only_process = filter_events(events, command="process-links")
    assert all(ev["command"] == "process-links" for ev in only_process)
    assert len(only_process) == 4

    only_url = filter_events(events, event="url_processed")
    assert all(ev["event"] == "url_processed" for ev in only_url)
    assert len(only_url) == 4

    combined = filter_events(events, since_days=7, command="process-links", event="url_processed", now=now)
    assert [ev["url"] for ev in combined] == ["fresh"]


def _make_url_event(**overrides) -> dict:
    base = {
        "event": "url_processed",
        "command": "process-links",
        "status": "ok",
        "domain": "example.com",
        "timestamp": 1_800_000_000,
    }
    base.update(overrides)
    return base


def test_success_rate_handles_empty_and_mixed():
    assert success_rate([]) == {
        "total": 0,
        "ok": 0,
        "fail": 0,
        "success_rate": 0.0,
        "by_command": {},
    }
    events = [
        _make_url_event(status="ok"),
        _make_url_event(status="ok"),
        _make_url_event(status="fail", command="refresh"),
        _make_url_event(status="http_403"),
        {"event": "run_start", "command": "process-links"},  # ignored
    ]
    result = success_rate(events)
    assert result["total"] == 4
    assert result["ok"] == 2
    assert result["fail"] == 2
    assert result["success_rate"] == 0.5
    assert result["by_command"]["process-links"]["total"] == 3
    assert result["by_command"]["process-links"]["ok"] == 2
    assert result["by_command"]["refresh"]["fail"] == 1


def test_error_class_breakdown_sorts_and_handles_missing():
    events = [
        _make_url_event(status="ok"),
        _make_url_event(status="http_403", error_class="blocked"),
        _make_url_event(status="http_403", error_class="blocked"),
        _make_url_event(status="fail", error_class="timeout"),
        _make_url_event(status="fail"),  # missing error_class
    ]
    rows = error_class_breakdown(events)
    assert rows[0]["error_class"] == "blocked"
    assert rows[0]["count"] == 2
    assert rows[0]["pct_of_failures"] == 0.5
    classes = {r["error_class"] for r in rows}
    assert "unknown" in classes
    assert "timeout" in classes


def test_top_failing_domains_excludes_clean_domains_and_limits():
    events = [
        _make_url_event(domain="a.com", status="ok"),
        _make_url_event(domain="a.com", status="ok"),
        _make_url_event(domain="b.com", status="fail", error_class="timeout"),
        _make_url_event(domain="b.com", status="fail", error_class="timeout"),
        _make_url_event(domain="b.com", status="fail", error_class="network"),
        _make_url_event(domain="c.com", status="http_403", error_class="blocked"),
    ]
    rows = top_failing_domains(events, n=10)
    domains = [r["domain"] for r in rows]
    assert "a.com" not in domains  # no failures
    assert domains[0] == "b.com"
    assert rows[0]["dominant_error_class"] == "timeout"
    assert rows[0]["fails"] == 3
    assert rows[0]["fail_rate"] == 1.0
    assert top_failing_domains(events, n=1)[0]["domain"] == "b.com"


def test_llm_mode_breakdown_tracks_fallback_and_prompt_version():
    events = [
        _make_url_event(status="ok", llm_mode="openai", fallback_used=False, prompt_version="v1"),
        _make_url_event(status="ok", llm_mode="openai", fallback_used=False, prompt_version="v1"),
        _make_url_event(status="ok", llm_mode="stub", fallback_used=True, prompt_version="v1"),
        _make_url_event(status="ok"),  # missing llm fields — pre-Milestone-B
        _make_url_event(status="fail", llm_mode="openai"),  # failures ignored
    ]
    result = llm_mode_breakdown(events)
    assert result["total"] == 4
    assert result["by_mode"]["openai"] == 2
    assert result["by_mode"]["stub"] == 1
    assert result["by_mode"]["unknown"] == 1
    assert result["fallback_used"] == 1
    assert result["by_prompt_version"]["v1"]["openai"] == 2
    assert "unknown" in result["by_prompt_version"]


def test_latency_summary_percentiles():
    events = [
        _make_url_event(llm_latency_ms=100, model="gpt-4o-mini", prompt_version="v1"),
        _make_url_event(llm_latency_ms=200, model="gpt-4o-mini", prompt_version="v1"),
        _make_url_event(llm_latency_ms=300, model="gpt-4o-mini", prompt_version="v1"),
        _make_url_event(llm_latency_ms=400, model="gpt-4o-mini", prompt_version="v1"),
        _make_url_event(llm_latency_ms=500, model="gpt-4o-mini", prompt_version="v1"),
        _make_url_event(),  # no latency — ignored
    ]
    rows = latency_summary(events)
    assert len(rows) == 1
    row = rows[0]
    assert row["count"] == 5
    assert row["p50"] == 300
    assert row["p95"] == 480
    assert row["p99"] == 496


def test_trend_by_day_buckets_by_utc_date():
    day1 = 1_800_000_000  # UTC date X
    day2 = day1 + 86400
    events = [
        _make_url_event(timestamp=day1, status="ok"),
        _make_url_event(timestamp=day1, status="fail"),
        _make_url_event(timestamp=day2, status="ok"),
        _make_url_event(timestamp=None, status="ok"),  # no timestamp, dropped
    ]
    rows = trend_by_day(events)
    assert len(rows) == 2
    assert rows[0]["date"] < rows[1]["date"]
    assert rows[0]["processed"] == 2
    assert rows[0]["ok"] == 1
    assert rows[0]["fail"] == 1
    assert rows[0]["fail_rate"] == 0.5
    assert rows[1]["processed"] == 1


def test_log_report_run_produces_text_sections(capsys):
    base = os.path.join(".test_tmp", f"report_{int(time.time() * 1000)}")
    os.makedirs(base, exist_ok=True)
    try:
        path = os.path.join(base, "run.jsonl")
        events = [
            {"event": "run_start", "command": "process-links", "timestamp": 1_800_000_000},
            {
                "event": "url_processed",
                "command": "process-links",
                "status": "ok",
                "domain": "ok.com",
                "timestamp": 1_800_000_000,
                "llm_mode": "openai",
                "fallback_used": False,
                "model": "gpt-4o-mini",
                "prompt_version": "v1",
                "llm_latency_ms": 250,
            },
            {
                "event": "url_processed",
                "command": "process-links",
                "status": "http_403",
                "error_class": "blocked",
                "http_status": 403,
                "domain": "bad.com",
                "timestamp": 1_800_000_000,
            },
        ]
        with open(path, "w", encoding="utf-8") as f:
            for ev in events:
                f.write(json.dumps(ev) + "\n")

        text = log_report_run([path])
        assert "Success rate" in text
        assert "Error classes" in text
        assert "blocked" in text
        assert "bad.com" in text
        assert "Top failing domains" in text
        assert "LLM mode" in text
        assert "gpt-4o-mini" in text
        assert "Daily trend" in text

        payload = log_report_run([path], as_json=True)
        parsed = json.loads(payload)
        assert parsed["success_rate"]["total"] == 2
        assert parsed["top_failing_domains"][0]["domain"] == "bad.com"

        rc = log_report_main(["--logs", path])
        assert rc == 0
        captured = capsys.readouterr()
        assert "Success rate" in captured.out
    finally:
        shutil.rmtree(base, ignore_errors=True)
