"""Plan, fetch offline, then apply a bounded scheduled catalogue sync.

Only the fetch stage talks to providers. It uses an ephemeral disk spool so
payload size does not determine RAM use and no DB transaction spans HTTP waits.
"""

from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from tempfile import TemporaryFile
from time import monotonic
from urllib.error import HTTPError

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import lazyload, load_only, selectinload

from backend.app import app
from backend.models import (
    Anime,
    AnimeStreamingService,
    Genre,
    JikanRefreshState,
    JikanSyncState,
    Manga,
    MangaAuthor,
    MangaGenre,
    db,
)
from backend.jobs.refresh_policy import (
    RefreshPolicy,
    content_snapshot,
    next_streaming_check,
    utc,
)
from backend.services.jikan_budget import (
    RequestBudget,
    RequestBudgetExhausted,
    ResponseCache,
)
from backend.services.jikan_client import JikanClient, JikanTemporaryError


@dataclass
class SyncMetrics:
    selected: int = 0
    changed: int = 0
    unchanged: int = 0
    removed: int = 0
    failed: int = 0
    records_succeeded: int = 0
    failure_reasons: dict = field(default_factory=dict)
    deferred: int = 0
    pages_applied: int = 0
    pages_failed: int = 0
    fetch_seconds: float = 0
    apply_seconds: float = 0
    cursors: dict = field(default_factory=dict)
    provider_caps: list = field(default_factory=list)


def _due_statement(kind, queue, now, policy):
    model = Anime if kind == "anime" else Manga
    state = JikanRefreshState
    status = func.upper(func.coalesce(model.status, ""))
    active = status.in_(("CURRENTLY_AIRING", "CURRENTLY AIRING", "PUBLISHING"))
    statement = (
        select(model.mal_id, active.label("active"))
        .outerjoin(
            state,
            and_(
                state.kind == kind, state.queue == queue, state.mal_id == model.mal_id
            ),
        )
        .where(model.mal_id > 0, model.is_adult.is_(False))
    )
    if kind != "anime":
        statement = statement.where(Manga.content_type == kind.upper())
    if queue == "streaming":
        statement = statement.where(~Anime.streaming_links.any())
    due = or_(state.next_attempt_at.is_(None), state.next_attempt_at <= now)
    if queue == "detail":
        # A listing can reveal that a formerly stable title is airing again.
        # It may accelerate the queue, but never mark detail-only fields fresh.
        due = or_(
            due,
            and_(
                active,
                state.last_failure.is_(None),
                state.last_attempt_at <= now - timedelta(days=policy.airing_days),
            ),
        )
    return statement.where(due).order_by(
        state.last_attempt_at.asc().nulls_first(), model.mal_id
    ), active


def plan_details(kind, queue, limit, now, policy):
    """Reserve half the detail slots for each active/archive queue; fill spare slots."""
    statement, active = _due_statement(kind, queue, now, policy)
    total = db.session.scalar(
        select(func.count()).select_from(statement.order_by(None).subquery())
    )
    if queue == "streaming":
        ids = list(db.session.scalars(statement.limit(limit)))
    else:
        # Bounded reads, even on the first run with no refresh-state rows.
        never = JikanRefreshState.last_attempt_at.is_(None)
        pools = deque(
            deque(db.session.scalars(statement.where(group, attempted).limit(limit)))
            for attempted in (never, ~never)
            for group in (active, ~active)
        )
        ids = []
        while pools and len(ids) < limit:
            pool = pools.popleft()
            if pool:
                ids.append(pool.popleft())
                if pool:
                    pools.append(pool)
    return [
        {"kind": kind, "queue": queue, "mal_id": mal_id} for mal_id in ids
    ], total - len(ids)


def plan_pages(now, page_limit, policy):
    from backend.jobs import jikan_etl as anime, manga_etl as manga

    states = {state.key: state for state in db.session.scalars(select(JikanSyncState))}
    plans = []
    for label, identity, cap in (
        ("current", anime._current_season_identity(now), 10),
        ("upcoming", anime._next_season_identity(now), 1),
    ):
        year, season = identity
        plans.append(
            dict(
                kind="season",
                key=f"{label}:{year}:{season}",
                year=year,
                season=season,
                cap=cap,
            )
        )
    for provider_type in ("tv", *anime.SUPPLEMENTAL_PROVIDER_TYPES):
        key = (
            anime.BULK_SEASON_STATE_KEY
            if provider_type == "tv"
            else anime.SUPPLEMENTAL_STATE_KEYS[provider_type]
        )
        plans.append(
            dict(
                kind="anime_page", key=key, provider_type=provider_type, cap=page_limit
            )
        )
    for provider_type in manga.MANGA_PROVIDER_TYPES:
        plans.append(
            dict(
                kind="manga_page",
                key=manga.MANGA_STATE_KEYS[provider_type],
                provider_type=provider_type,
                cap=page_limit,
            )
        )
    ready = []
    for plan in plans:
        state = states.get(plan["key"])
        plan["page"] = max(1, state.next_page) if state else 1
        interval = (
            policy.airing_days if plan["kind"] == "season" else policy.discovery_days
        )
        if (
            state
            and plan["page"] == 1
            and state.last_completed_at
            and utc(state.last_completed_at) + timedelta(days=interval) > now
        ):
            continue
        ready.append(plan)
    # Even a very small budget eventually visits every cursor after outages.
    ready.sort(
        key=lambda plan: (
            utc(states[plan["key"]].last_attempt_at)
            if plan["key"] in states and states[plan["key"]].last_attempt_at
            else datetime.min.replace(tzinfo=timezone.utc)
        )
    )
    return ready


def _failure(error):
    if isinstance(error, HTTPError):
        code = error.code
        error.close()
        if code == 404:
            return "not_found"
        if code not in {429, 500, 502, 503, 504}:
            raise error
    return "temporary"


def _fetch_record(client, item):
    method = client.get_anime_full if item["kind"] == "anime" else client.get_manga_full
    if item["queue"] == "streaming":
        method = client.get_anime_streaming
    try:
        payload = method(item["mal_id"])
    except (JikanTemporaryError, HTTPError) as error:
        return None, _failure(error)
    data = payload.get("data")
    if (
        not isinstance(data, dict)
        or isinstance(data.get("mal_id"), bool)
        or data.get("mal_id") != item["mal_id"]
    ):
        return None, "invalid_payload"
    if payload.get("_etl_basic_fallback"):
        data = {**data, "_etl_basic_fallback": True}
    return data, None


def _usable_streaming(data):
    from backend.jobs.jikan_etl import _streaming_values

    parsed = _streaming_values(data.get("streaming"))
    return parsed is not None and parsed[2]


def _complete_detail(kind, data):
    """A partial response may improve metadata without completing a refresh.

    Streaming has its own queue: its absence from a normal detail response does
    not invalidate the other metadata. Optional scalars (score, dates, etc.) may
    legitimately be null. Core fields and relationship arrays must be usable.
    """
    from backend.jobs.jikan_etl import _studio_values
    from backend.jobs.manga_etl import _author_values

    if not data or data.get("_etl_basic_fallback"):
        return False
    if any(
        not isinstance(data.get(key), str) or not data[key].strip()
        for key in ("title", "status")
    ):
        return False
    for key in ("genres", "explicit_genres", "themes", "demographics"):
        if key != "genres" and key not in data:
            continue
        entries = data.get(key)
        if not isinstance(entries, list) or any(
            not isinstance(entry, dict)
            or not isinstance(entry.get("name"), str)
            or not entry["name"].strip()
            for entry in entries
        ):
            return False
    parsed = (
        _studio_values(data.get("studios"))
        if kind == "anime"
        else _author_values(data.get("authors"))
    )
    return parsed is not None and parsed[2]


def fetch_work(client, budget, page_plans, queues, spool, metrics):
    """Round-robin each lane; unused shares are deliberately not borrowed."""
    lanes = deque()
    for plan in page_plans:
        lanes.append(("discovery", deque([dict(plan)])))
    for kind in ("anime", "manga", "manhwa"):
        lanes.append(("anime" if kind == "anime" else kind, deque(queues[kind])))
    lanes.append(("streaming", deque(queues["streaming"])))
    # Detail data is reused for streaming only when it actually includes that
    # field. Listings/basic fallbacks never suppress a needed full request.
    streaming_from_detail = {}
    streaming_ids = {item["mal_id"] for item in queues["streaming"]}
    started = monotonic()
    while lanes:
        lane, pending = lanes.popleft()
        if not pending:
            continue
        item = pending.popleft()
        budget.lane = lane
        is_page = "key" in item
        # Wait for this title's normal detail attempt before asking for streaming.
        if lane == "streaming" and any(
            other_lane == "anime"
            and any(i.get("mal_id") == item["mal_id"] for i in other)
            for other_lane, other in lanes
        ):
            pending.append(item)
            lanes.append((lane, pending))
            continue
        try:
            if is_page:
                if item["kind"] == "season":
                    page = client.get_season_page(
                        item["year"], item["season"], page=item["page"]
                    )
                elif item["kind"] == "anime_page":
                    page = client.get_anime_catalogue_page(
                        anime_type=item["provider_type"], page=item["page"]
                    )
                else:
                    page = client.get_manga_catalogue_page(
                        manga_type=item["provider_type"], page=item["page"]
                    )
                record = {**item, "result": asdict(page)}
                if page.has_next_page and item["cap"] > 1:
                    # Preserve the established provider cap for Manga/Manhwa.
                    if item["kind"] != "manga_page" or page.page < 1000:
                        pending.append(
                            {**item, "page": page.page + 1, "cap": item["cap"] - 1}
                        )
            else:
                data = (
                    streaming_from_detail.pop(item["mal_id"], None)
                    if lane == "streaming"
                    else None
                )
                if data is not None:
                    budget.avoided += 1
                    failure = None
                else:
                    data, failure = _fetch_record(client, item)
                record = {**item, "data": data, "failure": failure}
                if (
                    lane == "anime"
                    and data is not None
                    and _usable_streaming(data)
                    and item["mal_id"] in streaming_ids
                ):
                    streaming_from_detail[item["mal_id"]] = data
            spool.write(json.dumps(record) + "\n")
        except RequestBudgetExhausted:
            if not is_page:
                metrics.deferred += 1 + len(pending)
            continue
        except (JikanTemporaryError, HTTPError) as error:
            # A failed page ends only that cursor for this run. Do not advance it.
            spool.write(json.dumps({**item, "failure": _failure(error)}) + "\n")
            pending.clear()
        if pending:
            lanes.append((lane, pending))
    metrics.fetch_seconds = monotonic() - started


def _refresh_state(kind, mal_id, queue):
    key = (kind, mal_id, queue)
    for pending in db.session.new:
        if (
            isinstance(pending, JikanRefreshState)
            and (pending.kind, pending.mal_id, pending.queue) == key
        ):
            return pending
    state = db.session.get(JikanRefreshState, key)
    if state is None:
        state = JikanRefreshState(kind=kind, mal_id=mal_id, queue=queue, empty_streak=0)
        db.session.add(state)
    return state


def record_attempt(item, data, failure, now, policy):
    from backend.jobs.jikan_etl import _streaming_values

    state = _refresh_state(item["kind"], item["mal_id"], item["queue"])
    if state.last_attempt_at is not None and utc(state.last_attempt_at) == now:
        return state.last_failure
    if failure is None and item["queue"] == "streaming":
        parsed = _streaming_values(data.get("streaming")) if data else None
        if parsed is None or not parsed[2]:
            failure = "incomplete_streaming"
    elif failure is None and not _complete_detail(item["kind"], data):
        failure = "incomplete_detail"
    state.last_attempt_at = now
    state.last_failure = failure
    if failure is None:
        state.last_success_at = now
    if item["queue"] == "streaming":
        state.empty_streak, state.next_attempt_at = next_streaming_check(
            state.empty_streak,
            empty=data is not None and data.get("streaming") == [],
            failed=failure is not None,
            now=now,
            policy=policy,
        )
    else:
        days = (
            (30 if failure == "not_found" else policy.retry_days)
            if failure
            else policy.detail_days(data, now)
        )
        state.next_attempt_at = now + timedelta(days=days)
    return failure


def _safe_payload(data):
    """Malformed genre arrays must not clear known Manga genres."""
    from backend.jobs.manga_etl import is_adult_content

    result = dict(data)
    # Never sanitize away a valid adult classification embedded in a partly
    # malformed array; existing cleanup must still recognize that title.
    if is_adult_content(data):
        return result
    for key in ("genres", "explicit_genres", "themes", "demographics"):
        value = result.get(key)
        if key in result and (
            not isinstance(value, list)
            or any(
                not isinstance(entry, dict)
                or not isinstance(entry.get("name"), str)
                or not entry["name"].strip()
                for entry in value
            )
        ):
            # Dropping the malformed family keeps both normalized and detailed
            # existing genres. The dedicated relationship mappers remain additive.
            for field_name in ("genres", "explicit_genres", "themes", "demographics"):
                result.pop(field_name, None)
            break
    return result


def apply_details(items, now, policy, metrics):
    from backend.jobs import jikan_etl as anime, manga_etl as manga

    # Mixed queues share a batch transaction, but only successful payloads need
    # catalogue records and relationships; failed attempts need just queue state.
    ids = {"anime": [], "manga": []}
    for item in items:
        if item["data"] is not None:
            ids["anime" if item["kind"] == "anime" else "manga"].append(item["mal_id"])
    with app.app_context():
        try:
            rows = {}
            anime_details = {
                entry["mal_id"]
                for entry in items
                if entry["kind"] == "anime"
                and entry["queue"] == "detail"
                and entry["data"] is not None
            }
            groups = (
                ("anime", Anime, anime_details, anime._anime_etl_load_options()),
                (
                    "anime",
                    Anime,
                    set(ids["anime"]) - anime_details,
                    (
                        load_only(
                            Anime.animeID,
                            Anime.mal_id,
                            Anime.last_jikan_sync,
                            Anime.last_streaming_attempt,
                        ),
                        selectinload(Anime.streaming_links).selectinload(
                            AnimeStreamingService.streaming_service
                        ),
                    ),
                ),
                (
                    "manga",
                    Manga,
                    ids["manga"],
                    (
                        selectinload(Manga.genre_links)
                        .selectinload(MangaGenre.genre)
                        .lazyload("*"),
                        selectinload(Manga.author_links)
                        .selectinload(MangaAuthor.author)
                        .lazyload("*"),
                    ),
                ),
            )
            for kind, model, selected_ids, options in groups:
                if not selected_ids:
                    continue
                rows.setdefault(kind, {}).update(
                    {
                        row.mal_id: row
                        for row in db.session.scalars(
                            select(model)
                            .where(model.mal_id.in_(selected_ids))
                            .options(lazyload("*"), *options)
                        )
                    }
                )
            genres = (
                {
                    genre.name: genre
                    for genre in db.session.scalars(
                        select(Genre).options(lazyload("*"))
                    )
                }
                if rows and any(item["queue"] == "detail" for item in items)
                else {}
            )
            payloads = [
                entry["data"]
                if entry["queue"] == "detail"
                else {"streaming": entry["data"].get("streaming")}
                for entry in items
                if entry["data"] is not None
            ]
            authors = manga._author_caches(payloads) if ids["manga"] else None
            associations = anime._association_caches(payloads) if ids["anime"] else None
            stats = anime.AnimeAssociationStats()
            batch = SyncMetrics()
            with db.session.no_autoflush:
                for item in items:
                    data, failure = item["data"], item["failure"]
                    row = rows.get(
                        "anime" if item["kind"] == "anime" else "manga", {}
                    ).get(item["mal_id"])
                    if data is not None and row is not None:
                        data = _safe_payload(data)
                        before = content_snapshot(row)
                        old_sync = row.last_jikan_sync
                        if anime._is_hentai(data) or (
                            item["kind"] != "anime"
                            and manga._has_unsupported_content_type(data)
                        ):
                            db.session.delete(row)
                            batch.removed += 1
                        elif item["queue"] == "streaming":
                            if "streaming" in data:
                                associations = (
                                    associations
                                    or anime._streaming_association_caches()
                                )
                                anime._reconcile_streaming_services(
                                    row, data["streaming"], associations, stats
                                )
                            row.last_streaming_attempt = now
                        elif item["kind"] == "anime":
                            associations = anime._update_anime_with_associations(
                                row, data, genres, associations, stats
                            )
                        else:
                            authors = authors or manga._author_caches()
                            manga._update_manga(
                                row,
                                data,
                                genres,
                                expected_content_type=row.content_type,
                                authors=authors,
                            )
                        if row not in db.session.deleted:
                            changed = before != content_snapshot(row)
                            batch.changed += int(changed)
                            batch.unchanged += int(not changed)
                            # Detail freshness lives in the separate queue state.
                            row.last_jikan_sync = now if changed else old_sync
                    elif failure:
                        batch.failed += 1
                    recorded_failure = record_attempt(
                        item, item["data"], failure, now, policy
                    )
                    if recorded_failure and not failure:
                        batch.failed += 1
                    if recorded_failure:
                        batch.failure_reasons[recorded_failure] = (
                            batch.failure_reasons.get(recorded_failure, 0) + 1
                        )
                    elif row is not None:
                        batch.records_succeeded += 1
                    if (
                        item["kind"] == "anime"
                        and item["queue"] == "detail"
                        and data is not None
                        and _usable_streaming(data)
                    ):
                        record_attempt(
                            {**item, "queue": "streaming"}, data, None, now, policy
                        )
            db.session.commit()
            for key in (
                "changed",
                "unchanged",
                "removed",
                "failed",
                "records_succeeded",
            ):
                setattr(metrics, key, getattr(metrics, key) + getattr(batch, key))
            for reason, count in batch.failure_reasons.items():
                metrics.failure_reasons[reason] = (
                    metrics.failure_reasons.get(reason, 0) + count
                )
        except BaseException:
            db.session.rollback()
            raise


def apply_page(item, metrics):
    from backend.jobs import jikan_etl as anime, manga_etl as manga
    from backend.services.jikan_client import (
        JikanAnimePage,
        JikanMangaPage,
        JikanSeasonPage,
    )

    if "failure" in item:
        # Preserve existing recovery for a provider catalogue that shrank.
        page = (
            1 if item["failure"] == "not_found" and item["page"] > 1 else item["page"]
        )
        anime._record_page_error(item["key"], page, RuntimeError(item["failure"]))
        metrics.pages_failed += 1
        reason = "page_" + item["failure"]
        metrics.failure_reasons[reason] = metrics.failure_reasons.get(reason, 0) + 1
        metrics.cursors[item["key"]] = page
        return
    result = item["result"]
    result["entries"] = [_safe_payload(entry) for entry in result["entries"]]
    if item["kind"] == "manga_page":
        capped = result["page"] >= 1000 and result["has_next_page"]
        if capped:
            # Finish the accessible range with its cursor in the same commit.
            result["has_next_page"] = False
        applied = manga._apply_manga_page(
            JikanMangaPage(**result),
            provider_type=item["provider_type"],
            state_key=item["key"],
            track_changes=True,
        )
        if capped:
            metrics.provider_caps.append(item["key"])
    else:
        season = item["kind"] == "season"
        applied = anime._apply_season_page(
            (JikanSeasonPage if season else JikanAnimePage)(**result),
            state_key=item["key"],
            year=item.get("year"),
            season=item.get("season"),
            discover_missing=True,
            tv_only=False,
            allowed_types=None
            if season
            else frozenset({anime._anime_type(item["provider_type"])}),
            default_type=None if season else anime._anime_type(item["provider_type"]),
            track_changes=True,
        )
    metrics.pages_applied += 1
    metrics.selected += applied.saved
    metrics.changed += applied.changed
    metrics.unchanged += applied.saved - applied.changed
    metrics.removed += getattr(
        applied, "removed_adult", getattr(applied, "removed_hentai", 0)
    )
    metrics.cursors[item["key"]] = result["page"] + 1 if result["has_next_page"] else 1


def report(metrics, budget, outcome):
    values = {
        **asdict(metrics),
        "http_attempted": budget.attempted,
        "http_successful": budget.successful,
        "http_failed": budget.failed,
        "requests_avoided": budget.avoided,
        "request_limit": budget.limit,
        "exhausted_lanes": sorted(budget.exhausted),
        "outcome": outcome,
    }
    print("ETL efficiency: " + json.dumps(values, sort_keys=True))
    path = os.getenv("GITHUB_STEP_SUMMARY")
    if path:
        with Path(path).open("a", encoding="utf-8") as summary:
            summary.write("### Bounded catalogue sync\n\n")
            summary.write(
                "These are ETL counters, not measured Neon CU-hours or transfer.\n\n"
            )
            for key, value in values.items():
                summary.write(f"- {key}: {value}\n")


def run(
    *,
    limit=200,
    streaming_limit=100,
    page_limit=10,
    batch_size=25,
    request_budget=800,
    policy=None,
):
    from backend.jobs import jikan_etl as anime, manga_etl as manga

    if (
        limit < 2
        or min(streaming_limit, page_limit, batch_size) <= 0
        or request_budget < 40
    ):
        raise ValueError(
            "limit must be at least 2, request_budget at least 40, and other limits positive"
        )
    policy = policy or RefreshPolicy.from_env()
    now = datetime.now(timezone.utc)
    budget = RequestBudget(
        request_budget,
        lane_limits={
            "discovery": request_budget // 4,
            "anime": request_budget * 3 // 10,
            "manga": request_budget * 3 // 20,
            "manhwa": request_budget * 3 // 20,
            "streaming": request_budget * 3 // 20,
        },
    )
    client = JikanClient(budget=budget)
    metrics = SyncMetrics()
    outcome = "failed"
    apply_started = None
    try:
        with app.app_context():
            anime._ensure_schema()
            pages = plan_pages(now, page_limit, policy)
            metrics.cursors = {page["key"]: page["page"] for page in pages}
            queues = {}
            for kind, queue, cap, label in (
                ("anime", "detail", limit, "anime"),
                ("manga", "detail", (limit + 1) // 2, "manga"),
                ("manhwa", "detail", limit // 2, "manhwa"),
                ("anime", "streaming", streaming_limit, "streaming"),
            ):
                queues[label], deferred = plan_details(kind, queue, cap, now, policy)
                metrics.selected += len(queues[label])
                metrics.deferred += deferred
            db.session.remove()
            db.engine.dispose()
        with (
            TemporaryFile(mode="w+t", encoding="utf-8") as spool,
            TemporaryFile(mode="w+t", encoding="utf-8") as cache_file,
        ):
            client.response_cache = ResponseCache(cache_file)
            fetch_started = monotonic()
            try:
                fetch_work(client, budget, pages, queues, spool, metrics)
            finally:
                metrics.fetch_seconds = monotonic() - fetch_started
            spool.seek(0)
            apply_started = monotonic()
            pending = []
            for line in spool:
                item = json.loads(line)
                if "key" in item:
                    apply_page(item, metrics)
                else:
                    pending.append(item)
                    if len(pending) == batch_size:
                        apply_details(pending, now, policy, metrics)
                        pending.clear()
            if pending:
                apply_details(pending, now, policy, metrics)
        # Retain established adult-only cleanup; never prune to meet a size quota.
        metrics.removed += anime.remove_hentai_anime() + manga.remove_adult_manga()
        if metrics.changed or metrics.removed:
            anime._refresh_and_report_catalogue_facets()
        recoverable_failures = metrics.failed or metrics.pages_failed
        verified_progress = metrics.pages_applied or metrics.records_succeeded
        if not verified_progress and (recoverable_failures or budget.failed):
            raise RuntimeError(
                "ETL requests failed without verified progress; committed retry state is retained"
            )
        outcome = (
            "success_with_warnings"
            if recoverable_failures
            else "bounded_success"
            if budget.exhausted
            or metrics.deferred
            or metrics.provider_caps
            or any(page > 1 for page in metrics.cursors.values())
            else "success"
        )
        return metrics
    finally:
        if apply_started is not None:
            metrics.apply_seconds = monotonic() - apply_started
        report(metrics, budget, outcome)
