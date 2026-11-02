"""How a list endpoint says how many rows match without changing the shape of its answer.

A page of rows is still a plain JSON array, so existing callers keep working; the number of rows the
filters match, whatever `limit` and `offset` are, travels in `X-Total-Count`. The console's CORS
configuration exposes that header (`src.main`), which a browser would otherwise hide from a page on
another origin.
"""

from fastapi import Response

TOTAL_COUNT_HEADER = "X-Total-Count"


def set_total_count(response: Response, total: int) -> None:
    response.headers[TOTAL_COUNT_HEADER] = str(total)
