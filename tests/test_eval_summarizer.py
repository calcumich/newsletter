import os
import shutil

from observability.eval_summarizer import compute_metrics, evaluate_fixtures, load_fixtures


def test_load_fixtures_reads_jsonl():
    base = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".test_tmp"))
    try:
        os.makedirs(base, exist_ok=True)
        fixtures_path = os.path.join(base, "fixtures.jsonl")
        with open(fixtures_path, "w", encoding="utf-8") as f:
            f.write(
                "\n".join(
                    [
                        '{"id":"a","title":"One","url":"https://x","domain":"x","text":"A.","expected_category":"Other"}',
                        '{"id":"b","title":"Two","url":"https://y","domain":"y","text":"B.","expected_category":"Other"}',
                    ]
                )
            )
        fixtures = load_fixtures(fixtures_path)
        assert len(fixtures) == 2
        assert fixtures[0]["id"] == "a"
        assert fixtures[1]["id"] == "b"
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_evaluate_fixtures_stub_and_metrics():
    fixtures = [
        {
            "id": "a",
            "title": "One",
            "url": "https://x",
            "domain": "x",
            "text": "Sentence one. Sentence two.",
            "expected_category": "Other",
        },
        {
            "id": "b",
            "title": "Two",
            "url": "https://y",
            "domain": "y",
            "text": "Sentence one. Sentence two.",
            "expected_category": "Security",
        },
    ]
    results = evaluate_fixtures(fixtures, mode="stub", model="gpt-4o-mini")
    assert len(results) == 2
    assert all(r["summary_nonempty"] for r in results)
    assert all(r["fallback_used"] for r in results)
    metrics = compute_metrics(results)
    assert metrics["total"] == 2
    assert metrics["with_expected_category"] == 2
    assert metrics["category_accuracy"] == 0.5
