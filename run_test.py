"""Final integration test — reads config from watchlist.yaml, no Telegram sent."""
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import yaml
import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
SEP = "=" * 60

config = yaml.safe_load(open("C:/ticket-monitor/watchlist.yaml", encoding="utf-8"))


def test_kham(site):
    print(f"\n{SEP}")
    print(f"[{site['name']}] requests fetch")
    print(SEP)
    try:
        resp = requests.get(site["url"], headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        print(f"FETCH ERROR: {e}")
        return

    soup = BeautifulSoup(resp.text, "lxml")
    selector = site["selector"]
    el = soup.select_one(selector)
    if el is None:
        print(f"FAIL: selector '{selector}' not found in page!")
        return

    raw = el.get_text(strip=True)
    count = int("".join(c for c in raw.replace(",", "") if c.isdigit()) or "0")
    available = count > 0

    print(f"HTTP status   : {resp.status_code}")
    print(f"selector      : {selector}")
    print(f"element text  : {raw!r}")
    print(f"ticket count  : {count}")
    print(f"available     : {available}")

    rows = soup.select("table tr")
    print("table rows:")
    for row in rows:
        cells = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
        if any(c for c in cells if c):
            print(f"  {cells}")

    result = "AVAILABLE - would send Telegram" if available else "SOLD OUT (count=0)"
    print(f"\nRESULT: {result}")


def test_ticketplus(site):
    print(f"\n{SEP}")
    print(f"[{site['name']}] Playwright headless Chromium")
    print(SEP)

    target_text = site["target_text"]
    print(f"target_text   : {repr(target_text)}")
    print(f"code points   : {[hex(ord(c)) for c in target_text]}")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("ERROR: playwright not installed")
        return

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(
                user_agent=HEADERS["User-Agent"],
                locale="zh-TW",
            )
            page = ctx.new_page()
            print("Navigating...")
            page.goto(site["url"], timeout=30_000, wait_until="domcontentloaded")
            try:
                page.wait_for_selector(
                    ".activity-detail, .sold-out, [class*='ticket'], [class*='sold']",
                    timeout=8_000,
                )
            except Exception:
                pass
            page.wait_for_timeout(3_000)
            html = page.content()
            browser.close()
    except Exception as e:
        print(f"PLAYWRIGHT ERROR: {e}")
        return

    soup = BeautifulSoup(html, "lxml")
    page_text = soup.get_text()
    print(f"rendered size : {len(html):,} bytes")

    found = target_text in page_text
    available = not found
    print(f"target found  : {found}")
    print(f"available     : {available}")

    if found:
        idx = page_text.find(target_text)
        snippet = page_text[max(0, idx - 60):idx + 80].strip()
        print(f"context:\n  ...{snippet}...")

    result = "AVAILABLE - would send Telegram" if available else "SOLD OUT (target text present)"
    print(f"\nRESULT: {result}")


for site in config["sites"]:
    if site.get("skip"):
        print(f"\n{SEP}")
        print(f"[{site['name']}] SKIPPED (skip=true)")
        print(SEP)
        continue
    if site["check_type"] == "number_above_zero":
        test_kham(site)
    elif site["check_type"] == "text_disappear":
        test_ticketplus(site)

print(f"\n{SEP}")
print("All tests complete.")
print(SEP)
