"""Scheduling policy shared by the bounded catalogue worker and its tests."""

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import inspect


ACTIVE_STATUSES = frozenset({"CURRENTLY_AIRING", "AIRING", "PUBLISHING"})
UPCOMING_STATUSES = frozenset(
    {"NOT_YET_AIRED", "NOT_YET_AIRING", "NOT_YET_PUBLISHED", "UPCOMING"}
)
FINISHED_STATUSES = frozenset({"FINISHED", "FINISHED_AIRING"})
DETAIL_TIERS = ("active", "recent", "stable", "archived")


def utc(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


@dataclass(frozen=True)
class RefreshPolicy:
    airing_days: int = 3
    recent_days: int = 14
    stable_days: int = 90
    archived_days: int = 180
    retry_days: int = 1
    retry_max_days: int = 14
    discovery_days: int = 30
    recent_window_days: int = 180
    archived_after_days: int = 1825

    def __post_init__(self):
        if any(getattr(self, field) <= 0 for field in self.__dataclass_fields__):
            raise ValueError("Refresh intervals must be positive")
        if not (
            self.airing_days
            <= self.recent_days
            <= self.stable_days
            <= self.archived_days
        ):
            raise ValueError(
                "Refresh tier intervals must be ordered shortest to longest"
            )
        if self.retry_days > self.retry_max_days:
            raise ValueError("Retry maximum must not be shorter than its initial delay")
        if self.recent_window_days >= self.archived_after_days:
            raise ValueError("Recent window must end before the archive threshold")

    @classmethod
    def from_env(cls):
        return cls(
            **{
                field: int(os.getenv(f"ETL_{field.upper()}", str(default.default)))
                for field, default in cls.__dataclass_fields__.items()
            }
        )

    @staticmethod
    def normalized_status(value) -> str:
        return str(value or "").strip().upper().replace(" ", "_")

    def detail_tier(self, data: dict, now: datetime) -> str:
        """Classify complete detail data by its likelihood of changing."""
        status = self.normalized_status(data.get("status"))
        if status in ACTIVE_STATUSES | UPCOMING_STATUSES:
            return "active"
        if status not in FINISHED_STATUSES:
            return "recent"
        dates = data.get("aired") or data.get("published")
        end = dates.get("to") if isinstance(dates, dict) else None
        try:
            ended = utc(datetime.fromisoformat(end.replace("Z", "+00:00")))
        except (AttributeError, TypeError, ValueError):
            # A finished title with no trustworthy end date is unlikely to need
            # the same cadence as an airing title, but remains in normal rotation.
            return "stable"
        age = now - ended
        if age < timedelta(days=self.recent_window_days):
            return "recent"
        if age >= timedelta(days=self.archived_after_days):
            return "archived"
        return "stable"

    def tier_days(self, tier: str) -> int:
        if tier not in DETAIL_TIERS:
            raise ValueError(f"Unknown refresh tier: {tier}")
        return {
            "active": self.airing_days,
            "recent": self.recent_days,
            "stable": self.stable_days,
            "archived": self.archived_days,
        }[tier]

    def detail_days(self, data: dict, now: datetime) -> int:
        return self.tier_days(self.detail_tier(data, now))

    def retry_delay(self, failure: str, failure_streak: int) -> int:
        """Return bounded exponential backoff for a repeated item failure."""
        streak = max(1, failure_streak)
        base = 30 if failure == "not_found" else self.retry_days
        maximum = self.archived_days if failure == "not_found" else self.retry_max_days
        return min(maximum, base * (2 ** min(streak - 1, 10)))

    def next_eligible_estimates(self, now: datetime) -> dict[str, str]:
        days = {tier: self.tier_days(tier) for tier in DETAIL_TIERS} | {
            "retry": self.retry_days,
            "discovery": self.discovery_days,
        }
        return {
            tier: (now + timedelta(days=interval)).isoformat()
            for tier, interval in days.items()
        }


def next_streaming_check(
    streak: int,
    *,
    empty: bool,
    failed: bool,
    now: datetime,
    policy: RefreshPolicy,
    failure_streak: int = 1,
):
    if failed:
        return streak, now + timedelta(
            days=policy.retry_delay("temporary", failure_streak)
        )
    if empty:
        streak = min(streak + 1, 3)
        return streak, now + timedelta(days=(7, 30, 90)[streak - 1])
    return 0, now + timedelta(days=policy.stable_days)


def content_snapshot(row):
    """Compare business data, excluding ETL bookkeeping timestamps."""
    state = inspect(row)
    columns = tuple(
        (attribute.key, getattr(row, attribute.key))
        for attribute in inspect(type(row)).column_attrs
        if not attribute.key.startswith("last_") and attribute.key not in state.unloaded
    )
    links = []
    for relationship, entity, fields in (
        ("genre_links", "genre", ("name",)),
        ("studio_links", "studio", ("name", "mal_id")),
        ("streaming_links", "streaming_service", ("name",)),
        ("author_links", "author", ("name", "mal_id")),
    ):
        if not hasattr(type(row), relationship) or relationship in state.unloaded:
            continue
        values = [
            (
                tuple(getattr(getattr(link, entity), field) for field in fields),
                getattr(link, "url", None),
                getattr(link, "role", None),
            )
            for link in getattr(row, relationship)
            if not inspect(link).deleted
            and (state.session is None or link not in state.session.deleted)
        ]
        links.append((relationship, sorted(values, key=repr)))
    return repr((columns, links))
