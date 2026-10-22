#!/usr/bin/env python3
"""Browser-free Marketplace watcher for Android (Termux) or any small device.

Uses your logged-in Facebook cookie over plain HTTP, from your own connection.
Cookies Facebook refreshes are saved back to disk, so the session stays alive
instead of expiring like a copied cookie does.
"""

from __future__ import annotations

import argparse
import html as htmllib
import json
import logging
import os
import random
import re
import sys
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import requests

import scraper
from scraper import (
    Listing,
    LoginRequired,
    compile_pattern,
    description_from_json,
    json_blobs_from_graphql,
    json_blobs_from_html,
    listings_from_json,
    matches,
)

log = logging.getLogger("phone")

SEARCH_DOC_ID = "7111939778879383"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)

_DTSG_RES = (
    re.compile(r'"DTSGInitialData",\[\],\{"token":"([^"]+)"'),
    re.compile(r'name="fb_dtsg" value="([^"]+)"'),
)
_LSD_RE = re.compile(r'"LSD",\[\],\{"token":"([^"]+)"')
_OG_RE = r'<meta property="og:{}" content="([^"]*)"'


def _first(patterns, text: str) -> str:
    for p in patterns:
        m = p.search(text)
        if m:
            return m.group(1)
    return ""


def _logged_in_user(html: str) -> str:
    m = re.search(r'"USER_ID":"(\d+)"', html) or re.search(r'"actorID":"(\d+)"', html)
    return m.group(1) if m and m.group(1) != "0" else ""


def _save_debug(name: str, text: str) -> None:
    """Keep the last raw answer on the device for troubleshooting (never uploaded)."""
    path = Path("debug") / name
    path.parent.mkdir(exist_ok=True)
    path.write_text(text)


def is_login_page(url: str) -> bool:
    return "/login" in url or "/checkpoint" in url


# --------------------------------------------------------------------------- #
# Cookie persistence
# --------------------------------------------------------------------------- #

def load_session_cookies(session: requests.Session, saved: Path, initial: Path) -> None:
    """Prefer cookies saved by a previous run; fall back to the pasted cookie."""
    if saved.exists():
        for c in json.loads(saved.read_text()):
            session.cookies.set(c["name"], c["value"], domain=c["domain"], path=c["path"])
        return
    raw = os.getenv("FB_COOKIES_JSON") or (initial.read_text() if initial.exists() else "")
    if not raw.strip():
        raise SystemExit(f"No cookie found: paste your Facebook cookie into {initial}")
    for c in scraper.parse_cookies(raw.strip()):
        session.cookies.set(c["name"], c["value"], domain=c["domain"], path=c["path"])


def save_session_cookies(session: requests.Session, saved: Path) -> None:
    saved.parent.mkdir(parents=True, exist_ok=True)
    cookies = [
        {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path}
        for c in session.cookies
    ]
    saved.write_text(json.dumps(cookies))


# --------------------------------------------------------------------------- #
# Facebook requests
# --------------------------------------------------------------------------- #

class PhoneScraper:
    def __init__(self, args, session: requests.Session):
        self.args = args
        self.s = session
        self.detail_budget = args.max_details
        self.s.headers.update({
            "User-Agent": args.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "fr-CA,fr;q=0.9,en-CA;q=0.8,en;q=0.7",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin",
            "Upgrade-Insecure-Requests": "1",
        })

    def _get(self, url: str) -> str:
        r = self.s.get(url, timeout=30)
        if is_login_page(r.url):
            raise LoginRequired(r.url)
        r.raise_for_status()
        return r.text

    def search(self, query: str) -> dict[str, Listing]:
        a = self.args
        url = scraper.search_url(a.location_slug, query, a.radius, a.lat, a.lon)
        html = self._get(url)
        blobs = list(json_blobs_from_html(html))
        found: dict[str, Listing] = {}
        for blob in blobs:
            found.update(listings_from_json(blob))
        log.info("Search page: %d listings", len(found))
        if not found:
            _save_debug("search.html", html)
            title = re.search(r"<title[^>]*>([^<]*)", html)
            log.info(
                "Search page details: %d bytes, %d JSON blocks, title %r, "
                "logged in as account: %s, mentions marketplace_search: %s",
                len(html), len(blobs), title.group(1) if title else "",
                _logged_in_user(html) or "no",
                "marketplace_search" in html,
            )
            # The page didn't embed results; ask the GraphQL API like the page would.
            found = self._graphql_search(html, query)
            log.info("GraphQL search: %d listings", len(found))
        return found

    def _graphql_search(self, html: str, query: str) -> dict[str, Listing]:
        a = self.args
        dtsg = _first(_DTSG_RES, html)
        lsd = _first((_LSD_RE,), html)
        user = self.s.cookies.get("c_user", "")
        if not dtsg:
            log.warning("No fb_dtsg token on the search page; Facebook may want a login")
        variables = {
            "count": 24,
            "params": {
                "bqf": {"callsite": "COMMERCE_MKTPLACE_WWW", "query": query},
                "browse_request_params": {
                    "commerce_enable_local_pickup": True,
                    "commerce_enable_shipping": True,
                    "commerce_search_and_rp_available": True,
                    "commerce_search_and_rp_condition": None,
                    "commerce_search_and_rp_ctime_days": None,
                    "filter_location_latitude": a.lat,
                    "filter_location_longitude": a.lon,
                    "filter_price_lower_bound": 0,
                    "filter_price_upper_bound": 214748364700,
                    "filter_radius_km": a.radius,
                },
                "custom_request_params": {"surface": "SEARCH"},
            },
        }
        r = self.s.post(
            "https://www.facebook.com/api/graphql/",
            data={
                "av": user, "__user": user, "__a": "1",
                "fb_dtsg": dtsg, "lsd": lsd,
                "doc_id": SEARCH_DOC_ID,
                "variables": json.dumps(variables),
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "X-FB-LSD": lsd,
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Origin": "https://www.facebook.com",
                "Referer": scraper.search_url(a.location_slug, query, a.radius, a.lat, a.lon),
            },
            timeout=30,
        )
        found: dict[str, Listing] = {}
        for blob in json_blobs_from_graphql(r.text):
            if "errors" in blob:
                log.warning("GraphQL error: %s", str(blob["errors"])[:300])
            found.update(listings_from_json(blob))
        if not found:
            _save_debug("graphql.txt", r.text)
            edges = re.search(r'"edges":\[(.{0,80})', r.text)
            log.info("GraphQL details: HTTP %s, %d bytes, token found: %s, start: %s",
                     r.status_code, len(r.text), bool(dtsg),
                     edges.group(0) if edges else r.text[:200])
        return found

    def fill_details(self, listing: Listing) -> None:
        try:
            html = self._get(listing.url)
        except requests.RequestException as e:
            log.warning("Could not load %s: %s", listing.url, e)
            return
        for blob in json_blobs_from_html(html):
            if not listing.description:
                listing.description = description_from_json(blob, listing.id)
            if not listing.title:
                info = listings_from_json(blob).get(listing.id)
                if info:
                    listing.title, listing.price = info.title, info.price or listing.price
        if not listing.description:
            m = re.search(_OG_RE.format("description"), html)
            listing.description = htmllib.unescape(m.group(1)) if m else ""
        if not listing.title:
            m = re.search(_OG_RE.format("title"), html)
            listing.title = htmllib.unescape(m.group(1)) if m else ""


# --------------------------------------------------------------------------- #
# Watches (one Marketplace search each)
# --------------------------------------------------------------------------- #

@dataclass
class Watch:
    """A search to run. Without a regex, every word of the query must appear in the
    title or description (ignoring case, accents, spaces/dashes and a plural "s")."""

    query: str
    pattern: re.Pattern | None = None
    maybe: bool = False

    @property
    def slug(self) -> str:
        return re.sub(r"[^a-z0-9]+", "-", _normalize(self.query, keep_spaces=True)).strip("-")

    def matches(self, listing: Listing) -> bool:
        if self.pattern:
            return matches(listing, self.pattern)
        text = _normalize(f"{listing.title} {listing.description}")
        return all(_stem(w) in text for w in _normalize(self.query, keep_spaces=True).split())


def _normalize(text: str, keep_spaces: bool = False) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9 ]+" if keep_spaces else r"[^a-z0-9]+", "", text)


def _stem(word: str) -> str:
    return word[:-1] if len(word) > 4 and word.endswith("s") else word


def parse_watches(text: str) -> list[Watch]:
    """One search per line: "query", "query | regex" or "query | regex | maybe"."""
    watches = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        pattern = compile_pattern(parts[1]) if len(parts) > 1 and parts[1] else None
        maybe = len(parts) > 2 and parts[2].lower() == "maybe"
        watches.append(Watch(parts[0], pattern, maybe))
    return watches


def load_watches(args) -> list[Watch]:
    path = Path(args.watches)
    if path.exists():
        watches = parse_watches(path.read_text())
        if watches:
            return watches
    return [Watch(args.query, compile_pattern(args.pattern), maybe=True)]


def seen_path(state_dir: Path, watch: Watch) -> Path:
    path = state_dir / f"seen-{watch.slug}.json"
    legacy = state_dir / "seen.json"   # single-search versions stored 5x112 here
    if not path.exists() and watch.slug == scraper.DEFAULT_QUERY and legacy.exists():
        legacy.rename(path)
    return path


# --------------------------------------------------------------------------- #
# Main loop
# --------------------------------------------------------------------------- #

def in_quiet_hours(spec: str, now: datetime | None = None) -> bool:
    """spec like "1-6" means 01:00 to 05:59 local time; empty disables."""
    if not spec:
        return False
    start, end = (int(x) for x in spec.split("-"))
    hour = (now or datetime.now()).hour
    return start <= hour < end if start <= end else hour >= start or hour < end


def check_watch(args, fb: PhoneScraper, watch: Watch, state_dir: Path,
                notified: set[str], started: list[str]) -> None:
    path = seen_path(state_dir, watch)
    seen = scraper.load_seen(path)
    first_run = seen is None   # a new search: remember what's there, don't alert on it
    seen = seen or set()

    results = fb.search(watch.query)
    new = [l for lid, l in results.items() if args.notify_all or lid not in seen]
    log.info("[%s] %d new listings to check", watch.query, len(new))
    if first_run and not args.notify_all:
        seen.update(l.id for l in new)
        started.append(f"{watch.query}: {len(new)} current listings")
        scraper.save_seen(path, seen)
        return

    for listing in new:
        if not watch.matches(listing):
            if fb.detail_budget <= 0:
                continue   # check this one's description next time
            fb.detail_budget -= 1
            time.sleep(random.uniform(2, 5))
            fb.fill_details(listing)
        seen.add(listing.id)
        brand = ""
        if not watch.matches(listing):
            brand = scraper.maybe_brand(listing) if watch.maybe and args.maybe else ""
            if not brand:
                log.info("[%s] Skip (no match, description %d chars): %s %s", watch.query,
                         len(listing.description), listing.id, listing.title)
                continue
        if listing.id in notified and not args.notify_all:
            continue   # already sent by another search
        log.info("[%s] %s: %s %s", watch.query, f"Maybe ({brand})" if brand else "Match",
                 listing.id, listing.title)
        scraper.notify(listing, label=watch.query, maybe=brand)
        notified.add(listing.id)
    scraper.save_seen(path, seen)


def run_once(args, watches: list[Watch], session: requests.Session) -> None:
    state_dir = Path(args.state).parent
    login_flag = state_dir / "login_alert_sent"
    notified_path = state_dir / "notified.json"
    notified = scraper.load_seen(notified_path) or set()
    started: list[str] = []

    fb = PhoneScraper(args, session)
    try:
        for i, watch in enumerate(watches):
            if i:
                time.sleep(random.uniform(5, 15))   # don't fire searches back to back
            check_watch(args, fb, watch, state_dir, notified, started)
    except LoginRequired:
        log.error("Facebook asked to log in or verify the account")
        if not login_flag.exists():
            scraper.send(
                "Marketplace watcher: Facebook login needed",
                "Open Facebook, complete any security check, then paste a fresh cookie "
                "into cookies.txt and delete state/cookies.json.",
            )
            login_flag.parent.mkdir(parents=True, exist_ok=True)
            login_flag.touch()
        return
    finally:
        save_session_cookies(session, state_dir / "cookies.json")
        if notified:
            scraper.save_seen(notified_path, notified)

    login_flag.unlink(missing_ok=True)
    if started:
        scraper.send("Marketplace watcher: new searches started",
                     "\n".join(started) + f"\nWithin {args.radius} km. "
                     "You'll be notified of new listings.")


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--query", default=os.getenv("QUERY", scraper.DEFAULT_QUERY))
    p.add_argument("--pattern", default=os.getenv("PATTERN", scraper.DEFAULT_PATTERN))
    p.add_argument("--lat", type=float, default=float(os.getenv("LAT", scraper.DEFAULT_LAT)))
    p.add_argument("--lon", type=float, default=float(os.getenv("LON", scraper.DEFAULT_LON)))
    p.add_argument("--radius", type=int,
                   default=int(os.getenv("RADIUS_KM", scraper.DEFAULT_RADIUS_KM)))
    p.add_argument("--location-slug",
                   default=os.getenv("LOCATION_SLUG", scraper.DEFAULT_LOCATION_SLUG))
    p.add_argument("--watches", default=os.getenv("WATCHES_FILE", "watches.txt"),
                   help="File with one search per line (falls back to --query/--pattern)")
    p.add_argument("--max-details", type=int, default=int(os.getenv("MAX_DETAILS", 15)),
                   help="Max listing pages opened per check to read descriptions")
    p.add_argument("--state", default=os.getenv("STATE_FILE", "state/seen.json"))
    p.add_argument("--cookie-file", default=os.getenv("COOKIE_FILE", "cookies.txt"))
    p.add_argument("--user-agent",
                   default=os.getenv("FB_USER_AGENT", "").strip().strip("\"'") or DEFAULT_USER_AGENT)
    p.add_argument("--interval", type=float, default=float(os.getenv("INTERVAL_MIN", 10)),
                   help="Minutes between checks (randomised +/-25%%); 0 = run once")
    p.add_argument("--quiet-hours", default=os.getenv("QUIET_HOURS", "1-6"),
                   help='Local hours to skip, e.g. "1-6"; empty to never pause')
    p.add_argument("--notify-all", action="store_true",
                   help="Notify every current match, even ones already seen")
    p.add_argument("--no-maybe", dest="maybe", action="store_false",
                   default=os.getenv("MAYBE", "true").lower() != "false",
                   help="Don't send 'Maybe 5x112' alerts for VW/Audi/Mercedes/Skoda wheels")
    p.add_argument("--test-notify", action="store_true")
    return p.parse_args(argv)


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = parse_args(argv)
    if args.test_notify:
        scraper.send("Marketplace watcher test", "Notifications are working.")
        return 0

    watches = load_watches(args)
    log.info("Watching: %s", ", ".join(w.query for w in watches))
    session = requests.Session()
    load_session_cookies(session, Path(args.state).parent / "cookies.json", Path(args.cookie_file))

    while True:
        if in_quiet_hours(args.quiet_hours):
            log.info("Quiet hours, skipping this check")
        else:
            try:
                run_once(args, watches, session)
            except (requests.ConnectionError, requests.Timeout) as e:
                # Phone briefly offline (Wi-Fi/data switch, Doze): retry soon, quietly.
                log.warning("No internet connection (%s), retrying soon", type(e).__name__)
                if not args.interval:
                    return 1
                retry = random.uniform(60, 120)
                log.info("Next check in %.1f min", retry / 60)
                time.sleep(retry)
                continue
            except Exception:
                log.exception("Check failed")
                if not args.interval:
                    return 1
            args.notify_all = False
        if not args.interval:
            return 0
        delay = args.interval * 60 * random.uniform(0.75, 1.25)
        log.info("Next check in %.1f min", delay / 60)
        time.sleep(delay)


if __name__ == "__main__":
    sys.exit(main())
