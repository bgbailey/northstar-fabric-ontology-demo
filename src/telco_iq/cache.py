from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable, Generic, TypeVar

from telco_iq.contracts import Status
from telco_iq.errors import LabError

T = TypeVar("T")


@dataclass(frozen=True)
class CacheKey:
    tenant: str
    principal: str
    authorization_revision: str
    item: str
    schema_revision: str
    data_revision: str
    ontology_revision: str
    filters_hash: str


class ScopedCache(Generic[T]):
    """Protected reuse requires current authorization, including after in-flight loads."""

    def __init__(self, authorize: Callable[[CacheKey], Awaitable[bool]]):
        self.authorize = authorize
        self.values: dict[CacheKey, T] = {}
        self.inflight: dict[tuple[CacheKey, int], asyncio.Task[T]] = {}
        self.generations: dict[tuple[str, str], int] = {}

    def invalidate(self, tenant: str, principal: str) -> None:
        owner = (tenant, principal)
        self.generations[owner] = self.generations.get(owner, 0) + 1
        for key in list(self.values):
            if (key.tenant, key.principal) == owner:
                del self.values[key]

    async def get(self, key: CacheKey, load: Callable[[], Awaitable[T]]) -> T:
        owner = (key.tenant, key.principal)
        generation = self.generations.get(owner, 0)
        if not await self.authorize(key):
            self.invalidate(*owner)
            raise LabError("Current authorization denied cache access", Status.BLOCKED_AUTH)
        if key in self.values:
            return self.values[key]
        flight = (key, generation)
        task = self.inflight.get(flight)
        if task is None:
            task = asyncio.create_task(load())
            self.inflight[flight] = task
        try:
            value = await asyncio.shield(task)
            authorized = await self.authorize(key)
            if not authorized or self.generations.get(owner, 0) != generation:
                raise LabError("Authorization changed during fetch", Status.BLOCKED_AUTH)
            self.values[key] = value
            return value
        finally:
            if task.done() and self.inflight.get(flight) is task:
                del self.inflight[flight]
