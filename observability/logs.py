"""JSONL run-log loading and filtering for pipeline observability.

Reads event streams written by `ingest`, `process-links`, and `refresh` via
their `--log-jsonl` flag. Aggregation lives in `observability.log_stats`.
"""

from __future__ import annotations

import glob
import json
import time
from pathlib import Path
from typing import Iterable, List, Optional


def _iter_paths(paths: Iterable[str | Path]) -> List[Path]:
    resolved: List[Path] = []
    for entry in paths:
        entry_str = str(entry)
        if any(ch in entry_str for ch in "*?["):
            for match in glob.glob(entry_str, recursive=True):
                resolved.append(Path(match))
        else:
            resolved.append(Path(entry_str))
    seen = set()
    unique: List[Path] = []
    for path in resolved:
        key = str(path.resolve()) if path.exists() else str(path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def load_events(paths: Iterable[str | Path]) -> List[dict]:
    """Load events from one or more JSONL files.

    - `paths` may contain plain paths or glob patterns (`logs/*.jsonl`).
    - Malformed lines and missing files are skipped silently; each event is
      annotated with `_source_file` so callers can trace origin.
    """
    events: List[dict] = []
    for path in _iter_paths(paths):
        if not path.exists() or not path.is_file():
            continue
        source = str(path)
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                event.setdefault("_source_file", source)
                events.append(event)
    return events


def filter_events(
    events: Iterable[dict],
    *,
    since_days: Optional[int] = None,
    command: Optional[str] = None,
    event: Optional[str] = None,
    now: Optional[float] = None,
) -> List[dict]:
    """Return a subset of events matching the given filters.

    - `since_days` keeps events with `timestamp >= now - N*86400`. Events with
      no `timestamp` are dropped when this filter is active.
    - `command` / `event` match exact values against the respective fields.
    - `now` is the reference time (unix seconds); defaults to `time.time()`.
    """
    reference = now if now is not None else time.time()
    cutoff = reference - since_days * 86400 if since_days is not None else None
    result: List[dict] = []
    for ev in events:
        if cutoff is not None:
            ts = ev.get("timestamp")
            if not isinstance(ts, (int, float)) or ts < cutoff:
                continue
        if command is not None and ev.get("command") != command:
            continue
        if event is not None and ev.get("event") != event:
            continue
        result.append(ev)
    return result
