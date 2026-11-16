"""Apply only the explicitly approved single-BFF registration; never grant admin consent."""

import argparse
import hashlib
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import httpx

from telco_iq.approval import private_config_path
from telco_iq.auth import ReaderConfig

ROOT = Path(__file__).resolve().parents[1]


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--private-dir", type=Path, required=True)
    args = parser.parse_args()
    private = private_config_path(args.private_dir, ROOT)
    approval = read(private / "registration-approval.json")
    settings = read(private / "staged-settings.json")
    content = (private / "application-request.json").read_bytes()
    if (
        approval.get("approved") is not True
        or hashlib.sha256(content).hexdigest() != approval["applicationRequestSha256"]
        or approval["applicationRequestSha256"] != settings["application_request_sha256"]
        or approval["certificateThumbprint"] != settings["certificate_thumbprint"]
        or datetime.fromisoformat(approval["deadlineUtc"]) <= datetime.now(timezone.utc)
    ):
        raise RuntimeError("Approval/artifact/expiry mismatch")
    body = json.loads(content)
    azure = shutil.which("az")

    def az(*args):
        result = subprocess.run([azure, *args, "-o", "json", "--only-show-errors"],
                                capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError("Author CLI authentication failed; no credential fallback")
        return json.loads(result.stdout)

    account = az("account", "show")
    if account["tenantId"] != settings["tenant_id"] or account["user"]["name"] != settings["author_user"]:
        raise RuntimeError("Registration requires the verified DEV author")
    token = az("account", "get-access-token", "--resource", "https://graph.microsoft.com",
               "--tenant", settings["tenant_id"])["accessToken"]

    def save(name, value):
        (private / name).write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")

    with httpx.Client(base_url="https://graph.microsoft.com/v1.0/",
                      headers={"Authorization": "Bearer " + token}, timeout=45,
                      follow_redirects=False) as client:
        existing = client.get("applications", params={
            "$filter": "displayName eq '" + body["displayName"].replace("'", "''") + "'",
            "$select": "id,appId,displayName",
        })
        existing.raise_for_status()
        if existing.json()["value"] or (private / "registration-intent.json").exists():
            raise RuntimeError("Registration exists or was attempted; inspect receipts, never duplicate it")
        save("registration-intent.json", {
            "startedAt": datetime.now(timezone.utc).isoformat(),
            "requestSha256": approval["applicationRequestSha256"],
        })
        created = client.post("applications", json=body)
        save("application-create-receipt.json", {"status": created.status_code, "body": created.json()})
        created.raise_for_status()
        application = created.json()
        sp_result = client.get("servicePrincipals", params={
            "$filter": "appId eq '" + application["appId"] + "'", "$select": "id,appId,displayName",
        })
        sp_result.raise_for_status()
        principals = sp_result.json()["value"]
        if not principals:
            sp_result = client.post("servicePrincipals", json={"appId": application["appId"]})
            save("principal-create-receipt.json", {"status": sp_result.status_code, "body": sp_result.json()})
            sp_result.raise_for_status()
            principal = sp_result.json()
        elif len(principals) == 1:
            principal = principals[0]
        else:
            raise RuntimeError("New app's enterprise principal is ambiguous")
        save("owned-reader-identity.json", {
            "applicationObjectId": application["id"], "clientId": application["appId"],
            "servicePrincipalId": principal["id"], "ownership": "CREATED_BY_THIS_LAB",
        })
        owner = settings["allowed_object_ids"][0]
        for collection, item in (("applications", application["id"]), ("servicePrincipals", principal["id"])):
            owners = client.get(f"{collection}/{item}/owners", params={"$select": "id"})
            owners.raise_for_status()
            if owner not in [value["id"] for value in owners.json()["value"]]:
                assigned = client.post(f"{collection}/{item}/owners/$ref", json={
                    "@odata.id": "https://graph.microsoft.com/v1.0/directoryObjects/" + owner,
                })
                if assigned.status_code != 204:
                    assigned.raise_for_status()
                    raise RuntimeError("Owner assignment not acknowledged")
            verified = client.get(f"{collection}/{item}/owners", params={"$select": "id"})
            verified.raise_for_status()
            if owner not in [value["id"] for value in verified.json()["value"]]:
                raise RuntimeError("New object owner readback differs")
        observed = client.get("applications/" + application["id"], params={
            "$select": "id,appId,displayName,signInAudience,isFallbackPublicClient,web,requiredResourceAccess,keyCredentials",
        })
        observed.raise_for_status()
        result = observed.json()
        def permissions(value):
            return {entry["resourceAppId"]: {(scope["id"], scope["type"]) for scope in entry["resourceAccess"]}
                    for entry in value}

        if (
            result["signInAudience"] != "AzureADMyOrg"
            or result["web"]["redirectUris"] != body["web"]["redirectUris"]
            or result["isFallbackPublicClient"] is not False
            or permissions(result["requiredResourceAccess"]) != permissions(body["requiredResourceAccess"])
            or any(result["web"]["implicitGrantSettings"].values())
            or {k["keyId"] for k in result["keyCredentials"]} != {k["keyId"] for k in body["keyCredentials"]}
        ):
            raise RuntimeError("Reader registration readback differs from approval")
        save("application-readback.json", result)
        config = ReaderConfig(
            tenant_id=settings["tenant_id"], client_id=application["appId"],
            allowed_object_ids=settings["allowed_object_ids"],
            certificate_file=settings["certificate_file"],
            certificate_thumbprint=settings["certificate_thumbprint"],
            redirect_uri=settings["redirect_uri"], expires_at=settings["expires_at"],
            model_ids=settings["model_ids"], receipt_directory=settings["receipt_directory"],
        )
        (private / "reader-config.json").write_text(config.model_dump_json(indent=2) + "\n", encoding="utf-8")
        print(json.dumps({
            "application": body["displayName"], "status": "CREATED_AND_READ_BACK",
            "newIdentityObjects": 2, "permissions": "Three delegated Power BI scopes only",
            "owners": "Verified native DEV creator", "adminConsent": "NOT_REQUESTED",
            "userConsent": "AWAITING_INTERACTIVE_SIGN_IN",
            "credentialExpiry": settings["expires_at"],
        }, indent=2))


if __name__ == "__main__":
    main()
