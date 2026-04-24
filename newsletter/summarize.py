import json
import os
import re
import time
from typing import Iterable, List, Optional, Tuple

import requests

CATEGORY_SET = [
    "Backend",
    "Databases",
    "Distributed Systems",
    "Security",
    "ML Systems",
    "Programming Languages",
    "Dev Tools",
    "Product/Startups",
    "Other",
]
PROMPT_VERSION = "v1"


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


def _normalize_tags(tags: Optional[Iterable[object]]) -> List[str]:
    if not tags:
        return []
    cleaned: List[str] = []
    seen = set()
    for raw in tags:
        if not isinstance(raw, str):
            continue
        tag = raw.strip()
        if not tag:
            continue
        key = tag.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(tag)
    return cleaned


def normalize_summary_output(result: Optional[dict], *, fallback_text: str = "") -> dict:
    if not result:
        result = {}

    summary_raw = result.get("summary")
    summary = summary_raw.strip() if isinstance(summary_raw, str) else ""
    if not summary and fallback_text:
        summary, _ = summarize_text_stub(fallback_text)

    bullets_raw = result.get("bullets")
    bullets: List[str] = []
    if isinstance(bullets_raw, list):
        for item in bullets_raw:
            if isinstance(item, str):
                line = item.strip()
                if line:
                    bullets.append(line)
    if not bullets and fallback_text:
        _, bullets = summarize_text_stub(fallback_text)

    category_raw = result.get("category")
    category = category_raw.strip() if isinstance(category_raw, str) else ""
    category_lookup = {c.lower(): c for c in CATEGORY_SET}
    normalized_category = category_lookup.get(category.lower(), "Other")

    confidence_raw = result.get("confidence")
    try:
        confidence = float(confidence_raw)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    paywall_or_blocked = bool(result.get("paywall_or_blocked", False))
    tags = _normalize_tags(result.get("tags"))

    needs_review = normalized_category == "Other" and category.lower() not in {"", "other"}
    if confidence < 0.5:
        needs_review = True
    if needs_review and "needs-review" not in {tag.lower() for tag in tags}:
        tags.append("needs-review")

    return {
        "summary": summary,
        "bullets": bullets[:6],
        "category": normalized_category,
        "tags": tags[:8],
        "confidence": confidence,
        "paywall_or_blocked": paywall_or_blocked,
    }


def summarize_text(
    text: str,
    *,
    title: Optional[str],
    url: str,
    domain: str,
) -> dict:
    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    started = time.time()
    result = summarize_text_openai(text, title=title, url=url, domain=domain, model=model)
    if result:
        normalized = normalize_summary_output(result, fallback_text=text)
        normalized["_meta"] = {
            "llm_mode": "openai",
            "fallback_used": False,
            "model": model,
            "prompt_version": PROMPT_VERSION,
            "llm_latency_ms": int((time.time() - started) * 1000),
        }
        return normalized
    summary, bullets = summarize_text_stub(text)
    fallback = {
        "summary": summary,
        "bullets": bullets,
        "category": "Other",
        "tags": [],
        "confidence": 0.3,
        "paywall_or_blocked": False,
    }
    normalized = normalize_summary_output(fallback, fallback_text=text)
    normalized["_meta"] = {
        "llm_mode": "stub",
        "fallback_used": True,
        "model": model,
        "prompt_version": PROMPT_VERSION,
        "llm_latency_ms": int((time.time() - started) * 1000),
    }
    return normalized
