"""Bounded scalar-only listing refreshes, independent of full detail freshness."""

from datetime import timedelta
import math

from sqlalchemy import select
from sqlalchemy.orm import lazyload, load_only

from backend.jobs.refresh_policy import RefreshPolicy, utc
from backend.models import Anime, Manga, JikanRefreshState, JikanSyncState
from backend.services.jikan_client import JikanTemporaryError


SCALARS = ("score", "popularity", "members")


def fields(kind):
    return SCALARS + (
        ("episodes",) if kind == "anime" else ("chapters", "volumes")
    )


def validate_listing(kind, entries):
    """Reject a bad page before any scalar write or cursor advancement."""
    if not isinstance(entries, list) or len(entries) > 25:
        raise JikanTemporaryError("Invalid ongoing page size")
    seen = {}
    previous_id = 0
    for data in entries:
        if not isinstance(data, dict):
            raise JikanTemporaryError("Invalid ongoing record")
        mal_id = data.get("mal_id")
        if (
            type(mal_id) is not int
            or not 0 < mal_id <= 2147483647
            or mal_id < previous_id
        ):
            raise JikanTemporaryError("Invalid ongoing ID order")
        previous_id = mal_id
        expected_statuses = (
            {"CURRENTLY_AIRING", "AIRING"}
            if kind == "anime"
            else {"PUBLISHING"}
        )
        if not isinstance(data.get("status"), str) or (
            RefreshPolicy.normalized_status(data["status"])
            not in expected_statuses
        ):
            raise JikanTemporaryError("Ongoing filter was not respected")
        if kind != "anime" and str(data.get("type", "")).casefold() != kind:
            raise JikanTemporaryError("Ongoing media filter was not respected")
        for key in fields(kind):
            if key not in data:
                raise JikanTemporaryError(f"Ongoing listing omitted {key}")
            value = data[key]
            if value is not None and (
                type(value) not in (int, float)
                or (type(value) is float and not math.isfinite(value))
                or value < 0
                or (key != "score" and type(value) is not int)
                or (key != "score" and value > 2147483647)
                or (key == "score" and value > 10)
            ):
                raise JikanTemporaryError(f"Invalid ongoing {key}")
        # Identical overlap is safe; conflicting values are not authoritative.
        signature = tuple(data[key] for key in fields(kind))
        if mal_id in seen and seen[mal_id] != signature:
            raise JikanTemporaryError("Conflicting ongoing duplicate")
        seen[mal_id] = signature


def plan(session, kind, cap, page_limit, now, policy):
    # Spend at most 10% of this lane's selection capacity on multi-title pages.
    # Tiny diagnostic runs retain their previous detail-only behavior.
    pages = min(10, page_limit, cap // 10)
    if pages == 0:
        return None
    key = f"ongoing:{kind}:v1"
    state = session.get(JikanSyncState, key)
    page = max(1, state.next_page) if state else 1
    if (
        state
        and page == 1
        and state.last_completed_at
        and (
            utc(state.last_completed_at) + timedelta(days=policy.airing_days)
            > now
        )
    ):
        return None
    # Re-read one boundary page on multi-page resumes to reduce misses when the
    # status-filtered result set shrinks. Periodic full passes repair larger moves.
    if page > 1 and pages > 1:
        page -= 1
    return dict(kind="ongoing_page", media=kind, key=key, page=page, cap=pages)


def apply(session, item, now):
    """Apply known rows and the page cursor atomically; never touch relationships."""
    kind = item["media"]
    result = item["result"]
    validate_listing(kind, result["entries"])
    payloads = {entry["mal_id"]: entry for entry in result["entries"]}
    model = Anime if kind == "anime" else Manga
    statement = (
        select(model)
        .where(model.mal_id.in_(payloads), model.is_adult.is_(False))
        .options(
            lazyload("*"),
            load_only(
                model.mal_id,
                model.status,
                model.last_jikan_sync,
                *(getattr(model, key) for key in fields(kind)),
            ),
        )
    )
    if kind != "anime":
        statement = statement.where(Manga.content_type == kind.upper())
    rows = list(session.scalars(statement))
    states = {
        state.mal_id: state
        for state in session.scalars(
            select(JikanRefreshState).where(
                JikanRefreshState.kind == kind,
                JikanRefreshState.queue == "listing",
                JikanRefreshState.mal_id.in_(payloads),
            )
        )
    }
    changed = 0
    for row in rows:
        data = payloads[row.mal_id]
        values = {
            key: data[key] for key in fields(kind) if data[key] is not None
        }
        values["status"] = (
            "CURRENTLY_AIRING" if kind == "anime" else data["status"]
        )
        different = any(
            getattr(row, key) != value for key, value in values.items()
        )
        if different:
            for key, value in values.items():
                setattr(row, key, value)
            row.last_jikan_sync = now
            changed += 1
        state = states.get(row.mal_id)
        if state is None:
            state = JikanRefreshState(
                kind=kind, mal_id=row.mal_id, queue="listing"
            )
            session.add(state)
        state.last_attempt_at = state.last_success_at = now
        state.last_failure = None
    cursor = session.get(JikanSyncState, item["key"])
    if cursor is None:
        cursor = JikanSyncState(key=item["key"])
        session.add(cursor)
    cursor.next_page = result["page"] + 1 if result["has_next_page"] else 1
    cursor.last_attempt_at = now
    cursor.last_error = None
    if not result["has_next_page"]:
        cursor.last_completed_at = now
    # Commit belongs to the caller so cache generation shares this transaction.
    return dict(
        fetched=len(payloads),
        committed=len(rows),
        changed=changed,
        unchanged=len(rows) - changed,
        unknown=len(payloads) - len(rows),
        next_page=cursor.next_page,
    )
