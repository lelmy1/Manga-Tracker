#!/usr/bin/env python3
"""
Manga Tracker scraper
----------------------
Reads config.json (the series/search pages you're tracking), visits each one
with a real headless browser via Playwright (needed because sites like
Mandarake render their results with JavaScript — a plain HTTP request just
gets redirected to the homepage), extracts the current listings, figures out
which ones are new, and writes listings.json for manga-tracker.html to read.

Run this on a schedule (cron / Task Scheduler). Every 10-15 minutes is a
reasonable interval for personal use.

First-time setup:
    pip install playwright
    playwright install chromium

Usage:
    python scraper.py            # normal run
    python scraper.py --debug    # also saves the rendered HTML of each
                                  # page to debug_<id>.html, useful if a
                                  # site's selectors need adjusting
"""

import json
import re
import sys
import hashlib
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
LISTINGS_PATH = BASE_DIR / "listings.json"
SEEN_PATH = BASE_DIR / "seen_items.json"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# How long an item stays flagged "new" in listings.json after first being
# seen. This gives the webpage a wide window to notice it even if it isn't
# open at the exact moment the scraper runs.
NEW_WINDOW_HOURS = 24


def load_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default
    return default


def save_json(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def make_item_id(url, title):
    return hashlib.sha1((url + "|" + title).encode("utf-8")).hexdigest()[:16]


def extract_mandarake_items(page):
    """
    Pulls listing cards out of a rendered Mandarake search-results page.

    Mandarake's markup isn't publicly documented and can change, so this
    casts a fairly wide net: it looks for links to item detail pages and
    reads the title + nearby price text. If this comes back empty, run
    with --debug and check debug_<id>.html to see what actually loaded,
    then adjust the selectors below.
    """
    # Each result renders as two separate <a> tags sharing the same href -
    # one wrapping the thumbnail <img>, one wrapping the title text - so
    # group by href to merge them into a single item with both.
    entries = {}
    order = []
    links = page.query_selector_all("a[href*='/order/detailPage/item']")

    for link_el in links:
        href = link_el.get_attribute("href") or ""
        if not href:
            continue
        full_url = href if href.startswith("http") else "https://order.mandarake.co.jp" + href

        entry = entries.get(full_url)
        if entry is None:
            entry = {"title": "", "image": "", "price": ""}
            entries[full_url] = entry
            order.append(full_url)

        title = (link_el.get_attribute("title") or link_el.inner_text() or "").strip()
        if title and not entry["title"]:
            entry["title"] = title

        img_el = link_el.query_selector("img")
        if img_el and not entry["image"]:
            src = img_el.get_attribute("src") or ""
            if src:
                entry["image"] = src
            elif not entry["title"]:
                # Sometimes the title only exists as the thumbnail's alt text
                entry["title"] = (img_el.get_attribute("alt") or "").strip()

        # Look for a price in the surrounding card
        if not entry["price"]:
            container = link_el
            for _ in range(4):  # walk up a few ancestor levels looking for a price
                container = container.evaluate_handle("el => el.parentElement")
                container = container.as_element()
                if not container:
                    break
                price_el = container.query_selector("[class*='price']")
                if price_el:
                    entry["price"] = price_el.inner_text().strip()
                    break

    items = []
    for full_url in order:
        entry = entries[full_url]
        if not entry["title"]:
            continue
        items.append({
            "id": make_item_id(full_url, entry["title"]),
            "title": entry["title"],
            "price": entry["price"],
            "image": entry["image"],
            "url": full_url,
        })
    return items


def extract_yahoo_auctions_items(page):
    """
    Pulls listing cards out of a rendered Yahoo Auctions search-results page.

    Each result is an `a.Product__titleLink` carrying the title and price as
    data attributes (`data-auction-title`, `data-auction-price`), so no DOM
    walking is needed to find them.
    """
    items = []
    links = page.query_selector_all("a.Product__titleLink")

    for link_el in links:
        href = link_el.get_attribute("href") or ""
        if not href:
            continue
        title = (link_el.get_attribute("data-auction-title") or link_el.inner_text() or "").strip()
        if not title:
            continue

        price_raw = link_el.get_attribute("data-auction-price") or ""
        price = f"{int(price_raw):,}円" if price_raw.isdigit() else ""
        image = link_el.get_attribute("data-auction-img") or ""

        items.append({
            "id": make_item_id(href, title),
            "title": title,
            "price": price,
            "image": image,
            "url": href,
        })

    # De-dupe by id, preserve order
    seen_ids = set()
    unique = []
    for it in items:
        if it["id"] not in seen_ids:
            seen_ids.add(it["id"])
            unique.append(it)
    return unique


def extract_fril_items(page):
    """
    Pulls listing cards out of a rendered Fril/Rakuma (fril.jp) page - brand
    pages (/brand/{id}/category/{id}) and keyword search pages (/s?query=...)
    use different markup for the same info - the title link has a
    different class (link_brand_title vs. link_search_title) and the price
    span only sometimes carries a data-test attribute - so select through
    the stable wrapper classes (`.item-box__item-name`, `.item-box__item-price`)
    instead of anything template-specific. Sold-out items are skipped since
    they're no longer purchasable and just add noise to the "new listing"
    alerts.
    """
    items = []
    boxes = page.query_selector_all(".item-box")

    for box in boxes:
        if box.query_selector(".item-box__soldout_ribbon"):
            continue

        title_el = box.query_selector(".item-box__item-name a")
        if not title_el:
            continue
        href = title_el.get_attribute("href") or ""
        title = (title_el.inner_text() or title_el.get_attribute("title") or "").strip()
        if not href or not title:
            continue

        price = ""
        price_wrap = box.query_selector(".item-box__item-price")
        if price_wrap:
            for span in price_wrap.query_selector_all("span"):
                content = (span.get_attribute("data-content") or "").strip()
                if content and content != "JPY":
                    price = content + "円"
                    break

        image = ""
        img_el = box.query_selector(".item-box__image-wrapper img")
        if img_el:
            image = img_el.get_attribute("src") or img_el.get_attribute("data-original") or ""

        items.append({
            "id": make_item_id(href, title),
            "title": title,
            "price": price,
            "image": image,
            "url": href,
        })

    # De-dupe by id, preserve order
    seen_ids = set()
    unique = []
    for it in items:
        if it["id"] not in seen_ids:
            seen_ids.add(it["id"])
            unique.append(it)
    return unique


def extract_mercari_items(page):
    """
    Pulls listing cards out of a rendered Mercari (jp.mercari.com) search
    results page. The visible price shows a currency conversion based on
    the visitor's apparent location (e.g. "CA$11.18" from a Canadian IP,
    which is what GitHub Actions runners look like), so the JPY price is
    pulled from each card's aria-label instead, which always states the
    real yen price regardless of where the scraper happens to run from.
    """
    items = []
    cells = page.query_selector_all('[data-testid="item-cell"]')

    for cell in cells:
        link_el = cell.query_selector('a[data-testid="thumbnail-link"]')
        if not link_el:
            continue
        href = link_el.get_attribute("href") or ""
        if not href:
            continue
        full_url = href if href.startswith("http") else "https://jp.mercari.com" + href

        title_el = cell.query_selector('[data-testid="thumbnail-item-name"]')
        title = (title_el.inner_text() if title_el else "").strip()
        if not title:
            continue

        price = ""
        thumb_el = cell.query_selector(".merItemThumbnail")
        if thumb_el:
            aria = thumb_el.get_attribute("aria-label") or ""
            m = re.search(r"([\d,]+)円", aria)
            if m:
                price = m.group(1) + "円"

        image = ""
        img_el = cell.query_selector("img")
        if img_el:
            image = img_el.get_attribute("src") or ""

        items.append({
            "id": make_item_id(full_url, title),
            "title": title,
            "price": price,
            "image": image,
            "url": full_url,
        })

    # De-dupe by id, preserve order
    seen_ids = set()
    unique = []
    for it in items:
        if it["id"] not in seen_ids:
            seen_ids.add(it["id"])
            unique.append(it)
    return unique


# Maps a tracked page's hostname to the extractor that knows how to read its
# markup. Add an entry here (and a matching extract_*_items function above)
# whenever you start tracking a new site.
EXTRACTORS_BY_HOST = {
    "order.mandarake.co.jp": extract_mandarake_items,
    "auctions.yahoo.co.jp": extract_yahoo_auctions_items,
    "fril.jp": extract_fril_items,
    "jp.mercari.com": extract_mercari_items,
}

# order.mandarake.co.jp 302-redirects cold requests (no session cookie) to
# the plain homepage instead of serving results - visiting this page first
# picks up the tr_mndrk_user cookie it requires, same as a real visitor
# clicking through from the site's own language selector would.
WARMUP_URL_BY_HOST = {
    "order.mandarake.co.jp": "https://earth.mandarake.co.jp/",
}

# Default "networkidle" never fires on some sites (persistent background
# requests - analytics, polling, etc.) and just times out. Override per host
# when that happens; "domcontentloaded" + the fixed settle delay below is
# enough once the actual listing markup is server-rendered.
WAIT_UNTIL_BY_HOST = {
    "fril.jp": "domcontentloaded",
}

# Some sites hydrate results via a client-side API call after the page
# itself loads, and even "networkidle" doesn't reliably mean that call has
# resolved yet - a fixed sleep can race it. Wait for a specific selector's
# match *count to stop growing* instead where that happens (see
# wait_for_stable_count below).
WAIT_FOR_SELECTOR_BY_HOST = {
    "jp.mercari.com": '[data-testid="item-cell"]',
}

# jp.mercari.com virtualizes its results grid: every result's wrapper
# element exists in the DOM up front (so wait_for_stable_count's count
# looks "done" immediately), but only ones near the viewport actually have
# their content (title/link) populated - out of e.g. 118 wrapper elements
# only ~15 had real content without scrolling. That's what was making
# perfectly-existing listings look "new" every run: each scrape only ever
# captured whatever small, differently-truncated slice happened to be
# populated, so a listing missing from one run's slice looked brand new
# the next time it happened to be included. Needs actual scroll/wheel
# input (not just waiting, and not plain scrollTo - the real scroll
# container isn't the window) to populate more of it.
VIRTUALIZED_SCROLL_BY_HOST = {
    "jp.mercari.com": {
        "cell_selector": '[data-testid="item-cell"]',
        "populated_selector": 'a[data-testid="thumbnail-link"]',
    },
}


def wait_for_stable_count(page, selector, poll_interval_ms=700, max_wait_ms=12000):
    """
    Polls `selector`'s match count until it stops growing (unchanged on two
    consecutive checks) or `max_wait_ms` elapses. Returns the final count.
    A timeout just means it never stabilized in time (or genuinely has zero
    matches) - not an error, the extractor still runs on whatever's there.
    """
    start = time.monotonic()
    last_count = -1
    stable_checks = 0
    while (time.monotonic() - start) * 1000 < max_wait_ms:
        count = len(page.query_selector_all(selector))
        if count > 0 and count == last_count:
            stable_checks += 1
            if stable_checks >= 2:
                return count
        else:
            stable_checks = 0
        last_count = count
        page.wait_for_timeout(poll_interval_ms)
    return last_count


def load_virtualized_items(page, cell_selector, populated_selector, max_scrolls=15, pause_ms=1000, stable_rounds=2):
    """
    Repeatedly scrolls (via wheel input) until the number of `cell_selector`
    matches that also contain `populated_selector` stops growing, or
    `max_scrolls` is hit. See VIRTUALIZED_SCROLL_BY_HOST for why this is
    needed on top of wait_for_stable_count.
    """
    def populated_count():
        cells = page.query_selector_all(cell_selector)
        return sum(1 for c in cells if c.query_selector(populated_selector))

    last = -1
    stable = 0
    for _ in range(max_scrolls):
        page.mouse.wheel(0, 2500)
        page.wait_for_timeout(pause_ms)
        count = populated_count()
        if count == last:
            stable += 1
            if stable >= stable_rounds:
                break
        else:
            stable = 0
        last = count


def scrape_one(playwright, tracked, debug=False):
    hostname = urlparse(tracked["url"]).hostname or ""
    extractor = EXTRACTORS_BY_HOST.get(hostname)
    if extractor is None:
        print(f"  ! no extractor registered for host '{hostname}' — skipping")
        return []

    browser = playwright.chromium.launch(headless=True)
    context = browser.new_context(user_agent=USER_AGENT, locale="en-US")
    page = context.new_page()
    items = []
    try:
        wait_until = WAIT_UNTIL_BY_HOST.get(hostname, "networkidle")
        warmup_url = WARMUP_URL_BY_HOST.get(hostname)
        if warmup_url:
            page.goto(warmup_url, wait_until="networkidle", timeout=30000)
        page.goto(tracked["url"], wait_until=wait_until, timeout=30000)
        wait_selector = WAIT_FOR_SELECTOR_BY_HOST.get(hostname)
        if wait_selector:
            wait_for_stable_count(page, wait_selector)
        virt_cfg = VIRTUALIZED_SCROLL_BY_HOST.get(hostname)
        if virt_cfg:
            load_virtualized_items(page, virt_cfg["cell_selector"], virt_cfg["populated_selector"])
        page.wait_for_timeout(3000 if wait_until != "networkidle" else 1500)  # let lazy-loaded content settle
        if debug:
            debug_path = BASE_DIR / f"debug_{tracked['id']}.html"
            debug_path.write_text(page.content(), encoding="utf-8")
            print(f"  (debug HTML saved to {debug_path.name})")
        items = extractor(page)
    except Exception as e:
        print(f"  ! error loading {tracked['url']}: {e}")
    finally:
        browser.close()
    return items


def main():
    debug = "--debug" in sys.argv
    config = load_json(CONFIG_PATH, [])
    seen = load_json(SEEN_PATH, {})  # { tracked_id: { item_id: first_seen_iso } }
    listings = {}

    if not config:
        print("config.json is empty. Add a tracked series first — see README.md.")
        return

    now = datetime.now(timezone.utc)

    with sync_playwright() as p:
        for tracked in config:
            tid = tracked["id"]
            label = tracked.get("label") or tracked["url"]
            print(f"Checking {label} ...")

            items = scrape_one(p, tracked, debug=debug)
            tid_seen = seen.get(tid, {})
            is_first_run = tid not in seen

            new_count = 0
            for it in items:
                if it["id"] not in tid_seen:
                    tid_seen[it["id"]] = now.isoformat()
                    if not is_first_run:
                        new_count += 1

                first_seen = datetime.fromisoformat(tid_seen[it["id"]])
                age_hours = (now - first_seen).total_seconds() / 3600
                it["isNew"] = (age_hours < NEW_WINDOW_HOURS) and not is_first_run

            seen[tid] = tid_seen

            listings[tid] = {
                "label": tracked.get("label"),
                "url": tracked["url"],
                "lastChecked": now.isoformat(),
                "items": items,
                "newCount": new_count,
                "error": len(items) == 0,
            }

            if is_first_run:
                print(f"  -> baseline set ({len(items)} listing(s) found)")
            elif new_count:
                print(f"  -> {new_count} new listing(s) found")
            else:
                print(f"  -> no new listings ({len(items)} total)")

    save_json(LISTINGS_PATH, listings)
    save_json(SEEN_PATH, seen)
    print("Done. listings.json updated.")


if __name__ == "__main__":
    main()
