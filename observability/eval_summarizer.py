import argparse
import json
import os
from typing import Any

from newsletter.summarize import (
    normalize_summary_output,
    summarize_text,
    summarize_text_openai,
    summarize_text_stub,
)


def default_fixtures_path() -> str:
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    return os.path.join(repo_root, "eval", "fixtures.jsonl")


def load_fixtures(path: str) -> list[dict[str, Any]]:
    fixtures: list[dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            raw = line.strip()
            if not raw or raw.startswith("#"):
                continue
            fixtures.append(json.loads(raw))
    return fixtures


def summarize_fixture(fixture: dict[str, Any], mode: str, model: str) -> tuple[dict[str, Any], bool]:
    title = fixture.get("title")
    url = fixture.get("url", "")
    domain = fixture.get("domain", "")
    text = fixture.get("text", "")

    if mode == "stub":
        summary, bullets = summarize_text_stub(text)
        output = normalize_summary_output(
            {
                "summary": summary,
                "bullets": bullets,
                "category": "Other",
                "tags": [],
                "confidence": 0.3,
                "paywall_or_blocked": False,
            },
            fallback_text=text,
        )
        return output, True

    if mode == "openai":
        raw = summarize_text_openai(text, title=title, url=url, domain=domain, model=model)
        if raw is None:
            raise RuntimeError(f"OpenAI summarization failed for fixture id={fixture.get('id', '?')}")
        return normalize_summary_output(raw, fallback_text=text), False

    output = summarize_text(text, title=title, url=url, domain=domain)
    fallback_used = output.get("category") == "Other" and not output.get("tags")
    return output, fallback_used


def evaluate_fixtures(fixtures: list[dict[str, Any]], mode: str, model: str) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for fixture in fixtures:
        output, fallback_used = summarize_fixture(fixture, mode=mode, model=model)
        expected_category = fixture.get("expected_category")
        result = {
            "id": fixture.get("id"),
            "expected_category": expected_category,
            "predicted_category": output.get("category"),
            "category_match": (
                output.get("category") == expected_category if expected_category else None
            ),
            "needs_review": "needs-review" in {t.lower() for t in output.get("tags", [])},
            "confidence": float(output.get("confidence", 0.0)),
            "summary_nonempty": bool((output.get("summary") or "").strip()),
            "fallback_used": fallback_used,
        }
        results.append(result)
    return results


def compute_metrics(results: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(results)
    if total == 0:
        return {"total": 0}
    with_expectation = [r for r in results if r.get("category_match") is not None]
    matched = [r for r in with_expectation if r.get("category_match")]
    summary_nonempty = [r for r in results if r.get("summary_nonempty")]
    needs_review = [r for r in results if r.get("needs_review")]
    fallback_used = [r for r in results if r.get("fallback_used")]
    avg_confidence = sum(r.get("confidence", 0.0) for r in results) / total

    return {
        "total": total,
        "with_expected_category": len(with_expectation),
        "category_accuracy": (len(matched) / len(with_expectation)) if with_expectation else None,
        "summary_nonempty_rate": len(summary_nonempty) / total,
        "needs_review_rate": len(needs_review) / total,
        "fallback_used_rate": len(fallback_used) / total,
        "average_confidence": avg_confidence,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate summarizer quality on fixture set")
    parser.add_argument("--fixtures", default=default_fixtures_path(), help="Path to fixture JSONL")
    parser.add_argument("--mode", choices=["auto", "stub", "openai"], default="auto")
    parser.add_argument("--limit", type=int, default=0, help="Optional fixture limit")
    parser.add_argument("--model", default=os.getenv("OPENAI_MODEL", "gpt-4o-mini"))
    parser.add_argument("--output-json", default="", help="Optional output path for evaluation JSON")
    parser.add_argument("--show-samples", type=int, default=5, help="How many results to print")
    args = parser.parse_args()

    fixtures = load_fixtures(args.fixtures)
    if args.limit > 0:
        fixtures = fixtures[: args.limit]
    results = evaluate_fixtures(fixtures, mode=args.mode, model=args.model)
    metrics = compute_metrics(results)

    print("== Summarizer Evaluation ==")
    print(f"fixtures: {args.fixtures}")
    print(f"mode: {args.mode}")
    print(f"total: {metrics.get('total', 0)}")
    if metrics.get("category_accuracy") is not None:
        print(f"category_accuracy: {metrics['category_accuracy']:.2%}")
    print(f"summary_nonempty_rate: {metrics.get('summary_nonempty_rate', 0.0):.2%}")
    print(f"needs_review_rate: {metrics.get('needs_review_rate', 0.0):.2%}")
    print(f"fallback_used_rate: {metrics.get('fallback_used_rate', 0.0):.2%}")
    print(f"average_confidence: {metrics.get('average_confidence', 0.0):.3f}")

    print("\n== Sample Results ==")
    for row in results[: max(0, args.show_samples)]:
        print(
            f"- {row['id']}: expected={row['expected_category']} "
            f"predicted={row['predicted_category']} match={row['category_match']} "
            f"needs_review={row['needs_review']} fallback={row['fallback_used']}"
        )

    if args.output_json:
        payload = {"metrics": metrics, "results": results}
        with open(args.output_json, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        print(f"\nWrote evaluation output to: {args.output_json}")


if __name__ == "__main__":
    main()

