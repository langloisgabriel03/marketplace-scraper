import json
from datetime import datetime

import requests

import phone_watcher as pw
import scraper

P = scraper.compile_pattern(scraper.DEFAULT_PATTERN)

SEARCH_BLOB = {"data": {"marketplace_search": {"feed_units": {"edges": [
    {"node": {"listing": {"id": "111", "marketplace_listing_title": "Mags VW 5x112"}}},
    {"node": {"listing": {"id": "222", "marketplace_listing_title": "Mags 17po"}}},
]}}}}


class FakeResponse:
    def __init__(self, text, url="https://www.facebook.com/marketplace/"):
        self.text, self.url = text, url

    def raise_for_status(self):
        pass


def html_with(blob, extra=""):
    return f'<html>{extra}<script type="application/json" data-sjs>{json.dumps(blob)}</script></html>'


def make_session(pages, posts=None):
    s = requests.Session()
    s.cookies.set("c_user", "42", domain=".facebook.com", path="/")
    s.get = lambda url, timeout=None: pages(url)
    s.post = lambda url, **kw: posts(url, **kw)
    return s


def args_for(tmp_path, *extra):
    return pw.parse_args(["--state", str(tmp_path / "state" / "seen.json"), "--interval", "0", *extra])


def capture(monkeypatch):
    sent = []
    monkeypatch.setattr(scraper, "send", lambda title, body, click="": sent.append(title))
    monkeypatch.setattr(scraper, "notify", lambda l: sent.append("NEW " + l.id))
    monkeypatch.setattr(pw.time, "sleep", lambda s: None)
    return sent


def test_finds_matches_in_title_and_description(tmp_path, monkeypatch):
    sent = capture(monkeypatch)

    def pages(url):
        if "/item/222" in url:
            return FakeResponse('<meta property="og:description" content="Boulonnage 5 x 112" />')
        return FakeResponse(html_with(SEARCH_BLOB))

    args = args_for(tmp_path)
    pw.run_once(args, P, make_session(pages))           # first run: summary only
    assert sent == ["Marketplace watcher started (phone)"]
    sent.clear()
    pw.run_once(args_for(tmp_path, "--notify-all"), P, make_session(pages))
    assert sorted(sent) == ["NEW 111", "NEW 222"]
    assert (tmp_path / "state" / "cookies.json").exists()


def test_graphql_fallback_when_page_has_no_results(tmp_path, monkeypatch):
    capture(monkeypatch)
    posted = {}

    def pages(url):
        return FakeResponse('<html>"DTSGInitialData",[],{"token":"DT"} "LSD",[],{"token":"LS"}</html>')

    def posts(url, data=None, headers=None, timeout=None):
        posted.update(data)
        return FakeResponse(json.dumps(SEARCH_BLOB))

    fb = pw.PhoneScraper(args_for(tmp_path), make_session(pages, posts))
    assert set(fb.search()) == {"111", "222"}
    assert posted["fb_dtsg"] == "DT" and posted["lsd"] == "LS" and posted["av"] == "42"


def test_login_redirect_alerts_once(tmp_path, monkeypatch):
    sent = capture(monkeypatch)
    pages = lambda url: FakeResponse("", url="https://www.facebook.com/checkpoint/123/")
    for _ in range(2):
        pw.run_once(args_for(tmp_path), P, make_session(pages))
    assert sent == ["Marketplace watcher: Facebook login needed"]


def test_cookie_roundtrip(tmp_path):
    initial = tmp_path / "cookies.txt"
    initial.write_text("c_user=42; xs=abc")
    saved = tmp_path / "state" / "cookies.json"
    s = requests.Session()
    pw.load_session_cookies(s, saved, initial)
    s.cookies.set("xs", "rotated", domain=".facebook.com", path="/")
    pw.save_session_cookies(s, saved)
    s2 = requests.Session()
    pw.load_session_cookies(s2, saved, initial)
    assert s2.cookies.get("xs") == "rotated" and s2.cookies.get("c_user") == "42"


def test_quiet_hours():
    assert pw.in_quiet_hours("1-6", datetime(2026, 1, 1, 3))
    assert not pw.in_quiet_hours("1-6", datetime(2026, 1, 1, 6))
    assert pw.in_quiet_hours("23-5", datetime(2026, 1, 1, 0))
    assert not pw.in_quiet_hours("", datetime(2026, 1, 1, 3))
