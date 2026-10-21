#!/usr/bin/env python3
"""Facebook Marketplace watcher.

Searches Marketplace around a location, keeps listings whose title or
description mentions the keyword pattern (default: 5x112 bolt pattern), and
sends a notification for every listing it has not seen before.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote_plus

import requests

log = logging.getLogger("marketplace")

# G2M 1J5 -> Quebec City (Neufchatel / Lebourgneuf), QC
DEFAULT_LAT = 46.8530
DEFAULT_LON = -71.3290
DEFAULT_RADIUS_KM = 40
DEFAULT_QUERY = "5x112"
DEFAULT_LOCATION_SLUG = "quebec"

# Matches "5x112", "5 x 112", "5X112", "5*112", "5×112" but not "15x112" or "5x1120"
DEFAULT_PATTERN = r"(?<!\d)5\s*[x×*]\s*112(?!\d)"


@dataclass
class Listing:
    id: str
    title: str = ""
    price: str = ""
    location: str = ""
    description: str = ""
    image: str = ""

    @property
    def url(self) -> str:
        return f"https://www.facebook.com/marketplace/item/{self.id}/"


# --------------------------------------------------------------------------- #
# Parsing helpers (pure functions, unit tested)
# --------------------------------------------------------------------------- #

def compile_pattern(pattern: str) -> re.Pattern:
    return re.compile(pattern, re.IGNORECASE)


def matches(listing: Listing, pattern: re.Pattern) -> bool:
    return bool(pattern.search(listing.title or "") or pattern.search(listing.description or ""))


def _walk(obj):
    """Yield every dict nested anywhere inside obj."""
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            yield cur
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)


def _get(d, *path, default=""):
    for key in path:
        if not isinstance(d, dict):
            return default
        d = d.get(key)
        if d is None:
            return default
    return d


def listings_from_json(obj) -> dict[str, Listing]:
    """Extract search-result listings from any Marketplace JSON blob."""
    found: dict[str, Listing] = {}
    for d in _walk(obj):
        title = d.get("marketplace_listing_title")
        lid = d.get("id")
        if not title or not lid or not str(lid).isdigit():
            continue
        geo = _get(d, "location", "reverse_geocode", default={})
        location = ", ".join(x for x in (_get(geo, "city"), _get(geo, "state")) if x)
        found[str(lid)] = Listing(
            id=str(lid),
            title=title,
            price=_get(d, "listing_price", "formatted_amount"),
            location=location,
            image=_get(d, "primary_listing_photo", "image", "uri"),
            description=_get(d, "redacted_description", "text"),
        )
    return found


def description_from_json(obj, listing_id: str) -> str:
    """Find the description of a given listing in item-page JSON."""
    fallback = ""
    for d in _walk(obj):
        text = _get(d, "redacted_description", "text")
        if not text:
            continue
        if str(d.get("id", "")) == listing_id:
            return text
        fallback = fallback or text
    return fallback


_SCRIPT_JSON_RE = re.compile(
    r'<script type="application/json"[^>]*>(.*?)</script>', re.DOTALL
)


def json_blobs_from_html(html: str):
    for raw in _SCRIPT_JSON_RE.findall(html):
        try:
            yield json.loads(raw)
        except ValueError:
            continue


def json_blobs_from_graphql(body: str):
    """GraphQL responses may contain several JSON documents, one per line."""
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("for (;;);"):
            line = line[len("for (;;);"):]
        if not line.startswith("{"):
            continue
        try:
            yield json.loads(line)
        except ValueError:
            continue


def search_url(slug: str, query: str, radius_km: int, lat: float | None, lon: float | None) -> str:
    url = (
        f"https://www.facebook.com/marketplace/{slug}/search/"
        f"?query={quote_plus(query)}&radius={radius_km}&sortBy=creation_time_descend&exact=false"
    )
    if lat is not None and lon is not None:
        url += f"&latitude={lat}&longitude={lon}"
    return url


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #

def load_seen(path: Path) -> set[str] | None:
    if not path.exists():
        return None
    try:
        return set(json.loads(path.read_text()))
    except ValueError:
        log.warning("Could not read %s, starting fresh", path)
        return None


def save_seen(path: Path, seen: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Keep the file from growing forever; listing ids are roughly increasing.
    ids = sorted(seen, key=int)[-5000:]
    path.write_text(json.dumps(ids))


# --------------------------------------------------------------------------- #
# Notifications
# --------------------------------------------------------------------------- #

def notify(listing: Listing) -> None:
    title = f"5x112: {listing.title}"[:250]
    body = "\n".join(x for x in (listing.price, listing.location, listing.url) if x)
    send(title, body, click=listing.url)


def send(title: str, body: str, click: str = "") -> None:
    sent = False

    topic = os.getenv("NTFY_TOPIC")
    if topic:
        server = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
        headers = {"Title": title.encode("utf-8"), "Tags": "car"}
        if click:
            headers["Click"] = click
        _post(f"{server}/{topic}", data=body.encode("utf-8"), headers=headers)
        sent = True

    tg_token, tg_chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if tg_token and tg_chat:
        _post(
            f"https://api.telegram.org/bot{tg_token}/sendMessage",
            json={"chat_id": tg_chat, "text": f"{title}\n{body}"},
        )
        sent = True

    discord = os.getenv("DISCORD_WEBHOOK_URL")
    if discord:
        _post(discord, json={"content": f"**{title}**\n{body}"})
        sent = True

    if not sent:
        log.warning("No notifier configured (set NTFY_TOPIC, TELEGRAM_* or DISCORD_WEBHOOK_URL)")
    print(f"[NOTIFY] {title}\n{body}\n", flush=True)


def _post(url: str, **kwargs) -> None:
    try:
        r = requests.post(url, timeout=20, **kwargs)
        r.raise_for_status()
    except requests.RequestException as e:
        log.error("Notification failed: %s", e)


# --------------------------------------------------------------------------- #
# Scraping (Playwright)
# --------------------------------------------------------------------------- #

class LoginRequired(Exception):
    """Facebook redirected the search to its login page."""


class Scraper:
    def __init__(self, args):
        self.args = args

    def __enter__(self):
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self.browser = self._pw.chromium.launch(headless=not self.args.headful)
        self.context = self.browser.new_context(
            locale="fr-CA",
            timezone_id="America/Toronto",
            viewport={"width": 1280, "height": 1800},
            geolocation={"latitude": self.args.lat, "longitude": self.args.lon},
            permissions=["geolocation"],
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
            ),
        )
        cookies = _load_cookies(self.args.cookies)
        if cookies:
            self.context.add_cookies(cookies)
        return self

    def __exit__(self, *exc):
        self.browser.close()
        self._pw.stop()

    def search(self) -> dict[str, Listing]:
        a = self.args
        url = search_url(a.location_slug, a.query, a.radius, a.lat, a.lon)
        found: dict[str, Listing] = {}
        page = self.context.new_page()

        def on_response(resp):
            if "/api/graphql" not in resp.url:
                return
            try:
                body = resp.text()
            except Exception:
                return
            for blob in json_blobs_from_graphql(body):
                found.update(listings_from_json(blob))

        page.on("response", on_response)
        log.info("Opening %s", url)
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(4000)
        _dismiss_login_popup(page)

        for blob in json_blobs_from_html(page.content()):
            found.update(listings_from_json(blob))

        for _ in range(a.scrolls):
            page.mouse.wheel(0, 4000)
            page.wait_for_timeout(2500)
            _dismiss_login_popup(page)

        # DOM fallback: listing links we didn't get structured data for
        for href in page.eval_on_selector_all(
            'a[href*="/marketplace/item/"]', "els => els.map(e => e.href)"
        ):
            m = re.search(r"/marketplace/item/(\d+)", href)
            if m and m.group(1) not in found:
                found[m.group(1)] = Listing(id=m.group(1))

        if not found:
            # Usually a login wall or a block page; keep a snapshot to see which.
            debug = Path("debug")
            debug.mkdir(exist_ok=True)
            page.screenshot(path=str(debug / "search.png"), full_page=True)
            (debug / "search.html").write_text(page.content())
            log.warning("No listings found. Page title: %r, URL: %s (see debug/)",
                        page.title(), page.url)
            if "/login" in page.url:
                page.close()
                raise LoginRequired(page.url)

        page.close()
        log.info("Search returned %d listings", len(found))
        return found

    def fill_details(self, listing: Listing) -> None:
        """Open the item page to get the description (and title if missing)."""
        page = self.context.new_page()
        try:
            page.goto(listing.url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2500)
            html = page.content()
            for blob in json_blobs_from_html(html):
                if not listing.description:
                    listing.description = description_from_json(blob, listing.id)
                if not listing.title:
                    info = listings_from_json(blob).get(listing.id)
                    if info:
                        listing.title, listing.price = info.title, info.price or listing.price
            if not listing.description:
                listing.description = _meta(page, "og:description")
            if not listing.title:
                listing.title = _meta(page, "og:title")
        except Exception as e:
            log.warning("Could not load %s: %s", listing.url, e)
        finally:
            page.close()


def _meta(page, prop: str) -> str:
    el = page.query_selector(f'meta[property="{prop}"]')
    return (el.get_attribute("content") or "") if el else ""


def _dismiss_login_popup(page) -> None:
    for sel in ('div[aria-label="Close"]', 'div[aria-label="Fermer"]'):
        try:
            btn = page.query_selector(sel)
            if btn and btn.is_visible():
                btn.click(timeout=2000)
        except Exception:
            pass


def _load_cookies(path: str | None):
    """Load cookies from a 'Cookie-Editor' JSON export, or a raw
    "c_user=...; xs=..." cookie header copied from browser DevTools."""
    raw = os.getenv("FB_COOKIES_JSON")
    if not raw and path and Path(path).exists():
        raw = Path(path).read_text()
    raw = (raw or "").strip()
    if not raw:
        return []
    return parse_cookies(raw)


def parse_cookies(raw: str) -> list[dict]:
    if raw.lower().startswith("cookie:"):
        raw = raw[len("cookie:"):].strip()
    if not raw.startswith("["):
        items = [part.strip().split("=", 1) for part in raw.split(";")]
        return [
            {"name": k.strip(), "value": v.strip(), "domain": ".facebook.com",
             "path": "/", "secure": True, "httpOnly": False, "sameSite": "None"}
            for k, v in (i for i in items if len(i) == 2)
        ]
    cookies = []
    for c in json.loads(raw):
        cookie = {
            "name": c["name"],
            "value": c["value"],
            "domain": c.get("domain", ".facebook.com"),
            "path": c.get("path", "/"),
            "secure": c.get("secure", True),
            "httpOnly": c.get("httpOnly", False),
        }
        same_site = str(c.get("sameSite", "")).lower()
        cookie["sameSite"] = {"strict": "Strict", "lax": "Lax"}.get(same_site, "None")
        cookies.append(cookie)
    return cookies


# --------------------------------------------------------------------------- #
# Main loop
# --------------------------------------------------------------------------- #

def run_once(args, pattern: re.Pattern) -> None:
    state = Path(args.state)
    seen = load_seen(state)
    # An empty state means no search has worked yet (e.g. only login walls so far)
    first_run = not seen
    seen = seen or set()
    login_flag = state.parent / "login_alert_sent"

    with Scraper(args) as scraper:
        try:
            results = scraper.search()
        except LoginRequired:
            log.error("Facebook asked to log in; add or refresh FB cookies")
            if not login_flag.exists():
                send("Marketplace watcher: Facebook login needed",
                     "Facebook is showing a login page instead of results. "
                     "Add or refresh your Facebook cookies (FB_COOKIES_JSON).")
                login_flag.parent.mkdir(parents=True, exist_ok=True)
                login_flag.touch()
            return
        login_flag.unlink(missing_ok=True)
        new = [l for lid, l in results.items() if args.notify_all or lid not in seen]
        log.info("%d new listings to check", len(new))

        hits = []
        for listing in new:
            if not matches(listing, pattern):
                scraper.fill_details(listing)
            if matches(listing, pattern):
                hits.append(listing)
            else:
                log.info("Skip (no match): %s %s", listing.id, listing.title)
            seen.add(listing.id)

    if first_run and not (args.notify_existing or args.notify_all):
        log.info("First run: %d existing matches marked as seen", len(hits))
        send(
            "Marketplace watcher started",
            f"Watching '{args.query}' within {args.radius} km. "
            f"{len(hits)} existing listings found, you'll be notified of new ones.",
        )
    else:
        for listing in hits:
            notify(listing)

    save_seen(state, seen)


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--query", default=os.getenv("QUERY", DEFAULT_QUERY))
    p.add_argument("--pattern", default=os.getenv("PATTERN", DEFAULT_PATTERN),
                   help="Regex that must appear in title or description")
    p.add_argument("--lat", type=float, default=float(os.getenv("LAT", DEFAULT_LAT)))
    p.add_argument("--lon", type=float, default=float(os.getenv("LON", DEFAULT_LON)))
    p.add_argument("--radius", type=int, default=int(os.getenv("RADIUS_KM", DEFAULT_RADIUS_KM)))
    p.add_argument("--location-slug", default=os.getenv("LOCATION_SLUG", DEFAULT_LOCATION_SLUG))
    p.add_argument("--state", default=os.getenv("STATE_FILE", "state/seen.json"))
    p.add_argument("--cookies", default=os.getenv("FB_COOKIES_FILE", "cookies.json"))
    p.add_argument("--scrolls", type=int, default=int(os.getenv("SCROLLS", 3)))
    p.add_argument("--interval", type=int, default=int(os.getenv("INTERVAL_MIN", 0)),
                   help="Minutes between checks; 0 = run once and exit")
    p.add_argument("--notify-all", action="store_true",
                   default=os.getenv("NOTIFY_ALL", "").lower() == "true",
                   help="Notify every current match, even ones already seen")
    p.add_argument("--notify-existing", action="store_true",
                   help="On first run, notify for listings already online")
    p.add_argument("--headful", action="store_true", help="Show the browser window")
    p.add_argument("--test-notify", action="store_true", help="Send a test notification and exit")
    return p.parse_args(argv)


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = parse_args(argv)

    if args.test_notify:
        send("Marketplace watcher test", "Notifications are working.")
        return 0

    pattern = compile_pattern(args.pattern)
    while True:
        try:
            run_once(args, pattern)
        except Exception:
            log.exception("Run failed")
            if not args.interval:
                return 1
        if not args.interval:
            return 0
        log.info("Sleeping %d min", args.interval)
        time.sleep(args.interval * 60)


if __name__ == "__main__":
    sys.exit(main())
