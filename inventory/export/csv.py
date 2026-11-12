"""CSV responses for page tables and exports."""

import csv
import io
import json
from collections.abc import Iterable, Iterator

from fastapi.responses import StreamingResponse

_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def csv_cell(value) -> str:
    """One cell: lists and dicts as JSON, None as empty, formula-like text prefixed with '."""
    if value is None:
        return ""
    if isinstance(value, (list, dict)):
        return json.dumps(value)
    text = str(value)
    return "'" + text if text.startswith(_FORMULA_START) else text


def csv_lines(rows: Iterable[dict], columns: list[str] | None = None) -> Iterator[str]:
    rows = iter(rows)
    first = next(rows, None)
    if columns is None:
        columns = list(first) if first is not None else ["none"]
    buf = io.StringIO()
    writer = csv.writer(buf)

    def flush() -> str:
        text = buf.getvalue()
        buf.seek(0)
        buf.truncate()
        return text

    writer.writerow(columns)
    yield flush()
    for row in ([first] if first is not None else []):
        writer.writerow([csv_cell(row.get(c)) for c in columns])
        yield flush()
    for row in rows:
        writer.writerow([csv_cell(row.get(c)) for c in columns])
        yield flush()


def csv_response(rows: list[dict], filename: str, columns: list[str] | None = None) -> StreamingResponse:
    return StreamingResponse(
        csv_lines([dict(r) for r in rows], columns),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
