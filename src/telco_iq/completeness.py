import csv
import io
from decimal import Decimal, InvalidOperation

from telco_iq.contracts import Status
from telco_iq.errors import LabError


def embedded_csv(
    text: str, columns: dict[str, str], *, null_marker: str | None,
    expected_rows: int | None, complete: bool, max_bytes: int = 8_000_000,
) -> list[dict]:
    """Parse only reviewed CSV contracts; an inline preview is not the full resource."""
    if null_marker is None:
        raise LabError("CSV null encoding has not been observed/reviewed", Status.BLOCKED_CAPABILITY)
    if len(text.encode()) > max_bytes:
        raise LabError("Embedded CSV exceeds byte budget", Status.PARTIAL)
    reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
    if reader.fieldnames != list(columns):
        raise LabError("CSV header does not match reviewed output contract")
    rows = []
    try:
        for row in reader:
            if None in row or any(value is None for value in row.values()):
                raise LabError("CSV record column count mismatch")
            parsed = {}
            for key, value in row.items():
                if value == null_marker:
                    parsed[key] = None
                elif columns[key] == "string":
                    parsed[key] = value
                elif columns[key] == "int64":
                    parsed[key] = int(value)
                elif columns[key] == "decimal":
                    parsed[key] = Decimal(value)
                    if not parsed[key].is_finite():
                        raise LabError("Non-finite CSV decimal")
                elif columns[key] == "bool" and value in ("true", "false"):
                    parsed[key] = value == "true"
                else:
                    raise LabError("Unsupported CSV type/value; no lossy coercion")
            rows.append(parsed)
    except (csv.Error, ValueError, InvalidOperation) as error:
        raise LabError("CSV parse/type failure") from error
    if not complete or type(expected_rows) is not int or expected_rows != len(rows):
        raise LabError("CSV completeness not proven; preview/truncation is not a total", Status.PARTIAL)
    return rows
