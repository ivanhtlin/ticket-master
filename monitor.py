import os
import re
import json
import requests
import yaml
import time
import logging
from datetime import datetime, timezone, timedelta
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


_fetch_cache: dict[str, str | None] = {}


def fetch(site: dict) -> str | None:
    url = site["url"]
    if url in _fetch_cache:
        log.info("Using cached HTML for %s", url)
        return _fetch_cache[url]
    use_playwright = site.get("use_playwright", False)
    if use_playwright:
        html = fetch_playwright(url, site.get("wait_selector"))
    else:
        html = fetch_requests(url)
    _fetch_cache[url] = html
    return html


# ── Telegram ──────────────────────────────────────────────────────────────────

def send_telegram(token: str, chat_id: str, text: str) -> bool:
    api_url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    try:
        resp = requests.post(api_url, json=payload, timeout=10)
        resp.raise_for_status()
        log.info("Telegram notification sent: %s", text)
        return True
    except requests.RequestException as e:
        log.error("Telegram send failed: %s", e)
        return False


# ── TicketPlus API status check ───────────────────────────────────────────────

_TP_S3 = "https://apis.ticketplus.com.tw/config/api/v1/getS3"
_TP_API = "https://apis.ticketplus.com.tw/config/api/v1"
_TP_HEADERS = {**HEADERS, "Referer": "https://ticketplus.com.tw/"}


def check_ticketplus_status(site: dict) -> bool:
    """Check TicketPlus ticket areas via JSON API (no Playwright needed).
    Fires when any product transitions from soldout → onsale."""
    event_id = site["event_id"]
    session_id = site["session_id"]

    try:
        r = requests.get(
            f"{_TP_S3}?path=event/{event_id}/products.json",
            headers=_TP_HEADERS, timeout=15,
        )
        r.raise_for_status()
        all_products = r.json().get("products", [])
    except (requests.RequestException, ValueError) as e:
        log.error("[%s] Failed to fetch products.json: %s", site["name"], e)
        return False

    product_ids = [p["productId"] for p in all_products if p.get("sessionId") == session_id]
    if not product_ids:
        log.warning("[%s] No products found for session %s", site["name"], session_id)
        return False

    try:
        r = requests.get(
            f"{_TP_API}/get",
            params={"productId": ",".join(product_ids)},
            headers=_TP_HEADERS, timeout=15,
        )
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        log.error("[%s] Failed to fetch product status: %s", site["name"], e)
        return False

    if data.get("errCode") != "00":
        log.error("[%s] API error: %s", site["name"], data.get("errMsg"))
        return False

    current = {p["id"]: p["status"] for p in data["result"].get("product", [])}
    state_key = site.get("state_key", site["name"])
    state = load_state()
    prev = state.get(state_key, {})
    state[state_key] = current
    save_state(state)

    soldout_n = sum(1 for s in current.values() if s == "soldout")
    onsale_n = sum(1 for s in current.values() if s == "onsale")

    if not prev:
        log.info("[%s] First run — soldout=%d onsale=%d", site["name"], soldout_n, onsale_n)
        return False

    newly_available = [
        pid for pid, status in current.items()
        if status == "onsale" and prev.get(pid) == "soldout"
    ]
    log.info("[%s] soldout=%d onsale=%d newly_available=%d",
             site["name"], soldout_n, onsale_n, len(newly_available))

    if not newly_available:
        return False

    try:
        r2 = requests.get(
            f"{_TP_S3}?path=event/{event_id}/ticketAreas.json",
            headers=_TP_HEADERS, timeout=10,
        )
        r2.raise_for_status()
        area_map = {a["ticketAreaId"]: a["name"] for a in r2.json().get("ticketAreas", [])}
        pid_to_area = {p["productId"]: p.get("ticketAreaId", "") for p in all_products}
        area_names = [area_map.get(pid_to_area.get(pid, ""), pid) for pid in newly_available]
    except Exception:
        area_names = newly_available

    site["_area_names"] = area_names
    return True


# ── Check logic ───────────────────────────────────────────────────────────────

def check_site(site: dict) -> bool:
    """Return True if tickets are available (notification should fire)."""
    check_type = site["check_type"]

    if check_type == "ticketplus_status":
        return check_ticketplus_status(site)

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

    if check_type == "text_changed":
        target = site["target_text"]
        notify_on = site.get("notify_on", "any")  # "disappear", "appear", or "any"
        present = target in soup.get_text()
        state_key = site.get("state_key", site["name"])
        state = load_state()
        prev = state.get(state_key)
        state[state_key] = present
        save_state(state)
        if prev is None:
            log.info("[%s] first run, text_present=%s — no notification", site["name"], present)
            return False
        changed = present != prev
        log.info("[%s] target_text=%r prev_present=%s now_present=%s → changed=%s notify_on=%s",
                 site["name"], target, prev, present, changed, notify_on)
        if not changed:
            return False
        if notify_on == "disappear" and present:
            return False  # text appeared, not disappeared — skip
        if notify_on == "appear" and not present:
            return False  # text disappeared, not appeared — skip
        site["_text_present"] = present
        return True

    if check_type == "any_remaining_above_zero":
        pattern = site.get("pattern", r"剩餘\s*(\d[\d,]*)")
        matches = re.findall(pattern, soup.get_text())
        counts = [int(m.replace(",", "")) for m in matches]
        available = any(c > 0 for c in counts)
        log.info("[%s] pattern=%r counts=%s → available=%s",
                 site["name"], pattern, counts, available)
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


def _cell_count(row, count_col) -> int | None:
    """Extract count from a specific cell (count_col) or full row text."""
    if count_col is not None:
        cells = row.find_all(["td", "th"])
        if not cells:
            return None
        try:
            cell_text = cells[count_col].get_text(strip=True)
        except IndexError:
            return None
        if any(kw in cell_text for kw in ("已售完", "售完", "sold out", "Sold Out")):
            return 0
        m = re.search(r'\d[\d,]*', cell_text)
        return int(m.group().replace(",", "")) if m else 0
    else:
        row_text = row.get_text()
        m = re.search(r'剩餘\s*(\d[\d,]*)', row_text)
        if m:
            return int(m.group(1).replace(",", ""))
        if any(kw in row_text for kw in ("已售完", "售完", "sold out", "Sold Out")):
            return 0
        nums = [int(n.replace(",", "")) for n in re.findall(r'\d[\d,]*', row_text)]
        nums = [n for n in nums if n < 1_000]
        return min(nums) if nums else 0


def _extract_count(site: dict, soup: BeautifulSoup) -> int | None:
    """Extract ticket count. Supports row_contains (first match), row_contains_all (sum), or selector."""
    selector = site.get("selector", "")
    row_contains = site.get("row_contains", "")
    row_contains_all = site.get("row_contains_all", "")
    count_col = site.get("count_col")  # e.g. -1 for last cell

    keyword = row_contains or row_contains_all
    if keyword:
        aggregate = bool(row_contains_all)
        total = 0
        found = False
        for row in soup.find_all(["tr", "li"]):
            if keyword not in row.get_text():
                continue
            count = _cell_count(row, count_col)
            if count is None:
                continue
            found = True
            total += count
            if not aggregate:
                return count  # first-match mode: return immediately
        if not found:
            log.warning("[%s] %r not found in page", site["name"], keyword)
            samples = [el.get_text(strip=True)[:80] for el in soup.find_all(["li", "tr"])[:10]]
            log.warning("[%s] page sample elements: %s", site["name"], samples)
            return None
        return total  # aggregate mode returns sum; first-match already returned above

    if selector:
        el = soup.select_one(selector)
        if el is None:
            log.warning("[%s] selector %r not found", site["name"], selector)
            return None
        text = el.get_text(strip=True).replace(",", "")
        m = re.search(r'\d+', text)
        return int(m.group()) if m else 0

    log.warning("[%s] number_changed requires selector, row_contains, or row_contains_all", site["name"])
    return None


# ── Heartbeat ─────────────────────────────────────────────────────────────────

HEARTBEAT_HOUR_TWN = 9  # 台灣時間 09:00 發送
_TWN = timezone(timedelta(hours=8))


def maybe_send_heartbeat(tg: dict) -> None:
    now = datetime.now(_TWN)
    force = os.environ.get("HEARTBEAT_TEST") == "1"
    if not force and now.hour != HEARTBEAT_HOUR_TWN:
        return
    today = now.strftime("%Y-%m-%d")
    state = load_state()
    if not force and state.get("heartbeat_date") == today:
        return
    ok = send_telegram(
        tg["token"], tg["chat_id"],
        f"✅ 票務監控運作正常\n{today} {now.strftime('%H:%M')} (台灣時間)"
    )
    if ok:
        state["heartbeat_date"] = today
        save_state(state)


# ── Main loop ─────────────────────────────────────────────────────────────────

def run_once(config: dict) -> None:
    global _fetch_cache
    _fetch_cache = {}  # reset per-run cache
    tg = config["telegram"]
    if not tg.get("token"):
        log.error("TELEGRAM_TOKEN is not set — notifications will not be sent")
    if not tg.get("chat_id"):
        log.error("TELEGRAM_CHAT_ID is not set — notifications will not be sent")
    maybe_send_heartbeat(tg)
    for site in config["sites"]:
        if site.get("skip"):
            log.info("[%s] skipped (skip=true in config)", site["name"])
            continue
        if check_site(site):
            msg = site["message"]
            area_names = site.get("_area_names")
            if area_names:
                msg += "\n有票票區：" + "、".join(area_names)
            prev = site.get("_prev_count")
            curr = site.get("_curr_count")
            if prev is not None and curr is not None:
                direction = "▲" if curr > prev else "▼"
                msg = f"{msg}\n{direction} {prev} → {curr} 張"
            ok = send_telegram(tg["token"], tg["chat_id"], msg)
            if not ok and site.get("check_type") == "number_changed" and prev is not None:
                # Revert state so next run re-detects the change
                state = load_state()
                state_key = site.get("state_key", site["name"])
                state[state_key] = prev
                save_state(state)


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
