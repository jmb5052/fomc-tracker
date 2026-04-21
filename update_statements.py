"""
update_statements.py
====================
Checks the Federal Reserve website for new FOMC policy statements
and adds them to index.html automatically.

Normal mode (run by daily GitHub Action):
    python update_statements.py
    -> Checks for statements newer than the most recent one in index.html

Backfill mode (run once to populate history):
    python update_statements.py --backfill 2020-01-01
    -> Fetches all statements from that date forward, skipping any already present

Dependencies: requests, beautifulsoup4
Install:       pip install requests beautifulsoup4
"""

import argparse
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
REQUEST_DELAY = 2  # seconds between requests, to be polite to the Fed's servers


def extract_statement_text(url):
    """
    Fetch a Fed press release page and extract just the policy statement text.

    Uses 'For release at X:XX p.m.' as a reliable anchor point that works
    across all statement eras: pre-COVID format, COVID-era language
    ('The Federal Reserve is committed to using its full range of tools...'),
    the 2022-2023 hiking cycle, current format, and emergency meetings.
    """
    print("  Fetching %s ..." % url)
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as e:
        print("  Error fetching statement: %s" % e)
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["nav", "header", "footer", "script", "style", "aside"]):
        tag.decompose()

    full_text = soup.get_text("\n\n", strip=True)

    release_match = re.search(
        r"For release at \d+:\d+ [ap]\.m\.", full_text, re.IGNORECASE
    )
    media_match = re.search(r"For media inquiries", full_text)

    if not media_match:
        print("  Warning: could not find page anchors.")
        return None

    start = release_match.end() if release_match else 0
    raw = full_text[start : media_match.start()]

    SKIP = {"share", "share:", "pdf", ""}
    chunks = re.split(r"\n{2,}", raw)
    paragraphs = [
        c.strip()
        for c in chunks
        if c.strip() and c.strip().lower() not in SKIP and len(c.strip()) > 30
    ]

    if not paragraphs:
        print("  Warning: no paragraphs found.")
        return None

    return "\n\n".join(paragraphs)


def find_statement_urls_since(start_date):
    """Return sorted list of (date, url) for all FOMC statements on or after start_date."""
    found = []
    current_year = date.today().year

    for year in range(start_date.year, current_year + 1):
        listing_url = (
            "https://www.federalreserve.gov"
            "/newsevents/pressreleases/%d-press-fomc.htm" % year
        )
        print("Scanning %s ..." % listing_url)
        try:
            resp = requests.get(listing_url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
        except requests.RequestException as e:
            print("  Warning: could not fetch %d listing: %s" % (year, e))
            continue

        soup = BeautifulSoup(resp.text, "html.parser")
        for link in soup.find_all("a", href=True):
            m = re.search(r"monetary(\d{8})a\.htm", link["href"])
            if not m:
                continue
            stmt_date = datetime.strptime(m.group(1), "%Y%m%d").date()
            if stmt_date >= start_date:
                full_url = (
                    "https://www.federalreserve.gov"
                    "/newsevents/pressreleases/monetary%sa.htm" % m.group(1)
                )
                found.append((stmt_date, full_url))

        time.sleep(REQUEST_DELAY)

    return sorted(set(found))


def get_current_statements(html):
    match = re.search(
        r'<script type="application/json" id="stmt-data">\s*(.*?)\s*</script>',
        html, re.DOTALL,
    )
    if not match:
        raise ValueError("Could not find <script id='stmt-data'> block in index.html")
    return json.loads(match.group(1))


def update_index_html(html, updated_statements):
    match = re.search(
        r'(<script type="application/json" id="stmt-data">)\s*(.*?)\s*(</script>)',
        html, re.DOTALL,
    )
    if not match:
        raise ValueError("Could not find <script id='stmt-data'> block to update")
    new_json = json.dumps(updated_statements, indent=2, ensure_ascii=False)
    replacement = "%s\n%s\n%s" % (match.group(1), new_json, match.group(3))
    return html[: match.start()] + replacement + html[match.end() :]


def format_display_date(d):
    """Format date as 'Month D, YYYY' with no leading zero."""
    try:
        return d.strftime("%B %-d, %Y")  # Linux/macOS
    except ValueError:
        return d.strftime("%B %#d, %Y")  # Windows


def main():
    parser = argparse.ArgumentParser(description="Update FOMC statements in index.html")
    parser.add_argument(
        "--backfill",
        metavar="YYYY-MM-DD",
        help="Fetch all statements from this date forward (use for initial population)",
    )
    args = parser.parse_args()

    try:
        with open("index.html", "r", encoding="utf-8") as f:
            html = f.read()
    except FileNotFoundError:
        print("Error: index.html not found. Run this script from the repo root.")
        sys.exit(1)

    current_statements = get_current_statements(html)
    existing_dates = {s["isoDate"] for s in current_statements}

    if args.backfill:
        try:
            start_date = datetime.strptime(args.backfill, "%Y-%m-%d").date()
        except ValueError:
            print("Error: invalid date '%s'. Use YYYY-MM-DD format." % args.backfill)
            sys.exit(1)
        print("Backfill mode: fetching all statements since %s" % start_date)
    else:
        latest_iso = max(s["isoDate"] for s in current_statements)
        start_date = datetime.strptime(latest_iso, "%Y-%m-%d").date()
        print("Normal mode: checking for statements newer than %s" % latest_iso)

    candidates = find_statement_urls_since(start_date)
    new_candidates = [
        (d, url) for d, url in candidates
        if d.strftime("%Y-%m-%d") not in existing_dates
    ]

    if not new_candidates:
        print("No new statements found. Nothing to update.")
        return

    print("\nFound %d statement(s) to fetch." % len(new_candidates))

    new_entries = []
    for stmt_date, url in new_candidates:
        time.sleep(REQUEST_DELAY)
        text = extract_statement_text(url)
        if not text:
            print("  Skipping %s — could not extract text." % url)
            continue
        entry = {
            "date": format_display_date(stmt_date),
            "isoDate": stmt_date.strftime("%Y-%m-%d"),
            "url": url,
            "text": text,
        }
        new_entries.append(entry)
        print("  Added: %s" % entry["date"])

    if not new_entries:
        print("No entries could be extracted. index.html not changed.")
        return

    all_statements = current_statements + new_entries
    all_statements.sort(key=lambda s: s["isoDate"])

    updated_html = update_index_html(html, all_statements)
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(updated_html)

    print("\nDone. Added %d statement(s) to index.html." % len(new_entries))


if __name__ == "__main__":
    main()
