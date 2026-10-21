import json

import scraper
from scraper import Listing, compile_pattern, matches

P = compile_pattern(scraper.DEFAULT_PATTERN)


def test_pattern_variants():
    for text in ["Mags 5x112 17po", "5 x 112", "5X112", "5*112", "pattern 5×112", "(5x112)"]:
        assert P.search(text), text
    for text in ["15x112", "5x1120", "5x114.3", "5x120", "112x5"]:
        assert not P.search(text), text


def test_matches_title_or_description():
    assert matches(Listing(id="1", title="Roues VW 5x112"), P)
    assert matches(Listing(id="1", title="Mags 18po", description="Boulonnage 5 x 112"), P)
    assert not matches(Listing(id="1", title="Mags 18po", description="5x114.3"), P)


SEARCH_BLOB = {
    "data": {"marketplace_search": {"feed_units": {"edges": [
        {"node": {"listing": {
            "id": "123456789",
            "marketplace_listing_title": "Mags Audi 5x112",
            "listing_price": {"formatted_amount": "500 $"},
            "location": {"reverse_geocode": {"city": "Québec", "state": "QC"}},
            "primary_listing_photo": {"image": {"uri": "https://img"}},
        }}},
        {"node": {"listing": {"id": "987", "marketplace_listing_title": "Pneus hiver"}}},
    ]}}}
}


def test_listings_from_json():
    found = scraper.listings_from_json(SEARCH_BLOB)
    assert set(found) == {"123456789", "987"}
    l = found["123456789"]
    assert l.price == "500 $" and l.location == "Québec, QC" and l.image == "https://img"
    assert l.url == "https://www.facebook.com/marketplace/item/123456789/"


def test_graphql_multiline_and_html():
    body = "for (;;);" + json.dumps(SEARCH_BLOB) + "\n" + json.dumps({"extensions": {}})
    blobs = list(scraper.json_blobs_from_graphql(body))
    assert len(blobs) == 2
    html = f'<html><script type="application/json" data-sjs>{json.dumps(SEARCH_BLOB)}</script></html>'
    blobs = list(scraper.json_blobs_from_html(html))
    assert "123456789" in scraper.listings_from_json(blobs[0])


def test_description_from_json():
    blob = {"a": [{"id": "1", "redacted_description": {"text": "autre"}},
                  {"id": "42", "redacted_description": {"text": "Jantes 5x112"}}]}
    assert scraper.description_from_json(blob, "42") == "Jantes 5x112"


def test_search_url():
    url = scraper.search_url("quebec", "5x112", 40, 46.85, -71.33)
    assert url.startswith("https://www.facebook.com/marketplace/quebec/search/?query=5x112")
    assert "radius=40" in url and "latitude=46.85" in url


def test_seen_roundtrip(tmp_path):
    path = tmp_path / "s" / "seen.json"
    assert scraper.load_seen(path) is None
    scraper.save_seen(path, {"3", "1", "2"})
    assert scraper.load_seen(path) == {"1", "2", "3"}


def test_login_wall_alerts_once(tmp_path, monkeypatch):
    class Blocked:
        def __init__(self, args): pass
        def __enter__(self): return self
        def __exit__(self, *exc): pass
        def search(self): raise scraper.LoginRequired("https://www.facebook.com/login")

    sent = []
    monkeypatch.setattr(scraper, "Scraper", Blocked)
    monkeypatch.setattr(scraper, "send", lambda title, body, click="": sent.append(title))
    args = scraper.parse_args(["--state", str(tmp_path / "seen.json")])
    scraper.run_once(args, P)
    scraper.run_once(args, P)
    assert len(sent) == 1 and "login" in sent[0]


def test_parse_cookie_header():
    cookies = scraper.parse_cookies("cookie: c_user=123; xs=ab%3Acd=; datr=zz")
    assert [(c["name"], c["value"]) for c in cookies] == [
        ("c_user", "123"), ("xs", "ab%3Acd="), ("datr", "zz")]
    assert scraper.parse_cookies('[{"name": "xs", "value": "1"}]')[0]["name"] == "xs"
