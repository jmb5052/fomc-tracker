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

  Re-fetch everything with the current extractor (after parser fixes):
    python update_statements.py --refetch

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
    text = text.replace("\u2044", "/")   # fraction slash, e.g. from NFKC of "¼"
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
    r"The Committee decided|The Federal Open Market Committee|In light of|"
    r"The pace of recovery|The pace of economic)",
    re.IGNORECASE,
)

STOP_RE = re.compile(
    r"For media inquiries|Last Update:|Implementation Note|"
    r"Return to text|footnote \d",
    re.IGNORECASE,
)

INLINE_TAGS = ["a", "span", "em", "strong", "b", "i", "u", "nobr", "font", "abbr", "small", "sub", "cite"]
MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
DATE_RE = re.compile(r"\b(" + MONTHS + r") (\d{1,2}), (\d{4})\b")


def _page_text(soup):
    """
    Flatten a Fed press-release page to text with one chunk per paragraph.

    Fed pages wrap parts of sentences in inline tags (links, spans, number
    groups). get_text() treats every tag boundary as a break, which split
    "...by 1/4 percentage point <span>3-3/4 to</span> 4 percent" into pieces
    and let the short middle piece be thrown away. Unwrapping inline tags
    first keeps each paragraph whole.
    """
    for tag in soup(["nav", "header", "footer", "script", "style", "aside"]):
        tag.decompose()
    for br in soup.find_all("br"):
        br.replace_with(" ")
    for sup in soup.find_all("sup"):                 # footnote markers
        if re.fullmatch(r"\s*\[?\d+\]?\s*", sup.get_text()):
            sup.decompose()
        else:
            sup.unwrap()
    for blk in soup.find_all(["p", "li"]):           # related-document link lists
        if getattr(blk, "decomposed", False):
            continue
        txt = blk.get_text(" ", strip=True)
        links = " ".join(a.get_text(" ", strip=True) for a in blk.find_all("a")).strip()
        if txt and txt == links:
            blk.decompose()
    for tag in soup.find_all(INLINE_TAGS):
        tag.unwrap()
    soup.smooth()
    return soup.get_text("\n\n", strip=True)


def extract_statement_text(url):
    """
    Fetch a statement page. Returns (text, page_date).

    text is None if the page can't be parsed or isn't an FOMC statement.
    page_date is the release date printed at the top of the page, which
    corrects the occasional typo in the Fed's own links.
    """
    print("  Fetching %s ..." % url)
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        resp.encoding = "utf-8"
    except requests.RequestException as e:
        print("  Error: %s" % e)
        return None, None

    soup = BeautifulSoup(resp.text, "html.parser")
    full_text = _page_text(soup)

    release_m = re.search(r"For release at \d+:\d+ [ap]\.m\.|For immediate release", full_text, re.I)
    end_m = re.search(r"For media inquiries|Last Update:", full_text, re.I)

    # Release date: the last date printed before the release line
    page_date = None
    dates = DATE_RE.findall(full_text[: release_m.start()] if release_m else full_text[:400])
    if dates:
        mo, d, y = dates[-1]
        page_date = datetime.strptime("%s %s %s" % (mo, d, y), "%B %d %Y").date()

    # Strategy 1: everything between the release line and the closing line
    paragraphs = []
    if release_m and end_m and end_m.start() > release_m.end():
        raw = full_text[release_m.end() : end_m.start()]
        chunks = [" ".join(c.split()) for c in re.split(r"\n{2,}", raw)]
        paragraphs = [c for c in chunks if len(c) > 30 and c.lower() not in ("share", "share:", "pdf")]

    # Strategy 2: harvest <p> tags starting at a known opening phrase
    if not paragraphs:
        collecting = False
        for p in soup.find_all("p"):
            t = " ".join(p.get_text(" ", strip=True).split())
            if STOP_RE.search(t):
                break
            if not collecting and OPEN_RE.match(t):
                collecting = True
            if collecting and len(t.split()) >= 12:
                paragraphs.append(t)

    if not paragraphs:
        print("  WARNING: extraction failed. Page preview:")
        print("  " + full_text[:400].replace("\n", " "))
        return None, page_date

    text = clean_text("\n\n".join(paragraphs))
    if "Committee" not in text:
        print("  WARNING: page doesn't look like an FOMC statement: %s" % text[:100])
        return None, page_date
    return text, page_date


def fetch_entry(stmt_date, urls):
    """Try each candidate URL for a meeting date; return a statement entry or None."""
    for url in urls:
        time.sleep(REQUEST_DELAY)
        text, page_date = extract_statement_text(url)
        if not text:
            continue
        if page_date and page_date != stmt_date and abs((page_date - stmt_date).days) <= 20:
            print("  Note: page is dated %s but the link said %s -- using the page date." % (page_date, stmt_date))
            stmt_date = page_date
        return {
            "date":    format_display_date(stmt_date),
            "isoDate": stmt_date.strftime("%Y-%m-%d"),
            "url":     url,
            "text":    text,
        }
    print("  Skipping %s -- no usable statement found." % stmt_date)
    return None


# -- Policy rate parsing -------------------------------------------------------
#
# Every statement since 1994 names the policy rate in its decision sentence.
#   Range era (Dec 2008+):  "...target range for the federal funds rate at 3-1/2 to 3-3/4 percent"
#                           "...by 1/4 percentage point to 3-3/4 to 4 percent"
#                           "...of 0 to 1/4 percent"
#                           "...the current 0 to 1/4 percent target range for the federal funds rate" (2014-15)
#   Single-target era:      "...target for the federal funds rate at 5-1/4 percent"
#                           "...target for the federal funds rate 75 basis points to 3-1/2 percent"
# We store the upper bound of the range (or the single target before Dec 2008).
# Statements that never mention the rate (e.g. Aug 17 2007, Oct 11 2019) carry
# the previous meeting's rate forward.

_NUM = r"(\d+-\d+/\d+|\d+/\d+|\d+)"
RANGE_RE  = re.compile(r"target range for the federal funds rate[^.]*?\b" + _NUM + r" to " + _NUM + r" percent", re.I)
SINGLE_RE = re.compile(r"target for the federal funds rate[^.]*?\b(?:at|to|of) " + _NUM + r" percent", re.I)
# 2014-2015 wording puts the number first: "the current 0 to 1/4 percent target range for the federal funds rate"
RANGE_FIRST_RE = re.compile(r"\b" + _NUM + r" to " + _NUM + r" percent target range for the federal funds rate", re.I)


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
    policy = re.split(r"\bVoting (?:for|against)\b", text, 1)[0]   # never read the dissent paragraph
    policy = re.sub("[\u2010\u2011\u2012\u2013\u2212]", "-", policy)  # stored text may predate the hyphen fix
    policy = policy.replace("\u2044", "/")
    m = RANGE_RE.search(policy) or RANGE_FIRST_RE.search(policy)
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
    Return a sorted list of (date, [candidate urls]) for FOMC statements on or
    after start_date.

    Listing pages:
      2006-2019: federalreserve.gov/monetarypolicy/fomchistorical{year}.htm
      2020+:     federalreserve.gov/newsevents/pressreleases/{year}-press-fomc.htm

    Statement URLs are monetary[YYYYMMDD][a|b].htm. The suffix matters: some
    statements (e.g. Jan 22 and Dec 16, 2008) live at the 'b' URL while the
    'a' URL for that day is an unrelated release. Candidates are kept in the
    order they appear on the listing page and tried in that order.
    """
    found = {}
    current_year = date.today().year

    for year in range(max(start_date.year, 2006), current_year + 1):
        if year < 2020:
            listing_url = "https://www.federalreserve.gov/monetarypolicy/fomchistorical%d.htm" % year
        else:
            listing_url = "https://www.federalreserve.gov/newsevents/pressreleases/%d-press-fomc.htm" % year

        print("Scanning %s ..." % listing_url)
        try:
            resp = requests.get(listing_url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
        except requests.RequestException as e:
            print("  Warning: could not fetch %d listing: %s" % (year, e))
            continue

        soup = BeautifulSoup(resp.text, "html.parser")
        for link in soup.find_all("a", href=True):
            m = re.search(r"monetary/?(\d{8})([ab])\.htm", link["href"])
            if not m:
                continue
            stmt_date = datetime.strptime(m.group(1), "%Y%m%d").date()
            if stmt_date < start_date:
                continue
            url = ("https://www.federalreserve.gov/newsevents/pressreleases/monetary%s%s.htm"
                   % (m.group(1), m.group(2)))
            urls = found.setdefault(stmt_date, [])
            if url not in urls:
                urls.append(url)

        time.sleep(REQUEST_DELAY)

    return sorted(found.items())


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

def refetch_all(statements):
    """
    Rebuild every statement from the Fed's site with the current extractor.
    Entries that can't be re-fetched are kept as they are (minus their rate,
    which is re-derived).
    """
    old = {s["isoDate"]: s for s in statements}
    candidates = find_statement_urls_since(date(2006, 1, 1))
    tried = {d.strftime("%Y-%m-%d") for d, _ in candidates}
    print("\nRe-fetching %d statement(s)..." % len(candidates))

    rebuilt, kept = {}, 0
    for stmt_date, urls in candidates:
        entry = fetch_entry(stmt_date, urls)
        iso = stmt_date.strftime("%Y-%m-%d")
        if entry:
            rebuilt[entry["isoDate"]] = entry
        elif iso in old and "Committee" in old[iso]["text"]:
            rebuilt[iso] = {k: v for k, v in old[iso].items() if k != "rate"}
            kept += 1
    for iso, s in old.items():                      # anything the listings no longer show
        if iso not in tried and iso not in rebuilt:
            rebuilt[iso] = {k: v for k, v in s.items() if k != "rate"}
            kept += 1

    result = sorted(rebuilt.values(), key=lambda s: s["isoDate"])
    print("\nRebuilt %d statements (%d kept from the previous data)." % (len(result), kept))
    fill_rates(result)
    return result


def main():
    parser = argparse.ArgumentParser(
        description="Update FOMC statements in statements.json and index.html"
    )
    parser.add_argument("--backfill", metavar="YYYY-MM-DD",
                        help="Fetch all statements from this date forward")
    parser.add_argument("--sync", action="store_true",
                        help="Rebuild index.html from statements.json without fetching anything")
    parser.add_argument("--refetch", action="store_true",
                        help="Re-download every statement with the current extractor")
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

    if not statements and HTML_FILE.exists():
        with open(HTML_FILE, "r", encoding="utf-8") as f:
            html = f.read()
        m = re.search(r'<script type="application/json" id="stmt-data">\s*(.*?)\s*</script>', html, re.DOTALL)
        if m:
            statements = json.loads(m.group(1))
            print("Bootstrapped %d statements from index.html." % len(statements))
            save_json(statements)

    if args.refetch:
        statements = refetch_all(statements)
        save_json(statements)
        sync_html(statements)
        return

    if fill_rates(statements):
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
    new_candidates = [(d, urls) for d, urls in candidates if d.strftime("%Y-%m-%d") not in existing_dates]

    if not new_candidates:
        print("No new statements found.")
        sync_html(statements)
        return

    print("\nFound %d new statement(s) to fetch." % len(new_candidates))
    new_entries = []
    for stmt_date, urls in new_candidates:
        entry = fetch_entry(stmt_date, urls)
        if entry and entry["isoDate"] not in existing_dates:
            new_entries.append(entry)
            print("  Added: %s" % entry["date"])

    if not new_entries:
        print("No entries could be extracted. Nothing saved.")
        sync_html(statements)
        return

    all_statements = statements + new_entries
    fill_rates(all_statements)
    save_json(all_statements)
    sync_html(all_statements)
    print("\nDone. Added %d statement(s)." % len(new_entries))


if __name__ == "__main__":
    main()
