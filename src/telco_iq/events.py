"""Observed execution events, not agent thoughts or simulated tool activity."""

from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Literal

from pydantic import Field

from telco_iq.contracts import Contract, Status


class ExecutionEvent(Contract):
    run_id: str
    node_id: str | None = None
    operation: str
    phase: Literal["started", "completed", "failed", "skipped"]
    lane: str
    elapsed_ms: float = Field(default=0, ge=0)
    returned_rows: int | None = Field(default=None, ge=0)
    model: str | None = None
    receipt_id: str | None = None
    status: Status | None = None
    message: str | None = None
    observed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


EventSink = Callable[[ExecutionEvent], Awaitable[None]]
