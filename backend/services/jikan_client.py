"""Rate-limited client for Jikan-compatible catalogue APIs."""

from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from backend.services.jikan_budget import RequestBudget, ResponseCache


DEFAULT_BASE_URL = "https://api.tenrai.org/v1"
DEFAULT_FALLBACK_BASE_URL = "https://api.jikan.moe/v4"
REQUESTS_PER_SECOND = 3
# Leave a little headroom beneath both providers' public 60/minute cap.
REQUESTS_PER_MINUTE = 55
MAX_429_RETRIES = 4
MAX_ANIME_TRANSIENT_RETRIES = 1
MAX_SEASON_TRANSIENT_RETRIES = 2
MAX_TRANSIENT_RETRY_BUDGET = 100
# Match the providers' rolling per-minute window before probing the primary
# again; a longer Retry-After header still takes precedence.
PRIMARY_429_COOLDOWN_SECONDS = 60
REQUEST_TIMEOUT_SECONDS = 20
SERVER_ERROR_STATUS_CODES = frozenset({500, 502, 503, 504})
CATALOGUE_ANIME_TYPES = frozenset(
    {"tv", "movie", "ova", "ona", "special", "tv_special"}
)
CATALOGUE_MANGA_TYPES = frozenset({"manga", "manhwa"})
USER_AGENT = "KyoQuan/1.0 (+https://github.com/MirfTong/Anime-Recommendation-Website)"


class JikanTemporaryError(RuntimeError):
    """No compatible catalogue provider answered after bounded retries."""


@dataclass(frozen=True)
class JikanSeasonPage:
    """One Jikan seasonal page and the cursor required to continue it."""

    entries: list[dict[str, Any]]
    page: int
    has_next_page: bool


@dataclass(frozen=True)
class JikanAnimePage:
    """One page from a Jikan-compatible bulk anime catalogue."""

    entries: list[dict[str, Any]]
    page: int
    has_next_page: bool
    last_visible_page: int | None = None


@dataclass(frozen=True)
class JikanMangaPage:
    """One page from a Jikan-compatible manga catalogue."""

    entries: list[dict[str, Any]]
    page: int
    has_next_page: bool
    last_visible_page: int | None = None


class JikanClient:
    """Fetch Jikan-compatible resources within public request limits.

    Rate-limit retries and transient server retries have separate budgets. A
    shared transient budget prevents a broad Jikan outage from multiplying a
    1,000-record job into thousands of slow retry requests.
    """

    def __init__(
        self,
        *,
        opener: Callable[..., Any] = urlopen,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        transient_retry_budget: int = MAX_TRANSIENT_RETRY_BUDGET,
        base_url: str | None = None,
        fallback_base_url: str | None = None,
        streaming_base_url: str | None = None,
        budget: RequestBudget | None = None,
        response_cache: ResponseCache | None = None,
    ) -> None:
        if transient_retry_budget < 0:
            raise ValueError("transient_retry_budget cannot be negative")
        self._opener = opener
        self.budget = budget
        self.response_cache = response_cache
        self._clock = clock
        self._sleeper = sleeper
        self._request_times: deque[float] = deque()
        self._last_request_time: float | None = None
        self._primary_cooldown_until = 0.0
        self._transient_retries_remaining = transient_retry_budget
        configured_base_url = (
            base_url
            if base_url is not None
            else os.getenv("ANIME_API_BASE_URL", DEFAULT_BASE_URL)
        )
        configured_fallback_url = (
            fallback_base_url
            if fallback_base_url is not None
            else os.getenv("ANIME_API_FALLBACK_BASE_URL", DEFAULT_FALLBACK_BASE_URL)
        )
        self._base_url = self._normalize_base_url(configured_base_url, required=True)
        self._fallback_base_url = self._normalize_base_url(
            configured_fallback_url, required=False
        )
        if self._fallback_base_url == self._base_url:
            self._fallback_base_url = None
        configured_streaming_url = (
            streaming_base_url
            if streaming_base_url is not None
            else os.getenv(
                "ANIME_STREAMING_API_BASE_URL",
                self._fallback_base_url or self._base_url,
            )
        )
        self._streaming_base_url = self._normalize_base_url(
            configured_streaming_url, required=True
        )
        self._lock = threading.Lock()

    def get_anime_full(self, mal_id: int) -> dict[str, Any]:
        """Return full anime data, falling back to the basic endpoint."""
        self._validate_mal_id(mal_id)
        try:
            return self._get(
                f"/anime/{mal_id}/full",
                max_transient_retries=0,
                retry_network_errors=False,
            )
        except (HTTPError, JikanTemporaryError) as error:
            if (
                isinstance(error, HTTPError)
                and error.code not in SERVER_ERROR_STATUS_CODES
            ):
                raise
            payload = self.get_anime(mal_id)
            return (
                {**payload, "_etl_basic_fallback": True}
                if self.budget is not None
                else payload
            )

    def get_anime_streaming(self, mal_id: int) -> dict[str, Any]:
        """Fetch streaming metadata directly from its configured provider.

        Bulk catalogue traffic prefers Tenrai, but production measurements
        showed its full responses almost never contain streaming links. The
        streaming queue therefore uses Jikan (the default fallback) directly,
        avoiding a guaranteed no-op request before every useful request.
        """
        self._validate_mal_id(mal_id)
        return self._get_from_base(
            self._streaming_base_url,
            f"/anime/{mal_id}/full",
            max_transient_retries=MAX_ANIME_TRANSIENT_RETRIES,
            retry_network_errors=True,
        )

    def get_anime(self, mal_id: int) -> dict[str, Any]:
        """Return basic anime data with one bounded 5xx/network retry."""
        self._validate_mal_id(mal_id)
        return self._get(
            f"/anime/{mal_id}",
            max_transient_retries=MAX_ANIME_TRANSIENT_RETRIES,
            retry_network_errors=True,
        )

    def get_manga_full(self, mal_id: int) -> dict[str, Any]:
        """Return full manga data, falling back to the basic endpoint."""
        self._validate_mal_id(mal_id)
        try:
            return self._get(
                f"/manga/{mal_id}/full",
                max_transient_retries=0,
                retry_network_errors=False,
            )
        except (HTTPError, JikanTemporaryError) as error:
            if (
                isinstance(error, HTTPError)
                and error.code not in SERVER_ERROR_STATUS_CODES
            ):
                raise
            payload = self.get_manga(mal_id)
            return (
                {**payload, "_etl_basic_fallback": True}
                if self.budget is not None
                else payload
            )

    def get_manga(self, mal_id: int) -> dict[str, Any]:
        """Return basic manga data with one bounded 5xx/network retry."""
        self._validate_mal_id(mal_id)
        return self._get(
            f"/manga/{mal_id}",
            max_transient_retries=MAX_ANIME_TRANSIENT_RETRIES,
            retry_network_errors=True,
        )

    def get_season_page(
        self,
        year: int | None = None,
        season: str | None = None,
        *,
        page: int = 1,
    ) -> JikanSeasonPage:
        """Return one current or historical season page without hiding failures."""
        self._validate_season(year, season)
        if isinstance(page, bool) or not isinstance(page, int) or page <= 0:
            raise ValueError("page must be a positive integer")

        path = "/seasons/now" if year is None else f"/seasons/{year}/{season}"
        request_path = path if page == 1 else f"{path}?page={page}"
        payload = self._get_page_from_primary(
            request_path,
            max_transient_retries=MAX_SEASON_TRANSIENT_RETRIES,
            retry_network_errors=True,
        )
        data, pagination = self._page_data(
            payload, description="seasonal response", expected_page=page
        )
        return JikanSeasonPage(
            entries=data,
            page=page,
            has_next_page=pagination["has_next_page"],
        )

    def get_season_anime(
        self, year: int | None = None, season: str | None = None
    ) -> list[dict[str, Any]]:
        """Return every anime listed for a current or specified season."""
        page = 1
        anime: list[dict[str, Any]] = []
        while True:
            result = self.get_season_page(year, season, page=page)
            anime.extend(result.entries)
            if not result.has_next_page:
                return anime
            page += 1

    def get_anime_catalogue_page(
        self, *, anime_type: str = "tv", page: int = 1
    ) -> JikanAnimePage:
        """Return one MAL-ID-ordered page from the bulk anime search."""
        self._validate_page(page)
        anime_type = self._validate_catalogue_type(anime_type)
        payload = self._get_page_from_primary(
            f"/anime?type={anime_type}&limit=50&order_by=mal_id&sort=asc&page={page}",
            max_transient_retries=MAX_SEASON_TRANSIENT_RETRIES,
            retry_network_errors=True,
        )
        data, pagination = self._page_data(
            payload, description="anime catalogue response", expected_page=page
        )
        last_visible_page = pagination.get("last_visible_page")
        return JikanAnimePage(
            entries=data,
            page=page,
            has_next_page=pagination["has_next_page"],
            last_visible_page=last_visible_page,
        )

    def get_manga_catalogue_page(
        self, *, manga_type: str, page: int = 1
    ) -> JikanMangaPage:
        """Return one safe-for-work MAL-ID-ordered manga catalogue page."""
        self._validate_page(page)
        manga_type = self._validate_manga_catalogue_type(manga_type)
        payload = self._get_page_from_primary(
            (
                f"/manga?type={manga_type}&limit=50&order_by=mal_id"
                f"&sort=asc&page={page}&sfw=true"
            ),
            max_transient_retries=MAX_SEASON_TRANSIENT_RETRIES,
            retry_network_errors=True,
        )
        data, pagination = self._page_data(
            payload, description="manga catalogue response", expected_page=page
        )
        last_visible_page = pagination.get("last_visible_page")
        has_next_page = pagination["has_next_page"]
        return JikanMangaPage(
            entries=data,
            page=page,
            has_next_page=has_next_page,
            last_visible_page=last_visible_page,
        )

    def _get(
        self,
        path: str,
        *,
        max_transient_retries: int,
        retry_network_errors: bool,
    ) -> dict[str, Any]:
        """Fetch JSON from the primary provider, then a compatible fallback."""
        if self._fallback_base_url is not None and self._primary_cooldown_is_active():
            return self._get_from_base(
                self._fallback_base_url,
                path,
                max_transient_retries=max_transient_retries,
                retry_network_errors=retry_network_errors,
            )

        try:
            return self._get_from_base(
                self._base_url,
                path,
                max_transient_retries=max_transient_retries,
                retry_network_errors=retry_network_errors,
            )
        except HTTPError as error:
            if self._fallback_base_url is None:
                raise
            if error.code == 429:
                self._start_primary_cooldown(error)
            elif error.code not in SERVER_ERROR_STATUS_CODES:
                raise
        except JikanTemporaryError:
            if self._fallback_base_url is None:
                raise

        return self._get_from_base(
            self._fallback_base_url,
            path,
            max_transient_retries=max_transient_retries,
            retry_network_errors=retry_network_errors,
        )

    def _get_page_from_primary(
        self,
        path: str,
        *,
        max_transient_retries: int,
        retry_network_errors: bool,
    ) -> dict[str, Any]:
        """Fetch one cursor page without ever mixing pagination providers."""
        if self._primary_cooldown_is_active():
            raise JikanTemporaryError(
                f"Primary anime API is cooling down after rate limiting: {path}"
            )
        try:
            return self._get_from_base(
                self._base_url,
                path,
                max_transient_retries=max_transient_retries,
                retry_network_errors=retry_network_errors,
            )
        except HTTPError as error:
            if error.code == 429:
                self._start_primary_cooldown(error)
            raise

    def _get_from_base(
        self,
        base_url: str,
        path: str,
        *,
        max_transient_retries: int,
        retry_network_errors: bool,
    ) -> dict[str, Any]:
        """Fetch JSON with independent 429 and bounded transient retry budgets."""
        url = f"{base_url}{path}"
        cached = (
            self.response_cache.get(url) if self.response_cache is not None else None
        )
        if cached is not None:
            if self.budget is not None:
                self.budget.avoided += 1
            return cached
        rate_retries = transient_retries = 0
        while True:
            if self.budget is not None:
                self.budget.check()
            self._throttle()
            if self.budget is not None:
                self.budget.claim()
            request = Request(
                url,
                headers={"Accept": "application/json", "User-Agent": USER_AGENT},
            )
            try:
                with self._opener(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                    try:
                        payload = json.load(response)
                    except (json.JSONDecodeError, UnicodeDecodeError) as error:
                        raise JikanTemporaryError(
                            f"Anime API returned invalid JSON: {path}"
                        ) from error
                    if not isinstance(payload, dict):
                        raise JikanTemporaryError(
                            f"Anime API returned a non-object response: {path}"
                        )
                    if self.budget is not None:
                        self.budget.record_success()
                    if self.response_cache is not None:
                        self.response_cache.put(url, payload)
                    return payload
            except HTTPError as error:
                if self.budget is not None:
                    self.budget.record_failure()
                if error.code == 429 and rate_retries < MAX_429_RETRIES:
                    delay = self._retry_delay(error, rate_retries)
                    rate_retries += 1
                    error.close()
                    self._sleeper(delay)
                    continue
                if (
                    error.code in SERVER_ERROR_STATUS_CODES
                    and transient_retries < max_transient_retries
                    and self._claim_transient_retry()
                ):
                    delay = self._retry_delay(error, transient_retries)
                    transient_retries += 1
                    error.close()
                    self._sleeper(delay)
                    continue
                error.close()
                raise
            except (TimeoutError, URLError) as error:
                if self.budget is not None:
                    self.budget.record_failure()
                if (
                    retry_network_errors
                    and transient_retries < max_transient_retries
                    and self._claim_transient_retry()
                ):
                    self._sleeper(float(2**transient_retries))
                    transient_retries += 1
                    continue
                raise JikanTemporaryError(
                    f"Anime API request failed: {path}"
                ) from error
            except JikanTemporaryError:
                if self.budget is not None:
                    self.budget.record_failure()
                raise

    @staticmethod
    def _page_data(
        payload: dict[str, Any], *, description: str, expected_page: int
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Validate a page envelope before an ETL cursor can advance."""
        data = payload.get("data")
        pagination = payload.get("pagination")
        if not isinstance(data, list):
            raise JikanTemporaryError(f"Anime API {description} did not contain a list")
        if not isinstance(pagination, dict) or not isinstance(
            pagination.get("has_next_page"), bool
        ):
            raise JikanTemporaryError(f"Anime API {description} had invalid pagination")
        # Reject the whole page rather than dropping invalid entries and advancing
        # the durable cursor past titles that have never been applied.
        if any(
            not isinstance(entry, dict)
            or not isinstance(entry.get("mal_id"), int)
            or isinstance(entry["mal_id"], bool)
            or entry["mal_id"] <= 0
            for entry in data
        ):
            raise JikanTemporaryError(f"Anime API {description} had an invalid record")
        current_page = pagination.get("current_page")
        if current_page is not None and (
            isinstance(current_page, bool)
            or not isinstance(current_page, int)
            or current_page != expected_page
        ):
            raise JikanTemporaryError(
                f"Anime API {description} returned the wrong page"
            )
        has_next_page = pagination["has_next_page"]
        if has_next_page and not data:
            raise JikanTemporaryError(
                f"Anime API {description} returned an empty nonterminal page"
            )
        last_visible_page = pagination.get("last_visible_page")
        if last_visible_page is not None:
            if (
                isinstance(last_visible_page, bool)
                or not isinstance(last_visible_page, int)
                or last_visible_page <= 0
            ):
                raise JikanTemporaryError(
                    f"Anime API {description} had invalid pagination"
                )
            if (has_next_page and expected_page >= last_visible_page) or (
                not has_next_page and data and expected_page != last_visible_page
            ):
                raise JikanTemporaryError(
                    f"Anime API {description} pagination was inconsistent"
                )
        return data, pagination

    def _primary_cooldown_is_active(self) -> bool:
        with self._lock:
            return self._clock() < self._primary_cooldown_until

    def _start_primary_cooldown(self, error: HTTPError) -> None:
        cooldown_seconds = max(
            PRIMARY_429_COOLDOWN_SECONDS,
            self._retry_delay(error, MAX_429_RETRIES),
        )
        with self._lock:
            self._primary_cooldown_until = max(
                self._primary_cooldown_until,
                self._clock() + cooldown_seconds,
            )

    @staticmethod
    def _normalize_base_url(value: str, *, required: bool) -> str | None:
        normalized = value.strip().rstrip("/")
        if not normalized:
            if required:
                raise ValueError("base_url cannot be empty")
            return None
        if not normalized.startswith(("https://", "http://")):
            raise ValueError("base_url must be an HTTP(S) URL")
        return normalized

    def _claim_transient_retry(self) -> bool:
        """Reserve one retry from the process-wide outage budget."""
        with self._lock:
            if self._transient_retries_remaining <= 0:
                return False
            self._transient_retries_remaining -= 1
            return True

    def _throttle(self) -> None:
        """Block until the request meets per-second and per-minute caps."""
        with self._lock:
            now = self._clock()
            while self._request_times and now - self._request_times[0] >= 60:
                self._request_times.popleft()

            delays = [0.0]
            if self._last_request_time is not None:
                delays.append(self._last_request_time + (1 / REQUESTS_PER_SECOND) - now)
            if len(self._request_times) >= REQUESTS_PER_MINUTE:
                delays.append(self._request_times[0] + 60 - now)

            delay = max(delays)
            if delay > 0:
                self._sleeper(delay)
                now = self._clock()
                while self._request_times and now - self._request_times[0] >= 60:
                    self._request_times.popleft()

            self._request_times.append(now)
            self._last_request_time = now

    @staticmethod
    def _validate_mal_id(mal_id: int) -> None:
        if isinstance(mal_id, bool) or not isinstance(mal_id, int) or mal_id <= 0:
            raise ValueError("mal_id must be a positive integer")

    @staticmethod
    def _validate_page(page: int) -> None:
        if isinstance(page, bool) or not isinstance(page, int) or page <= 0:
            raise ValueError("page must be a positive integer")

    @staticmethod
    def _validate_catalogue_type(anime_type: str) -> str:
        if not isinstance(anime_type, str):
            raise ValueError("anime_type must be a supported anime type")
        normalized = anime_type.strip().lower()
        if normalized not in CATALOGUE_ANIME_TYPES:
            raise ValueError(
                "anime_type must be tv, movie, ova, ona, special, or tv_special"
            )
        return normalized

    @staticmethod
    def _validate_manga_catalogue_type(manga_type: str) -> str:
        if not isinstance(manga_type, str):
            raise ValueError("manga_type must be manga or manhwa")
        normalized = manga_type.strip().lower()
        if normalized not in CATALOGUE_MANGA_TYPES:
            raise ValueError("manga_type must be manga or manhwa")
        return normalized

    @staticmethod
    def _validate_season(year: int | None, season: str | None) -> None:
        if (year is None) != (season is None):
            raise ValueError("year and season must be supplied together")
        if season is not None and season not in {"winter", "spring", "summer", "fall"}:
            raise ValueError("season must be winter, spring, summer, or fall")

    @staticmethod
    def _retry_delay(error: HTTPError, attempt: int) -> float:
        """Use Retry-After when available, otherwise exponential backoff."""
        retry_after = error.headers.get("Retry-After") if error.headers else None
        if retry_after:
            try:
                return max(0.0, float(retry_after))
            except ValueError:
                pass
        return float(2**attempt)


_default_client = JikanClient()


def get_anime(mal_id: int) -> dict[str, Any]:
    """Return the parsed Jikan v4 basic-anime payload for a MAL ID."""
    return _default_client.get_anime(mal_id)


def get_anime_full(mal_id: int) -> dict[str, Any]:
    """Return the parsed Jikan v4 full-anime payload for a MAL ID."""
    return _default_client.get_anime_full(mal_id)


def get_anime_streaming(mal_id: int) -> dict[str, Any]:
    """Return a full payload enriched with fallback streaming metadata."""
    return _default_client.get_anime_streaming(mal_id)


def get_manga(mal_id: int) -> dict[str, Any]:
    """Return the parsed Jikan-compatible basic-manga payload."""
    return _default_client.get_manga(mal_id)


def get_manga_full(mal_id: int) -> dict[str, Any]:
    """Return the parsed Jikan-compatible full-manga payload."""
    return _default_client.get_manga_full(mal_id)


def get_season_page(
    year: int | None = None,
    season: str | None = None,
    *,
    page: int = 1,
) -> JikanSeasonPage:
    """Return one parsed Jikan seasonal page."""
    return _default_client.get_season_page(year, season, page=page)


def get_season_anime(
    year: int | None = None, season: str | None = None
) -> list[dict[str, Any]]:
    """Return every anime from a current or specified season."""
    return _default_client.get_season_anime(year, season)


def get_anime_catalogue_page(
    *, anime_type: str = "tv", page: int = 1
) -> JikanAnimePage:
    """Return one parsed page from Jikan's bulk anime catalogue."""
    return _default_client.get_anime_catalogue_page(anime_type=anime_type, page=page)


def get_manga_catalogue_page(*, manga_type: str, page: int = 1) -> JikanMangaPage:
    """Return one parsed safe-for-work manga catalogue page."""
    return _default_client.get_manga_catalogue_page(manga_type=manga_type, page=page)
