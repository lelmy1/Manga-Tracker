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
import sys
import hashlib
from datetime import datetime, timezone
from pathlib import Path

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
    items = []
    links = page.query_selector_all("a[href*='/order/detailPage/item']")

    for link_el in links:
        href = link_el.get_attribute("href") or ""
        if not href:
            continue
        title = (link_el.get_attribute("title") or link_el.inner_text() or "").strip()
        if not title:
            # Sometimes the title sits in a child element instead
            title_el = link_el.query_selector("img")
            if title_el:
                title = (title_el.get_attribute("alt") or "").strip()
        if not title:
            continue

        # Look for a price in the surrounding card
        price = ""
        container = link_el
        for _ in range(4):  # walk up a few ancestor levels looking for a price
            container = container.evaluate_handle("el => el.parentElement")
            container = container.as_element()
            if not container:
                break
            price_el = container.query_selector("[class*='price']")
            if price_el:
                price = price_el.inner_text().strip()
                break

        full_url = href if href.startswith("http") else "https://order.mandarake.co.jp" + href
        items.append({
            "id": make_item_id(full_url, title),
            "title": title,
            "price": price,
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


def scrape_one(playwright, tracked, debug=False):
    browser = playwright.chromium.launch(headless=True)
    context = browser.new_context(user_agent=USER_AGENT, locale="en-US")
    page = context.new_page()
    items = []
    try:
        page.goto(tracked["url"], wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(1500)  # let lazy-loaded content settle
        if debug:
            debug_path = BASE_DIR / f"debug_{tracked['id']}.html"
            debug_path.write_text(page.content(), encoding="utf-8")
            print(f"  (debug HTML saved to {debug_path.name})")
        items = extract_mandarake_items(page)
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
