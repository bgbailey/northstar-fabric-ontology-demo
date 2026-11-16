"""Real browser check against the running, explicitly offline localhost app."""

import json
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

root = Path(__file__).resolve().parents[1]
output = root / ".local" / "browser"
output.mkdir(parents=True, exist_ok=True)
errors = []
expected_statuses = [e["status"] for e in json.loads(
    (root / "eval" / "validation" / "register.json").read_text(encoding="utf-8")
)["experiments"]]
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(channel="msedge", headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 1100})
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto("http://127.0.0.1:8765", wait_until="networkidle")
    expect(page.get_by_text("OFFLINE_SYNTHETIC", exact=True)).to_be_visible()
    page.get_by_role("button", name="Validate and execute locally").click()
    expect(page.get_by_role("heading", name="COMPLETE", exact=False)).to_be_visible(timeout=15000)
    expect(page.locator("tbody tr")).to_have_count(10)
    expect(page.locator("details")).to_have_count(2)
    page.screenshot(path=str(output / "two-model-execution.png"), full_page=True)
    page.get_by_role("button", name="Experiments", exact=True).click()
    expect(page.locator("tbody tr")).to_have_count(24)
    assert page.locator("tbody tr td:last-child").all_text_contents() == expected_statuses
    page.get_by_role("button", name="Comparison", exact=True).click()
    expect(page.get_by_role("heading", name="Ask both approaches")).to_be_visible()
    expect(page.get_by_role("button", name="Run both approaches")).to_be_disabled()
    page.get_by_role("button", name="Execute", exact=True).click()
    page.get_by_label("Natural-language lane (approval blocked)").fill("What is revenue?")
    page.get_by_role("button", name="Check planner").click()
    expect(page.get_by_role("alert")).to_contain_text("BLOCKED_CAPABILITY")
    browser.close()
if errors:
    raise AssertionError(errors)
receipt = {
    "lane": "OFFLINE_SYNTHETIC_BROWSER",
    "completedAt": datetime.now(timezone.utc).isoformat(),
    "status": "PASS",
    "checks": {
        "two_model_rows": 10, "source_receipts": 2,
        "live_experiments_not_run": expected_statuses.count("NOT_RUN"),
        "unconfigured_planner_blocked": True,
        "browser_errors": errors,
    },
    "limitations": "Not live Fabric, DAX, source RLS/OLS or paid-model planning",
}
(output / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
print(json.dumps(receipt))
