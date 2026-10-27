"""Service-grade collectors: every function RETURNS what it managed to collect plus an exact statement of what it did
not (coverage + gaps). Nothing here raises away collected evidence on a budget/provider failure. Read-only by
construction: no signing, no custody, no wallet management, no payment logic (see service/payments for that)."""
from .types import COLLECTOR_VERSION, Coverage, Gap, Result, Span  # noqa: F401
