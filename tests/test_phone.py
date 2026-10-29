import json
from datetime import datetime

import requests

import phone_watcher as pw
import scraper

P = scraper.compile_pattern(scraper.DEFAULT_PATTERN)
W5 = [pw.Watch("5x112", P, maybe=True)]

SEARCH_BLOB = {"data": {"marketplace_search": {"feed_units": {"edges": [
    {"node": {"listing": {"id": "111", "marketplace_listing_title": "Mags VW 5x112"}}},
    {"node": {"listing": {"id": "222", "marketplace_listing_title": "Mags 17po"}}},
]}}}}


class FakeResponse:
    def __init__(self, text, url="https://www.facebook.com/marketplace/"):
        self.text, self.url, self.status_code = text, url, 200

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
    monkeypatch.setattr(scraper, "notify", lambda l, maybe="", label="5x112": sent.append(
        ("MAYBE " if maybe else "NEW ") + l.id))
    monkeypatch.setattr(pw.time, "sleep", lambda s: None)
    return sent


def test_finds_matches_in_title_and_description(tmp_path, monkeypatch):
    sent = capture(monkeypatch)

    def pages(url):
        if "/item/222" in url:
            return FakeResponse('<meta property="og:description" content="Boulonnage 5 x 112" />')
        return FakeResponse(html_with(SEARCH_BLOB))

    args = args_for(tmp_path)
    pw.run_once(args, W5, make_session(pages))           # first run: summary only
    assert sent == ["Marketplace watcher: new searches started"]
    sent.clear()
    pw.run_once(args_for(tmp_path, "--notify-all"), W5, make_session(pages))
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
    assert set(fb.search("5x112")) == {"111", "222"}
    assert posted["fb_dtsg"] == "DT" and posted["lsd"] == "LS" and posted["av"] == "42"


def test_login_redirect_alerts_once(tmp_path, monkeypatch):
    sent = capture(monkeypatch)
    pages = lambda url: FakeResponse("", url="https://www.facebook.com/checkpoint/123/")
    for _ in range(2):
        pw.run_once(args_for(tmp_path), W5, make_session(pages))
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


def test_maybe_alert_for_vw_wheels(tmp_path, monkeypatch):
    sent = capture(monkeypatch)
    blob = {"data": {"marketplace_search": {"feed_units": {"edges": [
        {"node": {"listing": {"id": "333", "marketplace_listing_title": "Mags volkswagen 17\""}}},
        {"node": {"listing": {"id": "444", "marketplace_listing_title": "Mags VW 5x100"}}},
    ]}}}}
    pages = lambda url: FakeResponse(html_with(blob))
    pw.run_once(args_for(tmp_path, "--notify-all"), W5, make_session(pages))
    assert sent == ["MAYBE 333"]
    sent.clear()
    pw.run_once(args_for(tmp_path, "--notify-all", "--no-maybe"), W5, make_session(pages))
    assert sent == []


def test_offline_retries_soon_without_traceback(tmp_path, monkeypatch, caplog):
    sleeps = []

    def offline(*a, **k):
        raise requests.ConnectionError("Failed to resolve 'www.facebook.com'")

    calls = iter([offline, KeyboardInterrupt])

    def fake_run_once(args, watches, session):
        step = next(calls)
        if step is KeyboardInterrupt:
            raise KeyboardInterrupt
        step()

    (tmp_path / "cookies.txt").write_text("c_user=42; xs=abc")
    monkeypatch.setattr(pw, "run_once", fake_run_once)
    monkeypatch.setattr(pw.time, "sleep", sleeps.append)
    args = ["--state", str(tmp_path / "state" / "seen.json"), "--cookie-file",
            str(tmp_path / "cookies.txt"), "--quiet-hours", "", "--interval", "10"]
    try:
        pw.main(args)
    except KeyboardInterrupt:
        pass
    assert len(sleeps) == 1 and 60 <= sleeps[0] <= 120
    assert "No internet connection" in caplog.text and "Traceback" not in caplog.text


def test_parse_watches_and_keyword_matching():
    watches = pw.parse_watches(open("watches.txt").read())
    assert [w.query for w in watches] == ["5x112", "simpsons", "spiderman", "audi a3", "mag audi"]
    assert watches[0].pattern and watches[0].maybe and not watches[1].maybe
    simpsons, spiderman, a3, audi_wheels = watches[1:]
    assert audi_wheels.queries == ["mag audi", "mags audi", "roue audi", "roues audi"]
    assert spiderman.queries == ["spiderman", "spider-man", "spidey"]
    assert spiderman.excludes == ("costume", "chandail", "vêtement")

    def L(title, description=""):
        return scraper.Listing(id="1", title=title, description=description)

    assert simpsons.matches(L("Figurine Simpson"))
    assert simpsons.matches(L("Lot jouets", "Les Simpsons saison 1"))
    assert spiderman.matches(L("SPIDER MAN lego"))
    assert not spiderman.matches(L("Costume Spider-Man 5 ans"))
    assert not spiderman.matches(L("Costumes Spiderman"))
    assert not spiderman.matches(L("Chandails spiderman 6 ans"))
    assert not spiderman.matches(L("Lot de VÊTEMENTS Spider-Man"))
    assert not spiderman.matches(L("Spidey", "Vetement pour enfant"))
    assert spiderman.matches(L("Figurine Spidey"))
    assert not spiderman.matches(L("Spider plant"))
    assert a3.matches(L("Audi A3 2015 Sportback"))
    assert a3.matches(L("Mags", "Viennent de mon Audi A3"))
    assert not a3.matches(L("Audi A4 2015"))
    assert audi_wheels.matches(L("4 roues Audi A4")) and audi_wheels.matches(L("Mag Audi Q5"))
    assert not audi_wheels.matches(L("Mags VW 17"))
    assert [pw.slugify(q) for q in spiderman.queries] == [
        "spiderman", "spider-man", "spidey"]


def test_multiple_watches_new_search_and_dedup(tmp_path, monkeypatch):
    sent = capture(monkeypatch)
    blobs = {
        "5x112": SEARCH_BLOB,
        "spiderman": {"data": {"marketplace_search": {"feed_units": {"edges": [
            {"node": {"listing": {"id": "555", "marketplace_listing_title": "Spider-Man lego"}}}]}}}},
        "spider": {"data": {"marketplace_search": {"feed_units": {"edges": [
            {"node": {"listing": {"id": "555", "marketplace_listing_title": "Spider-Man lego"}}},
            {"node": {"listing": {"id": "666", "marketplace_listing_title": "Spider plant"}}}]}}}},
    }
    current = {"5x112": [], "spiderman": [], "spider": []}

    def pages(url):
        if "/item/" in url:
            return FakeResponse("<html></html>")
        q = url.split("query=")[1].split("&")[0]
        edges = [e for e in blobs[q]["data"]["marketplace_search"]["feed_units"]["edges"]
                 if e["node"]["listing"]["id"] in current[q]]
        return FakeResponse(html_with({"data": {"marketplace_search": {"feed_units": {"edges": edges}}}}))

    watches = [pw.Watch("5x112", P, True), pw.Watch("spiderman"), pw.Watch("spider")]
    empty_api = lambda url, **kw: FakeResponse("{}")
    state = tmp_path / "state"
    state.mkdir()
    scraper.save_seen(state / "seen.json", {"111"})          # old single-search state
    current["5x112"] = ["111"]

    pw.run_once(args_for(tmp_path), watches, make_session(pages, empty_api))
    assert sent == ["Marketplace watcher: new searches started"]   # spiderman + spider are new
    assert (state / "seen-5x112.json").exists() and not (state / "seen.json").exists()

    sent.clear()
    current.update({"5x112": ["111", "222"], "spiderman": ["555"], "spider": ["555", "666"]})
    pw.run_once(args_for(tmp_path), watches, make_session(pages, empty_api))
    # 222 has no 5x112; 555 matches both spider searches but is sent once
    assert sent == ["NEW 555", "NEW 666"]


def test_detail_budget_defers_extra_listings(tmp_path, monkeypatch):
    capture(monkeypatch)
    opened = []

    def pages(url):
        if "/item/" in url:
            opened.append(url)
            return FakeResponse("<html></html>")
        return FakeResponse(html_with(SEARCH_BLOB))

    state = tmp_path / "state"
    scraper.save_seen(state / "seen-5x112.json", set())
    pw.run_once(args_for(tmp_path, "--max-details", "0"), W5, make_session(pages))
    assert opened == []
    assert scraper.load_seen(state / "seen-5x112.json") == {"111"}   # 222 retried next time


def test_notify_all_sends_each_listing_once_across_spellings(tmp_path, monkeypatch):
    sent = capture(monkeypatch)
    blob = {"data": {"marketplace_search": {"feed_units": {"edges": [
        {"node": {"listing": {"id": "555", "marketplace_listing_title": "Spider-Man lego"}}}]}}}}
    pages = lambda url: FakeResponse(html_with(blob))
    watch = pw.Watch("spiderman", alternatives=("spider-man",))
    pw.run_once(args_for(tmp_path, "--notify-all"), [watch], make_session(pages))
    assert sent == ["NEW 555"]
