"""Local-only: stage one BFF registration and an OS-protected, short-lived certificate."""

import argparse
import base64
import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from telco_iq.approval import private_config_path
from telco_iq.auth import dpapi

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--private-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    private = private_config_path(args.private_dir, ROOT)
    output = private_config_path(args.output, ROOT)
    source = json.loads((private / "p0-approval.json").read_text(encoding="utf-8-sig"))
    identity = json.loads((private / "verified-reader-identity.json").read_text(encoding="utf-8-sig"))
    permissions = json.loads((private / "powerbi-permission-catalogue.json").read_text(encoding="utf-8-sig"))
    if identity["userType"] != "Member" or identity["userPrincipalName"].lower() != source["authorUser"].lower():
        raise RuntimeError("Verified native DEV identity required")
    required = {"Item.Read.All", "Item.Execute.All", "Dataset.Read.All"}
    if (
        {s["value"] for s in permissions["scopes"]} != required
        or not all(s["enabled"] and s["type"] == "User" for s in permissions["scopes"])
    ):
        raise RuntimeError("Delegated permission catalogue does not match approved design")
    deadline = datetime.fromisoformat(source["deadlineUtc"])
    if deadline <= datetime.now(timezone.utc):
        raise RuntimeError("Lab deadline elapsed")
    output.mkdir(parents=True, exist_ok=True)
    key_path = output / "bff-key.dpapi"
    if key_path.exists() or (output / "application-request.json").exists():
        raise RuntimeError("Reader staging already exists; do not rotate keys or overwrite silently")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Telco IQ Demo BFF")])
    cert = (
        x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc) - timedelta(minutes=5))
        .not_valid_after(deadline)
        .sign(key, hashes.SHA256())
    )
    private_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
    )
    key_path.write_bytes(dpapi(private_pem, decrypt=False))
    cert_der = cert.public_bytes(serialization.Encoding.DER)
    (output / "bff-certificate.cer").write_bytes(cert_der)
    thumbprint = cert.fingerprint(hashes.SHA1()).hex()
    body = {
        "displayName": "TelcoIQ-Demo-BFF-20260929",
        "description": "Synthetic local demo; delegated user only; teardown after approved lab window",
        "signInAudience": "AzureADMyOrg", "isFallbackPublicClient": False,
        "web": {
            "redirectUris": ["http://localhost:8765/auth/callback"],
            "implicitGrantSettings": {"enableAccessTokenIssuance": False, "enableIdTokenIssuance": False},
        },
        "requiredResourceAccess": [{
            "resourceAppId": permissions["appId"],
            "resourceAccess": [{"id": s["id"], "type": "Scope"} for s in permissions["scopes"]],
        }],
        "keyCredentials": [{
            "keyId": str(uuid.uuid4()), "displayName": "Telco IQ bounded demo certificate",
            "type": "AsymmetricX509Cert", "usage": "Verify",
            "startDateTime": cert.not_valid_before_utc.isoformat(),
            "endDateTime": deadline.isoformat(), "key": base64.b64encode(cert_der).decode(),
        }],
        "tags": ["telco-iq-ontology-lab", "owned-demo-resource"],
    }
    request_bytes = (json.dumps(body, indent=2) + "\n").encode()
    (output / "application-request.json").write_bytes(request_bytes)
    settings = {
        "approved": False, "tenant_id": source["tenantId"], "author_user": source["authorUser"],
        "allowed_object_ids": [identity["id"]], "certificate_file": str(key_path),
        "certificate_thumbprint": thumbprint,
        "redirect_uri": "http://localhost:8765/auth/callback", "expires_at": source["deadlineUtc"],
        "model_ids": {
            model: json.loads((private / f"p0-{model}.json").read_text(encoding="utf-8-sig"))["id"]
            for model in ("subscriptions", "finance")
        },
        "receipt_directory": str(output / "reader-receipts"),
        "application_request_sha256": hashlib.sha256(request_bytes).hexdigest(),
        "consent": "Interactive current-user consent only; no tenant-wide admin-consent operation",
    }
    (output / "staged-settings.json").write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "cloudMutations": 0, "application": body["displayName"],
        "applicationRequestSha256": settings["application_request_sha256"],
        "certificateThumbprint": thumbprint, "certificateExpiry": source["deadlineUtc"],
        "credentialStorage": "Windows DPAPI current-user protected; outside source/OneDrive",
        "scopes": sorted(required), "status": "STAGED_UNAPPROVED",
    }, indent=2))


if __name__ == "__main__":
    main()
