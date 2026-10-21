"""Check whether Marketplace's GraphQL search answers without logging in.

Usage: python probe_graphql.py [query] [pages]
"""
import json
import sys

import requests

query = sys.argv[1] if len(sys.argv) > 1 else "5x112"
pages = int(sys.argv[2]) if len(sys.argv) > 2 else 3


def variables(cursor=None):
    v = {
        "count": 24,
        "params": {
            "bqf": {"callsite": "COMMERCE_MKTPLACE_WWW", "query": query},
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
    if cursor:
        v["cursor"] = cursor
    return v


cursor = None
total = 0
for page in range(1, pages + 1):
    r = requests.post(
        "https://www.facebook.com/api/graphql/",
        headers={
            "sec-fetch-site": "same-origin",
            "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
        },
        data={"variables": json.dumps(variables(cursor)), "doc_id": "7111939778879383"},
        timeout=30,
    )
    try:
        data = json.loads(r.text.splitlines()[0])
        feed = data["data"]["marketplace_search"]["feed_units"]
    except Exception:
        print(f"page {page}: HTTP {r.status_code}, unexpected answer:", r.text[:300])
        break
    edges = feed.get("edges") or []
    print(f"page {page}: {len(edges)} results")
    for e in edges:
        node = e.get("node") or {}
        l = node.get("listing") or {}
        if l:
            total += 1
            price = (l.get("listing_price") or {}).get("formatted_amount")
            print("  ", l.get("id"), "|", l.get("marketplace_listing_title"), "|", price)
        else:
            print("   (other:", node.get("__typename"), ")")
    info = feed.get("page_info") or {}
    if not info.get("has_next_page"):
        print("no more pages")
        break
    cursor = info.get("end_cursor")

print(f"query={query!r} listings found: {total}")
