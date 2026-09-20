"""Incrementally populate and refresh Manga and Manhwa catalogue rows."""

from __future__ import annotations

import os
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from time import sleep
from typing import Any
from urllib.error import HTTPError

from sqlalchemy import delete, or_, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import lazyload, selectinload

from backend.app import app
from backend.models import (
    Author,
    Genre,
    JikanSyncState,
    Manga,
    MangaAuthor,
    MangaGenre,
    db,
)
from backend.services.jikan_client import (
    JikanMangaPage,
    JikanTemporaryError,
    get_manga_catalogue_page,
    get_manga_full,
)


ADULT_GENRE_NAMES = frozenset({"hentai", "erotica"})
MANGA_PROVIDER_TYPES = ("manga", "manhwa")
MANGA_CONTENT_TYPES = {"manga": "MANGA", "manhwa": "MANHWA"}
MANGA_STATE_KEYS = {
    provider_type: f"bulk:catalogue:{provider_type}:v1"
    for provider_type in MANGA_PROVIDER_TYPES
}
SKIPPABLE_STATUS_CODES = frozenset({404, 429, 500, 502, 503, 504})
TEMPORARY_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
DEFAULT_MANGA_MAX_PAGES = 40
DEFAULT_MANGA_REFRESH_LIMIT = 1000
MAX_CONSECUTIVE_FAILURES = 3
PROVIDER_MAX_PAGE = 1000
DEADLOCK_SQLSTATE = "40P01"
AUTHOR_NAME_MAX_LENGTH = 200
AUTHOR_ROLE_MAX_LENGTH = 100


@dataclass
class AuthorSyncStats:
    payloads_processed: int = 0
    titles_updated: int = 0
    authors_processed: int = 0
    authors_created: int = 0
    links_created: int = 0
    links_removed: int = 0
    roles_updated: int = 0
    malformed_entries: int = 0
    reconciliation_failures: int = 0

    def add(self, other: "AuthorSyncStats") -> None:
        for attribute in self.__dataclass_fields__:
            setattr(
                self,
                attribute,
                getattr(self, attribute) + getattr(other, attribute),
            )


@dataclass
class AuthorCaches:
    by_name: dict[str, Author]
    by_mal_id: dict[int, Author]


@dataclass
class MangaPageApplyResult:
    saved: int = 0
    changed: int = 0
    inserted: int = 0
    updated: int = 0
    removed_adult: int = 0
    skipped: int = 0
    author_stats: AuthorSyncStats = field(default_factory=AuthorSyncStats)


@dataclass
class MangaTypeSyncResult:
    pages_attempted: int = 0
    pages_completed: int = 0
    pages_failed: int = 0
    inserted: int = 0
    updated: int = 0
    removed_adult: int = 0
    complete: bool = False
    next_page: int = 1
    provider_page_limit_exceeded: bool = False
    author_stats: AuthorSyncStats = field(default_factory=AuthorSyncStats)


@dataclass(frozen=True)
class MangaCatalogueSyncResult:
    scans: dict[str, MangaTypeSyncResult]

    @property
    def pages_completed(self) -> int:
        return sum(scan.pages_completed for scan in self.scans.values())

    @property
    def pages_failed(self) -> int:
        return sum(scan.pages_failed for scan in self.scans.values())

    @property
    def inserted(self) -> int:
        return sum(scan.inserted for scan in self.scans.values())

    @property
    def updated(self) -> int:
        return sum(scan.updated for scan in self.scans.values())

    @property
    def removed_adult(self) -> int:
        return sum(scan.removed_adult for scan in self.scans.values())

    @property
    def author_stats(self) -> AuthorSyncStats:
        stats = AuthorSyncStats()
        for scan in self.scans.values():
            stats.add(scan.author_stats)
        return stats


@dataclass
class MangaRefreshResult:
    selected: int = 0
    updated: int = 0
    removed_adult: int = 0
    removed_unsupported: int = 0
    not_found: int = 0
    temporary_errors: int = 0
    invalid_payloads: int = 0
    author_stats: AuthorSyncStats = field(default_factory=AuthorSyncStats)

    @property
    def success_rate(self) -> float:
        handled = self.updated + self.removed_adult + self.removed_unsupported
        return handled / self.selected if self.selected else 0.0


def _names(entries: Any) -> list[str]:
    if not isinstance(entries, list):
        return []
    names = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    return list(dict.fromkeys(names))


def _detailed_genres(data: dict[str, Any]) -> list[str]:
    names = []
    for field in ("genres", "explicit_genres", "themes", "demographics"):
        names.extend(name.lower() for name in _names(data.get(field)))
    return list(dict.fromkeys(names))


def is_adult_content(data: dict[str, Any]) -> bool:
    """Recognize provider classifications that are restricted to adults."""
    for field in ("genres", "explicit_genres", "themes", "demographics"):
        if any(
            name.casefold() in ADULT_GENRE_NAMES
            for name in _names(data.get(field))
        ):
            return True
    rating = data.get("rating")
    return isinstance(rating, str) and "hentai" in rating.casefold()


def _valid_score(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return float(value)


def _valid_catalogue_metric(value: Any) -> int | None:
    """Return a non-negative provider count/rank, or preserve old data."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _normalized_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _display_author_name(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    name = " ".join(unicodedata.normalize("NFKC", value).split())
    return name if name and len(name) <= AUTHOR_NAME_MAX_LENGTH else None


def _normalized_author_name(value: Any) -> str | None:
    name = _display_author_name(value)
    return name.casefold() if name is not None else None


def _author_role(entry: dict[str, Any]) -> str | None:
    role = _normalized_text(entry.get("role"))
    if role is None:
        provider_type = _normalized_text(entry.get("type"))
        if provider_type and provider_type.casefold() not in {"people", "person"}:
            role = provider_type
    if role is None or len(role) > AUTHOR_ROLE_MAX_LENGTH:
        return None
    return role


def _author_values(
    value: Any,
) -> tuple[list[tuple[int | None, str, str, str | None]], int, bool] | None:
    """Parse authors; None means the existing relationships must be preserved."""
    if not isinstance(value, list):
        return None
    parsed: dict[str, tuple[int | None, str, str, str | None]] = {}
    malformed = 0
    for entry in value:
        if not isinstance(entry, dict):
            malformed += 1
            continue
        name = _display_author_name(entry.get("name"))
        normalized_name = _normalized_author_name(name)
        if name is None or normalized_name is None:
            malformed += 1
            continue
        provider_id = entry.get("mal_id")
        if provider_id is not None and (
            isinstance(provider_id, bool)
            or not isinstance(provider_id, int)
            or provider_id <= 0
        ):
            provider_id = None
            malformed += 1
        role = _author_role(entry)
        existing = parsed.get(normalized_name)
        if existing is None or (existing[3] is None and role is not None):
            parsed[normalized_name] = (
                provider_id,
                name,
                normalized_name,
                role,
            )
    return list(parsed.values()), malformed, malformed == 0


def _author_caches(payloads=None) -> AuthorCaches:
    query = select(Author).options(lazyload("*"))
    if payloads is not None:
        names, ids = set(), set()
        for data in payloads:
            parsed = _author_values(data.get("authors"))
            for provider_id, _name, normalized, _role in parsed[0] if parsed else []:
                names.add(normalized)
                if provider_id is not None:
                    ids.add(provider_id)
        query = query.where(or_(
            Author.mal_id.in_(ids), Author.normalized_name.in_(names)
        ))
    authors = list(db.session.scalars(query))
    return AuthorCaches(
        by_name={author.normalized_name: author for author in authors},
        by_mal_id={
            author.mal_id: author
            for author in authors
            if author.mal_id is not None
        },
    )


def _author_for_value(
    provider_id: int | None,
    name: str,
    normalized_name: str,
    caches: AuthorCaches,
    stats: AuthorSyncStats,
) -> Author:
    author = caches.by_name.get(normalized_name)
    if author is None and provider_id is not None:
        author = caches.by_mal_id.get(provider_id)
    if author is None:
        author = Author(
            mal_id=provider_id,
            name=name,
            normalized_name=normalized_name,
        )
        db.session.add(author)
        stats.authors_created += 1
    else:
        # A stable provider ID wins identity lookups. Avoid adopting a
        # normalized name already owned by another row.
        if (
            author.normalized_name != normalized_name
            and normalized_name not in caches.by_name
        ):
            caches.by_name.pop(author.normalized_name, None)
            author.normalized_name = normalized_name
        author.name = name
        if (
            author.mal_id is None
            and provider_id is not None
            and provider_id not in caches.by_mal_id
        ):
            author.mal_id = provider_id
    caches.by_name[author.normalized_name] = author
    if author.mal_id is not None:
        caches.by_mal_id[author.mal_id] = author
    return author


def _reconcile_authors(
    manga: Manga,
    value: Any,
    caches: AuthorCaches,
    stats: AuthorSyncStats,
) -> None:
    stats.payloads_processed += 1
    parsed = _author_values(value)
    if parsed is None:
        stats.malformed_entries += 1
        stats.reconciliation_failures += 1
        return
    values, malformed, complete = parsed
    stats.malformed_entries += malformed
    if malformed:
        stats.reconciliation_failures += 1
    stats.authors_processed += len(values)
    links_by_name = {
        link.author.normalized_name: link for link in manga.author_links
    }
    desired_authors: set[int] = set()
    changed = False
    for provider_id, name, normalized_name, role in values:
        author = _author_for_value(
            provider_id, name, normalized_name, caches, stats
        )
        desired_authors.add(id(author))
        link = links_by_name.get(author.normalized_name)
        if link is None:
            link = next(
                (
                    existing
                    for existing in manga.author_links
                    if existing.author is author
                ),
                None,
            )
        if link is None:
            manga.author_links.append(MangaAuthor(author=author, role=role))
            stats.links_created += 1
            changed = True
        elif link.role != role:
            link.role = role
            stats.roles_updated += 1
            changed = True
    if complete:
        for link in list(manga.author_links):
            if id(link.author) not in desired_authors:
                db.session.delete(link)
                stats.links_removed += 1
                changed = True
    if changed:
        stats.titles_updated += 1


def _publication_year(data: dict[str, Any]) -> int | None:
    published = data.get("published")
    if not isinstance(published, dict):
        return None
    properties = published.get("prop")
    start_properties = (
        properties.get("from") if isinstance(properties, dict) else None
    )
    year = (
        start_properties.get("year")
        if isinstance(start_properties, dict)
        else None
    )
    if isinstance(year, int) and not isinstance(year, bool) and year > 0:
        return year
    start = published.get("from")
    if isinstance(start, str):
        match = re.match(r"^(\d{4})", start.strip())
        if match:
            return int(match.group(1))
    return None


def _jpg_images(data: dict[str, Any]) -> dict[str, Any]:
    images = data.get("images")
    if not isinstance(images, dict):
        return {}
    jpg = images.get("jpg")
    return jpg if isinstance(jpg, dict) else {}


def _content_type(value: Any, *, fallback: str) -> str | None:
    if isinstance(value, str):
        normalized = value.strip().lower()
        if not normalized:
            return fallback
        if normalized in MANGA_CONTENT_TYPES:
            return MANGA_CONTENT_TYPES[normalized]
        return None
    return fallback if value is None else None


def _has_unsupported_content_type(data: dict[str, Any]) -> bool:
    value = data.get("type")
    return (
        isinstance(value, str)
        and bool(value.strip())
        and value.strip().lower() not in MANGA_CONTENT_TYPES
    )


def _update_manga(
    manga: Manga,
    data: dict[str, Any],
    genres: dict[str, Genre],
    *,
    expected_content_type: str,
    authors: AuthorCaches | None = None,
    author_stats: AuthorSyncStats | None = None,
) -> None:
    manga.is_adult = is_adult_content(data)
    resolved_content_type = _content_type(
        data.get("type"), fallback=expected_content_type
    )
    # A sparse detail payload may omit its type. An explicit unsupported type
    # must never reclassify a Manga/Manhwa row into an invalid catalogue type.
    manga.content_type = resolved_content_type or expected_content_type
    manga.title = data.get("title") or manga.title
    manga.alternative_title = (
        data.get("title_english")
        or data.get("title_japanese")
        or manga.alternative_title
    )
    if "synopsis" in data:
        manga.synopsis = _normalized_text(data.get("synopsis"))
    if "type" in data:
        manga.manga_type = _normalized_text(data.get("type"))
    if "published" in data:
        manga.publication_year = _publication_year(data)
    if "status" in data:
        manga.status = _normalized_text(data.get("status"))
    if "score" in data:
        manga.score = _valid_score(data.get("score"))
    for field_name in ("popularity", "members"):
        if field_name in data:
            metric = _valid_catalogue_metric(data.get(field_name))
            if metric is not None:
                setattr(manga, field_name, metric)
    if "chapters" in data:
        manga.chapters = data.get("chapters")
    if "volumes" in data:
        manga.volumes = data.get("volumes")
    manga.mal_url = data.get("url") or manga.mal_url

    images = _jpg_images(data)
    manga.image_url = (
        images.get("large_image_url")
        or images.get("image_url")
        or manga.image_url
    )

    genre_names = _names(data.get("genres"))
    if "genres" in data:
        manga.legacy_genres = genre_names
        links_by_name = {link.genre.name: link for link in manga.genre_links}
        for name in genre_names:
            genre = genres.get(name)
            if genre is None:
                genre = Genre(name=name)
                db.session.add(genre)
                genres[name] = genre
            if name not in links_by_name:
                manga.genre_links.append(MangaGenre(genre=genre))
        for name, link in links_by_name.items():
            if name not in genre_names:
                db.session.delete(link)

    if any(
        field in data
        for field in ("genres", "explicit_genres", "themes", "demographics")
    ):
        manga.genres_detailed = _detailed_genres(data)
    if "authors" in data:
        if authors is None:
            authors = AuthorCaches(by_name={}, by_mal_id={})
        _reconcile_authors(
            manga,
            data.get("authors"),
            authors,
            author_stats or AuthorSyncStats(),
        )
    manga.last_jikan_sync = datetime.now(timezone.utc)


def _new_manga(
    data: dict[str, Any],
    genres: dict[str, Genre],
    *,
    expected_content_type: str,
    authors: AuthorCaches | None = None,
    author_stats: AuthorSyncStats | None = None,
) -> Manga:
    mal_id = data["mal_id"]
    images = _jpg_images(data)
    manga = Manga(
        mal_id=mal_id,
        content_type=expected_content_type,
        title=data.get("title") or f"MAL manga {mal_id}",
        alternative_title=None,
        synopsis=None,
        manga_type=None,
        publication_year=None,
        status=None,
        score=None,
        popularity=_valid_catalogue_metric(data.get("popularity")),
        members=_valid_catalogue_metric(data.get("members")),
        is_adult=is_adult_content(data),
        chapters=None,
        volumes=None,
        mal_url=data.get("url") or f"https://myanimelist.net/manga/{mal_id}",
        image_url=images.get("large_image_url") or images.get("image_url") or "",
        legacy_genres=[],
        genres_detailed=[],
    )
    _update_manga(
        manga,
        data,
        genres,
        expected_content_type=expected_content_type,
        authors=authors,
        author_stats=author_stats,
    )
    return manga


def _sync_state(key: str) -> JikanSyncState:
    state = db.session.get(JikanSyncState, key)
    if state is None:
        state = JikanSyncState(key=key, next_page=1)
        db.session.add(state)
    return state


def _next_page(key: str) -> int:
    with app.app_context():
        state = db.session.get(JikanSyncState, key)
        return max(1, state.next_page) if state is not None else 1


def _record_page_error(key: str, page: int, error: BaseException) -> None:
    with app.app_context():
        state = _sync_state(key)
        state.next_page = page
        state.last_attempt_at = datetime.now(timezone.utc)
        state.last_error = f"{type(error).__name__}: {error}"[:500]
        db.session.commit()


def _is_database_deadlock(error: OperationalError) -> bool:
    """Identify PostgreSQL's retryable deadlock error without masking others."""
    return getattr(error.orig, "pgcode", None) == DEADLOCK_SQLSTATE


def _apply_manga_page(
    page_result: JikanMangaPage,
    *,
    provider_type: str,
    state_key: str,
    track_changes: bool = False,
) -> MangaPageApplyResult:
    from backend.jobs.refresh_policy import (
        content_snapshot,
        facet_snapshot,
        mark_publication,
    )

    expected_content_type = MANGA_CONTENT_TYPES[provider_type]
    data_by_mal_id: dict[int, dict[str, Any]] = {}
    adult_ids: set[int] = set()

    for entry in page_result.entries:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("mal_id"), int)
            or isinstance(entry.get("mal_id"), bool)
            or entry["mal_id"] <= 0
        ):
            continue
        if is_adult_content(entry):
            adult_ids.add(entry["mal_id"])
            continue
        entry_content_type = _content_type(
            entry.get("type"), fallback=expected_content_type
        )
        if entry_content_type != expected_content_type:
            continue
        data_by_mal_id[entry["mal_id"]] = entry

    for mal_id in adult_ids:
        data_by_mal_id.pop(mal_id, None)

    ids = list(dict.fromkeys([*data_by_mal_id, *adult_ids]))
    with app.app_context():
        existing = {
            manga.mal_id: manga
            for manga in db.session.scalars(
                select(Manga)
                .where(Manga.mal_id.in_(ids))
                .options(
                    lazyload("*"),
                    selectinload(Manga.genre_links).selectinload(MangaGenre.genre),
                    selectinload(Manga.author_links).selectinload(
                        MangaAuthor.author
                    ),
                )
            )
        }
        # Genre's inverse Anime/Manga relationships are not needed while
        # linking this page. Keep them lazy so a small Manga import never
        # triggers a catalogue-wide Anime relationship query.
        genres = {
            genre.name: genre
            for genre in db.session.scalars(
                select(Genre).options(lazyload("*"))
            )
        }
        authors = (
            _author_caches(data_by_mal_id.values()) if track_changes else _author_caches()
        )
        result = MangaPageApplyResult(
            skipped=len(page_result.entries) - len(data_by_mal_id)
        )
        facets_changed = False

        for mal_id in adult_ids:
            manga = existing.pop(mal_id, None)
            if manga is not None:
                db.session.delete(manga)
                result.removed_adult += 1

        for mal_id, data in data_by_mal_id.items():
            manga = existing.get(mal_id)
            before = old_sync = None
            before_facets = (
                facet_snapshot(manga)
                if track_changes and manga is not None
                else None
            )
            if track_changes and manga is not None:
                before, old_sync = content_snapshot(manga), manga.last_jikan_sync
            if manga is None:
                manga = _new_manga(
                    data,
                    genres,
                    expected_content_type=expected_content_type,
                    authors=authors,
                    author_stats=result.author_stats,
                )
                db.session.add(manga)
                existing[mal_id] = manga
                result.inserted += 1
            else:
                result.updated += 1
                _update_manga(
                    manga,
                    data,
                    genres,
                    expected_content_type=expected_content_type,
                    authors=authors,
                    author_stats=result.author_stats,
                )
            result.saved += 1
            if track_changes:
                changed = before != content_snapshot(manga)
                facets_changed |= before_facets != facet_snapshot(manga)
                result.changed += int(changed)
                if not changed:
                    manga.last_jikan_sync = old_sync

        state = _sync_state(state_key)
        state.next_page = (
            page_result.page + 1 if page_result.has_next_page else 1
        )
        state.last_attempt_at = datetime.now(timezone.utc)
        state.last_error = None
        if not page_result.has_next_page:
            state.last_completed_at = datetime.now(timezone.utc)
        if track_changes and (result.changed or result.removed_adult):
            mark_publication(
                db.session,
                facets_changed=facets_changed or bool(result.removed_adult),
            )
        db.session.commit()
        return result


def _sync_manga_type(
    *,
    provider_type: str,
    state_key: str,
    max_pages: int = DEFAULT_MANGA_MAX_PAGES,
    max_consecutive_failures: int = MAX_CONSECUTIVE_FAILURES,
    fetch_page: Callable[..., JikanMangaPage] = get_manga_catalogue_page,
) -> MangaTypeSyncResult:
    if max_pages <= 0:
        raise ValueError("max_pages must be positive")
    if max_consecutive_failures <= 0:
        raise ValueError("max_consecutive_failures must be positive")

    page = _next_page(state_key)
    result = MangaTypeSyncResult(next_page=page)
    consecutive_failures = 0

    for _ in range(max_pages):
        result.pages_attempted += 1
        try:
            fetched = fetch_page(manga_type=provider_type, page=page)
        except JikanTemporaryError as error:
            _record_page_error(state_key, page, error)
            result.pages_failed += 1
            result.next_page = page
            consecutive_failures += 1
            if consecutive_failures >= max_consecutive_failures:
                break
            continue
        except HTTPError as error:
            status_code = error.code
            error.close()
            if status_code not in SKIPPABLE_STATUS_CODES:
                raise
            if status_code == 404 and page > 1:
                _record_page_error(state_key, 1, error)
                result.pages_failed += 1
                result.complete = True
                result.next_page = 1
                break
            _record_page_error(state_key, page, error)
            result.pages_failed += 1
            result.next_page = page
            if status_code == 429:
                break
            consecutive_failures += 1
            if consecutive_failures >= max_consecutive_failures:
                break
            continue

        consecutive_failures = 0
        if (
            fetched.last_visible_page is not None
            and fetched.last_visible_page > PROVIDER_MAX_PAGE
        ):
            result.provider_page_limit_exceeded = True

        try:
            applied = _apply_manga_page(
                fetched,
                provider_type=provider_type,
                state_key=state_key,
            )
        except OperationalError as error:
            if not _is_database_deadlock(error):
                raise
            # The page commit did not complete. Leave its cursor in place so
            # this bounded retry cannot skip catalogue records.
            with app.app_context():
                db.session.rollback()
            _record_page_error(state_key, page, error)
            result.pages_failed += 1
            result.next_page = page
            consecutive_failures += 1
            if consecutive_failures >= max_consecutive_failures:
                break
            sleep(consecutive_failures)
            continue
        result.pages_completed += 1
        result.inserted += applied.inserted
        result.updated += applied.updated
        result.removed_adult += applied.removed_adult
        result.author_stats.add(applied.author_stats)
        if not fetched.has_next_page:
            result.complete = True
            result.next_page = 1
            break
        if fetched.page >= PROVIDER_MAX_PAGE:
            result.provider_page_limit_exceeded = True
            result.complete = True
            limit_error = JikanTemporaryError(
                f"{provider_type} catalogue exceeds provider page "
                f"limit {PROVIDER_MAX_PAGE}"
            )
            # The accessible catalogue cycle is complete. Start at page 1 next
            # run so pages below the provider cap continue receiving updates.
            _record_page_error(state_key, 1, limit_error)
            result.next_page = 1
            break
        page = fetched.page + 1
        result.next_page = page

    return result


def sync_manga_catalogue(
    *,
    max_pages: int = DEFAULT_MANGA_MAX_PAGES,
    max_consecutive_failures: int = MAX_CONSECUTIVE_FAILURES,
    fetch_page: Callable[..., JikanMangaPage] = get_manga_catalogue_page,
) -> MangaCatalogueSyncResult:
    """Discover and update Manga and Manhwa with independent cursors."""
    scans = {}
    for provider_type in MANGA_PROVIDER_TYPES:
        scans[provider_type] = _sync_manga_type(
            provider_type=provider_type,
            state_key=MANGA_STATE_KEYS[provider_type],
            max_pages=max_pages,
            max_consecutive_failures=max_consecutive_failures,
            fetch_page=fetch_page,
        )
    return MangaCatalogueSyncResult(scans=scans)


def _fetch_manga_data(
    mal_id: int,
    fetch_manga: Callable[[int], dict[str, Any]],
) -> tuple[dict[str, Any] | None, str | None]:
    try:
        payload = fetch_manga(mal_id)
    except JikanTemporaryError:
        return None, "temporary"
    except HTTPError as error:
        status_code = error.code
        error.close()
        if status_code == 404:
            return None, "not_found"
        if status_code in TEMPORARY_STATUS_CODES:
            return None, "temporary"
        db.session.rollback()
        raise
    data = payload.get("data")
    if not isinstance(data, dict):
        return None, "invalid_payload"
    payload_mal_id = data.get("mal_id")
    if (
        isinstance(payload_mal_id, bool)
        or not isinstance(payload_mal_id, int)
        or payload_mal_id != mal_id
    ):
        return None, "invalid_payload"
    if _has_unsupported_content_type(data):
        return None, "unsupported_type"
    return data, None


def refresh_manga_catalogue(
    *,
    limit: int = DEFAULT_MANGA_REFRESH_LIMIT,
    batch_size: int = 25,
    fetch_manga: Callable[[int], dict[str, Any]] = get_manga_full,
) -> MangaRefreshResult:
    """Refresh the oldest-attempted Manga and Manhwa detail records."""
    if limit <= 0:
        raise ValueError("limit must be positive")
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")

    with app.app_context():
        manga_rows = list(
            db.session.scalars(
                select(Manga)
                .options(
                    lazyload("*"),
                    selectinload(Manga.genre_links).selectinload(MangaGenre.genre),
                    selectinload(Manga.author_links).selectinload(
                        MangaAuthor.author
                    ),
                )
                .order_by(
                    Manga.last_jikan_attempt.asc().nulls_first(),
                    Manga.mangaID,
                )
                .limit(limit)
            )
        )
        genres = {
            genre.name: genre
            for genre in db.session.scalars(
                select(Genre).options(lazyload("*"))
            )
        }
        authors = _author_caches()
        result = MangaRefreshResult(selected=len(manga_rows))

        for attempted, manga in enumerate(manga_rows, start=1):
            manga.last_jikan_attempt = datetime.now(timezone.utc)
            data, failure = _fetch_manga_data(manga.mal_id, fetch_manga)
            if data is not None:
                if is_adult_content(data):
                    db.session.delete(manga)
                    result.removed_adult += 1
                else:
                    _update_manga(
                        manga,
                        data,
                        genres,
                        expected_content_type=manga.content_type,
                        authors=authors,
                        author_stats=result.author_stats,
                    )
                    result.updated += 1
            elif failure == "not_found":
                result.not_found += 1
            elif failure == "temporary":
                result.temporary_errors += 1
            elif failure == "unsupported_type":
                db.session.delete(manga)
                result.removed_unsupported += 1
            else:
                result.invalid_payloads += 1

            if attempted % batch_size == 0:
                db.session.commit()

        db.session.commit()
        return result


def remove_adult_manga() -> int:
    """Delete stored Manga or Manhwa marked Hentai or Erotica."""
    with app.app_context():
        manga_ids = list(
            db.session.scalars(
                text(
                    "SELECT DISTINCT manga.manga_id FROM manga "
                    "WHERE manga.is_adult = TRUE OR EXISTS ("
                    "SELECT 1 FROM manga_genre "
                    "JOIN genre ON genre.id = manga_genre.genre_id "
                    "WHERE manga_genre.manga_id = manga.manga_id "
                    "AND LOWER(TRIM(genre.name)) IN ('hentai', 'erotica')"
                    ") OR EXISTS ("
                    "SELECT 1 FROM unnest(manga.genres) AS legacy(value) "
                    "WHERE LOWER(TRIM(legacy.value)) IN ('hentai', 'erotica')"
                    ") OR EXISTS ("
                    "SELECT 1 FROM unnest(manga.genres_detailed) AS detail(value) "
                    "WHERE LOWER(TRIM(detail.value)) IN ('hentai', 'erotica')"
                    ")"
                )
            )
        )
        if manga_ids:
            db.session.execute(
                delete(MangaGenre).where(MangaGenre.manga_id.in_(manga_ids))
            )
            db.session.execute(delete(Manga).where(Manga.mangaID.in_(manga_ids)))
        db.session.commit()
        return len(manga_ids)


def _workflow_warning(title: str, message: str) -> None:
    if os.getenv("GITHUB_ACTIONS") == "true":
        print(f"::warning title={title}::{message}")
    else:
        print(f"WARNING: {title}: {message}")


def _append_step_summary(title: str, rows: list[tuple[str, Any]]) -> None:
    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    with Path(summary_path).open("a", encoding="utf-8") as summary:
        summary.write(f"### {title}\n\n")
        for label, value in rows:
            summary.write(f"- **{label}:** {value}\n")
        summary.write("\n")


def report_manga_catalogue(result: MangaCatalogueSyncResult) -> None:
    author_stats = result.author_stats
    details = []
    for provider_type, scan in result.scans.items():
        label = MANGA_CONTENT_TYPES[provider_type].title()
        details.append(
            f"{label}: pages={scan.pages_completed}, inserted={scan.inserted}, "
            f"updated={scan.updated}, removed_adult={scan.removed_adult}, "
            f"failed={scan.pages_failed}, next_page={scan.next_page}"
        )
    print(
        "Manga catalogue sync: "
        f"pages={result.pages_completed}, failed={result.pages_failed}, "
        f"inserted={result.inserted}, updated={result.updated}, "
        f"removed_adult={result.removed_adult}, "
        f"author_payloads={author_stats.payloads_processed}, "
        f"author_links_created={author_stats.links_created}, "
        f"author_links_removed={author_stats.links_removed}, "
        f"author_failures={author_stats.reconciliation_failures}. "
        + "; ".join(details)
    )
    _append_step_summary(
        "Manga and Manhwa catalogue sync",
        [
            ("Pages completed", result.pages_completed),
            ("Pages failed", result.pages_failed),
            ("Titles inserted", result.inserted),
            ("Titles updated", result.updated),
            ("Adult titles removed", result.removed_adult),
            ("Author payloads processed", author_stats.payloads_processed),
            ("Authors processed", author_stats.authors_processed),
            ("Author records created", author_stats.authors_created),
            ("Author relationships created", author_stats.links_created),
            ("Author relationships removed", author_stats.links_removed),
            ("Author roles updated", author_stats.roles_updated),
            (
                "Malformed author entries skipped",
                author_stats.malformed_entries,
            ),
            (
                "Author reconciliation failures",
                author_stats.reconciliation_failures,
            ),
            *[
                (
                    MANGA_CONTENT_TYPES[provider_type].title(),
                    (
                        f"{scan.pages_completed} pages, {scan.inserted} inserted, "
                        f"{scan.updated} updated, {scan.pages_failed} failed, "
                        f"next page {scan.next_page}"
                    ),
                )
                for provider_type, scan in result.scans.items()
            ],
        ],
    )
    for provider_type, scan in result.scans.items():
        if scan.pages_failed:
            _workflow_warning(
                f"{MANGA_CONTENT_TYPES[provider_type].title()} sync degraded",
                (
                    f"{scan.pages_failed}/{scan.pages_attempted} pages failed; "
                    f"page {scan.next_page} will be retried."
                ),
            )
        if scan.provider_page_limit_exceeded:
            _workflow_warning(
                "Provider manga page limit exceeded",
                (
                    f"{provider_type} reports more than {PROVIDER_MAX_PAGE} pages; "
                    "the provider cannot expose the complete catalogue."
                ),
            )


def report_manga_cleanup(removed: int) -> None:
    """Report the pre-import cleanup that is separate from page removals."""
    print(f"Adult Manga/Manhwa cleanup: removed={removed}.")
    _append_step_summary(
        "Adult Manga and Manhwa cleanup",
        [("Stored adult titles removed", removed)],
    )


def report_manga_refresh(result: MangaRefreshResult) -> None:
    author_stats = result.author_stats
    print(
        "Manga detail refresh: "
        f"selected={result.selected}, updated={result.updated}, "
        f"removed_adult={result.removed_adult}, not_found={result.not_found}, "
        f"removed_unsupported={result.removed_unsupported}, "
        f"temporary={result.temporary_errors}, invalid={result.invalid_payloads}, "
        f"author_payloads={author_stats.payloads_processed}, "
        f"author_links_created={author_stats.links_created}, "
        f"author_links_removed={author_stats.links_removed}, "
        f"author_failures={author_stats.reconciliation_failures}, "
        f"success_rate={result.success_rate:.1%}."
    )
    _append_step_summary(
        "Manga and Manhwa detail refresh",
        [
            ("Selected", result.selected),
            ("Updated", result.updated),
            ("Adult titles removed", result.removed_adult),
            ("Unsupported titles removed", result.removed_unsupported),
            ("Not found", result.not_found),
            ("Temporary failures", result.temporary_errors),
            ("Invalid payloads", result.invalid_payloads),
            ("Author payloads processed", author_stats.payloads_processed),
            ("Authors processed", author_stats.authors_processed),
            ("Author records created", author_stats.authors_created),
            ("Author relationships created", author_stats.links_created),
            ("Author relationships removed", author_stats.links_removed),
            ("Author roles updated", author_stats.roles_updated),
            (
                "Malformed author entries skipped",
                author_stats.malformed_entries,
            ),
            (
                "Author reconciliation failures",
                author_stats.reconciliation_failures,
            ),
            ("Success rate", f"{result.success_rate:.1%}"),
        ],
    )
