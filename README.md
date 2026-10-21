# Marketplace scraper: 5x112 watcher

Watches Facebook Marketplace for listings with **"5x112"** in the title or
description within **40 km of G2M 1J5 (Québec, QC)**, and sends you a push
notification for each new one.

How it works:
1. It opens the Marketplace search for `5x112` (Québec, 40 km radius, newest first) in headless Chromium.
2. It reads the results from Facebook's own JSON, with page links as a fallback.
3. For each listing it hasn't seen before, it checks the title and opens the listing to read the description.
4. If `5x112` appears (it also catches `5 x 112`, `5X112`, `5*112` and `5×112`, but not `15x112` or `5x1120`), it notifies you.
5. Seen listing IDs are saved in `state/seen.json`, so you're never notified twice for the same listing.

On the first run, listings that are already online are marked as seen and you get one
"watcher started" message. Pass `--notify-existing` to get them all instead.

## 1. Set up notifications (pick one or more)

**ntfy (easiest, free, no account):** install the **ntfy** app (iOS/Android) and subscribe
to a hard-to-guess topic name, e.g. `gabriel-5x112-a8f3k`. Then set `NTFY_TOPIC` to that name.

**Telegram:** set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
**Discord:** set `DISCORD_WEBHOOK_URL`.

## 2a. Run on your own computer (most reliable)

Facebook trusts home internet connections more than cloud servers, so this is the best option.

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install chromium

export NTFY_TOPIC=gabriel-5x112-a8f3k               # Windows: set NTFY_TOPIC=...
python scraper.py --test-notify                     # check that your phone gets it
python scraper.py --interval 10                     # check every 10 minutes, forever
```

## 2b. Run on GitHub Actions (free, no computer needed)

`.github/workflows/watch.yml` runs every 15 minutes. In the repo, go to
**Settings → Secrets and variables → Actions** and add `NTFY_TOPIC` (or the Telegram/Discord secrets).
You can start a run by hand from the **Actions** tab.

Facebook sometimes hides results from GitHub's servers behind a login wall. If runs keep
finding `0 listings`, add your Facebook cookies (see below) as the `FB_COOKIES_JSON` secret,
or run it on your own computer.

## Facebook cookies (optional)

Searching while logged out usually works from home, but GitHub's servers get a login page.
Sign in to Facebook in your browser, then either:

- **DevTools:** press F12 → **Network** tab → reload facebook.com → click the first request
  → **Request Headers** → copy the whole value of `cookie` (starts with `datr=` or `sb=`), or
- **Cookie-Editor extension:** Export → JSON.

Put it in the `FB_COOKIES_JSON` secret (or a `cookies.json` file next to `scraper.py`).
Stay logged in on that browser: logging out kills the cookies. When they expire you get a
"Facebook login needed" notification. Consider using a secondary account: automated access
goes against Facebook's terms, and the account could be restricted.

## Options

| Flag / env var | Default | |
|---|---|---|
| `--query` / `QUERY` | `5x112` | Text typed into Marketplace search |
| `--pattern` / `PATTERN` | 5x112 regex | Must appear in the title or description |
| `--radius` / `RADIUS_KM` | `40` | Search radius in km |
| `--lat`, `--lon` / `LAT`, `LON` | G2M 1J5 | Search centre |
| `--location-slug` / `LOCATION_SLUG` | `quebec` | Marketplace city in the URL |
| `--interval` / `INTERVAL_MIN` | `0` | Minutes between checks (0 = run once) |
| `--scrolls` / `SCROLLS` | `3` | How many times to scroll for more results |
| `--headful` | off | Show the browser window (useful for debugging) |

## Tests

```bash
pip install pytest && python -m pytest
```
