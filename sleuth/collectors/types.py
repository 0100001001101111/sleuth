"""Typed, JSON-serialisable collection results. `Coverage` says exactly which block spans were completed, which are
missing and at which head/finality the scan was anchored, so a report can state "checked through block N" and never
describe a failed call as "no activity"."""
import time
from dataclasses import asdict, dataclass, field

COLLECTOR_VERSION = "collectors/0.1.0"   # bump whenever a predicate, window rule or output shape changes


@dataclass(frozen=True)
class Span:
    start: int
    end: int   # inclusive

    def __post_init__(self):
        if self.start < 0 or self.end < self.start:
            raise ValueError(f"bad span {self.start}-{self.end}")

    @property
    def blocks(self):
        return self.end - self.start + 1


@dataclass
class Gap:
    kind: str          # e.g. "rpc_error", "budget", "unsupported", "no_index"
    detail: str
    span: Span | None = None


@dataclass
class Coverage:
    requested: Span
    completed: list = field(default_factory=list)   # [Span]
    missing: list = field(default_factory=list)     # [Span]
    head_block: int | None = None
    head_hash: str | None = None
    finality: str = "latest"                        # latest | safe | finalized

    @property
    def complete(self):
        return not self.missing

    @property
    def checked_through(self):
        """Highest block below which coverage is contiguous from the requested start, or None if nothing was covered."""
        pos, done = self.requested.start, None
        for s in sorted(self.completed, key=lambda s: s.start):
            if s.start > pos:
                break
            done = max(done or s.end, s.end)
            pos = done + 1
        return done


@dataclass
class Result:
    items: list
    coverage: Coverage
    gaps: list = field(default_factory=list)       # [Gap]
    next_checkpoint: int | None = None             # first block not yet completed (resume point), None when complete
    source: str = "rpc"
    collector_version: str = COLLECTOR_VERSION
    observed_at: float = field(default_factory=time.time)   # unix seconds, UTC

    def to_dict(self):
        d = asdict(self)
        d["coverage"]["complete"] = self.coverage.complete
        d["coverage"]["checked_through"] = self.coverage.checked_through
        return d
