import asyncio
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

from telco_iq.contracts import Status
from telco_iq.errors import LabError


def retry_after(value: str | None, attempt: int, *, now: datetime | None = None) -> float:
    if value is None:
        return min(2 ** attempt, 8)
    try:
        seconds = float(value)
    except ValueError:
        try:
            deadline = parsedate_to_datetime(value)
            seconds = (deadline - (now or datetime.now(timezone.utc))).total_seconds()
        except (ValueError, TypeError) as error:
            raise LabError("Invalid Retry-After header") from error
    return max(0, seconds)


class ReadOnlyRetryTransport(httpx.AsyncBaseTransport):
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None, max_wait: float = 45):
        self.inner = transport or httpx.AsyncHTTPTransport()
        self.max_wait = max_wait

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        for attempt in range(3):
            response = await self.inner.handle_async_request(request)
            if response.status_code not in (429, 503, 504) or attempt == 2:
                return response
            delay = retry_after(response.headers.get("Retry-After"), attempt)
            await response.aclose()
            if delay > self.max_wait:
                raise LabError("Retry-After exceeds request deadline; no early retry", Status.TIMED_OUT)
            await asyncio.sleep(delay)
        raise AssertionError("Retry loop exhausted without response")

    async def aclose(self) -> None:
        await self.inner.aclose()
