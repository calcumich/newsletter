from newsletter.cli import main

try:
    from bs4 import BeautifulSoup
except Exception:  # pragma: no cover - optional import
    BeautifulSoup = None


if __name__ == "__main__":
    main(beautiful_soup_available=BeautifulSoup is not None)
