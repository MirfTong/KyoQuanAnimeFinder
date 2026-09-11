"""Optional per-run HTTP accounting; retries and fallbacks consume the same budget."""

from dataclasses import dataclass, field
import json


class ResponseCache:
    """A run-local disk cache keyed by full provider URL; failures are not cached."""

    def __init__(self, spool):
        self.spool = spool
        self.offsets = {}

    def get(self, url):
        offset = self.offsets.get(url)
        if offset is None:
            return None
        self.spool.seek(offset)
        return json.loads(self.spool.readline())

    def put(self, url, payload):
        self.spool.seek(0, 2)
        self.offsets[url] = self.spool.tell()
        self.spool.write(json.dumps(payload) + "\n")


class RequestBudgetExhausted(Exception):
    """A clean scheduling boundary, not an API failure eligible for retry."""


@dataclass
class RequestBudget:
    limit: int
    lane: str = "default"
    lane_limits: dict[str, int] = field(default_factory=dict)
    lane_attempts: dict[str, int] = field(default_factory=dict)
    lane_successful: dict[str, int] = field(default_factory=dict)
    lane_failed: dict[str, int] = field(default_factory=dict)
    attempted: int = 0
    successful: int = 0
    failed: int = 0
    avoided: int = 0
    exhausted: set[str] = field(default_factory=set)

    def check(self) -> None:
        if self.attempted >= self.limit or self.lane_attempts.get(
            self.lane, 0
        ) >= self.lane_limits.get(self.lane, self.limit):
            self.exhausted.add(self.lane)
            raise RequestBudgetExhausted(self.lane)

    def claim(self) -> None:
        self.check()
        self.attempted += 1
        self.lane_attempts[self.lane] = self.lane_attempts.get(self.lane, 0) + 1

    def record_success(self) -> None:
        self.successful += 1
        self.lane_successful[self.lane] = self.lane_successful.get(self.lane, 0) + 1

    def record_failure(self) -> None:
        self.failed += 1
        self.lane_failed[self.lane] = self.lane_failed.get(self.lane, 0) + 1
