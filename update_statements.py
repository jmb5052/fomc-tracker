"""
update_statements.py
====================
Checks the Federal Reserve website for new FOMC policy statements
and adds them to index.html automatically.

Normal mode (run by daily GitHub Action):
    python update_statements.py

Backfill mode (run once to populate history):
    python update_statements.py --backfill 2020-01-01

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

# Realistic browser UA — the Fed's server rejects obvious bot agents
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
REQUEST_TIMEOUT = 30
REQUEST_DELAY   = 3  # seconds between requests


# ── Text extraction ───────────────────────────────────────────────────────────

def extract_statement_text(url):
    """
    Fetch a statement page and extract policy text.

    Strategy 1 (preferred): anchor on 'For release at' ... 'For media inquiries'
    Strategy 2 (fallback):  collect all <p> tags with 20+ words that look like
                            policy prose, stopping before the media contact line.

    Prints a diagnostic snippet if both strategies fail so future parse errors
    are easy to debug.
    """
    print("  Fetching %s ..." % url)
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as e:
        print("  Error: %s" % e)
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["nav", "header", "footer", "script", "style", "aside"]):
        tag.decompose()

    # ── Strategy 1: anchor-based ──────────────────────────────────────────────
    full_text = soup.get_text("\n\n", strip=True)

    release_m = re.search(r"For release at \d+:\d+ [ap]\.m\.", full_text, re.IGNORECASE)
    media_m   = re.search(r"For media inquiries", full_text)

    if release_m and media_m and media_m.start() > release_m.end():
        raw = full_text[release_m.end() : media_m.start()]
        SKIP = {"share", "share:", "pdf", ""}
        chunks = re.split(r"\n{2,}", raw)
        paragraphs = [
            c.strip() for c in chunks
            if c.strip() and c.strip().lower() not in SKIP and len(c.strip()) > 30
        ]
        if paragraphs:
            return "\n\n".join(paragraphs)

    # ── Strategy 2: paragraph tag harvest ────────────────────────────────────
    # Collect <p> elements that look like statement prose.
    # Stop when we hit the media-contact line.
    STOP_RE = re.compile(r"For media inquiries|Implementation Note|Last Update", re.I)
    OPEN_RE = re.compile(
        r"^(Available indicators|Recent indicators|Economic activity|"
        r"The Federal Reserve is committed|The Committee seeks|"
        r"Information received since|Job gains|Labor market|"
        r"The labor market)",
        re.IGNORECASE,
    )

    all_p = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
    collecting = False
    paragraphs = []

    for p in all_p:
        if STOP_RE.search(p):
            break
        if not collecting and OPEN_RE.match(p):
            collecting = True
        if collecting and len(p.split()) >= 15:
            paragraphs.append(p)

    if paragraphs:
        return "\n\n".join(paragraphs)

    # ── Both strategies failed — print diagnostic ─────────────────────────────
    print("  WARNING: could not extract text. First 600 chars of page:")
    print("  " + full_text[:600].replace("\n", " "))
    return None


# ── URL discovery ─────────────────────────────────────────────────────────────

def find_statement_urls_since(start_date):
    """Return sorted list of (date, url) for all statements on or after start_date."""
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


# ── index.html helpers ────────────────────────────────────────────────────────

def get_current_statements(html):
    m = re.search(
        r'<script type="application/json" id="stmt-data">\s*(.*?)\s*</script>',
        html, re.DOTALL,
    )
    if not m:
        raise ValueError("Could not find <script id='stmt-data'> block in index.html")
    return json.loads(m.group(1))


def update_index_html(html, updated_statements):
    m = re.search(
        r'(<script type="application/json" id="stmt-data">)\s*(.*?)\s*(</script>)',
        html, re.DOTALL,
    )
    if not m:
        raise ValueError("Could not find stmt-data block to update")
    new_json = json.dumps(updated_statements, indent=2, ensure_ascii=False)
    replacement = "%s\n%s\n%s" % (m.group(1), new_json, m.group(3))
    return html[: m.start()] + replacement + html[m.end() :]


def format_display_date(d):
    try:
        return d.strftime("%B %-d, %Y")   # Linux/macOS
    except ValueError:
        return d.strftime("%B %#d, %Y")   # Windows


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--backfill", metavar="YYYY-MM-DD",
        help="Fetch all statements from this date forward",
    )
    args = parser.parse_args()

    try:
        with open("index.html", "r", encoding="utf-8") as f:
            html = f.read()
    except FileNotFoundError:
        print("Error: index.html not found. Run from the repo root.")
        sys.exit(1)

    current_statements = get_current_statements(html)
    existing_dates = {s["isoDate"] for s in current_statements}

    if args.backfill:
        try:
            start_date = datetime.strptime(args.backfill, "%Y-%m-%d").date()
        except ValueError:
            print("Error: use YYYY-MM-DD format.")
            sys.exit(1)
        print("Backfill mode: fetching statements since %s" % start_date)
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
            print("  Skipping %s" % url)
            continue
        entry = {
            "date":    format_display_date(stmt_date),
            "isoDate": stmt_date.strftime("%Y-%m-%d"),
            "url":     url,
            "text":    text,
        }
        new_entries.append(entry)
        print("  Added: %s" % entry["date"])

    if not new_entries:
        print("No entries extracted. index.html not changed.")
        return

    all_statements = current_statements + new_entries
    all_statements.sort(key=lambda s: s["isoDate"])

    updated_html = update_index_html(html, all_statements)
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(updated_html)

    print("\nDone. Added %d statement(s)." % len(new_entries))


if __name__ == "__main__":
    main()
