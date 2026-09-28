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
    # NFKC turns the Fed's non-breaking hyphen (U+2011) into U+2010, so
    # normalize every hyphen/dash variant to a plain ASCII hyphen.
    for ch in ("\u2010", "\u2011", "\u2012", "\u2013", "\u2212"):
        text = text.replace(ch, "-")
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


# -- Policy rate parsing -------------------------------------------------------
#
# Every statement since 1994 names the policy rate in its decision sentence.
#   Range era (Dec 2008+):  "...target range for the federal funds rate at 3-1/2 to 3-3/4 percent"
#                           "...by 1/4 percentage point to 3-3/4 to 4 percent"
#                           "...of 0 to 1/4 percent"
#   Single-target era:      "...target for the federal funds rate at 5-1/4 percent"
#                           "...target for the federal funds rate 75 basis points to 3-1/2 percent"
# We store the upper bound of the range (or the single target before Dec 2008).
# Statements that never mention the rate (e.g. Aug 17 2007, Oct 11 2019) carry
# the previous meeting's rate forward.

_NUM = r"(\d+-\d+/\d+|\d+/\d+|\d+)"
RANGE_RE  = re.compile(r"target range for the federal funds rate[^.]*?\b" + _NUM + r" to " + _NUM + r" percent", re.I)
SINGLE_RE = re.compile(r"target for the federal funds rate[^.]*?\b(?:at|to|of) " + _NUM + r" percent", re.I)


def _num(s):
    """'3-3/4' -> 3.75, '1/4' -> 0.25, '4' -> 4.0"""
    if "-" in s:
        whole, frac = s.split("-", 1)
    elif "/" in s:
        whole, frac = "0", s
    else:
        whole, frac = s, ""
    val = float(whole)
    if frac:
        n, d = frac.split("/")
        val += float(n) / float(d)
    return val


def parse_rate(text):
    """Return the policy rate (range upper bound or single target), or None."""
    policy = text.split("Voting for", 1)[0]   # never read the dissent paragraph
    policy = re.sub("[\u2010\u2011\u2012\u2013\u2212]", "-", policy)  # stored text may predate the hyphen fix
    m = RANGE_RE.search(policy)
    if m:
        return _num(m.group(2))
    m = SINGLE_RE.search(policy)
    if m:
        return _num(m.group(1))
    return None


# Confirmed against FRED DFEDTAR / DFEDTARU. Used only to check the parser.
_V = {
    2006: ("0131 0328 0510 0629 0808 0920 1025 1212", "4.5 4.75 5 5.25 5.25 5.25 5.25 5.25"),
    2007: ("0131 0321 0509 0628 0807 0817 0918 1031 1211", "5.25 5.25 5.25 5.25 5.25 5.25 4.75 4.5 4.25"),
    2008: ("0122 0130 0318 0430 0625 0805 0916 1008 1029 1216", "3.5 3 2.25 2 2 2 2 1.5 1 0.25"),
    2015: ("1216", "0.5"),
    2016: ("1214", "0.75"),
    2017: ("0315 0614 1213", "1 1.25 1.5"),
    2018: ("0321 0613 0926 1219", "1.75 2 2.25 2.5"),
    2019: ("0731 0918 1011 1030", "2.25 2 2 1.75"),
    2020: ("0303 0315", "1.25 0.25"),
    2022: ("0316 0504 0615 0727 0921 1102 1214", "0.5 1 1.75 2.5 3.25 4 4.5"),
    2023: ("0201 0322 0503 0726", "4.75 5 5.25 5.5"),
    2024: ("0918 1107 1218", "5 4.75 4.5"),
    2025: ("0917 1029 1210", "4.25 4 3.75"),
    2026: ("0916", "4"),
}
VERIFIED = {}
for _y, (_ds, _rs) in _V.items():
    for _d, _r in zip(_ds.split(), _rs.split()):
        VERIFIED["%d-%s-%s" % (_y, _d[:2], _d[2:])] = float(_r)


def fill_rates(statements):
    """
    Give every statement a 'rate'. Parses the stored text for any statement
    missing one; carries the previous rate forward when the text has none.
    Returns True if anything changed.
    """
    changed, prev, mismatches = False, None, 0
    for s in sorted(statements, key=lambda s: s["isoDate"]):
        if "rate" not in s:
            r = parse_rate(s["text"])
            how = "parsed"
            if r is None:
                r, how = prev, "carried forward"
            if r is not None:
                s["rate"] = r
                changed = True
                print("  Rate %s: %.2f%% (%s)" % (s["isoDate"], r, how))
        exp = VERIFIED.get(s["isoDate"])
        if exp is not None and s.get("rate") is not None and abs(s["rate"] - exp) > 1e-9:
            mismatches += 1
            print("  WARNING rate mismatch %s: got %.2f, expected %.2f" % (s["isoDate"], s["rate"], exp))
        if s.get("rate") is not None:
            prev = s["rate"]
    if changed:
        print("Rate check: %d mismatch(es) against verified values." % mismatches)
    return changed


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
            m = re.search(r"monetary/?(\d{8})[ab]\.htm", link["href"])
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

    rates_changed = fill_rates(statements)
    if rates_changed:
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
    fill_rates(all_statements)
    save_json(all_statements)
    sync_html(all_statements)
    print("\nDone. Added %d statement(s)." % len(new_entries))


if __name__ == "__main__":
    main()
