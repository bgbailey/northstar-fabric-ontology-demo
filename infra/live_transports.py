"""Lazy, selected-Azure-CLI author transport, never a default credential chain.

PrivateCliConfig accepts the existing expectedUser/expectedTenant/expectedSub
spelling and explicit CLI/profile aliases. Pin this file as scope.liveConfig AND
an approval artifact. Changing this module invalidates every old code inventory.
Only `python -m infra.hero_deploy execute --stage ... --cli-config ...` constructs
this factory; plan/dry-run/bind do not run CLI commands or acquire tokens.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import AliasChoices, Field, ValidationError

from infra.powerbi_refresh import FABRIC_RESOURCE, POWERBI_RESOURCE, PowerBIRefresh, validate_principal
from infra.renewal import Record, Renewal, fabric_path, local_file, powerbi_path, private_path, read_json

if TYPE_CHECKING:
    from infra.hero_deploy import HeroScope


class PrivateCliConfig(Record):
    schemaVersion: Literal["hero-live-cli-v1"] = "hero-live-cli-v1"
    expectedUser: str = Field(validation_alias=AliasChoices("expectedUser", "authorUser"))
    expectedTenant: str = Field(validation_alias=AliasChoices("expectedTenant", "expectedTenantId", "tenantId"))
    expectedSubscription: str = Field(validation_alias=AliasChoices(
        "expectedSubscription", "expectedSubscriptionId", "expectedSub", "subscriptionId",
    ))
    azureCliExecutable: str = Field(validation_alias=AliasChoices("azureCliExecutable", "azureCliPath"))
    azureConfigDirectory: str = Field(validation_alias=AliasChoices("azureConfigDirectory", "azureConfigDir"))


def validate_config(path: Path, scope: HeroScope, lease: Renewal) -> PrivateCliConfig:
    try:
        config = PrivateCliConfig.model_validate(read_json(private_path(path)))
    except ValidationError:
        raise RuntimeError("Private CLI configuration schema invalid; inspect privately") from None
    validate_principal(lease, scope.principal)
    if (
        config.expectedUser, config.expectedTenant, config.expectedSubscription,
        config.azureCliExecutable, config.azureConfigDirectory,
    ) != (
        scope.principal.authorUser, scope.principal.tenantId, scope.principal.subscriptionId,
        scope.principal.azureCliExecutable, scope.principal.azureConfigDirectory,
    ):
        raise RuntimeError("Pinned private CLI/profile/expected principal differs from scope")
    executable, profile = Path(config.azureCliExecutable), Path(config.azureConfigDirectory)
    if not executable.is_file() or not profile.is_dir():
        raise RuntimeError("Explicit private CLI executable/profile unavailable; no ambient fallback")
    return config


def token_expiry(result: dict, resource: str, config: PrivateCliConfig, *, current: bool) -> int:
    """Fail-closed claim consistency checks, NOT a replacement for service signature validation."""
    try:
        token = result["accessToken"]
        if not isinstance(token, str) or any(char.isspace() for char in token):
            raise ValueError
        header, payload, signature = token.split(".")
        if not signature:
            raise ValueError

        def decoded(value):
            return json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))

        claims = decoded(payload)
        principal = [claims[key] for key in ("preferred_username", "upn", "unique_name") if key in claims]
        expiry = claims["exp"]
        metadata_expiry = result["expires_on"]
        if type(metadata_expiry) is not int and not (
            isinstance(metadata_expiry, str) and metadata_expiry.isascii() and metadata_expiry.isdecimal()
        ):
            raise ValueError
        metadata_expiry = int(metadata_expiry)
        now = time.time()
        if (
            decoded(header).get("alg") not in {"RS256", "PS256"}
            or result["tokenType"] != "Bearer" or result["tenant"] != config.expectedTenant
            or result["subscription"] != config.expectedSubscription
            or claims["aud"] != resource or claims["tid"] != config.expectedTenant
            or not principal or any(
                not isinstance(value, str) or value.casefold() != config.expectedUser.casefold()
                for value in principal
            )
            or not isinstance(claims.get("scp"), str) or not claims["scp"].strip()
            or claims.get("idtyp") == "app" or type(expiry) is not int
            or type(claims.get("nbf")) is not int or claims["nbf"] > now
            or (current and min(expiry, metadata_expiry) <= now + 60)
        ):
            raise ValueError
    except (KeyError, TypeError, ValueError, AttributeError):
        raise RuntimeError("Selected CLI token identity/audience/expiry invalid; no credential fallback") from None
    return min(expiry, metadata_expiry)


class CliTokens:
    def __init__(self, scope: HeroScope, lease: Renewal, config_path: Path):
        self.scope, self.lease, self.config_path = scope, lease, config_path
        self.config = validate_config(config_path, scope, lease)
        self.cache: dict[str, dict] = {}

    def cli(self, *args) -> dict:
        self.lease.check_current()
        if validate_config(self.config_path, self.scope, self.lease) != self.config:
            raise RuntimeError("Private CLI selection changed")
        environment = {
            key: value for key, value in os.environ.items()
            if not key.upper().startswith(("AZURE_", "ARM_", "MSI_", "IDENTITY_"))
        }
        environment.update({
            "AZURE_CONFIG_DIR": self.config.azureConfigDirectory,
            "AZURE_CORE_COLLECT_TELEMETRY": "no",
            "AZURE_EXTENSION_USE_DYNAMIC_INSTALL": "no",
            "AZURE_CORE_ONLY_SHOW_ERRORS": "true",
        })
        try:
            result = subprocess.run(
                [self.config.azureCliExecutable, *args, "--output", "json", "--only-show-errors"],
                env=environment, capture_output=True, text=True, timeout=60, check=False, shell=False,
            )
            if result.returncode:
                raise ValueError
            value = json.loads(result.stdout)
            if not isinstance(value, dict):
                raise ValueError
        except (OSError, ValueError, subprocess.SubprocessError):
            raise RuntimeError("Selected CLI command failed; no login, fallback or sensitive output") from None
        self.lease.check_current()
        return value

    def get(self, resource: str) -> str:
        if resource not in {FABRIC_RESOURCE, POWERBI_RESOURCE}:
            raise RuntimeError("Unreviewed token resource")
        self.lease.check_current()
        cached = self.cache.get(resource)
        if cached is not None and token_expiry(cached, resource, self.config, current=False) > time.time() + 60:
            return cached["accessToken"]
        identity = self.cli("account", "show", "--subscription", self.config.expectedSubscription)
        user = identity.get("user")
        if (
            identity.get("tenantId"), identity.get("id"),
            identity.get("environmentName"), identity.get("state"),
        ) != (
            self.config.expectedTenant, self.config.expectedSubscription,
            "AzureCloud", "Enabled",
        ) or (
            not isinstance(user, dict) or user.get("type") != "user"
            or not isinstance(user.get("name"), str)
            or user["name"].casefold() != self.config.expectedUser.casefold()
        ):
            raise RuntimeError("Actual selected CLI user/tenant/subscription/cloud/state mismatch")
        result = self.cli(
            "account", "get-access-token", "--subscription", self.config.expectedSubscription,
            "--resource", resource,
        )
        token_expiry(result, resource, self.config, current=True)
        self.cache[resource] = result
        return result["accessToken"]


class AudienceClient:
    def __init__(self, client: httpx.Client, tokens: CliTokens, resource: str):
        if resource not in {FABRIC_RESOURCE, POWERBI_RESOURCE}:
            raise RuntimeError("Unreviewed token transport resource")
        self.client, self.tokens, self.resource = client, tokens, resource
        self.origin = FABRIC_RESOURCE if resource == FABRIC_RESOURCE else "https://api.powerbi.com"

    def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != urlsplit(self.origin).netloc:
            raise RuntimeError("Token transport host/scheme/userinfo/port denied")
        canonical = (fabric_path if self.resource == FABRIC_RESOURCE else powerbi_path)(url)
        if url != self.origin + canonical:
            raise RuntimeError("Token transport requires an exact canonical audience path")
        allowed = {"json", "headers", "timeout", "follow_redirects"}
        headers = kwargs.pop("headers", {})
        if (
            set(kwargs) - allowed or kwargs.get("follow_redirects") not in (None, False)
            or not isinstance(headers, dict)
            or set(headers) - ({"x-ms-fabric-skill"} if self.resource == FABRIC_RESOURCE else set())
        ):
            raise RuntimeError("Unreviewed token transport options/headers/redirects")
        self.tokens.lease.check_current()
        response = self.client.request(
            method, url, **(kwargs | {"follow_redirects": False}),
            headers=headers | {"Authorization": "Bearer " + self.tokens.get(self.resource)},
        )
        if 300 <= response.status_code < 400:
            raise RuntimeError("Service redirect denied; no cross-host token forwarding")
        if 200 <= response.status_code < 300 and response.content:
            try:
                body = response.json()
            except ValueError:
                raise RuntimeError("Non-JSON service result cannot prove completion") from None
            operation_state = (
                self.resource == FABRIC_RESOURCE and method == "GET"
                and parsed.path.startswith("/v1/operations/") and not parsed.path.endswith("/result")
                and isinstance(body, dict) and body.get("status") in {"NotStarted", "Running", "Succeeded"}
            )
            if not isinstance(body, dict) or any(
                key in body and (body[key] is not None or not operation_state)
                for key in ("error", "errors", "errorCode")
            ) or any(body.get(key) is not None and body.get(key) is not False for key in (
                "partial", "isPartial",
            )):
                raise RuntimeError("Service error/partial/unknown result cannot prove completion")
        return response


class LiveTransports:
    """One lazy factory instance per explicit finite CLI execution; always close clients."""

    def __init__(self, scope_file: Path, config_path: Path, *, stage: str):
        self.scope_file, self.config_path, self.stage = scope_file, config_path, stage
        self.clients: list[httpx.Client] = []
        self.tokens: CliTokens | None = None
        self.constructed = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        for client in self.clients:
            client.close()
        if self.tokens is not None:
            self.tokens.cache.clear()

    def __call__(self, scope: HeroScope, lease: Renewal):
        from infra.hero_deploy import FabricTransport, validate_scope

        verified, _, _ = validate_scope(lease.private, self.scope_file, lease)
        selected = Path(scope.liveConfig.file) if scope.liveConfig is not None else None
        if selected is not None and not selected.is_absolute():
            selected = local_file(lease.private, str(selected))
        if (
            self.constructed or scope.model_dump() != verified.model_dump() or self.stage != scope.phase
            or scope.liveConfig is None
            or private_path(self.config_path) != private_path(selected)
        ):
            raise RuntimeError("Explicit stage/pinned private configuration/factory reuse mismatch")
        self.constructed = True
        lease.verify_lifecycle_policy()
        self.tokens = CliTokens(scope, lease, self.config_path)
        # Tokens are acquired independently and recheck selected CLI identity each time.
        self.tokens.get(FABRIC_RESOURCE)
        if scope.phase == "models":
            self.tokens.get(POWERBI_RESOURCE)
        for _ in range(2):
            self.clients.append(httpx.Client(timeout=45, follow_redirects=False, trust_env=False))
        fabric = FabricTransport(
            lease, AudienceClient(self.clients[0], self.tokens, FABRIC_RESOURCE), principal=scope.principal,
            token_resource=FABRIC_RESOURCE, proof_level="AUTHOR_SERVICE",
        )
        refresh = PowerBIRefresh(
            lease, AudienceClient(self.clients[1], self.tokens, POWERBI_RESOURCE), principal=scope.principal,
            token_resource=POWERBI_RESOURCE, proof_level="AUTHOR_SERVICE",
        )
        return fabric, refresh
