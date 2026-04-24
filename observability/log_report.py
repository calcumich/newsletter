"""Text report over pipeline JSONL logs.

Usage:
    python -m observability.log_report --logs "logs/*.jsonl" --since-days 7
    python -m observability.log_report --logs logs/process.jsonl --json

Answers "what broke this week?" without launching a notebook. Uses the
aggregations in `observability.log_stats`.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Iterable, List, Optional

from observability.log_stats import (
    error_class_breakdown,
    latency_summary,
    llm_mode_breakdown,
    success_rate,
    top_failing_domains,
    trend_by_day,
)
from observability.logs import filter_events, load_events


def build_report(events: List[dict], *, top_domains: int = 10) -> dict:
    return {
        "success_rate": success_rate(events),
        "error_class_breakdown": error_class_breakdown(events),
        "top_failing_domains": top_failing_domains(events, n=top_domains),
        "llm_mode_breakdown": llm_mode_breakdown(events),
        "latency_summary": latency_summary(events),
        "trend_by_day": trend_by_day(events),
    }


def _fmt_pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _fmt_ms(value: Optional[float]) -> str:
    return "-" if value is None else f"{value:.0f}ms"


def format_report(report: dict, *, header: Optional[str] = None) -> str:
    lines: list[str] = []
    if header:
        lines.append(header)
        lines.append("")

    overall = report["success_rate"]
    lines.append("## Success rate")
    lines.append(
        f"  total={overall['total']}  ok={overall['ok']}  fail={overall['fail']}  "
        f"success={_fmt_pct(overall['success_rate'])}"
    )
    for cmd, bucket in sorted(overall["by_command"].items()):
        lines.append(
            f"    {cmd}: total={bucket['total']}  ok={bucket['ok']}  "
            f"fail={bucket['fail']}  success={_fmt_pct(bucket['success_rate'])}"
        )
    lines.append("")

    lines.append("## Error classes")
    error_rows = report["error_class_breakdown"]
    if not error_rows:
        lines.append("  (no failures)")
    else:
        for row in error_rows:
            lines.append(
                f"  {row['error_class']:<14} count={row['count']:<5} "
                f"({_fmt_pct(row['pct_of_failures'])} of failures)"
            )
    lines.append("")

    lines.append("## Top failing domains")
    domain_rows = report["top_failing_domains"]
    if not domain_rows:
        lines.append("  (no failures)")
    else:
        for row in domain_rows:
            lines.append(
                f"  {row['domain']:<40} fails={row['fails']:<4} "
                f"of {row['processed']:<4} ({_fmt_pct(row['fail_rate'])}) "
                f"dominant={row['dominant_error_class']}"
            )
    lines.append("")

    llm = report["llm_mode_breakdown"]
    lines.append("## LLM mode")
    lines.append(f"  successful summaries total={llm['total']}  fallback_used={llm['fallback_used']}")
    for mode, count in sorted(llm["by_mode"].items(), key=lambda kv: -kv[1]):
        lines.append(f"    {mode}: {count}")
    if llm["by_prompt_version"]:
        lines.append("  by prompt_version:")
        for pv, modes in sorted(llm["by_prompt_version"].items()):
            mode_str = ", ".join(f"{m}={c}" for m, c in sorted(modes.items()))
            lines.append(f"    {pv}: {mode_str}")
    lines.append("")

    lines.append("## Latency (llm_latency_ms)")
    latency_rows = report["latency_summary"]
    if not latency_rows:
        lines.append("  (no latency data)")
    else:
        for row in latency_rows:
            lines.append(
                f"  model={row['model']} prompt={row['prompt_version']} "
                f"n={row['count']}  p50={_fmt_ms(row['p50'])} "
                f"p95={_fmt_ms(row['p95'])} p99={_fmt_ms(row['p99'])}"
            )
    lines.append("")

    lines.append("## Daily trend")
    trend_rows = report["trend_by_day"]
    if not trend_rows:
        lines.append("  (no dated events)")
    else:
        for row in trend_rows:
            lines.append(
                f"  {row['date']}  processed={row['processed']:<4} "
                f"ok={row['ok']:<4} fail={row['fail']:<4} "
                f"fail_rate={_fmt_pct(row['fail_rate'])}"
            )
    return "\n".join(lines)


def run(
    log_paths: Iterable[str],
    *,
    since_days: Optional[int] = None,
    command: Optional[str] = None,
    top_domains: int = 10,
    as_json: bool = False,
    now: Optional[float] = None,
) -> str:
    events = load_events(log_paths)
    filtered = filter_events(events, since_days=since_days, command=command, now=now)
    url_events = [ev for ev in filtered if ev.get("event") == "url_processed"]
    report = build_report(url_events, top_domains=top_domains)
    if as_json:
        return json.dumps(report, indent=2, sort_keys=True, default=str)
    header_bits = [
        f"Pipeline log report",
        f"  logs: {', '.join(str(p) for p in log_paths)}",
        f"  events_loaded={len(events)}  url_events={len(url_events)}",
    ]
    if since_days is not None:
        header_bits.append(f"  window: last {since_days}d")
    if command:
        header_bits.append(f"  command: {command}")
    return format_report(report, header="\n".join(header_bits))


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m observability.log_report",
        description="Summarize pipeline JSONL run logs.",
    )
    parser.add_argument(
        "--logs",
        nargs="+",
        required=True,
        help="One or more JSONL paths or glob patterns (e.g. logs/*.jsonl).",
    )
    parser.add_argument("--since-days", type=int, default=None, help="Only include events from the last N days.")
    parser.add_argument("--command", default=None, help="Filter events to a single command (ingest/process-links/refresh).")
    parser.add_argument("--top-domains", type=int, default=10, help="How many failing domains to list.")
    parser.add_argument("--json", action="store_true", dest="as_json", help="Emit the report as JSON.")
    args = parser.parse_args(argv)

    output = run(
        args.logs,
        since_days=args.since_days,
        command=args.command,
        top_domains=args.top_domains,
        as_json=args.as_json,
    )
    print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
