import os
import re
import time
from typing import Iterable, List, Optional
from urllib.parse import urlsplit


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
    if why_it_matters:
        lines.extend(["", "# Why it matters", why_it_matters.strip()])
    return "\n".join(lines).strip() + "\n"


def slugify_filename(text: str) -> str:
    safe = re.sub(r"[^\w\s.-]", "", text, flags=re.UNICODE)
    safe = re.sub(r"\s+", " ", safe).strip()
    if not safe:
        safe = "Newsletter"
    return safe[:120]


def write_issue_note(
    vault_path: str,
    issues_subdir: str,
    msg,
    links: List[tuple[str, Optional[str]]],
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
    domain_map: dict[str, List[tuple[str, Optional[str]]]] = {}
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
