"""
update_statements.py
====================
Fetches FOMC policy statements from federalreserve.gov and maintains
both statements.json (canonical backup) and index.html (web display).

USAGE:
  Normal (daily GitHub Action):
    python update_statements.py

  Backfill (run once to populate history):
    python update_statements.py --backfill 2006-01-01

  Sync only (rebuild index.html from statements.json, no fetching):
    python update_statements.py --sync

LISTING PAGE FORMATS:
  2006-2019: federalreserve.gov/monetarypolicy/fomchistorical{year}.htm
  2020+:     federalreserve.gov/newsevents/pressreleases/{year}-press-fomc.htm

PARSER NOTES:
  Start anchors: "For release at X:XX" (modern) or "For immediate release" (pre-2012)
  End anchors:   "For media inquiries" (modern) or "Last Update:" (pre-2012)

DEPENDENCIES: requests, beautifulsoup4
"""

import argparse
import json
import re
import sys
import time
import unicodedata
from datetime import date, datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

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
REQUEST_DELAY   = 3

JSON_FILE  = Path("statements.json")
HTML_FILE  = Path("index.html")


# -- Text cleaning -------------------------------------------------------------

def clean_text(text):
    """Normalize Unicode and fix common encoding artifacts from Fed pages."""
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\u2011", "-")
    text = text.replace("\u2013", "-")
    text = text.replace("\u2014", " - ")
    text = re.sub(r"a[\x80-\xbf][\x80-\xbf]", "-", text)
    text = re.sub(r"\[\d+\]", "", text)
    text = re.sub(r"  +", " ", text)
    return text.strip()


# -- Statement extraction ------------------------------------------------------

OPEN_RE = re.compile(
    r"^(Available indicators|Recent indicators|Economic activity|"
    r"The Federal Reserve is committed|The Committee seeks|"
    r"Information received since|Job gains|Labor market conditions|"
    r"The labor market|Consistent with its statutory|"
    r"The Committee decided|In light of|"
    r"The pace of recovery|The pace of economic)",
    re.IGNORECASE,
)

STOP_RE = re.compile(
    r"For media inquiries|Last Update:|Implementation Note|"
    r"Return to text|footnote \d",
    re.IGNORECASE,
)


def extract_statement_text(url):
    """
    Fetch a statement page and extract policy text.

    Strategy 1: anchor-based (start/end markers).
    Strategy 2: paragraph harvest using known opening phrases.
    """
    print("  Fetching %s ..." % url)
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        resp.encoding = "utf-8"
    except requests.RequestException as e:
        print("  Error: %s" % e)
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["nav", "header", "footer", "script", "style", "aside"]):
        tag.decompose()

    full_text = soup.get_text("\n\n", strip=True)

    # Strategy 1: anchor-based
    release_m = re.search(
        r"For release at \d+:\d+ [ap]\.m\.|For immediate release",
        full_text, re.IGNORECASE
    )
    end_m = re.search(r"For media inquiries|Last Update:", full_text, re.IGNORECASE)

    if release_m and end_m and end_m.start() > release_m.end():
        raw = full_text[release_m.end() : end_m.start()]
        SKIP = {"share", "share:", "pdf", ""}
        chunks = re.split(r"\n{2,}", raw)
        paragraphs = [
            c.strip() for c in chunks
            if c.strip() and c.strip().lower() not in SKIP and len(c.strip()) > 30
        ]
        if paragraphs:
            return clean_text("\n\n".join(paragraphs))

    # Strategy 2: paragraph harvest
    all_p = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
    collecting, paragraphs = False, []
    for p in all_p:
        if STOP_RE.search(p):
            break
        if not collecting and OPEN_RE.match(p):
            collecting = True
        if collecting and len(p.split()) >= 12:
            paragraphs.append(p)
    if paragraphs:
        return clean_text("\n\n".join(paragraphs))

    print("  WARNING: extraction failed. Page preview:")
    print("  " + full_text[:400].replace("\n", " "))
    return None


# -- URL discovery -------------------------------------------------------------

def find_statement_urls_since(start_date):
    """
    Return sorted list of (date, url) for all FOMC statements on or after
    start_date.

    Listing page formats:
      2006-2019: federalreserve.gov/monetarypolicy/fomchistorical{year}.htm
      2020+:     federalreserve.gov/newsevents/pressreleases/{year}-press-fomc.htm

    Individual statement URLs follow monetary[YYYYMMDD][a|b].htm.
    The b suffix appears on a small number of pre-2012 statements; we
    capture it but normalize all URLs to the a form for consistency.
    """
    found = []
    current_year = date.today().year

    for year in range(max(start_date.year, 2006), current_year + 1):
        if year < 2020:
            listing_url = (
                "https://www.federalreserve.gov"
                "/monetarypolicy/fomchistorical%d.htm" % year
            )
        else:
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
            m = re.search(r"monetary(\d{8})[ab]\.htm", link["href"])
            if not m:
                continue
            stmt_date = datetime.strptime(m.group(1), "%Y%m%d").date()
            if stmt_date < start_date:
                continue
            full_url = (
                "https://www.federalreserve.gov"
                "/newsevents/pressreleases/monetary%sa.htm" % m.group(1)
            )
            found.append((stmt_date, full_url))

        time.sleep(REQUEST_DELAY)

    return sorted(set(found))


# -- JSON backup ---------------------------------------------------------------

def load_json():
    if JSON_FILE.exists():
        with open(JSON_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def save_json(statements):
    statements_sorted = sorted(statements, key=lambda s: s["isoDate"])
    with open(JSON_FILE, "w", encoding="utf-8") as f:
        json.dump(statements_sorted, f, indent=2, ensure_ascii=False)
    print("Saved %d statements to %s." % (len(statements_sorted), JSON_FILE))


# -- index.html sync -----------------------------------------------------------

def sync_html(statements):
    if not HTML_FILE.exists():
        print("Warning: %s not found -- skipping HTML sync." % HTML_FILE)
        return

    with open(HTML_FILE, "r", encoding="utf-8") as f:
        html = f.read()

    m = re.search(
        r'(<script type="application/json" id="stmt-data">)\s*(.*?)\s*(</script>)',
        html, re.DOTALL,
    )
    if not m:
        print("Warning: stmt-data block not found in %s." % HTML_FILE)
        return

    new_json = json.dumps(
        sorted(statements, key=lambda s: s["isoDate"]),
        indent=2, ensure_ascii=False
    )
    replacement = "%s\n%s\n%s" % (m.group(1), new_json, m.group(3))
    updated = html[: m.start()] + replacement + html[m.end() :]

    with open(HTML_FILE, "w", encoding="utf-8") as f:
        f.write(updated)
    print("Synced %d statements into %s." % (len(statements), HTML_FILE))


# -- Date formatting -----------------------------------------------------------

def format_display_date(d):
    try:
        return d.strftime("%B %-d, %Y")
    except ValueError:
        return d.strftime("%B %#d, %Y")


# -- Main ----------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Update FOMC statements in statements.json and index.html"
    )
    parser.add_argument(
        "--backfill", metavar="YYYY-MM-DD",
        help="Fetch all statements from this date forward",
    )
    parser.add_argument(
        "--sync", action="store_true",
        help="Rebuild index.html from statements.json without fetching anything",
    )
    args = parser.parse_args()

    if args.sync:
        statements = load_json()
        if not statements:
            print("No statements in %s -- nothing to sync." % JSON_FILE)
            return
        sync_html(statements)
        print("Sync complete.")
        return

    statements = load_json()

    if not statements:
        if HTML_FILE.exists():
            with open(HTML_FILE, "r", encoding="utf-8") as f:
                html = f.read()
            m = re.search(
                r'<script type="application/json" id="stmt-data">\s*(.*?)\s*</script>',
                html, re.DOTALL,
            )
            if m:
                statements = json.loads(m.group(1))
                print("Bootstrapped %d statements from index.html." % len(statements))
                save_json(statements)

    existing_dates = {s["isoDate"] for s in statements}

    if args.backfill:
        try:
            start_date = datetime.strptime(args.backfill, "%Y-%m-%d").date()
        except ValueError:
            print("Error: use YYYY-MM-DD format.")
            sys.exit(1)
        print("Backfill mode: fetching statements since %s" % start_date)
    elif statements:
        latest_iso = max(s["isoDate"] for s in statements)
        start_date = datetime.strptime(latest_iso, "%Y-%m-%d").date()
        print("Normal mode: checking for statements newer than %s" % latest_iso)
    else:
        print("No existing statements found. Run with --backfill YYYY-MM-DD.")
        sys.exit(1)

    candidates = find_statement_urls_since(start_date)
    new_candidates = [
        (d, url) for d, url in candidates
        if d.strftime("%Y-%m-%d") not in existing_dates
    ]

    if not new_candidates:
        print("No new statements found.")
        sync_html(statements)
        return

    print("\nFound %d new statement(s) to fetch." % len(new_candidates))

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
        print("No entries could be extracted. Nothing saved.")
        return

    all_statements = statements + new_entries
    save_json(all_statements)
    sync_html(all_statements)
    print("\nDone. Added %d statement(s)." % len(new_entries))


if __name__ == "__main__":
    main()
