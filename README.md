# Ghent Real Estate — Immoweb Scraper

Daily scraper that collects all real-estate listings on [Immoweb](https://www.immoweb.be)
for the **14 official deelgemeenten of Stad Gent**:

Gent, Afsnee, Desteldonk, Drongen, Gentbrugge, Ledeberg, Mariakerke, Mendonk,
Oostakker, Sint-Amandsberg, Sint-Denijs-Westrem, Sint-Kruis-Winkel, Wondelgem,
Zwijnaarde (postal codes 9000, 9030–9032, 9040–9042, 9050–9052).

## What it does

- Scrapes **4 market segments**, each saved to its own CSV in `data/`:
  - `houses_for_sale.csv`
  - `houses_for_rent.csv`
  - `apartments_for_sale.csv`
  - `apartments_for_rent.csv`
- Walks **all pages** of Immoweb's search results, then fetches each new
  listing's detail page for the full data set: price, address, surfaces
  (living / plot / garden / terrace), bedrooms, bathrooms, EPC score and
  kWh/m², construction year, condition, heating type, parking, elevator,
  furnished, flood zone, cadastral income, agency, and more.
- **Upserts by listing ID**: new listings are appended, existing ones are
  refreshed (with a new `scrape_date`); historical rows are never deleted.
- **Lifecycle tracking**: a listing that drops out of the search results
  (sold / rented / withdrawn) is flagged `active=False` with a
  `disappeared_date`; its `last_seen` is frozen at the last day it appeared.
  This makes time-on-market (`last_seen − first_seen`) and absorption
  measurable. A safety guard skips this marking when a run sees fewer than
  half the previously-active listings (likely a block or partial scrape), so
  a bad scrape never mass-flags the inventory as gone.
- **Complete raw capture**: every detail-page fetch also archives the full
  `window.classified` JSON (~200 fields, including free-text descriptions
  and photo lists) to `data/raw/{id}.json.gz`, so fields not flattened into
  the CSVs can always be extracted later.
- **Polite scraping**: random 2–5 s delay between requests and realistic
  browser User-Agent headers.
- **Block resilience**: on HTTP 403/429, CAPTCHA pages, or empty responses it
  automatically falls back to `cloudscraper`, then to a headless Chromium via
  Playwright, before giving up.

## Automation

`.github/workflows/scrape.yml` runs the scraper **daily at 07:00 UTC** and
commits updated CSVs back to the repository (only when something changed),
using the built-in `GITHUB_TOKEN`. It can also be triggered manually from the
Actions tab.

## Running locally

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
playwright install chromium   # optional, only needed for the fallback
python scraper.py
```

Useful flags for testing:

```bash
python scraper.py --max-pages 1 --max-details 5
```

`--delay-min` / `--delay-max` (or env vars `SCRAPE_DELAY_MIN` /
`SCRAPE_DELAY_MAX`) control the politeness delay.

## Data notes

- Every row carries `first_seen`, `last_seen`, `scrape_date` (ISO dates),
  an `active` flag (`True`/`False`), and a `disappeared_date` for listings
  that have left the market.
- Prices are EUR: sale price for `for-sale` rows, monthly rent for
  `for-rent` rows.
- Data is scraped from public Immoweb pages for personal/research use.
  Respect Immoweb's terms of service when reusing it.
