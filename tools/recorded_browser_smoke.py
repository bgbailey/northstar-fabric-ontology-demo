"""Exercise the explicit recorded-native viewer; never a live Fabric test."""

import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

root = Path(__file__).resolve().parents[1]
output = root / ".local" / "browser" / "recorded-native"
output.mkdir(parents=True, exist_ok=True)
errors, requests = [], []

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(channel="msedge", headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 1100})
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("request", lambda request: requests.append(request.url))
    page.goto("http://127.0.0.1:8765", wait_until="networkidle")
    expect(page.get_by_text("RECORDED LIVE", exact=True)).to_be_visible()
    expect(page.get_by_text("NOT RECORDED", exact=True)).to_be_visible()
    expect(page.get_by_text("80,336", exact=True)).to_be_visible()
    expect(page.locator(".react-flow__node")).to_have_count(4)
    expect(page.locator(".react-flow__edge")).to_have_count(3)
    expect(page.get_by_role("heading", name="8 metrics on this entity", exact=True)).to_be_visible()
    page.get_by_label("Account Month, Finance source", exact=True).click()
    expect(page.get_by_role("heading", name="6 metrics on this entity", exact=True)).to_be_visible()
    page.locator(".metric-list summary").filter(has_text="Recognized Service Revenue").click()
    expect(page.locator(".metric-list")).to_contain_text("Home table")
    page.get_by_role("button", name="Full ontology", exact=True).click()
    expect(page.locator(".react-flow__node")).to_have_count(13)
    expect(page.locator(".react-flow__edge")).to_have_count(21)
    page.get_by_role("button", name="Account path", exact=True).click()
    page.locator(".evidence-drawer > summary").click()
    expect(page.locator(".evidence-drawer tbody tr")).to_have_count(10)
    expect(page.locator(".evidence-drawer pre")).to_have_count(2)
    expect(page.locator(".evidence-drawer")).to_contain_text("[Active Lines EOM]")
    expect(page.locator(".evidence-drawer")).to_contain_text("[Recognized Service Revenue]")
    page.screenshot(path=str(output / "light.png"), full_page=True)
    page.goto("http://127.0.0.1:8765/?scoutTheme=dark", wait_until="networkidle")
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")
    expect(page.locator(".react-flow__node")).to_have_count(4)
    page.screenshot(path=str(output / "dark.png"), full_page=True)
    browser.close()

if errors:
    raise AssertionError(errors)
if any(urlsplit(url).hostname not in {"localhost", "127.0.0.1"} for url in requests):
    raise AssertionError("Recorded viewing unexpectedly attempted a non-local request")
receipt = {
    "lane": "RECORDED_NATIVE_DEMO_BROWSER_NOT_LIVE_EXECUTION",
    "completedAt": datetime.now(timezone.utc).isoformat(), "status": "PASS",
    "checks": {"focusNodes": 4, "focusEdges": 3, "allNodes": 13, "allEdges": 21,
               "selectedMetricCounts": [8, 6], "accountRows": 10, "displayedDax": 2,
               "themes": ["light", "dark"], "nonLocalRequests": 0, "browserErrors": errors},
}
(output / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
print(json.dumps(receipt, indent=2))
