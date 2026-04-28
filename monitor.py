import os
import re
import json
import requests
import yaml
import time
import logging
from bs4 import BeautifulSoup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

STATE_FILE = os.environ.get("MONITOR_STATE_FILE", "monitor_state.json")


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
}


def load_config(path: str = "watchlist.yaml") -> dict:
    with open(path, encoding="utf-8") as f:
        config = yaml.safe_load(f)
    # Environment variables take priority over placeholders in yaml
    if os.environ.get("TELEGRAM_TOKEN"):
        config["telegram"]["token"] = os.environ["TELEGRAM_TOKEN"]
    if os.environ.get("TELEGRAM_CHAT_ID"):
        config["telegram"]["chat_id"] = os.environ["TELEGRAM_CHAT_ID"]
    return config


# ── HTTP fetch (for plain server-rendered pages) ─────────────────────────────

def fetch_requests(url: str) -> str | None:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        return resp.text
    except requests.RequestException as e:
        log.error("Fetch failed for %s: %s", url, e)
        return None


# ── Playwright fetch (for SPAs and Cloudflare-protected pages) ───────────────

def fetch_playwright(url: str, wait_selector: str | None = None) -> str | None:
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        log.error("playwright not installed. Run: pip install playwright && playwright install chromium")
        return None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(
                user_agent=HEADERS["User-Agent"],
                locale="zh-TW",
                timezone_id="Asia/Taipei",
                viewport={"width": 1280, "height": 800},
                extra_http_headers={
                    "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
                    "sec-ch-ua-mobile": "?0",
                    "sec-ch-ua-platform": '"Windows"',
                },
            )
            page = ctx.new_page()
            page.goto(url, timeout=30_000, wait_until="domcontentloaded")

            if wait_selector:
                try:
                    page.wait_for_selector(wait_selector, timeout=8_000)
                except PWTimeout:
                    log.warning("wait_selector %r timed out on %s — using fixed delay", wait_selector, url)

            page.wait_for_timeout(3_000)
            html = page.content()
            browser.close()
            return html
    except Exception as e:
        log.error("Playwright fetch failed for %s: %s", url, e)
        return None


def fetch(site: dict) -> str | None:
    use_playwright = site.get("use_playwright", False)
    url = site["url"]
    if use_playwright:
        wait_selector = site.get("wait_selector")
        return fetch_playwright(url, wait_selector)
    return fetch_requests(url)


# ── Telegram ──────────────────────────────────────────────────────────────────

def send_telegram(token: str, chat_id: str, text: str) -> None:
    api_url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    try:
        resp = requests.post(api_url, json=payload, timeout=10)
        resp.raise_for_status()
        log.info("Telegram notification sent: %s", text)
    except requests.RequestException as e:
        log.error("Telegram send failed: %s", e)


# ── Check logic ───────────────────────────────────────────────────────────────

def check_site(site: dict) -> bool:
    """Return True if tickets are available (notification should fire)."""
    html = fetch(site)
    if html is None:
        return False

    soup = BeautifulSoup(html, "lxml")
    check_type = site["check_type"]

    if check_type == "text_disappear":
        found = site["target_text"] in soup.get_text()
        available = not found
        log.info(
            "[%s] target_text=%r present=%s → available=%s",
            site["name"], site["target_text"], found, available,
        )
        return available

    if check_type == "number_above_zero":
        selector = site.get("selector", "")
        if not selector:
            log.warning("[%s] selector is empty, skipping", site["name"])
            return False
        el = soup.select_one(selector)
        if el is None:
            log.warning("[%s] selector %r not found in page", site["name"], selector)
            return False
        text = el.get_text(strip=True).replace(",", "")
        digits = "".join(c for c in text if c.isdigit())
        count = int(digits) if digits else 0
        available = count > 0
        log.info("[%s] selector=%r text=%r count=%d → available=%s",
                 site["name"], selector, text, count, available)
        return available

    if check_type == "css_disappear":
        # Element present = sold out; element gone = available
        selector = site.get("selector", "")
        if not selector:
            log.warning("[%s] selector is empty, skipping", site["name"])
            return False
        el = soup.select_one(selector)
        available = el is None
        log.info("[%s] selector=%r found=%s → available=%s",
                 site["name"], selector, el is not None, available)
        return available

    if check_type == "number_changed":
        count = _extract_count(site, soup)
        if count is None:
            return False
        state_key = site.get("state_key", site["name"])
        state = load_state()
        prev = state.get(state_key)
        state[state_key] = count
        save_state(state)
        if prev is None:
            log.info("[%s] first run, count=%d — no notification", site["name"], count)
            return False
        changed = count != prev
        log.info("[%s] prev=%s now=%d → changed=%s", site["name"], prev, count, changed)
        if changed:
            site["_prev_count"] = prev
            site["_curr_count"] = count
        return changed

    log.warning("[%s] Unknown check_type: %s", site["name"], check_type)
    return False


def _extract_count(site: dict, soup: BeautifulSoup) -> int | None:
    """Extract ticket count using selector + optional row_contains filter."""
    selector = site.get("selector", "")
    row_contains = site.get("row_contains", "")

    if row_contains:
        # Walk <tr> / <li> elements; find the one whose text contains row_contains
        for row in soup.find_all(["tr", "li"]):
            row_text = row.get_text()
            if row_contains not in row_text:
                continue
            # "剩餘 N" pattern → available count
            m = re.search(r'剩餘\s*(\d[\d,]*)', row_text)
            if m:
                return int(m.group(1).replace(",", ""))
            # Explicit sold-out markers → 0
            if any(kw in row_text for kw in ("已售完", "售完", "sold out", "Sold Out")):
                return 0
            # Fall back: pick smallest number in row (remaining < price)
            nums = [int(n.replace(",", "")) for n in re.findall(r'\d[\d,]*', row_text)]
            nums = [n for n in nums if n < 1_000]   # prices are typically ≥ 1000
            if nums:
                return min(nums)
            return 0  # row found but no parseable count → treat as 0
        log.warning("[%s] row_contains=%r not found in page", site["name"], row_contains)
        # dump first 10 li/tr texts to help diagnose what the page actually contains
        samples = [el.get_text(strip=True)[:80] for el in soup.find_all(["li", "tr"])[:10]]
        log.warning("[%s] page sample elements: %s", site["name"], samples)
        return None

    if selector:
        el = soup.select_one(selector)
        if el is None:
            log.warning("[%s] selector %r not found", site["name"], selector)
            return None
        text = el.get_text(strip=True).replace(",", "")
        m = re.search(r'\d+', text)
        return int(m.group()) if m else 0

    log.warning("[%s] number_changed requires selector or row_contains", site["name"])
    return None


# ── Main loop ─────────────────────────────────────────────────────────────────

def run_once(config: dict) -> None:
    tg = config["telegram"]
    if not tg.get("token"):
        log.error("TELEGRAM_TOKEN is not set — notifications will not be sent")
    if not tg.get("chat_id"):
        log.error("TELEGRAM_CHAT_ID is not set — notifications will not be sent")
    for site in config["sites"]:
        if site.get("skip"):
            log.info("[%s] skipped (skip=true in config)", site["name"])
            continue
        if check_site(site):
            msg = site["message"]
            # Append count-change info for number_changed type
            prev = site.get("_prev_count")
            curr = site.get("_curr_count")
            if prev is not None and curr is not None:
                direction = "▲" if curr > prev else "▼"
                msg = f"{msg}\n{direction} {prev} → {curr} 張"
            send_telegram(tg["token"], tg["chat_id"], msg)


def main(interval: int = 60) -> None:
    config = load_config()
    log.info(
        "Monitoring %d site(s) every %ds. Press Ctrl+C to stop.",
        len(config["sites"]), interval,
    )
    while True:
        run_once(config)
        time.sleep(interval)


if __name__ == "__main__":
    main()
