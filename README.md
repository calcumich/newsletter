# Newsletter Pipeline

Minimal Gmail ingestion pipeline that extracts newsletter links and (optionally) fetches article pages.

## Tests

Install deps:
```bash
python -m pip install -r requirements.txt
```

Run tests:
```bash
python -m pytest
```

Notes:
- Tests create a temporary `.test_tmp` directory in the repo (gitignored).
