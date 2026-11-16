"""Verify actual local SLA streaming, not a live Fabric or application-model run."""

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

output = Path(__file__).resolve().parents[1] / ".local/browser/sla-engine"
output.mkdir(parents=True, exist_ok=True)
errors, requests = [], []
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(channel="msedge", headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 1100})
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("request", lambda request: requests.append(request.url))
    page.goto("http://127.0.0.1:8766", wait_until="networkidle")
    expect(page.get_by_text("OFFLINE_SYNTHETIC", exact=True)).to_be_visible()
    page.get_by_role("button", name="Load SLA fixture", exact=True).click()
    expect(page.get_by_label("Plan JSON")).to_have_value(re.compile("contract_key"))
    contract_nodes = len(json.loads(page.get_by_label("Plan JSON").input_value())["nodes"])
    page.get_by_role("button", name="Validate and execute locally", exact=True).click()
    expect(page.get_by_role("heading", name="COMPLETE", exact=False)).to_be_visible(timeout=30000)
    expect(page.locator(".execution-node.complete")).to_have_count(contract_nodes)
    expect(page.locator(".execution-result tbody tr")).to_have_count(2)
    page.screenshot(path=str(output / "july-contracts.png"), full_page=True)

    page.get_by_label("Result grain").select_option("accounts")
    page.get_by_role("button", name="Load SLA fixture", exact=True).click()
    expect(page.get_by_label("Plan JSON")).to_have_value(re.compile("finance"))
    account_nodes = len(json.loads(page.get_by_label("Plan JSON").input_value())["nodes"])
    page.get_by_role("button", name="Validate and execute locally", exact=True).click()
    expect(page.get_by_role("heading", name="COMPLETE", exact=False)).to_be_visible(timeout=30000)
    expect(page.locator(".execution-node.complete")).to_have_count(account_nodes)
    expect(page.locator(".execution-result tbody tr")).to_have_count(2)
    expect(page.get_by_text("finance.contribution_margin", exact=True)).to_be_visible()
    expect(page.get_by_text("-1300", exact=True)).to_be_visible()
    expect(page.get_by_text("-400", exact=True)).to_be_visible()
    page.screenshot(path=str(output / "july-accounts.png"), full_page=True)

    page.get_by_label("Service month").select_option("2026-08")
    page.get_by_role("button", name="Load SLA fixture", exact=True).click()
    expect(page.get_by_label("Plan JSON")).to_have_value(re.compile("2026-08-01"))
    page.get_by_role("button", name="Validate and execute locally", exact=True).click()
    expect(page.get_by_role("heading", name="BLOCKED_CAPABILITY", exact=False)).to_be_visible(timeout=30000)
    expect(page.get_by_text("Empty cohort: target source not queried; no target clock or total observed", exact=True)).to_be_visible()
    expect(page.locator(".execution-node.failed")).to_have_count(1)
    page.screenshot(path=str(output / "august-empty.png"), full_page=True)
    browser.close()
if errors:
    raise AssertionError(errors)
if any(urlsplit(url).hostname not in {"127.0.0.1", "localhost"} for url in requests):
    raise AssertionError("Local fixture UI attempted a non-local request")
receipt = {
    "status": "PASS", "lane": "OFFLINE_SYNTHETIC_BROWSER",
    "completedAt": datetime.now(timezone.utc).isoformat(),
    "checks": {
        "july_contract_view": "COMPLETE", "july_account_view": "COMPLETE",
        "august_empty_view": "BLOCKED_CAPABILITY_NO_FINANCIAL_TOTAL",
        "real_node_events": True, "nonLocalRequests": 0, "browserErrors": errors,
    },
    "limitation": "Actual local SQL engine fixture. Not LLM planning, native SLA construction or live Fabric execution.",
}
(output / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
print(json.dumps(receipt, indent=2))
