#!/usr/bin/env python3
"""Immoweb scraper for all 14 deelgemeenten of Stad Gent.

Scrapes houses/apartments for sale/rent, enriches each listing from its
detail page, and upserts results into 4 CSV files (one per combination).

Politeness: random 2-5s delay between requests, realistic User-Agent.
Blocking (403/429/CAPTCHA/empty responses) triggers automatic fallback to
cloudscraper and then Playwright before giving up.
"""

import argparse
import csv
import json
import os
import random
import re
import sys
import time
from datetime import date
from pathlib import Path

import requests

BASE = "https://www.immoweb.be"
DATA_DIR = Path(__file__).parent / "data"

# Postal codes covering the 14 official deelgemeenten of Stad Gent:
# 9000 Gent, 9030 Mariakerke, 9031 Drongen, 9032 Wondelgem,
# 9040 Sint-Amandsberg, 9041 Oostakker,
# 9042 Desteldonk + Mendonk + Sint-Kruis-Winkel,
# 9050 Gentbrugge + Ledeberg, 9051 Afsnee + Sint-Denijs-Westrem,
# 9052 Zwijnaarde
POSTAL_CODES = "9000,9030,9031,9032,9040,9041,9042,9050,9051,9052"

COMBOS = [
    ("house", "for-sale", "houses_for_sale.csv"),
    ("house", "for-rent", "houses_for_rent.csv"),
    ("apartment", "for-sale", "apartments_for_sale.csv"),
    ("apartment", "for-rent", "apartments_for_rent.csv"),
]

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 "
    "Firefox/126.0",
]

FIELDNAMES = [
    "id", "url", "title", "price", "address", "zip_code", "municipality",
    "property_type", "subtype", "transaction_type",
    "living_area_m2", "plot_area_m2", "garden", "garden_area_m2",
    "terrace", "terrace_area_m2", "bedrooms", "bathrooms",
    "epc_score", "epc_kwh_m2", "construction_year", "condition",
    "heating_type", "parking_indoor", "parking_outdoor", "elevator",
    "furnished", "flood_zone", "cadastral_income", "agency",
    "first_seen", "scrape_date",
]


class BlockedError(Exception):
    pass


def deep_get(obj, *keys):
    """Walk nested dicts/lists safely; return None on any miss."""
    for key in keys:
        if obj is None:
            return None
        if isinstance(key, int):
            if isinstance(obj, list) and len(obj) > key:
                obj = obj[key]
            else:
                return None
        elif isinstance(obj, dict):
            obj = obj.get(key)
        else:
            return None
    return obj


class Fetcher:
    """HTTP fetcher that escalates requests -> cloudscraper -> Playwright."""

    BLOCK_MARKERS = ("captcha", "cf-challenge", "access denied", "blocked")

    def __init__(self, delay_min=2.0, delay_max=5.0):
        self.delay_min = delay_min
        self.delay_max = delay_max
        self.mode = "requests"
        self.session = self._new_session()
        self._browser = None
        self._page = None

    def _new_session(self):
        s = requests.Session()
        s.headers.update({
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,nl;q=0.8",
        })
        return s

    def _sleep(self):
        time.sleep(random.uniform(self.delay_min, self.delay_max))

    def _looks_blocked(self, status, text):
        if status in (403, 429):
            return True
        low = text[:4000].lower()
        return any(marker in low for marker in self.BLOCK_MARKERS)

    def _escalate(self):
        if self.mode == "requests":
            try:
                import cloudscraper
                self.session = cloudscraper.create_scraper()
                self.mode = "cloudscraper"
                print("  [fetcher] blocked -> falling back to cloudscraper")
                return True
            except ImportError:
                pass
        if self.mode in ("requests", "cloudscraper"):
            try:
                from playwright.sync_api import sync_playwright
                self._pw = sync_playwright().start()
                self._browser = self._pw.chromium.launch(headless=True)
                ctx = self._browser.new_context(
                    user_agent=random.choice(USER_AGENTS))
                self._page = ctx.new_page()
                self.mode = "playwright"
                print("  [fetcher] blocked -> falling back to Playwright")
                return True
            except ImportError:
                pass
        return False

    def _fetch_once(self, url):
        if self.mode == "playwright":
            resp = self._page.goto(url, wait_until="domcontentloaded",
                                   timeout=45000)
            if self._page.locator("pre").count():
                text = self._page.locator("pre").first.inner_text()
            else:
                text = self._page.content()
            return (resp.status if resp else 0), text
        resp = self.session.get(url, timeout=30)
        return resp.status_code, resp.text

    def get(self, url, retries=3):
        """GET with polite delay, retry, and automatic fallback escalation."""
        self._sleep()
        last_err = None
        for attempt in range(retries):
            try:
                status, text = self._fetch_once(url)
                if self._looks_blocked(status, text):
                    raise BlockedError(f"HTTP {status} / block marker")
                if status >= 400:
                    raise BlockedError(f"HTTP {status}")
                return text
            except BlockedError as err:
                last_err = err
                print(f"  [fetcher] {err} on attempt {attempt + 1} "
                      f"(mode={self.mode})")
                if not self._escalate():
                    time.sleep(10 * (attempt + 1))
            except Exception as err:  # network errors, timeouts
                last_err = err
                print(f"  [fetcher] error: {err} (attempt {attempt + 1})")
                time.sleep(5 * (attempt + 1))
        raise BlockedError(f"giving up on {url}: {last_err}")

    def close(self):
        if self._browser:
            self._browser.close()
            self._pw.stop()


def search_url(property_type, transaction_type, page):
    return (f"{BASE}/en/search-results/{property_type}/{transaction_type}"
            f"?countries=BE&postalCodes={POSTAL_CODES}"
            f"&page={page}&orderBy=newest")


def iter_search_results(fetcher, property_type, transaction_type,
                        max_pages=None):
    """Yield raw search-result items across all pages."""
    page = 1
    total = None
    while True:
        text = fetcher.get(search_url(property_type, transaction_type, page))
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            raise BlockedError("search response was not JSON (likely blocked)")
        results = payload.get("results") or []
        if total is None:
            total = payload.get("totalItems")
            print(f"  {total} total listings reported")
        if not results:
            break
        yield from results
        page += 1
        if max_pages and page > max_pages:
            break
        if total and (page - 1) * 30 >= total:
            break


def parse_search_item(item, transaction_type):
    """Extract the fields available directly in a search-result item."""
    pid = item.get("id")
    prop = item.get("property") or {}
    loc = prop.get("location") or {}
    if transaction_type == "for-sale":
        price = deep_get(item, "transaction", "sale", "price")
    else:
        price = deep_get(item, "transaction", "rental", "monthlyRentalPrice")
    street = loc.get("street") or ""
    number = loc.get("number") or ""
    address = f"{street} {number}".strip() or None
    subtype = (prop.get("subtype") or "").lower() or None
    locality = loc.get("locality")
    title = " ".join(p for p in [
        (prop.get("subtype") or prop.get("type") or "").capitalize(),
        transaction_type.replace("-", " "),
        f"in {locality}" if locality else "",
    ] if p).strip()
    return {
        "id": pid,
        "url": f"{BASE}/en/classified/{pid}",
        "title": title or None,
        "price": price,
        "address": address,
        "zip_code": loc.get("postalCode"),
        "municipality": locality,
        "property_type": (prop.get("type") or "").lower() or None,
        "subtype": subtype,
        "transaction_type": transaction_type,
        "living_area_m2": prop.get("netHabitableSurface"),
        "plot_area_m2": prop.get("landSurface"),
        "bedrooms": prop.get("bedroomCount"),
        "epc_score": deep_get(item, "transaction", "certificates",
                              "epcScore"),
        "agency": item.get("customerName"),
    }


def parse_detail_page(html, row):
    """Enrich a row with fields from the window.classified JSON blob."""
    match = re.search(r"window\.classified\s*=\s*(\{.*?\});\s*\n", html,
                      re.DOTALL)
    if not match:
        match = re.search(r"window\.classified\s*=\s*(\{.*\});", html)
    if not match:
        return row
    try:
        c = json.loads(match.group(1))
    except json.JSONDecodeError:
        return row

    prop = c.get("property") or {}
    trans = c.get("transaction") or {}
    loc = prop.get("location") or {}

    street = loc.get("street") or ""
    number = loc.get("number") or ""
    address = f"{street} {number}".strip()

    flood = (deep_get(prop, "constructionPermit", "floodZoneType")
             or deep_get(prop, "land", "floodZoneType"))
    furnished = (deep_get(trans, "rental", "isFurnished")
                 if trans.get("rental") is not None
                 else deep_get(trans, "sale", "isFurnished"))

    updates = {
        "address": address or row.get("address"),
        "zip_code": loc.get("postalCode") or row.get("zip_code"),
        "municipality": loc.get("locality") or row.get("municipality"),
        "subtype": (prop.get("subtype") or "").lower()
                   or row.get("subtype"),
        "living_area_m2": prop.get("netHabitableSurface")
                          or row.get("living_area_m2"),
        "plot_area_m2": deep_get(prop, "land", "surface")
                        or row.get("plot_area_m2"),
        "garden": prop.get("hasGarden"),
        "garden_area_m2": prop.get("gardenSurface"),
        "terrace": prop.get("hasTerrace"),
        "terrace_area_m2": prop.get("terraceSurface"),
        "bedrooms": prop.get("bedroomCount") or row.get("bedrooms"),
        "bathrooms": prop.get("bathroomCount"),
        "epc_score": deep_get(trans, "certificates", "epcScore")
                     or row.get("epc_score"),
        "epc_kwh_m2": deep_get(trans, "certificates",
                               "primaryEnergyConsumptionPerSqm"),
        "construction_year": deep_get(prop, "building", "constructionYear"),
        "condition": deep_get(prop, "building", "condition"),
        "heating_type": deep_get(prop, "energy", "heatingType"),
        "parking_indoor": prop.get("parkingCountIndoor"),
        "parking_outdoor": prop.get("parkingCountOutdoor"),
        "elevator": prop.get("hasLift"),
        "furnished": furnished,
        "flood_zone": flood,
        "cadastral_income": deep_get(trans, "sale", "cadastralIncome"),
        "agency": deep_get(c, "customers", 0, "name"),
    }
    for key, value in updates.items():
        if value is not None:
            row[key] = value
    return row


def load_existing(csv_path):
    """Load existing CSV into an ordered dict keyed by listing id."""
    rows = {}
    if csv_path.exists():
        with open(csv_path, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                if row.get("id"):
                    rows[str(row["id"])] = row
    return rows


def save_csv(csv_path, rows):
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDNAMES,
                                extrasaction="ignore")
        writer.writeheader()
        for row in rows.values():
            writer.writerow(row)


def scrape_combo(fetcher, property_type, transaction_type, csv_path,
                 max_pages=None, max_details=None):
    today = date.today().isoformat()
    existing = load_existing(csv_path)
    print(f"\n=== {property_type} {transaction_type} "
          f"({len(existing)} known listings) ===")

    seen_this_run = set()
    new_count = updated_count = detail_count = 0

    for item in iter_search_results(fetcher, property_type, transaction_type,
                                    max_pages=max_pages):
        row = parse_search_item(item, transaction_type)
        pid = str(row.get("id") or "")
        if not pid or pid in seen_this_run:
            continue  # skip duplicates within this run
        seen_this_run.add(pid)

        if pid in existing:
            # Known listing: refresh price/basics, keep enriched fields.
            merged = existing[pid]
            for key, value in row.items():
                if value is not None:
                    merged[key] = value
            merged["scrape_date"] = today
            merged.setdefault("first_seen", today)
            existing[pid] = merged
            updated_count += 1
            continue

        # New listing: fetch the detail page for the full data set.
        if max_details is None or detail_count < max_details:
            try:
                html = fetcher.get(row["url"])
                row = parse_detail_page(html, row)
                detail_count += 1
            except BlockedError as err:
                print(f"  [detail] {pid}: {err} — keeping search-level data")
        row["first_seen"] = today
        row["scrape_date"] = today
        existing[pid] = row
        new_count += 1
        if new_count % 25 == 0:
            save_csv(csv_path, existing)  # checkpoint long runs

    save_csv(csv_path, existing)
    print(f"  done: {new_count} new, {updated_count} updated, "
          f"{len(existing)} total -> {csv_path.name}")
    return new_count + updated_count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-pages", type=int, default=None,
                        help="limit search pages per combo (for testing)")
    parser.add_argument("--max-details", type=int, default=None,
                        help="limit detail-page fetches per combo")
    parser.add_argument("--delay-min", type=float,
                        default=float(os.environ.get("SCRAPE_DELAY_MIN", 2)))
    parser.add_argument("--delay-max", type=float,
                        default=float(os.environ.get("SCRAPE_DELAY_MAX", 5)))
    args = parser.parse_args()

    fetcher = Fetcher(delay_min=args.delay_min, delay_max=args.delay_max)
    failures = []
    try:
        for property_type, transaction_type, filename in COMBOS:
            try:
                scrape_combo(fetcher, property_type, transaction_type,
                             DATA_DIR / filename,
                             max_pages=args.max_pages,
                             max_details=args.max_details)
            except BlockedError as err:
                print(f"!! {property_type} {transaction_type} failed: {err}")
                failures.append(filename)
    finally:
        fetcher.close()

    if len(failures) == len(COMBOS):
        print("All combinations failed — exiting with error.")
        sys.exit(1)
    if failures:
        print(f"Partial failure for: {', '.join(failures)}")


if __name__ == "__main__":
    main()
