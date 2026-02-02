import json
import os
import re
from typing import List, Optional, Tuple

import requests


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
