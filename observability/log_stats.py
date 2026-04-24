"""Pure aggregation functions over pipeline JSONL events.

All functions take an iterable of event dicts (as loaded by
`observability.logs.load_events`) and return plain data structures. No
pandas dependency; notebooks are free to wrap the results in DataFrames.

Events are expected to carry at minimum an `event` field. `url_processed`
events are the primary unit of analysis; older events from before
Milestone B may be missing `error_class`, `llm_mode`, etc. — missing
fields are reported as `None` / `"unknown"` rather than dropped.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Iterable, List, Optional


def _url_events(events: Iterable[dict]) -> List[dict]:
    return [ev for ev in events if ev.get("event") == "url_processed"]


def _is_ok(event: dict) -> bool:
    return event.get("status") == "ok"


def _percentile(values: List[float], pct: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    rank = (len(ordered) - 1) * (pct / 100.0)
    lo = int(rank)
    hi = min(lo + 1, len(ordered) - 1)
    frac = rank - lo
    return ordered[lo] + (ordered[hi] - ordered[lo]) * frac


def success_rate(events: Iterable[dict]) -> dict:
    """Overall and per-command ok/fail counts on url_processed events."""
    url_events = _url_events(events)
    total = len(url_events)
    ok = sum(1 for ev in url_events if _is_ok(ev))
    fail = total - ok
    by_command: dict[str, dict] = {}
    for ev in url_events:
        cmd = ev.get("command") or "unknown"
        bucket = by_command.setdefault(cmd, {"total": 0, "ok": 0, "fail": 0})
        bucket["total"] += 1
        if _is_ok(ev):
            bucket["ok"] += 1
        else:
            bucket["fail"] += 1
    for bucket in by_command.values():
        bucket["success_rate"] = bucket["ok"] / bucket["total"] if bucket["total"] else 0.0
    return {
        "total": total,
        "ok": ok,
        "fail": fail,
        "success_rate": ok / total if total else 0.0,
        "by_command": by_command,
    }


def error_class_breakdown(events: Iterable[dict]) -> List[dict]:
    """Count failed url_processed events grouped by `error_class`.

    Failures missing `error_class` are reported as `"unknown"`. Result is
    sorted by count descending.
    """
    failures = [ev for ev in _url_events(events) if not _is_ok(ev)]
    counter = Counter((ev.get("error_class") or "unknown") for ev in failures)
    total = sum(counter.values())
    rows = [
        {
            "error_class": name,
            "count": count,
            "pct_of_failures": count / total if total else 0.0,
        }
        for name, count in counter.most_common()
    ]
    return rows


def top_failing_domains(events: Iterable[dict], n: int = 10) -> List[dict]:
    """Return up to `n` domains with the most failures.

    Each row carries `processed`, `fails`, `fail_rate`, and the most common
    `error_class` seen on that domain (`dominant_error_class`).
    """
    by_domain: dict[str, dict] = {}
    for ev in _url_events(events):
        domain = ev.get("domain") or "unknown"
        bucket = by_domain.setdefault(
            domain,
            {"processed": 0, "fails": 0, "error_classes": Counter()},
        )
        bucket["processed"] += 1
        if not _is_ok(ev):
            bucket["fails"] += 1
            bucket["error_classes"][ev.get("error_class") or "unknown"] += 1
    rows = []
    for domain, bucket in by_domain.items():
        if bucket["fails"] == 0:
            continue
        dominant = bucket["error_classes"].most_common(1)[0][0]
        rows.append(
            {
                "domain": domain,
                "processed": bucket["processed"],
                "fails": bucket["fails"],
                "fail_rate": bucket["fails"] / bucket["processed"],
                "dominant_error_class": dominant,
            }
        )
    rows.sort(key=lambda r: (-r["fails"], r["domain"]))
    return rows[:n]


def llm_mode_breakdown(events: Iterable[dict]) -> dict:
    """Counts of LLM mode usage across successful url_processed events.

    Also breaks down by `prompt_version` so prompt/model version changes
    can be tracked across time. Events missing `llm_mode` are counted as
    `"unknown"` (typically pre-Milestone-B runs).
    """
    ok_events = [ev for ev in _url_events(events) if _is_ok(ev)]
    by_mode: Counter = Counter()
    fallback_used = 0
    by_prompt: dict[str, Counter] = defaultdict(Counter)
    for ev in ok_events:
        mode = ev.get("llm_mode") or "unknown"
        by_mode[mode] += 1
        if ev.get("fallback_used"):
            fallback_used += 1
        prompt_version = ev.get("prompt_version") or "unknown"
        by_prompt[prompt_version][mode] += 1
    return {
        "total": len(ok_events),
        "by_mode": dict(by_mode),
        "fallback_used": fallback_used,
        "by_prompt_version": {pv: dict(counts) for pv, counts in by_prompt.items()},
    }


def latency_summary(events: Iterable[dict]) -> List[dict]:
    """p50/p95/p99 of `llm_latency_ms`, grouped by (model, prompt_version)."""
    groups: dict[tuple, list[float]] = defaultdict(list)
    for ev in _url_events(events):
        latency = ev.get("llm_latency_ms")
        if not isinstance(latency, (int, float)):
            continue
        key = (ev.get("model") or "unknown", ev.get("prompt_version") or "unknown")
        groups[key].append(float(latency))
    rows = []
    for (model, prompt_version), values in groups.items():
        rows.append(
            {
                "model": model,
                "prompt_version": prompt_version,
                "count": len(values),
                "p50": _percentile(values, 50),
                "p95": _percentile(values, 95),
                "p99": _percentile(values, 99),
            }
        )
    rows.sort(key=lambda r: (-r["count"], r["model"], r["prompt_version"]))
    return rows


def trend_by_day(events: Iterable[dict]) -> List[dict]:
    """Per-day processed/ok/fail counts, sorted oldest to newest.

    Days are UTC `YYYY-MM-DD`. Events with no `timestamp` are skipped.
    """
    by_day: dict[str, dict] = {}
    for ev in _url_events(events):
        ts = ev.get("timestamp")
        if not isinstance(ts, (int, float)):
            continue
        day = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        bucket = by_day.setdefault(day, {"date": day, "processed": 0, "ok": 0, "fail": 0})
        bucket["processed"] += 1
        if _is_ok(ev):
            bucket["ok"] += 1
        else:
            bucket["fail"] += 1
    rows = list(by_day.values())
    for row in rows:
        row["fail_rate"] = row["fail"] / row["processed"] if row["processed"] else 0.0
    rows.sort(key=lambda r: r["date"])
    return rows
