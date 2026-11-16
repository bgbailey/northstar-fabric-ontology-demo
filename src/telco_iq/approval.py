from datetime import datetime, timezone
from pathlib import Path

from pydantic import Field

from telco_iq.contracts import Contract
from telco_iq.errors import LabError


class Approval(Contract):
    target_alias: str
    target_digest: str
    artifact_digest: str
    operations: list[str] = Field(min_length=1)
    estimated_cost: float = Field(ge=0)
    cap: float = Field(ge=0)
    currency: str
    expires_at: datetime
    ttl_hours: int = Field(gt=0)
    teardown_owner: str = Field(min_length=1)
    teardown_mechanism: str = Field(min_length=1)
    confirmation_receipt: str = Field(min_length=1)

    def authorize(self, artifact_digest: str, target_digest: str, operation: str) -> None:
        if self.target_alias != "APPROVED_LAB_ONLY":
            raise LabError("Only explicitly approved lab targets are permitted")
        if self.expires_at.tzinfo is None or self.expires_at <= datetime.now(timezone.utc):
            raise LabError("Approval expired or has no timezone")
        if self.artifact_digest != artifact_digest or self.target_digest != target_digest:
            raise LabError("Approval is bound to another artifact revision/target")
        if operation not in self.operations or self.estimated_cost > self.cap:
            raise LabError("Operation/cost outside explicit approval")


def teardown_preview(owned_items: list[dict], target_workspace: str) -> list[dict]:
    seen = set()
    for item in owned_items:
        if (
            item.get("workspace") != target_workspace or not item.get("id")
            or item.get("type") == "Capacity" or item.get("ownership") != "CREATED_BY_THIS_LAB"
            or item["id"] in seen
        ):
            raise LabError("Teardown ownership or workspace invariant violated")
        seen.add(item["id"])
    return [{"operation": "delete_item", "id": i["id"], "type": i["type"]} for i in owned_items]


def private_config_path(path: Path, repo: Path) -> Path:
    resolved = path.resolve()
    if resolved.is_relative_to(repo.resolve()) or "onedrive" in str(resolved).lower():
        raise LabError("Private target/auth configuration must be outside repository and OneDrive")
    return resolved
