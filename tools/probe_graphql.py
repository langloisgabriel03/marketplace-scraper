"""One-off check: does Marketplace's GraphQL search answer without logging in?"""
import json
import sys

import requests

variables = {
    "count": 24,
    "params": {
        "bqf": {"callsite": "COMMERCE_MKTPLACE_WWW", "query": "5x112"},
        "browse_request_params": {
            "commerce_enable_local_pickup": True,
            "commerce_enable_shipping": True,
            "commerce_search_and_rp_available": True,
            "commerce_search_and_rp_condition": None,
            "commerce_search_and_rp_ctime_days": None,
            "filter_location_latitude": 46.853,
            "filter_location_longitude": -71.329,
            "filter_price_lower_bound": 0,
            "filter_price_upper_bound": 214748364700,
            "filter_radius_km": 40,
        },
        "custom_request_params": {"surface": "SEARCH"},
    },
}
r = requests.post(
    "https://www.facebook.com/api/graphql/",
    headers={
        "sec-fetch-site": "same-origin",
        "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    },
    data={"variables": json.dumps(variables), "doc_id": "7111939778879383"},
    timeout=30,
)
print("HTTP", r.status_code)
body = r.text
print(body[:800])
try:
    edges = json.loads(body.splitlines()[0])["data"]["marketplace_search"]["feed_units"]["edges"]
except Exception as e:
    print("Could not parse listings:", repr(e))
    sys.exit(0)
for e in edges:
    l = (e.get("node") or {}).get("listing") or {}
    if l:
        print(l.get("id"), "|", l.get("marketplace_listing_title"), "|",
              (l.get("listing_price") or {}).get("formatted_amount"))
print("listings:", len(edges))
