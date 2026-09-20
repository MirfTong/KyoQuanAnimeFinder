"""Flask REST API and single-service React application host."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from random import randrange
from threading import Lock
from time import monotonic
from typing import Any

from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory
from sqlalchemy import Date, cast, func, literal, or_, select, union_all
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import lazyload, load_only, noload, selectinload

from backend.models import (
    Anime,
    AnimeStreamingService,
    Author,
    CatalogueFacet,
    Genre,
    JikanRefreshState,
    JikanSyncState,
    Manga,
    MangaAuthor,
    SiteVisit,
    StreamingService,
    Studio,
    db,
)
from backend.schema import ensure_anime_schema


load_dotenv()

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ["DATABASE_URL"]
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db.init_app(app)

API_PREFIX = "/api/v1"
DATABASE_UNAVAILABLE_MESSAGE = (
    "The catalogue is temporarily unavailable because its database cannot be "
    "reached. Please try again shortly."
)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_BUILD_DIR = PROJECT_ROOT / "static" / "react"
MAX_PAGE_SIZE = 100
MAX_TAG_OPTIONS = 100
VALID_SEASONS = frozenset({"winter", "spring", "summer", "fall"})
VALID_ANIME_STATUSES = frozenset(
    {"CURRENTLY_AIRING", "FINISHED_AIRING", "NOT_YET_AIRED"}
)
ANIME_STATUS_ALIASES = {
    "AIRING": "CURRENTLY_AIRING",
    "CURRENTLY_AIRING": "CURRENTLY_AIRING",
    "FINISHED": "FINISHED_AIRING",
    "FINISHED_AIRING": "FINISHED_AIRING",
    "NOT_YET_AIRED": "NOT_YET_AIRED",
    "NOT_YET_AIRING": "NOT_YET_AIRED",
}
COMMON_SORTS = frozenset(
    {"top_rated", "newest", "oldest", "title", "most_popular", "most_members"}
)
ANIME_SORTS = COMMON_SORTS | {"most_episodes"}
PRINT_SORTS = COMMON_SORTS | {"most_chapters"}
TYPE_ALIASES = {"TV SPECIAL": "SPECIAL"}
CONTENT_TYPE_SCOPES = {
    "ANIME": frozenset({"ANIME"}),
    "MANGA": frozenset({"MANGA"}),
    "MANHWA": frozenset({"MANHWA"}),
    "ALL": frozenset({"ANIME", "MANGA", "MANHWA"}),
}
CACHE_TTL_SECONDS = 300
MAX_CACHE_ENTRIES = 512
CACHE_GENERATION_POLL_SECONDS = 15
ANALYTICS_CACHE_TTL_SECONDS = 60
ANALYTICS_COOKIE_NAME = "kyoquan_visitor"
ANALYTICS_COOKIE_MAX_AGE_SECONDS = 30 * 24 * 60 * 60
ANALYTICS_CATEGORY = "frontend"
ANALYTICS_EXCLUDED_PATHS = frozenset({"/health", "/healthz", "/ready", "/readyz"})
ANALYTICS_BOT_MARKERS = (
    "bot",
    "crawler",
    "spider",
    "slurp",
    "facebookexternalhit",
    "uptimerobot",
    "kube-probe",
    "elb-healthchecker",
    "github-actions",
    "go-http-client",
    "headlesschrome",
    "insomnia",
    "okhttp",
    "postmanruntime",
    "python-requests",
    "curl/",
    "wget/",
)
MULTI_VALUE_FILTERS = frozenset(
    {
        "author",
        "exclude_genre",
        "exclude_tag",
        "genre",
        "season",
        "status",
        "streaming_service",
        "studio",
        "tag",
        "type",
    }
)
CASE_INSENSITIVE_MULTI_FILTERS = MULTI_VALUE_FILTERS.difference(
    {"exclude_genre", "exclude_tag", "genre", "tag"}
)


class TtlCache:
    """Small process-local cache for repeatable catalogue metadata queries."""

    def __init__(
        self,
        *,
        max_entries: int = MAX_CACHE_ENTRIES,
        ttl_seconds: int = CACHE_TTL_SECONDS,
    ) -> None:
        self._values: dict[tuple[Any, ...], tuple[float, Any]] = {}
        self._lock = Lock()
        self._max_entries = max(1, max_entries)
        self._ttl_seconds = max(1, ttl_seconds)

    def get_or_create(self, key: tuple[Any, ...], factory):
        now = monotonic()
        with self._lock:
            cached = self._values.get(key)
            if cached is not None and cached[0] > now:
                return cached[1]

        value = factory()
        with self._lock:
            expired = [
                existing_key
                for existing_key, (expires_at, _) in self._values.items()
                if expires_at <= now
            ]
            for existing_key in expired:
                self._values.pop(existing_key, None)
            while len(self._values) >= self._max_entries:
                oldest_key = min(
                    self._values,
                    key=lambda existing_key: self._values[existing_key][0],
                )
                self._values.pop(oldest_key, None)
            self._values[key] = (now + self._ttl_seconds, value)
        return value

    def clear(self) -> None:
        with self._lock:
            self._values.clear()


response_cache = TtlCache()
analytics_response_cache = TtlCache(
    max_entries=4,
    ttl_seconds=ANALYTICS_CACHE_TTL_SECONDS,
)


class CacheGenerationMonitor:
    """Invalidate local caches when a separate ETL process publishes data."""

    def __init__(self) -> None:
        self._generation: str | None = None
        self._initialized = False
        self._next_check = 0.0
        self._lock = Lock()

    def refresh_if_needed(self) -> None:
        now = monotonic()
        with self._lock:
            if now < self._next_check:
                return
            self._next_check = now + CACHE_GENERATION_POLL_SECONDS

        try:
            generation = db.session.scalar(
                select(JikanSyncState.last_completed_at).where(
                    JikanSyncState.key == "catalogue_cache_generation"
                )
            )
        except SQLAlchemyError:
            db.session.rollback()
            return
        token = generation.isoformat() if generation is not None else None
        with self._lock:
            previous = self._generation
            initialized = self._initialized
            self._generation = token
            self._initialized = True
        if initialized and token != previous:
            response_cache.clear()


cache_generation_monitor = CacheGenerationMonitor()


def _analytics_cookie_is_secure() -> bool:
    """Use a Secure cookie for HTTPS deployments, with a local override."""
    configured = os.getenv("ANALYTICS_COOKIE_SECURE")
    if configured is not None:
        return configured.strip().casefold() in {"1", "true", "yes", "on"}
    forwarded_proto = request.headers.get("X-Forwarded-Proto", "")
    return (
        request.is_secure
        or forwarded_proto.casefold().split(",")[0].strip() == "https"
    )


def _is_valid_analytics_cookie(value: str | None) -> bool:
    return bool(
        value
        and 32 <= len(value) <= 128
        and all(character.isalnum() or character in "-_" for character in value)
    )


def _analytics_request_is_trackable(response) -> bool:
    """Allow only successful browser HTML navigations into visit analytics."""
    if request.method != "GET" or not 200 <= response.status_code < 400:
        return False
    if response.mimetype != "text/html":
        return False
    if request.path in ANALYTICS_EXCLUDED_PATHS or request.path.startswith(
        (API_PREFIX, "/assets/", "/static/")
    ):
        return False
    user_agent = request.headers.get("User-Agent", "").casefold()
    if not user_agent.startswith("mozilla/"):
        return False
    if any(marker in user_agent for marker in ANALYTICS_BOT_MARKERS):
        return False
    # Real navigations advertise HTML. Keeping an absent header compatible
    # with minimal browser test clients, while known probes are rejected above.
    accept = request.headers.get("Accept", "").casefold()
    return not accept or "text/html" in accept


def _record_site_visit(visitor_token: str) -> None:
    """Atomically aggregate one anonymous browser visit for the current day."""
    now = datetime.now(timezone.utc)
    token_hash = hashlib.sha256(visitor_token.encode("utf-8")).hexdigest()
    statement = postgresql_insert(SiteVisit).values(
        visitor_token_hash=token_hash,
        visit_date=now.date(),
        route=ANALYTICS_CATEGORY,
        visit_count=1,
        first_visited_at=now,
        last_visited_at=now,
    )
    statement = statement.on_conflict_do_update(
        constraint="uq_site_visit_visitor_day_route",
        set_={
            "visit_count": SiteVisit.visit_count + 1,
            "last_visited_at": now,
        },
    )
    db.session.execute(statement)
    db.session.commit()
    analytics_response_cache.clear()


def _analytics_period_rows(bucket, *, since: date) -> list[dict[str, int | str]]:
    rows = db.session.execute(
        select(
            bucket.label("period"),
            func.coalesce(func.sum(SiteVisit.visit_count), 0).label("visits"),
            func.count(func.distinct(SiteVisit.visitor_token_hash)).label(
                "unique_visitors"
            ),
        )
        .where(SiteVisit.visit_date >= since)
        .group_by(bucket)
        .order_by(bucket)
    ).all()
    serialized_rows: list[dict[str, int | str]] = []
    for period, visits, unique_visitors in rows:
        period_date = period.date() if isinstance(period, datetime) else period
        serialized_rows.append(
            {
                "period": period_date.isoformat(),
                "visits": int(visits or 0),
                "unique_visitors": int(unique_visitors or 0),
            }
        )
    return serialized_rows


def _analytics_report() -> dict[str, Any]:
    """Return bounded, aggregate-only metrics for the token-protected route."""
    today = datetime.now(timezone.utc).date()
    total_visits, total_visitors = db.session.execute(
        select(
            func.coalesce(func.sum(SiteVisit.visit_count), 0),
            func.count(func.distinct(SiteVisit.visitor_token_hash)),
        )
    ).one()
    category_rows = db.session.execute(
        select(
            SiteVisit.route,
            func.coalesce(func.sum(SiteVisit.visit_count), 0),
            func.count(func.distinct(SiteVisit.visitor_token_hash)),
        )
        .group_by(SiteVisit.route)
        .order_by(SiteVisit.route)
    ).all()
    return {
        "total": {
            "visits": int(total_visits or 0),
            "unique_visitors": int(total_visitors or 0),
        },
        "daily": _analytics_period_rows(
            SiteVisit.visit_date,
            since=today - timedelta(days=6),
        ),
        "weekly": _analytics_period_rows(
            cast(func.date_trunc("week", SiteVisit.visit_date), Date),
            since=today - timedelta(weeks=11),
        ),
        "monthly": _analytics_period_rows(
            cast(func.date_trunc("month", SiteVisit.visit_date), Date),
            since=today - timedelta(days=366),
        ),
        "categories": [
            {
                "category": route,
                "visits": int(visits or 0),
                "unique_visitors": int(unique_visitors or 0),
            }
            for route, visits, unique_visitors in category_rows
        ],
    }


def _admin_analytics_access_error():
    expected_token = os.getenv("ADMIN_ANALYTICS_TOKEN")
    if not expected_token:
        return (
            jsonify({"error": {"message": "Analytics access is unavailable"}}),
            403,
        )

    authorization = request.headers.get("Authorization", "")
    scheme, _, supplied_token = authorization.partition(" ")
    if scheme.casefold() != "bearer" or not supplied_token:
        response = jsonify({"error": {"message": "Authorization required"}})
        response.status_code = 401
        response.headers["WWW-Authenticate"] = "Bearer"
        return response
    if not hmac.compare_digest(supplied_token, expected_token):
        return jsonify({"error": {"message": "Forbidden"}}), 403
    return None


@app.before_request
def invalidate_stale_catalogue_cache() -> None:
    if request.method == "GET" and request.path.startswith(API_PREFIX):
        cache_generation_monitor.refresh_if_needed()


@dataclass(frozen=True)
class CommonFilters:
    query: str
    min_score: float | None
    min_year: int | None
    max_year: int | None
    genres: tuple[str, ...]
    tags: tuple[str, ...]
    excluded_genres: tuple[str, ...]
    excluded_tags: tuple[str, ...]


@dataclass(frozen=True)
class AnimeFilters:
    min_episodes: int | None
    max_episodes: int | None
    anime_types: tuple[str, ...]
    seasons: tuple[str, ...]
    statuses: tuple[str, ...]
    studios: tuple[str, ...]
    streaming_services: tuple[str, ...]

    @property
    def active(self) -> bool:
        return bool(
            self.min_episodes
            or self.max_episodes
            or self.anime_types
            or self.seasons
            or self.statuses
            or self.studios
            or self.streaming_services
        )


@dataclass(frozen=True)
class MangaFilters:
    statuses: tuple[str, ...]
    authors: tuple[str, ...]
    min_chapters: int | None
    max_chapters: int | None
    min_volumes: int | None
    max_volumes: int | None

    @property
    def active(self) -> bool:
        return bool(
            self.statuses
            or self.authors
            or self.min_chapters
            or self.max_chapters
            or self.min_volumes
            or self.max_volumes
        )


def _current_season_identity(now: datetime | None = None) -> tuple[int, str]:
    """Return the current anime season using Japan's calendar."""
    japan_time = timezone(timedelta(hours=9))
    current = (now or datetime.now(timezone.utc)).astimezone(japan_time)
    seasons = ("winter", "spring", "summer", "fall")
    return current.year, seasons[(current.month - 1) // 3]


def _next_season_identity(now: datetime | None = None) -> tuple[int, str]:
    """Return the next anime season using Japan's calendar."""
    year, season = _current_season_identity(now)
    seasons = ("winter", "spring", "summer", "fall")
    next_index = seasons.index(season) + 1
    if next_index == len(seasons):
        return year + 1, seasons[0]
    return year, seasons[next_index]


class ApiError(Exception):
    """A validation error that should be returned as JSON."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        self.message = message
        self.status_code = status_code


@app.errorhandler(ApiError)
def handle_api_error(error: ApiError):
    return jsonify({"error": {"message": error.message}}), error.status_code


@app.errorhandler(SQLAlchemyError)
def handle_database_error(_error: SQLAlchemyError):
    """Keep database outages from becoming blank or provider-generated pages."""
    try:
        db.session.rollback()
    except Exception:
        # The connection may already be gone; the response must still be safe.
        pass
    response = jsonify(
        {
            "error": {
                "code": "database_unavailable",
                "message": DATABASE_UNAVAILABLE_MESSAGE,
            }
        }
    )
    response.status_code = 503
    response.headers["Retry-After"] = "300"
    return response


@app.errorhandler(404)
def handle_not_found(_error):
    if request.path.startswith(API_PREFIX):
        return jsonify({"error": {"message": "Resource not found"}}), 404
    return _error


def _integer_argument(name: str, *, minimum: int, maximum: int | None = None) -> int | None:
    value = request.args.get(name)
    if value is None or value == "":
        return None
    try:
        parsed = int(value)
    except ValueError as error:
        raise ApiError(f"{name} must be an integer") from error
    if parsed < minimum or (maximum is not None and parsed > maximum):
        limit = f" between {minimum} and {maximum}" if maximum is not None else f" at least {minimum}"
        raise ApiError(f"{name} must be{limit}")
    return parsed


def _float_argument(name: str, *, minimum: float, maximum: float) -> float | None:
    value = request.args.get(name)
    if value is None or value == "":
        return None
    try:
        parsed = float(value)
    except ValueError as error:
        raise ApiError(f"{name} must be a number") from error
    if not minimum <= parsed <= maximum:
        raise ApiError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def _list_argument(name: str) -> list[str]:
    """Read repeatable and comma-separated filter parameters."""
    values = []
    for raw_value in request.args.getlist(name):
        values.extend(value.strip() for value in raw_value.split(",") if value.strip())
    return list(dict.fromkeys(values))


def _preview_requested() -> bool:
    """Return whether the card client requested a compact list payload."""
    return request.args.get("preview", "").strip().casefold() in {
        "1",
        "true",
        "yes",
    }


def _request_filter_signature(*, exclude: set[str]) -> tuple[Any, ...]:
    signature = []
    for key in request.args:
        if key in exclude:
            continue
        if key in MULTI_VALUE_FILTERS:
            values = tuple(
                sorted(
                    {
                        (
                            value.strip().casefold()
                            if key in CASE_INSENSITIVE_MULTI_FILTERS
                            else value.strip()
                        )
                        for raw_value in request.args.getlist(key)
                        for value in raw_value.split(",")
                        if value.strip()
                    }
                )
            )
        else:
            values = tuple(
                value.strip().casefold() if key == "q" else value.strip()
                for value in request.args.getlist(key)
            )
        signature.append((key, values))
    return tuple(sorted(signature))


def _cached_scalar_count(key: tuple[Any, ...], statement) -> int:
    return response_cache.get_or_create(
        key,
        lambda: int(db.session.scalar(statement) or 0),
    )


def _normalized_type(value: str) -> str:
    """Match filter input to the canonical uppercase database type."""
    normalized = " ".join(value.replace("_", " ").split()).upper()
    return TYPE_ALIASES.get(normalized, normalized)


def _normalized_content_type(value: str, *, allow_all: bool = True) -> str:
    normalized = value.strip().upper()
    valid_types = CONTENT_TYPE_SCOPES if allow_all else {
        key: scope for key, scope in CONTENT_TYPE_SCOPES.items() if key != "ALL"
    }
    if normalized not in valid_types:
        choices = "ANIME, MANGA, MANHWA, or ALL" if allow_all else (
            "ANIME, MANGA, or MANHWA"
        )
        raise ApiError(f"content_type must be {choices}")
    return normalized


def _content_type_argument(*, default: str) -> str:
    value = request.args.get("content_type")
    if value is None or not value.strip():
        return default
    return _normalized_content_type(value)


def _sort_argument(content_types: frozenset[str] | set[str]) -> str:
    """Validate sorting against the selected media scope."""
    sort = request.args.get("sort", "top_rated").strip().lower()
    normalized_types = frozenset(content_types)
    if normalized_types == frozenset({"ANIME"}):
        valid_sorts = ANIME_SORTS
    elif normalized_types.issubset({"MANGA", "MANHWA"}):
        valid_sorts = PRINT_SORTS
    else:
        valid_sorts = COMMON_SORTS
    if sort not in valid_sorts:
        raise ApiError(
            "sort must be " + ", ".join(sorted(valid_sorts))
        )
    return sort


def _normalized_status(value: str) -> str:
    """Normalize UI constants such as NOT_YET_PUBLISHED to provider labels."""
    return " ".join(value.replace("_", " ").split()).casefold()


def _normalized_anime_status(value: str) -> str:
    normalized = "_".join(value.replace("-", " ").split()).upper()
    status = ANIME_STATUS_ALIASES.get(normalized)
    if status is None:
        raise ApiError(
            "anime status must be CURRENTLY_AIRING, FINISHED_AIRING, "
            "or NOT_YET_AIRED"
        )
    return status


def _normalized_entity_name(value: str) -> str:
    """Normalize relationship filter values exactly as the ETL does."""
    normalized = " ".join(
        unicodedata.normalize("NFKC", value).split()
    ).casefold()
    if not normalized:
        raise ApiError("filter names cannot be blank")
    return normalized


def _escaped_search_pattern(query: str) -> str:
    escaped = (
        query.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )
    return f"%{escaped}%"


def _common_filter_values() -> CommonFilters:
    min_year = _integer_argument("min_year", minimum=1, maximum=3000)
    max_year = _integer_argument("max_year", minimum=1, maximum=3000)
    if min_year is not None and max_year is not None and min_year > max_year:
        raise ApiError("min_year cannot be greater than max_year")
    return CommonFilters(
        query=request.args.get("q", "").strip(),
        min_score=_float_argument("min_score", minimum=0, maximum=10),
        min_year=min_year,
        max_year=max_year,
        genres=tuple(_list_argument("genre")),
        tags=tuple(_list_argument("tag")),
        excluded_genres=tuple(_list_argument("exclude_genre")),
        excluded_tags=tuple(_list_argument("exclude_tag")),
    )


def _anime_filter_values(*, include_status: bool = True) -> AnimeFilters:
    seasons = tuple(season.lower() for season in _list_argument("season"))
    invalid_seasons = set(seasons).difference(VALID_SEASONS)
    if invalid_seasons:
        raise ApiError("season must be winter, spring, summer, or fall")
    min_episodes = _integer_argument(
        "min_episodes", minimum=1, maximum=10000
    )
    max_episodes = _integer_argument(
        "max_episodes", minimum=1, maximum=10000
    )
    if (
        min_episodes is not None
        and max_episodes is not None
        and min_episodes > max_episodes
    ):
        raise ApiError("min_episodes cannot be greater than max_episodes")
    return AnimeFilters(
        min_episodes=min_episodes,
        max_episodes=max_episodes,
        anime_types=tuple(
            _normalized_type(value) for value in _list_argument("type")
        ),
        seasons=seasons,
        statuses=(
            tuple(
                _normalized_anime_status(value)
                for value in _list_argument("status")
            )
            if include_status
            else ()
        ),
        studios=tuple(
            _normalized_entity_name(value)
            for value in _list_argument("studio")
        ),
        streaming_services=tuple(
            _normalized_entity_name(value)
            for value in _list_argument("streaming_service")
        ),
    )


def _manga_filter_values(*, include_status: bool = True) -> MangaFilters:
    min_chapters = _integer_argument(
        "min_chapters", minimum=1, maximum=1_000_000
    )
    max_chapters = _integer_argument(
        "max_chapters", minimum=1, maximum=1_000_000
    )
    min_volumes = _integer_argument(
        "min_volumes", minimum=1, maximum=100_000
    )
    max_volumes = _integer_argument(
        "max_volumes", minimum=1, maximum=100_000
    )
    if (
        min_chapters is not None
        and max_chapters is not None
        and min_chapters > max_chapters
    ):
        raise ApiError("min_chapters cannot be greater than max_chapters")
    if (
        min_volumes is not None
        and max_volumes is not None
        and min_volumes > max_volumes
    ):
        raise ApiError("min_volumes cannot be greater than max_volumes")
    return MangaFilters(
        statuses=(
            tuple(
                _normalized_status(value)
                for value in _list_argument("status")
            )
            if include_status
            else ()
        ),
        authors=tuple(
            _normalized_entity_name(value)
            for value in _list_argument("author")
        ),
        min_chapters=min_chapters,
        max_chapters=max_chapters,
        min_volumes=min_volumes,
        max_volumes=max_volumes,
    )


def _serialize_anime(
    anime: Anime,
    *,
    detailed: bool = False,
    preview: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": anime.animeID,
        "mal_id": anime.mal_id,
        "content_type": "ANIME",
        "title": anime.title,
        "type": anime.type,
        "season": anime.season,
        "year": anime.year,
        "score": anime.score,
        "popularity": anime.popularity,
        "members": anime.members,
        "episodes": anime.episodes,
        "image_url": anime.image_url,
        "genres": [genre.name for genre in anime.genre_entries],
    }
    if detailed or not preview:
        payload.update(
            {
                "alternative_title": anime.alternative_title,
                "status": anime.status,
                "mal_url": anime.mal_url,
                "sequel": anime.sequel,
            }
        )
        payload["studios"] = [
            {"mal_id": studio.mal_id, "name": studio.name}
            for studio in sorted(
                anime.studio_entries,
                key=lambda entry: entry.name.casefold(),
            )
        ]
        payload["streaming_services"] = [
            {
                "name": link.streaming_service.name,
                "url": link.url,
            }
            for link in sorted(
                anime.streaming_links,
                key=lambda entry: entry.streaming_service.name.casefold(),
            )
        ]
    if detailed:
        payload["synopsis"] = anime.synopsis
        payload["genres_detailed"] = anime.genres_detailed
        payload["last_jikan_sync"] = (
            anime.last_jikan_sync.isoformat() if anime.last_jikan_sync else None
        )
    return payload


def _serialize_manga(
    manga: Manga,
    *,
    detailed: bool = False,
    preview: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": manga.mangaID,
        "mal_id": manga.mal_id,
        "content_type": manga.content_type,
        "title": manga.title,
        "type": manga.manga_type,
        "manga_type": manga.manga_type,
        "year": manga.publication_year,
        "publication_year": manga.publication_year,
        "score": manga.score,
        "popularity": manga.popularity,
        "members": manga.members,
        "chapters": manga.chapters,
        "volumes": manga.volumes,
        "image_url": manga.image_url,
        "genres": [genre.name for genre in manga.genre_entries],
    }
    if detailed or not preview:
        payload.update(
            {
                "alternative_title": manga.alternative_title,
                "status": manga.status,
                "mal_url": manga.mal_url,
            }
        )
        payload["authors"] = [
            {
                "mal_id": link.author.mal_id,
                "name": link.author.name,
                "role": link.role,
            }
            for link in sorted(
                manga.author_links,
                key=lambda entry: (
                    entry.author.name.casefold(),
                    entry.role or "",
                ),
            )
        ]
    if detailed:
        payload["synopsis"] = manga.synopsis
        payload["genres_detailed"] = manga.genres_detailed or []
        payload["last_jikan_sync"] = (
            manga.last_jikan_sync.isoformat() if manga.last_jikan_sync else None
        )
    return payload


def _attach_verified_refresh(
    payload: dict[str, Any], kind: str, mal_id: int
) -> None:
    """Expose independent detail/listing verification in one indexed query."""
    timestamps = dict(
        db.session.execute(
            select(
                JikanRefreshState.queue, JikanRefreshState.last_success_at
            ).where(
                JikanRefreshState.kind == kind,
                JikanRefreshState.queue.in_(("detail", "listing")),
                JikanRefreshState.mal_id == mal_id,
            )
        ).all()
    )
    for queue, field in (
        ("detail", "last_verified_refresh"),
        ("listing", "last_listing_refresh"),
    ):
        timestamp = timestamps.get(queue)
        payload[field] = timestamp.isoformat() if timestamp else None


def _public_statement(model, *, preview: bool = False, detailed: bool = False):
    """Return the indexed, ETL-maintained public catalogue query."""
    statement = (
        select(model)
        .where(model.is_adult.is_(False))
        .options(lazyload("*"), selectinload(model.genre_entries))
    )
    if not detailed:
        if model is Anime:
            anime_columns = [
                Anime.animeID,
                Anime.mal_id,
                Anime.title,
                Anime.type,
                Anime.season,
                Anime.year,
                Anime.score,
                Anime.popularity,
                Anime.members,
                Anime.episodes,
                Anime.image_url,
            ]
            if not preview:
                anime_columns.extend(
                    [
                        Anime.alternative_title,
                        Anime.status,
                        Anime.mal_url,
                        Anime.sequel,
                    ]
                )
            statement = statement.options(load_only(*anime_columns))
        elif model is Manga:
            manga_columns = [
                Manga.mangaID,
                Manga.mal_id,
                Manga.content_type,
                Manga.title,
                Manga.manga_type,
                Manga.publication_year,
                Manga.score,
                Manga.popularity,
                Manga.members,
                Manga.chapters,
                Manga.volumes,
                Manga.image_url,
            ]
            if not preview:
                manga_columns.extend(
                    [
                        Manga.alternative_title,
                        Manga.status,
                        Manga.mal_url,
                    ]
                )
            statement = statement.options(load_only(*manga_columns))
    if model is Anime:
        if preview:
            statement = statement.options(
                noload(Anime.studio_entries),
                noload(Anime.streaming_links),
            )
        else:
            statement = statement.options(
                selectinload(Anime.studio_entries),
                selectinload(Anime.streaming_links).selectinload(
                    AnimeStreamingService.streaming_service
                ),
            )
    elif model is Manga:
        if preview:
            statement = statement.options(noload(Manga.author_links))
        else:
            statement = statement.options(
                selectinload(Manga.author_links).selectinload(
                    MangaAuthor.author
                )
            )
    return statement


def _anime_statement(*, preview: bool = False, detailed: bool = False):
    """Base public anime query."""
    return _public_statement(Anime, preview=preview, detailed=detailed)


def _manga_statement(
    content_types: frozenset[str] | set[str] | None = None,
    *,
    preview: bool = False,
    detailed: bool = False,
):
    """Base public Manga/Manhwa query."""
    statement = _public_statement(
        Manga, preview=preview, detailed=detailed
    )
    if content_types is not None:
        statement = statement.where(
            Manga.content_type.in_(sorted(content_types))
        )
    return statement


def _apply_common_filters(
    statement,
    model,
    year_column,
    filters: CommonFilters,
):
    if filters.query:
        pattern = _escaped_search_pattern(filters.query)
        statement = statement.where(
            or_(
                model.title.ilike(pattern, escape="\\"),
                model.alternative_title.ilike(pattern, escape="\\"),
            )
        )
    if filters.min_score is not None:
        statement = statement.where(model.score >= filters.min_score)
    if filters.min_year is not None:
        statement = statement.where(year_column >= filters.min_year)
    if filters.max_year is not None:
        statement = statement.where(year_column <= filters.max_year)
    for genre in filters.genres:
        statement = statement.where(
            model.genre_entries.any(Genre.name == genre)
        )
    for tag in filters.tags:
        statement = statement.where(model.genres_detailed.contains([tag]))
    # Relationship ``any`` compiles to an EXISTS subquery.  Negating it keeps
    # exclusion filtering in PostgreSQL instead of loading genre links into
    # Python, and makes an include/exclude conflict safely return no matches.
    for genre in filters.excluded_genres:
        statement = statement.where(
            ~model.genre_entries.any(Genre.name == genre)
        )
    for tag in filters.excluded_tags:
        statement = statement.where(
            ~model.genres_detailed.contains([tag])
        )
    return statement


def _filtered_anime_statement(
    common_filters: CommonFilters | None = None,
    anime_filters: AnimeFilters | None = None,
    *,
    preview: bool = False,
):
    common_filters = common_filters or _common_filter_values()
    anime_filters = anime_filters or _anime_filter_values()
    statement = _apply_common_filters(
        _anime_statement(preview=preview), Anime, Anime.year, common_filters
    )
    if anime_filters.min_episodes is not None:
        statement = statement.where(
            Anime.episodes >= anime_filters.min_episodes
        )
    if anime_filters.max_episodes is not None:
        statement = statement.where(
            Anime.episodes <= anime_filters.max_episodes
        )
    if anime_filters.anime_types:
        statement = statement.where(
            Anime.type.in_(anime_filters.anime_types)
        )
    if anime_filters.seasons:
        statement = statement.where(Anime.season.in_(anime_filters.seasons))
    if anime_filters.statuses:
        statement = statement.where(Anime.status.in_(anime_filters.statuses))
    if anime_filters.studios:
        statement = statement.where(
            Anime.studio_entries.any(
                Studio.normalized_name.in_(anime_filters.studios)
            )
        )
    if anime_filters.streaming_services:
        statement = statement.where(
            Anime.streaming_links.any(
                AnimeStreamingService.streaming_service.has(
                    StreamingService.normalized_name.in_(
                        anime_filters.streaming_services
                    )
                )
            )
        )
    return statement


def _filtered_manga_statement(
    content_types: frozenset[str] | set[str],
    common_filters: CommonFilters | None = None,
    manga_filters: MangaFilters | None = None,
    *,
    preview: bool = False,
):
    common_filters = common_filters or _common_filter_values()
    manga_filters = manga_filters or _manga_filter_values()
    statement = _apply_common_filters(
        _manga_statement(content_types, preview=preview),
        Manga,
        Manga.publication_year,
        common_filters,
    )
    if manga_filters.statuses:
        statement = statement.where(
            func.lower(func.trim(Manga.status)).in_(
                manga_filters.statuses
            )
        )
    if manga_filters.authors:
        statement = statement.where(
            Manga.author_links.any(
                MangaAuthor.author.has(
                    Author.normalized_name.in_(manga_filters.authors)
                )
            )
        )
    if manga_filters.min_chapters is not None:
        statement = statement.where(
            Manga.chapters >= manga_filters.min_chapters
        )
    if manga_filters.max_chapters is not None:
        statement = statement.where(
            Manga.chapters <= manga_filters.max_chapters
        )
    if manga_filters.min_volumes is not None:
        statement = statement.where(
            Manga.volumes >= manga_filters.min_volumes
        )
    if manga_filters.max_volumes is not None:
        statement = statement.where(
            Manga.volumes <= manga_filters.max_volumes
        )
    return statement


def _catalogue_rows_subquery(
    content_types: frozenset[str] | set[str],
    *,
    branch_limit: int | None = None,
    sort: str = "top_rated",
):
    """Build one normalized identity stream for deterministic mixed paging."""
    common_filters = _common_filter_values()
    effective_types = set(content_types)
    includes_anime = "ANIME" in effective_types
    includes_print = bool(
        effective_types.intersection({"MANGA", "MANHWA"})
    )
    if includes_anime and includes_print and _list_argument("status"):
        raise ApiError(
            "status cannot be shared across mixed content; select Anime, "
            "Manga, or Manhwa first"
        )
    anime_filters = _anime_filter_values(include_status=not includes_print)
    manga_filters = _manga_filter_values(include_status=not includes_anime)

    # A medium-specific filter cannot sensibly match rows from the other
    # medium. This also makes such filters useful when content_type=ALL.
    if anime_filters.active:
        effective_types.intersection_update({"ANIME"})
    if manga_filters.active:
        effective_types.difference_update({"ANIME"})

    branches = []
    if "ANIME" in effective_types:
        branches.append(
            _filtered_anime_statement(
                common_filters, anime_filters, preview=True
            )
            .with_only_columns(
                literal("ANIME").label("content_type"),
                Anime.animeID.label("record_id"),
                Anime.mal_id.label("mal_id"),
                Anime.score.label("score"),
                Anime.popularity.label("popularity"),
                Anime.members.label("members"),
                Anime.title.label("title"),
                Anime.year.label("year"),
                Anime.episodes.label("length"),
            )
            .order_by(None)
        )

    manga_content_types = sorted(
        effective_types.intersection({"MANGA", "MANHWA"})
    )
    for manga_content_type in manga_content_types:
        branches.append(
            _filtered_manga_statement(
                {manga_content_type},
                common_filters,
                manga_filters,
                preview=True,
            )
            .with_only_columns(
                literal(manga_content_type).label("content_type"),
                Manga.mangaID.label("record_id"),
                Manga.mal_id.label("mal_id"),
                Manga.score.label("score"),
                Manga.popularity.label("popularity"),
                Manga.members.label("members"),
                Manga.title.label("title"),
                Manga.publication_year.label("year"),
                Manga.chapters.label("length"),
            )
            .order_by(None)
        )

    if not branches:
        return None
    if branch_limit is not None and len(branches) > 1:
        limited_branches = []
        for index, branch in enumerate(branches):
            branch_rows = branch.subquery(f"catalogue_branch_{index}")
            limited_branches.append(
                select(
                    branch_rows.c.content_type,
                    branch_rows.c.record_id,
                    branch_rows.c.mal_id,
                    branch_rows.c.score,
                    branch_rows.c.popularity,
                    branch_rows.c.members,
                    branch_rows.c.title,
                    branch_rows.c.year,
                    branch_rows.c.length,
                )
                .order_by(*_catalogue_order_clauses(branch_rows, sort))
                .limit(branch_limit)
            )
        branches = limited_branches
    combined = branches[0] if len(branches) == 1 else union_all(*branches)
    return combined.subquery("catalogue_rows")


def _catalogue_order_clauses(catalogue_rows, sort: str):
    score_order = catalogue_rows.c.score.desc().nullslast()
    title_order = (
        func.lower(catalogue_rows.c.title),
        catalogue_rows.c.title,
    )
    if sort == "newest":
        primary_order = (
            catalogue_rows.c.year.desc().nullslast(),
            score_order,
            *title_order,
        )
    elif sort == "oldest":
        primary_order = (
            catalogue_rows.c.year.asc().nullslast(),
            score_order,
            *title_order,
        )
    elif sort == "title":
        primary_order = (*title_order, score_order)
    elif sort == "most_popular":
        primary_order = (
            catalogue_rows.c.popularity.asc().nullslast(),
            score_order,
            *title_order,
        )
    elif sort == "most_members":
        primary_order = (
            catalogue_rows.c.members.desc().nullslast(),
            score_order,
            *title_order,
        )
    elif sort in {"most_episodes", "most_chapters"}:
        primary_order = (
            catalogue_rows.c.length.desc().nullslast(),
            score_order,
            *title_order,
        )
    else:
        primary_order = (score_order, *title_order)
    return (
        *primary_order,
        catalogue_rows.c.content_type,
        catalogue_rows.c.mal_id.nulls_last(),
        catalogue_rows.c.record_id,
    )


def _ordered_catalogue_rows(catalogue_rows, sort: str):
    statement = select(
        catalogue_rows.c.content_type,
        catalogue_rows.c.record_id,
        catalogue_rows.c.mal_id,
        catalogue_rows.c.score,
        catalogue_rows.c.title,
    )
    return statement.order_by(*_catalogue_order_clauses(catalogue_rows, sort))


def _catalogue_freshness(
    content_types: frozenset[str] | set[str],
) -> str | None:
    """Return a cached latest successful ETL timestamp for this scope."""
    normalized_types = frozenset(content_types)

    def load_freshness() -> str | None:
        timestamps = []
        if "ANIME" in normalized_types:
            anime_timestamp = db.session.scalar(
                select(func.max(Anime.last_jikan_sync)).where(
                    Anime.is_adult.is_(False)
                )
            )
            if anime_timestamp is not None:
                timestamps.append(anime_timestamp)
        if normalized_types.intersection({"MANGA", "MANHWA"}):
            manga_timestamp = db.session.scalar(
                select(func.max(Manga.last_jikan_sync)).where(
                    Manga.is_adult.is_(False),
                    Manga.content_type.in_(
                        sorted(
                            normalized_types.intersection(
                                {"MANGA", "MANHWA"}
                            )
                        )
                    ),
                )
            )
            if manga_timestamp is not None:
                timestamps.append(manga_timestamp)
        if not timestamps:
            return None

        def comparable(timestamp: datetime) -> datetime:
            return (
                timestamp.replace(tzinfo=timezone.utc)
                if timestamp.tzinfo is None
                else timestamp.astimezone(timezone.utc)
            )

        return max(timestamps, key=comparable).isoformat()

    return response_cache.get_or_create(
        ("catalogue-freshness", tuple(sorted(normalized_types))),
        load_freshness,
    )


def _serialize_catalogue_rows(
    rows,
    *,
    preview: bool = False,
) -> list[dict[str, Any]]:
    """Hydrate normalized identity rows without losing their global order."""
    anime_ids = [
        row.record_id for row in rows if row.content_type == "ANIME"
    ]
    manga_ids = [
        row.record_id for row in rows if row.content_type != "ANIME"
    ]
    entries_by_key: dict[tuple[str, int], Anime | Manga] = {}

    if anime_ids:
        anime = db.session.scalars(
            _anime_statement(preview=preview).where(
                Anime.animeID.in_(anime_ids)
            )
        ).all()
        entries_by_key.update(
            {("ANIME", entry.animeID): entry for entry in anime}
        )
    if manga_ids:
        manga = db.session.scalars(
            _manga_statement(preview=preview).where(
                Manga.mangaID.in_(manga_ids)
            )
        ).all()
        entries_by_key.update(
            {
                (entry.content_type, entry.mangaID): entry
                for entry in manga
            }
        )

    items = []
    for row in rows:
        entry = entries_by_key.get((row.content_type, row.record_id))
        if isinstance(entry, Anime):
            items.append(_serialize_anime(entry, preview=preview))
        elif isinstance(entry, Manga):
            items.append(_serialize_manga(entry, preview=preview))
    return items


def _ordered_anime_statement(statement, sort: str):
    """Apply the same deterministic sort contract to the anime alias route."""
    score_order = Anime.score.desc().nullslast()
    title_order = (func.lower(Anime.title), Anime.title)
    if sort == "newest":
        order = (Anime.year.desc().nullslast(), score_order, *title_order)
    elif sort == "oldest":
        order = (Anime.year.asc().nullslast(), score_order, *title_order)
    elif sort == "title":
        order = (*title_order, score_order)
    elif sort == "most_episodes":
        order = (
            Anime.episodes.desc().nullslast(),
            score_order,
            *title_order,
        )
    elif sort == "most_popular":
        order = (
            Anime.popularity.asc().nullslast(),
            score_order,
            *title_order,
        )
    elif sort == "most_members":
        order = (
            Anime.members.desc().nullslast(),
            score_order,
            *title_order,
        )
    else:
        order = (score_order, *title_order)
    return statement.order_by(*order, Anime.mal_id.nulls_last(), Anime.animeID)


def _list_catalogue(content_types: frozenset[str] | set[str]):
    page = _integer_argument("page", minimum=1) or 1
    per_page = (
        _integer_argument(
            "per_page", minimum=1, maximum=MAX_PAGE_SIZE
        )
        or 24
    )
    sort = _sort_argument(content_types)
    preview = _preview_requested()
    catalogue_rows = _catalogue_rows_subquery(content_types)
    if catalogue_rows is None:
        total = 0
        items = []
    else:
        count_key = (
            "catalogue-total",
            tuple(sorted(content_types)),
            _request_filter_signature(
                exclude={
                    "content_type",
                    "limit",
                    "page",
                    "per_page",
                    "preview",
                    "sort",
                }
            ),
        )
        total = _cached_scalar_count(
            count_key,
            select(func.count()).select_from(catalogue_rows),
        )
        page_rows = _catalogue_rows_subquery(
            content_types,
            branch_limit=page * per_page,
            sort=sort,
        )
        rows = db.session.execute(
            _ordered_catalogue_rows(page_rows, sort)
            .offset((page - 1) * per_page)
            .limit(per_page)
        ).all()
        items = _serialize_catalogue_rows(rows, preview=preview)
    return jsonify(
        {
            "items": items,
            "pagination": {
                "page": page,
                "per_page": per_page,
                "total": total,
                "pages": (total + per_page - 1) // per_page,
            },
            "updated_at": _catalogue_freshness(content_types),
        }
    )


def _sampled_random_statement(
    content_types: frozenset[str] | set[str],
    limit: int,
):
    """Randomize only a small PostgreSQL physical sample, not whole tables."""
    branches = []
    if "ANIME" in content_types:
        sampled_anime = Anime.__table__.tablesample(
            func.system(2), name="sampled_anime"
        )
        branches.append(
            select(
                literal("ANIME").label("content_type"),
                sampled_anime.c.anime_id.label("record_id"),
                sampled_anime.c.mal_id.label("mal_id"),
                sampled_anime.c.score.label("score"),
                sampled_anime.c.title.label("title"),
            ).where(sampled_anime.c.is_adult.is_(False))
        )
    print_types = set(content_types).intersection({"MANGA", "MANHWA"})
    if print_types:
        sampled_manga = Manga.__table__.tablesample(
            func.system(2), name="sampled_manga"
        )
        branches.append(
            select(
                sampled_manga.c.content_type.label("content_type"),
                sampled_manga.c.manga_id.label("record_id"),
                sampled_manga.c.mal_id.label("mal_id"),
                sampled_manga.c.score.label("score"),
                sampled_manga.c.title.label("title"),
            ).where(
                sampled_manga.c.is_adult.is_(False),
                sampled_manga.c.content_type.in_(sorted(print_types)),
            )
        )
    if not branches:
        return None
    sample = (
        branches[0] if len(branches) == 1 else union_all(*branches)
    ).subquery("sampled_catalogue")
    return (
        select(
            sample.c.content_type,
            sample.c.record_id,
            sample.c.mal_id,
            sample.c.score,
            sample.c.title,
        )
        .order_by(func.random())
        .limit(limit)
    )


def _sampled_random_rows(
    content_types: frozenset[str] | set[str],
    limit: int,
):
    statement = _sampled_random_statement(content_types, limit)
    if statement is None:
        return []
    return db.session.execute(statement).all()


def _random_id_bounds(catalogue_rows) -> tuple[int, int] | None:
    """Return stable candidate bounds for indexed filtered random seeking."""
    minimum, maximum = db.session.execute(
        select(
            func.min(catalogue_rows.c.record_id),
            func.max(catalogue_rows.c.record_id),
        ).select_from(catalogue_rows)
    ).one()
    if minimum is None or maximum is None:
        return None
    return int(minimum), int(maximum)


def _random_window_statement(
    catalogue_rows,
    *,
    pivot: int,
    limit: int,
    wrap: bool = False,
):
    """Seek from a random primary-key pivot without a full random sort."""
    predicate = (
        catalogue_rows.c.record_id < pivot
        if wrap
        else catalogue_rows.c.record_id >= pivot
    )
    return (
        select(
            catalogue_rows.c.content_type,
            catalogue_rows.c.record_id,
            catalogue_rows.c.mal_id,
            catalogue_rows.c.score,
            catalogue_rows.c.title,
        )
        .where(predicate)
        .order_by(
            catalogue_rows.c.record_id,
            catalogue_rows.c.content_type,
        )
        .limit(limit)
    )


def _random_catalogue(content_types: frozenset[str] | set[str]):
    limit = _integer_argument("limit", minimum=1, maximum=12) or 6
    preview = _preview_requested()
    catalogue_rows = _catalogue_rows_subquery(content_types)
    if catalogue_rows is None:
        return jsonify({"items": []})
    filter_signature = _request_filter_signature(
        exclude={
            "content_type",
            "limit",
            "page",
            "per_page",
            "preview",
            "sort",
        }
    )
    rows = []
    if not filter_signature and db.engine.dialect.name == "postgresql":
        rows = _sampled_random_rows(content_types, limit)
        if len(rows) >= limit:
            return jsonify(
                {"items": _serialize_catalogue_rows(rows, preview=preview)}
            )

    bounds_key = (
        "catalogue-random-id-bounds",
        tuple(sorted(content_types)),
        filter_signature,
    )
    bounds = response_cache.get_or_create(
        bounds_key,
        lambda: _random_id_bounds(catalogue_rows),
    )
    if bounds is None:
        return jsonify({"items": []})

    minimum_id, maximum_id = bounds
    pivot = randrange(minimum_id, maximum_id + 1)
    rows = db.session.execute(
        _random_window_statement(
            catalogue_rows,
            pivot=pivot,
            limit=limit,
        )
    ).all()
    if len(rows) < limit:
        # Treat IDs as circular. The strict wrap predicate prevents duplicate
        # rows while allowing small or sparse filtered catalogues to return as
        # many distinct matches as they actually contain.
        rows.extend(
            db.session.execute(
                _random_window_statement(
                    catalogue_rows,
                    pivot=pivot,
                    limit=limit - len(rows),
                    wrap=True,
                )
            ).all()
        )
    return jsonify(
        {"items": _serialize_catalogue_rows(rows, preview=preview)}
    )


def _catalogue_detail_response(content_type: str, mal_id: int):
    normalized_type = _normalized_content_type(
        content_type, allow_all=False
    )
    if normalized_type == "ANIME":
        entry = db.session.scalar(
            _anime_statement(detailed=True).where(Anime.mal_id == mal_id)
        )
        if entry is None:
            raise ApiError("Anime not found", 404)
        payload = _serialize_anime(entry, detailed=True)
        _attach_verified_refresh(payload, "anime", mal_id)
        return jsonify({"item": payload})

    entry = db.session.scalar(
        _manga_statement({normalized_type}, detailed=True).where(
            Manga.mal_id == mal_id
        )
    )
    if entry is None:
        label = "Manga" if normalized_type == "MANGA" else "Manhwa"
        raise ApiError(f"{label} not found", 404)
    payload = _serialize_manga(entry, detailed=True)
    _attach_verified_refresh(payload, normalized_type.casefold(), mal_id)
    return jsonify({"item": payload})


@app.get(f"{API_PREFIX}/anime")
def list_anime():
    """Search, filter, and deterministically sort anime."""
    page = _integer_argument("page", minimum=1) or 1
    per_page = _integer_argument("per_page", minimum=1, maximum=MAX_PAGE_SIZE) or 24
    sort = _sort_argument(CONTENT_TYPE_SCOPES["ANIME"])
    preview = _preview_requested()
    statement = _filtered_anime_statement(preview=preview)
    total = _cached_scalar_count(
        (
            "anime-total",
            _request_filter_signature(
                exclude={"page", "per_page", "preview", "sort"}
            ),
        ),
        select(func.count()).select_from(statement.order_by(None).subquery()),
    )
    items = db.session.scalars(
        _ordered_anime_statement(statement, sort)
        .offset((page - 1) * per_page)
        .limit(per_page)
    ).all()
    return jsonify(
        {
            "items": [
                _serialize_anime(anime, preview=preview) for anime in items
            ],
            "pagination": {
                "page": page,
                "per_page": per_page,
                "total": total,
                "pages": (total + per_page - 1) // per_page,
            },
            "updated_at": _catalogue_freshness(
                CONTENT_TYPE_SCOPES["ANIME"]
            ),
        }
    )


@app.get(f"{API_PREFIX}/anime/random")
def random_anime():
    """Return a small random selection of public anime."""
    return _random_catalogue(CONTENT_TYPE_SCOPES["ANIME"])


@app.get(f"{API_PREFIX}/anime/seasonal")
def popular_current_season():
    """Return a ranked current or upcoming Japan-season window."""
    limit = _integer_argument("limit", minimum=1, maximum=12) or 6
    page = _integer_argument("page", minimum=1) or 1
    preview = _preview_requested()
    period = request.args.get("period", "current").strip().lower()
    if period not in {"current", "next"}:
        raise ApiError("period must be current or next")
    sort = request.args.get("sort", "most_popular").strip().lower()
    if sort not in {"top_rated", "most_popular", "most_members"}:
        raise ApiError("seasonal sort must be top_rated, most_popular, or most_members")

    year, season = (
        _next_season_identity() if period == "next" else _current_season_identity()
    )
    common_filters = _common_filter_values()
    filters = [
        Anime.year == year,
        Anime.season == season,
    ]
    if sort == "top_rated":
        filters.append(Anime.score.is_not(None))
    if sort == "most_popular":
        order = (
            Anime.popularity.asc().nullslast(),
            Anime.members.desc().nullslast(),
            Anime.score.desc().nullslast(),
            Anime.title,
        )
    elif sort == "most_members":
        order = (
            Anime.members.desc().nullslast(),
            Anime.popularity.asc().nullslast(),
            Anime.score.desc().nullslast(),
            Anime.title,
        )
    else:
        order = (Anime.score.desc(), Anime.title)
    public_season = _apply_common_filters(
        _anime_statement(preview=True), Anime, Anime.year, common_filters
    ).where(*filters).order_by(None).subquery()
    total = _cached_scalar_count(
        (
            "seasonal-total",
            period,
            year,
            season,
            sort,
            _request_filter_signature(
                exclude={"content_type", "limit", "page", "per_page", "preview", "sort"}
            ),
        ),
        select(func.count()).select_from(public_season),
    )
    anime = db.session.scalars(
        _apply_common_filters(
            _anime_statement(preview=preview), Anime, Anime.year, common_filters
        )
        .where(*filters)
        .order_by(*order)
        .offset((page - 1) * limit)
        .limit(limit)
    ).all()
    return jsonify(
        {
            "items": [
                _serialize_anime(entry, preview=preview) for entry in anime
            ],
            "period": period,
            "sort": sort,
            "season": season,
            "year": year,
            "pagination": {
                "page": page,
                "per_page": limit,
                "total": total,
                "pages": (total + limit - 1) // limit,
            },
        }
    )


@app.get(f"{API_PREFIX}/anime/<int:mal_id>")
def anime_detail(mal_id: int):
    """Return the full record for one MyAnimeList anime ID."""
    anime = db.session.scalar(
        _anime_statement(detailed=True).where(Anime.mal_id == mal_id)
    )
    if anime is None:
        raise ApiError("Anime not found", 404)
    payload = _serialize_anime(anime, detailed=True)
    _attach_verified_refresh(payload, "anime", mal_id)
    return jsonify({"item": payload})


@app.get(f"{API_PREFIX}/catalogue")
def list_catalogue():
    """Search Anime, Manga, Manhwa, or the combined public catalogue."""
    scope = _content_type_argument(default="ALL")
    return _list_catalogue(CONTENT_TYPE_SCOPES[scope])


@app.get(f"{API_PREFIX}/catalogue/random")
def random_catalogue():
    """Return a scoped random selection from the combined catalogue."""
    scope = _content_type_argument(default="ALL")
    return _random_catalogue(CONTENT_TYPE_SCOPES[scope])


@app.get(f"{API_PREFIX}/catalogue/<content_type>/<int:mal_id>")
def catalogue_detail(content_type: str, mal_id: int):
    """Return a type-qualified detail record without MAL-ID ambiguity."""
    return _catalogue_detail_response(content_type, mal_id)


@app.get(f"{API_PREFIX}/manga")
def list_manga():
    return _list_catalogue(CONTENT_TYPE_SCOPES["MANGA"])


@app.get(f"{API_PREFIX}/manga/random")
def random_manga():
    return _random_catalogue(CONTENT_TYPE_SCOPES["MANGA"])


@app.get(f"{API_PREFIX}/manga/<int:mal_id>")
def manga_detail(mal_id: int):
    return _catalogue_detail_response("MANGA", mal_id)


@app.get(f"{API_PREFIX}/manhwa")
def list_manhwa():
    return _list_catalogue(CONTENT_TYPE_SCOPES["MANHWA"])


@app.get(f"{API_PREFIX}/manhwa/random")
def random_manhwa():
    return _random_catalogue(CONTENT_TYPE_SCOPES["MANHWA"])


@app.get(f"{API_PREFIX}/manhwa/<int:mal_id>")
def manhwa_detail(mal_id: int):
    return _catalogue_detail_response("MANHWA", mal_id)


def _load_genre_names(
    content_types: frozenset[str],
) -> tuple[str, ...]:
    return tuple(
        db.session.scalars(
            select(CatalogueFacet.value)
            .where(
                CatalogueFacet.content_type.in_(sorted(content_types)),
                CatalogueFacet.facet_type == "genre",
            )
            .distinct()
            .order_by(CatalogueFacet.value)
        ).all()
    )


def _load_detailed_tag_names(
    content_types: frozenset[str],
) -> tuple[str, ...]:
    return tuple(
        db.session.scalars(
            select(CatalogueFacet.value)
            .where(
                CatalogueFacet.content_type.in_(sorted(content_types)),
                CatalogueFacet.facet_type == "tag",
            )
            .distinct()
            .order_by(CatalogueFacet.value)
        ).all()
    )


def _load_facet_names(
    content_types: frozenset[str],
    facet_type: str,
) -> tuple[str, ...]:
    return tuple(
        db.session.scalars(
            select(CatalogueFacet.value)
            .where(
                CatalogueFacet.content_type.in_(sorted(content_types)),
                CatalogueFacet.facet_type == facet_type,
            )
            .distinct()
            .order_by(CatalogueFacet.value)
        ).all()
    )


def _load_facet_page(
    content_types: frozenset[str],
    facet_type: str,
    query: str,
    *,
    offset: int,
    limit: int,
) -> tuple[tuple[str, ...], bool]:
    """Read one indexed facet page without materializing the full catalogue."""
    statement = select(CatalogueFacet.value).where(
        CatalogueFacet.content_type.in_(sorted(content_types)),
        CatalogueFacet.facet_type == facet_type,
    )
    if query:
        statement = statement.where(
            CatalogueFacet.value.ilike(
                _escaped_search_pattern(query), escape="\\"
            )
        )
    values = tuple(
        db.session.scalars(
            statement
            .distinct()
            .order_by(CatalogueFacet.value)
            .offset(offset)
            .limit(limit + 1)
        ).all()
    )
    return values[:limit], len(values) > limit


def _paginated_facet_response(
    content_types: frozenset[str],
    facet_type: str,
    query: str,
    limit: int,
):
    offset = _integer_argument("offset", minimum=0) or 0
    items, has_more = response_cache.get_or_create(
        (
            "facet-page",
            tuple(sorted(content_types)),
            facet_type,
            query,
            offset,
            limit,
        ),
        lambda: _load_facet_page(
            content_types,
            facet_type,
            query,
            offset=offset,
            limit=limit,
        ),
    )
    return jsonify(
        {
            "items": items,
            "pagination": {
                "offset": offset,
                "limit": limit,
                "has_more": has_more,
            },
        }
    )


def _searched_facet_response(
    facet_type: str,
    *,
    default_scope: str = "ANIME",
):
    scope = _content_type_argument(default=default_scope)
    content_types = CONTENT_TYPE_SCOPES[scope]
    query = request.args.get("q", "").strip().casefold()
    limit = (
        _integer_argument("limit", minimum=1, maximum=MAX_TAG_OPTIONS)
        if "limit" in request.args
        else None
    )
    if limit is not None:
        return _paginated_facet_response(
            content_types, facet_type, query, limit
        )
    values = response_cache.get_or_create(
        (facet_type, tuple(sorted(content_types))),
        lambda: _load_facet_names(content_types, facet_type),
    )
    if query:
        values = tuple(value for value in values if query in value.casefold())
    return _facet_values_response(values, limit)


def _facet_values_response(
    values: tuple[str, ...],
    limit: int | None,
):
    """Return a backward-compatible full list or one incremental UI page."""
    if limit is None:
        return jsonify({"items": values})
    offset = _integer_argument("offset", minimum=0) or 0
    items = values[offset : offset + limit]
    return jsonify(
        {
            "items": items,
            "pagination": {
                "offset": offset,
                "limit": limit,
                "total": len(values),
                "has_more": offset + len(items) < len(values),
            },
        }
    )


def _combined_numeric_bounds(
    *bounds: tuple[int | float | None, int | float | None],
) -> dict[str, int | float] | None:
    minimums = [minimum for minimum, _ in bounds if minimum is not None]
    maximums = [maximum for _, maximum in bounds if maximum is not None]
    if not minimums or not maximums:
        return None
    return {"min": min(minimums), "max": max(maximums)}


def _load_filter_ranges(
    content_types: frozenset[str],
) -> dict[str, dict[str, int | float] | None]:
    anime_ranges: dict[str, tuple[int | float | None, int | float | None]] = {}
    print_ranges: dict[str, tuple[int | float | None, int | float | None]] = {}
    if "ANIME" in content_types:
        row = db.session.execute(
            select(
                func.min(Anime.year),
                func.max(Anime.year),
                func.min(Anime.score),
                func.max(Anime.score),
                func.min(Anime.episodes),
                func.max(Anime.episodes),
            ).where(Anime.is_adult.is_(False))
        ).one()
        anime_ranges = {
            "year": (row[0], row[1]),
            "score": (row[2], row[3]),
            "episodes": (row[4], row[5]),
        }
    print_types = content_types.intersection({"MANGA", "MANHWA"})
    if print_types:
        row = db.session.execute(
            select(
                func.min(Manga.publication_year),
                func.max(Manga.publication_year),
                func.min(Manga.score),
                func.max(Manga.score),
                func.min(Manga.chapters),
                func.max(Manga.chapters),
                func.min(Manga.volumes),
                func.max(Manga.volumes),
            ).where(
                Manga.is_adult.is_(False),
                Manga.content_type.in_(sorted(print_types)),
            )
        ).one()
        print_ranges = {
            "year": (row[0], row[1]),
            "score": (row[2], row[3]),
            "chapters": (row[4], row[5]),
            "volumes": (row[6], row[7]),
        }
    return {
        "year": _combined_numeric_bounds(
            anime_ranges.get("year", (None, None)),
            print_ranges.get("year", (None, None)),
        ),
        "score": _combined_numeric_bounds(
            anime_ranges.get("score", (None, None)),
            print_ranges.get("score", (None, None)),
        ),
        "episodes": _combined_numeric_bounds(
            anime_ranges.get("episodes", (None, None))
        ),
        "chapters": _combined_numeric_bounds(
            print_ranges.get("chapters", (None, None))
        ),
        "volumes": _combined_numeric_bounds(
            print_ranges.get("volumes", (None, None))
        ),
    }


@app.get(f"{API_PREFIX}/genres")
def list_genres():
    scope = _content_type_argument(default="ANIME")
    content_types = CONTENT_TYPE_SCOPES[scope]
    genres = response_cache.get_or_create(
        ("genres", tuple(sorted(content_types))),
        lambda: _load_genre_names(content_types),
    )
    return jsonify({"items": genres})


@app.get(f"{API_PREFIX}/tags")
def list_detailed_tags():
    """Search one cached detailed-tag catalogue for the selected content."""
    scope = _content_type_argument(default="ANIME")
    content_types = CONTENT_TYPE_SCOPES[scope]
    query = request.args.get("q", "").strip().casefold()
    limit = (
        _integer_argument("limit", minimum=1, maximum=MAX_TAG_OPTIONS)
        if "limit" in request.args
        else None
    )
    if limit is not None:
        return _paginated_facet_response(
            content_types, "tag", query, limit
        )
    all_tags = response_cache.get_or_create(
        ("tags", tuple(sorted(content_types))),
        lambda: _load_detailed_tag_names(content_types),
    )
    if query:
        all_tags = tuple(
            tag for tag in all_tags if query in tag.casefold()
        )
    return _facet_values_response(all_tags, limit)


@app.get(f"{API_PREFIX}/studios")
def list_studios():
    """Search precomputed animation-studio filter options."""
    return _searched_facet_response("studio")


@app.get(f"{API_PREFIX}/streaming-services")
def list_streaming_services():
    """Search precomputed anime streaming-service filter options."""
    return _searched_facet_response("streaming_service")


@app.get(f"{API_PREFIX}/authors")
def list_authors():
    """Search precomputed public Manga and Manhwa author options."""
    return _searched_facet_response("author", default_scope="ALL")


@app.get(f"{API_PREFIX}/filter-ranges")
def list_filter_ranges():
    """Return cached public numeric bounds for accessible range controls."""
    scope = _content_type_argument(default="ANIME")
    content_types = CONTENT_TYPE_SCOPES[scope]
    ranges = response_cache.get_or_create(
        ("filter-ranges", tuple(sorted(content_types))),
        lambda: _load_filter_ranges(content_types),
    )
    return jsonify({"ranges": ranges})


@app.get(f"{API_PREFIX}/admin/analytics/visits")
def admin_visit_analytics():
    """Return aggregate visit metrics to an authorized administrator only."""
    access_error = _admin_analytics_access_error()
    if access_error is not None:
        return access_error
    try:
        report = analytics_response_cache.get_or_create(
            ("admin-visit-analytics",),
            _analytics_report,
        )
    except SQLAlchemyError:
        db.session.rollback()
        return (
            jsonify(
                {"error": {"message": "Analytics are temporarily unavailable"}}
            ),
            503,
        )

    response = jsonify(report)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.after_request
def track_public_frontend_visit(response):
    """Record an anonymous aggregate visit without affecting site delivery."""
    if not _analytics_request_is_trackable(response):
        return response

    visitor_token = request.cookies.get(ANALYTICS_COOKIE_NAME)
    is_new_visitor = not _is_valid_analytics_cookie(visitor_token)
    if is_new_visitor:
        visitor_token = secrets.token_urlsafe(32)
        response.set_cookie(
            ANALYTICS_COOKIE_NAME,
            visitor_token,
            max_age=ANALYTICS_COOKIE_MAX_AGE_SECONDS,
            secure=_analytics_cookie_is_secure(),
            httponly=True,
            samesite="Lax",
            path="/",
        )
    try:
        _record_site_visit(visitor_token)
    except SQLAlchemyError:
        # Analytics is optional: discard only its failed transaction and keep
        # serving the frontend response normally.
        db.session.rollback()
    except Exception:
        # Do not allow an unexpected analytics issue to affect public traffic.
        db.session.rollback()
    return response


@app.after_request
def add_frontend_cache_headers(response):
    """Cache fingerprinted bundles while keeping the app shell fresh."""
    if request.method != "GET" or response.status_code != 200:
        return response
    if request.path.startswith("/assets/"):
        response.headers["Cache-Control"] = (
            "public, max-age=31536000, immutable"
        )
    elif response.mimetype == "text/html":
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/")
@app.get("/<path:path>")
def react_app(path: str = ""):
    """Serve the built React app and let its router handle browser routes."""
    requested_file = FRONTEND_BUILD_DIR / path
    if path and requested_file.is_file():
        return send_from_directory(FRONTEND_BUILD_DIR, path)
    return send_from_directory(FRONTEND_BUILD_DIR, "index.html")


def initialize_database_schema() -> bool:
    """Apply schema updates without preventing the static frontend from booting."""
    try:
        ensure_anime_schema()
    except SQLAlchemyError:
        try:
            db.session.rollback()
        except Exception:
            pass
        app.logger.warning(
            "Database schema initialization is unavailable; starting in "
            "frontend-only degraded mode."
        )
        return False
    return True


with app.app_context():
    initialize_database_schema()


if __name__ == "__main__":
    app.run(debug=True, port=8000)
