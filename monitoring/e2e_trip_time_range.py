#!/usr/bin/env python3
"""End-to-end check: the trips dashboard time range follows the selected trip.

Drives a real browser against a running Grafana and records every scenario in
a JSON artifact. Scenarios:

1. open a trip with the default relative range -> range becomes the trip window
2. select another trip in the dropdown -> range becomes the new trip window
3. drag-zoom inside a chart -> zoom is kept, not reset
4. press refresh -> zoom is kept
5. reload the zoomed URL -> zoom is kept
6. browser back -> previous trip window
7. open a relative range such as "Last 7 days" -> range becomes the trip window

Usage:
    GRAFANA_URL=https://grafana.example GRAFANA_USER=admin GRAFANA_PASSWORD=... \
        python3 monitoring/e2e_trip_time_range.py --device ZKUCALJ0 --out result.json

Requires Python Playwright and a Chrome or Chromium binary.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.request
from base64 import b64encode
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import Page, sync_playwright

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_dashboard import TRIP_WINDOW_PAD_MS  # noqa: E402

SETTLE_MS = 6000
PICKER = "data-testid Dashboard template variables Variable Value DropDown value link text "
CHART = "data-testid Panel header Road speed and engine speed"


def api(base: str, auth: str, path: str, body: dict | None = None) -> dict:
    request = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": "Basic " + auth, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def trip_windows(base: str, auth: str, device: str) -> list[tuple[str, int, int]]:
    """Return the two newest trips with samples, as (trip, window start, window end)."""
    sql = (
        "SELECT trip_id, timeline_start_ms, timeline_end_ms FROM trip "
        f"WHERE device_id = '{device}' AND timeline_start_ms IS NOT NULL AND timeline_end_ms IS NOT NULL "
        "ORDER BY timeline_start_ms DESC LIMIT 2"
    )
    query = {"refId": "A", "datasource": {"uid": "freematics-history"}, "queryText": sql, "rawQueryText": sql,
             "queryType": "table", "timeColumns": []}
    result = api(base, auth, "/api/ds/query", {"queries": [query], "from": "now-1h", "to": "now"})
    trips, starts, ends = result["results"]["A"]["frames"][0]["data"]["values"]
    return [(trip, int(start) - TRIP_WINDOW_PAD_MS, int(end) + TRIP_WINDOW_PAD_MS)
            for trip, start, end in zip(trips, starts, ends)]


def url_range(page: Page) -> tuple[str, str]:
    query = parse_qs(urlparse(page.url).query)
    return query.get("from", [""])[0], query.get("to", [""])[0]


def as_ms(value: str) -> int | None:
    if value.isdigit():
        return int(value)
    try:
        return int(dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--device", required=True)
    parser.add_argument("--dashboard", default="freematics-trips")
    parser.add_argument("--chrome", default="/usr/bin/google-chrome")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    base = os.environ["GRAFANA_URL"].rstrip("/")
    user, password = os.environ["GRAFANA_USER"], os.environ["GRAFANA_PASSWORD"]
    auth = b64encode(f"{user}:{password}".encode()).decode()
    version = api(base, auth, "/api/health")["version"]
    (trip_b, b_start, b_end), (trip_a, a_start, a_end) = trip_windows(base, auth, args.device)
    results: list[dict] = []

    def record(name: str, page: Page, passed: bool, expected: str) -> None:
        results.append({"scenario": name, "passed": passed, "expected": expected, "url": page.url})
        print(f"[e2e] {'PASS' if passed else 'FAIL'} {name}: expected {expected}; got {url_range(page)}")

    def is_window(page: Page, start: int, end: int) -> bool:
        # Grafana rewrites epoch-millisecond URL values as ISO timestamps.
        return tuple(as_ms(value) for value in url_range(page)) == (start, end)

    dashboard = f"{base}/d/{args.dashboard}?orgId=1&var-device={args.device}"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=args.chrome, args=["--headless=new"])
        page = browser.new_context(viewport={"width": 1800, "height": 1100}).new_page()
        page.goto(f"{base}/login")
        page.fill("input[name=user]", user)
        page.fill("input[name=password]", password)
        page.click("button[type=submit]")
        page.wait_for_url(lambda url: "/login" not in url)

        page.goto(f"{dashboard}&var-trip={trip_a}&from=now-90d&to=now")
        page.wait_for_timeout(SETTLE_MS)
        record("open trip with relative range", page, is_window(page, a_start, a_end), f"{trip_a} window")

        page.get_by_test_id(PICKER + trip_a).click()
        page.get_by_text(trip_b, exact=True).last.click()
        page.wait_for_timeout(SETTLE_MS)
        record("select another trip in dropdown", page, is_window(page, b_start, b_end), f"{trip_b} window")

        box = page.get_by_test_id(CHART).first.bounding_box()
        y = box["y"] + box["height"] * 0.45
        page.mouse.move(box["x"] + box["width"] * 0.3, y)
        page.mouse.down()
        page.mouse.move(box["x"] + box["width"] * 0.6, y, steps=10)
        page.mouse.up()
        page.wait_for_timeout(SETTLE_MS)
        zoomed = url_range(page)
        zoom_from, zoom_to = as_ms(zoomed[0]), as_ms(zoomed[1])
        inside = zoom_from is not None and zoom_to is not None and b_start <= zoom_from < zoom_to <= b_end
        record("drag-zoom inside chart", page, inside and not is_window(page, b_start, b_end),
               f"sub-range inside {trip_b} window")

        page.get_by_test_id("data-testid RefreshPicker run button").click()
        page.wait_for_timeout(SETTLE_MS)
        record("refresh keeps zoom", page, url_range(page) == zoomed, f"unchanged {zoomed}")

        page.reload()
        page.wait_for_timeout(SETTLE_MS)
        record("reload keeps zoom", page, url_range(page) == zoomed, f"unchanged {zoomed}")

        page.go_back()
        page.wait_for_timeout(SETTLE_MS)
        record("browser back", page, is_window(page, b_start, b_end), f"{trip_b} window")

        page.goto(f"{dashboard}&var-trip={trip_b}&from=now-7d&to=now")
        page.wait_for_timeout(SETTLE_MS)
        record("relative range resets to trip", page, is_window(page, b_start, b_end), f"{trip_b} window")
        browser.close()

    artifact = {
        "command": " ".join([os.path.basename(sys.executable)] + sys.argv),
        "ran_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "grafana_url": base,
        "grafana_version": version,
        "dashboard": args.dashboard,
        "trips": {trip_a: [a_start, a_end], trip_b: [b_start, b_end]},
        "results": results,
        "passed": all(item["passed"] for item in results),
    }
    with open(args.out, "w") as handle:
        json.dump(artifact, handle, indent=2)
        handle.write("\n")
    print(f"[e2e] {'all passed' if artifact['passed'] else 'FAILED'}; artifact: {args.out}")
    return 0 if artifact["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
