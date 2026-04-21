"""
update_statements.py
====================
Checks the Federal Reserve website for new FOMC policy statements
and adds them to index.html automatically.

Run manually:   python update_statements.py
Run by GitHub Action: automatically on weekdays at 6pm ET

Dependencies: requests, beautifulsoup4
Install:       pip install requests beautifulsoup4
"""

import json
import re
import sys
import time
from datetime import date, datetime

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; FOMC-Tracker/1.0; +https://github.com)"
}
REQUEST_TIMEOUT = 30
REQUEST_DELAY = 2  # seconds between requests, to be polite


def get_current_statements(html: str) -> list:
    """Extract the JSON data block from index.html."""
    match = re.search(
        r'<script type="application/json" id="stmt-data">\s*(.*?)\s*</script>',
        html,
        re.DOTALL,
    )
    if not match:
        raise ValueError("Could not find <script id='stmt-data'> block in index.html")
    return json.loads(match.group(1))


def find_new_statement_urls(latest_iso: str) -> list:
    """
    Scrape the Fed's FOMC press releases page and return a sorted list of
    (date, url) tuples for statements newer than latest_iso (YYYY-MM-DD).
    """
    latest = datetime.strptime(latest_iso, "%Y-%m-%d").date()
    found = []

    # Check current year, and previous year in case we're running in January
    current_year = date.today().year
    years = [current_year]
    if date.today().month == 1:
        years.append(current_year - 1)

    for year in years:
        url = f"https://www.federalreserve.gov/newsevents/pressreleases/{year}-press-fomc.htm"
        print(f"Checking {url} ...")
        try:
            resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"  Warning: could not fetch {url}: {e}")
            continue

        soup = BeautifulSoup(resp.text, "html.parser")
        for link in soup.find_all("a", href=True):
            href = link["href"]
            m = re.search(r"monetary(\d{8})a\.htm", href)
            if not m:
                continue
            stmt_date = datetime.strptime(m.group(1), "%Y%m%d").date()
            if stmt_date > latest:
                full_url = (
                    "https://www.federalreserve.gov"
                    f"/newsevents/pressreleases/monetary{m.group(1)}a.htm"
                )
                found.append((stmt_date, full_url))

        time.sleep(REQUEST_DELAY)

    return sorted(set(found))


def extract_statement_text(url: str) -> str | None:
    """
    Fetch a Fed press release page and extract just the policy statement text,
    with paragraphs separated by double newlines.
    Returns None if extraction fails.
    """
    print(f"  Fetching {url} ...")
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"  Error fetching statement: {e}")
        return None

    soup = BeautifulSoup(resp.text, "html.parser")

    # Strip navigation, scripts, styles, and other boilerplate
    for tag in soup(["nav", "header", "footer", "script", "style", "aside"]):
        tag.decompose()

    paragraphs = []
    collecting = False

    # Opening paragraph patterns used historically by the Fed
    OPENING_PATTERNS = re.compile(
        r"^(Available indicators|Recent indicators|Economic activity|"
        r"The labor market|Information received since|Job gains)",
        re.IGNORECASE,
    )
    STOP_PHRASES = ("For media inquiries", "Implementation Note", "Last Update:")

    for p in soup.find_all("p"):
        # Normalize internal whitespace
        text = " ".join(p.get_text().split())
        if not text:
            continue

        if not collecting:
            if OPENING_PATTERNS.match(text):
                collecting = True

        if collecting:
            if any(text.startswith(stop) for stop in STOP_PHRASES):
                break
            if len(text) > 20:
                paragraphs.append(text)

    if not paragraphs:
        print("  Warning: no statement paragraphs found — page structure may have changed.")
        return None

    return "\n\n".join(paragraphs)


def format_display_date(d: date) -> str:
    """Format a date as 'Month D, YYYY' (no leading zero on day)."""
    # %-d works on Linux/macOS; on Windows use %#d
    try:
        return d.strftime("%-d").join([d.strftime("%B "), d.strftime(", %Y")])
    except ValueError:
        return d.strftime("%B %#d, %Y")  # Windows fallback


def update_index_html(html: str, new_entries: list) -> str:
    """Replace the JSON data block in index.html with the updated statement list."""
    match = re.search(
        r'(<script type="application/json" id="stmt-data">)\s*(.*?)\s*(</script>)',
        html,
        re.DOTALL,
    )
    if not match:
        raise ValueError("Could not find <script id='stmt-data'> block to update")

    existing = json.loads(match.group(2))
    existing.extend(new_entries)

    updated_json = json.dumps(existing, indent=2, ensure_ascii=False)
    replacement = f"{match.group(1)}\n{updated_json}\n{match.group(3)}"
    return html[: match.start()] + replacement + html[match.end() :]


def main():
    # Read index.html
    try:
        with open("index.html", "r", encoding="utf-8") as f:
            html = f.read()
    except FileNotFoundError:
        print("Error: index.html not found. Run this script from the repo root.")
        sys.exit(1)

    # Get current latest statement date
    statements = get_current_statements(html)
    latest_iso = max(s["isoDate"] for s in statements)
    print(f"Latest statement in tracker: {latest_iso}")

    # Find any newer statements on the Fed's website
    new_urls = find_new_statement_urls(latest_iso)
    if not new_urls:
        print("No new statements found. Nothing to update.")
        return

    print(f"Found {len(new_urls)} new statement(s).")

    # Fetch and parse each new statement
    new_entries = []
    for stmt_date, url in new_urls:
        time.sleep(REQUEST_DELAY)
        text = extract_statement_text(url)
        if not text:
            print(f"  Skipping {url} — could not extract text.")
            continue

        entry = {
            "date": format_display_date(stmt_date),
            "isoDate": stmt_date.strftime("%Y-%m-%d"),
            "url": url,
            "text": text,
        }
        new_entries.append(entry)
        print(f"  Added: {entry['date']}")

    if not new_entries:
        print("No entries could be extracted. index.html not changed.")
        return

    # Write updated index.html
    updated_html = update_index_html(html, new_entries)
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(updated_html)

    print(f"\nDone. Added {len(new_entries)} statement(s) to index.html.")
    print("The GitHub Action will commit and push this change automatically.")


if __name__ == "__main__":
    main()
