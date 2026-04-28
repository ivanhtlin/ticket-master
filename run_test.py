"""Integration test — reads config from watchlist.yaml, no Telegram sent."""
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import re
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

import os
_here = os.path.dirname(os.path.abspath(__file__))
config = yaml.safe_load(open(os.path.join(_here, "watchlist.yaml"), encoding="utf-8"))


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


def test_tixcraft(site):
    """Test number_changed check type with Playwright + HTML dump for selector discovery."""
    print(f"\n{SEP}")
    print(f"[{site['name']}] TixCraft Playwright fetch")
    print(SEP)

    row_contains = site.get("row_contains", "")
    selector = site.get("selector", "")
    print(f"row_contains  : {row_contains!r}")
    print(f"selector      : {selector!r}")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("ERROR: playwright not installed")
        return

    html = None
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
            wait_sel = site.get("wait_selector", "")
            if wait_sel:
                try:
                    page.wait_for_selector(wait_sel, timeout=8_000)
                except Exception:
                    print(f"  wait_selector {wait_sel!r} timed out")
            page.wait_for_timeout(3_000)
            html = page.content()
            browser.close()
    except Exception as e:
        print(f"PLAYWRIGHT ERROR: {e}")
        return

    soup = BeautifulSoup(html, "lxml")
    print(f"rendered size : {len(html):,} bytes")

    # --- dump all table rows so user can find the right selector ---
    print("\n[TABLE ROWS]")
    for i, row in enumerate(soup.find_all("tr")):
        cells = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
        if any(cells):
            print(f"  tr[{i}]: {cells}")

    print("\n[LIST ITEMS containing '區' or '票']")
    for li in soup.find_all(["li", "div", "span"]):
        text = li.get_text(strip=True)
        if ("區" in text or "票" in text) and len(text) < 120 and not li.find_all(["li", "div"]):
            print(f"  <{li.name} class={li.get('class')}> {text!r}")

    # --- try row_contains extraction (mirrors monitor._extract_count logic) ---
    if row_contains:
        print(f"\n[EXTRACTION] row_contains={row_contains!r}")
        for row in soup.find_all(["tr", "li"]):
            row_text = row.get_text()
            if row_contains not in row_text:
                continue
            m = re.search(r'剩餘\s*(\d[\d,]*)', row_text)
            if m:
                count = int(m.group(1).replace(",", ""))
                print(f"  via '剩餘' pattern → count={count}")
                break
            if any(kw in row_text for kw in ("已售完", "售完", "sold out", "Sold Out")):
                print(f"  via sold-out keyword → count=0")
                break
            nums = [int(n.replace(",", "")) for n in re.findall(r'\d[\d,]*', row_text)]
            nums_small = [n for n in nums if n < 1_000]
            print(f"  row text: {row_text.strip()[:100]!r}")
            count = min(nums_small) if nums_small else 0
            print(f"  fallback → count={count}")
            break

    # --- try CSS selector ---
    if selector:
        print(f"\n[EXTRACTION] selector={selector!r}")
        el = soup.select_one(selector)
        if el:
            print(f"  text: {el.get_text(strip=True)!r}")
        else:
            print(f"  FAIL: selector not found")


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
    elif site["check_type"] == "number_changed":
        test_tixcraft(site)

print(f"\n{SEP}")
print("All tests complete.")
print(SEP)
