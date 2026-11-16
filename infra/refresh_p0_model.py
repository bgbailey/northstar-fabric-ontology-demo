"""One-shot approved model refresh with resumable, read-only monitoring."""

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx

if __package__:
    from .fabric_p0 import Author, load
    from .renewal import powerbi_path
else:
    from fabric_p0 import Author, load
    from renewal import powerbi_path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--private-dir", type=Path, required=True)
    parser.add_argument("--model", choices=["subscriptions", "finance"], required=True)
    parser.add_argument("--poll-only", action="store_true")
    args = parser.parse_args()
    author = Author(args.private_dir)
    try:
        item = author.owned(args.model)
        workspace = author.owned("workspace")["id"]
        token = author.az_json(
            "account", "get-access-token", "--resource", "https://analysis.windows.net/powerbi/api",
            "--tenant", author.approval["tenantId"],
        )["accessToken"]
        base = f"https://api.powerbi.com/v1.0/myorg/groups/{workspace}/datasets/{item['id']}/refreshes"
        state_path = author.private / f"p0-{args.model}-refresh-request.json"
        with httpx.Client(headers={"Authorization": "Bearer " + token}, timeout=45,
                          follow_redirects=False) as client:
            if not args.poll_only:
                if state_path.exists():
                    raise RuntimeError("Refresh already attempted; monitor the recorded request")
                prior = client.get(base, params={"$top": 10})
                prior.raise_for_status()
                existing = prior.json()["value"]
                if any(r["status"] in ("Unknown", "InProgress") for r in existing):
                    raise RuntimeError("Existing refresh is active; no duplicate submission")
                state = {
                    "model": args.model, "startedAt": datetime.now(timezone.utc).isoformat(),
                    "status": "SUBMITTING", "priorRequestIds": [r["requestId"] for r in existing],
                    "expectedRefreshType": "ViaApi",
                }
                author.save(state_path.name, state)
                response = client.post(base, json={"notifyOption": "NoNotification"})
                state["httpStatus"] = response.status_code
                state["requestId"] = (
                    urlparse(response.headers["Location"]).path.rsplit("/", 1)[-1]
                    if response.headers.get("Location") else None
                )
                state["retryAfter"] = response.headers.get("Retry-After", "10")
                state["response"] = response.json() if response.content else None
                state["status"] = "ACCEPTED" if response.status_code == 202 else "REJECTED"
                state["location"] = response.headers.get("Location")
                author.save(state_path.name, state)
                response.raise_for_status()
                if response.status_code != 202:
                    raise RuntimeError("Unexpected refresh acceptance status")
            state = load(state_path)
            location = state.get("location")
            if (
                not location or not state.get("requestId")
                or powerbi_path(location) != powerbi_path(base) + "/" + state["requestId"]
            ):
                raise RuntimeError("Exact accepted refresh identity/Location absent; reconcile, never adopt history")
            stop = time.monotonic() + 300
            while time.monotonic() < stop:
                if datetime.fromisoformat(author.approval["deadlineUtc"]) <= datetime.now(timezone.utc):
                    raise RuntimeError("Approved refresh window expired; no further requests")
                delay = int(state.get("retryAfter", "10"))
                if time.monotonic() + delay > stop:
                    break
                time.sleep(delay)
                response = client.get(base, params={"$top": 10})
                response.raise_for_status()
                # Direct Lake can append automatic framing entries alongside our explicit refresh.
                candidates = [
                    r for r in response.json()["value"]
                    if r["requestId"] not in state["priorRequestIds"]
                    and r["refreshType"] == state.get("expectedRefreshType", "ViaApi")
                ]
                candidates = [r for r in candidates if r["requestId"] == state["requestId"]]
                if not candidates:
                    continue
                if len(candidates) != 1:
                    raise RuntimeError("Refresh identity ambiguous; do not guess or resubmit")
                current = candidates[0]
                author.save(f"p0-{args.model}-refresh-latest.json", current)
                if current["status"] in ("Completed", "Failed", "Cancelled", "Disabled"):
                    print(json.dumps({"model": args.model, "refresh": current}, indent=2))
                    if current["status"] != "Completed":
                        raise RuntimeError("Refresh failed; inspect credentials/binding, no automatic retry")
                    return
            raise TimeoutError("Refresh still pending; use --poll-only, never a second POST")
    finally:
        author.client.close()


if __name__ == "__main__":
    main()
