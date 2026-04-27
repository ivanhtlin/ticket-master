import os
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
    # Environment variable takes priority over placeholder in yaml
    env_token = os.environ.get("TELEGRAM_TOKEN")
    if env_token:
        config["telegram"]["token"] = env_token
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

    log.warning("[%s] Unknown check_type: %s", site["name"], check_type)
    return False


# ── Main loop ─────────────────────────────────────────────────────────────────

def run_once(config: dict) -> None:
    tg = config["telegram"]
    for site in config["sites"]:
        if site.get("skip"):
            log.info("[%s] skipped (skip=true in config)", site["name"])
            continue
        if check_site(site):
            send_telegram(tg["token"], tg["chat_id"], site["message"])


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
