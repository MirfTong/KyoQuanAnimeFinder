import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import SQLAlchemyError

from backend.app import (
    CacheGenerationMonitor,
    FRONTEND_BUILD_DIR,
    TtlCache,
    _attach_verified_refresh,
    _analytics_report,
    _record_site_visit,
    _anime_statement,
    _catalogue_rows_subquery,
    _filtered_anime_statement,
    _filtered_manga_statement,
    _manga_statement,
    _normalized_anime_status,
    _normalized_content_type,
    _normalized_status,
    _normalized_type,
    _next_season_identity,
    _ordered_catalogue_rows,
    _ordered_anime_statement,
    _random_catalogue,
    _random_window_statement,
    _request_filter_signature,
    _sampled_random_statement,
    _serialize_anime,
    _serialize_manga,
    app,
    analytics_response_cache,
    initialize_database_schema,
    response_cache,
)
from backend.models import (
    Anime,
    AnimeStreamingService,
    Author,
    Manga,
    MangaAuthor,
    StreamingService,
    Studio,
    db,
)


class AppTests(unittest.TestCase):
    def setUp(self):
        response_cache.clear()
        analytics_response_cache.clear()
        self.client = app.test_client()

    def test_anime_list_returns_paginated_json(self):
        response = self.client.get("/api/v1/anime?min_year=2020&per_page=2")

        body = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(body["items"]), 2)
        self.assertEqual(body["pagination"]["per_page"], 2)
        self.assertGreaterEqual(body["items"][0]["year"], 2020)
        self.assertIn("status", body["items"][0])
        self.assertIn("studios", body["items"][0])
        self.assertIn("streaming_services", body["items"][0])

    def test_preview_lists_omit_detail_only_relationship_payloads(self):
        anime = self.client.get(
            "/api/v1/anime", query_string={"preview": 1, "per_page": 1}
        ).get_json()["items"][0]
        manga = self.client.get(
            "/api/v1/manga", query_string={"preview": 1, "per_page": 1}
        ).get_json()["items"][0]

        self.assertNotIn("studios", anime)
        self.assertNotIn("streaming_services", anime)
        self.assertNotIn("authors", manga)
        self.assertIn("genres", anime)
        self.assertIn("genres", manga)

        mixed_response = self.client.get(
            "/api/v1/catalogue",
            query_string={
                "content_type": "ALL",
                "preview": 1,
                "per_page": 3,
            },
        )
        self.assertEqual(mixed_response.status_code, 200)
        self.assertTrue(mixed_response.get_json()["items"])

    def test_preview_statements_do_not_select_large_detail_columns(self):
        anime_sql = str(_anime_statement(preview=True)).lower()
        manga_sql = str(_manga_statement({"MANGA"}, preview=True)).lower()

        self.assertNotIn("anime.synopsis", anime_sql)
        self.assertNotIn("anime.genres_detailed", anime_sql)
        self.assertNotIn("anime.last_jikan_sync", anime_sql)
        self.assertNotIn("manga.synopsis", manga_sql)
        self.assertNotIn("manga.genres_detailed", manga_sql)
        self.assertNotIn("manga.last_jikan_sync", manga_sql)

    def test_fingerprinted_assets_receive_immutable_cache_headers(self):
        asset = next((FRONTEND_BUILD_DIR / "assets").glob("*.js"))

        response = self.client.get(f"/assets/{asset.name}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers["Cache-Control"],
            "public, max-age=31536000, immutable",
        )

    def test_ttl_cache_evicts_entries_when_bounded(self):
        cache = TtlCache(max_entries=2)
        cache.get_or_create(("first",), lambda: 1)
        cache.get_or_create(("second",), lambda: 2)
        cache.get_or_create(("third",), lambda: 3)

        self.assertEqual(len(cache._values), 2)
        self.assertNotIn(("first",), cache._values)

    def test_etl_generation_change_invalidates_process_cache(self):
        monitor = CacheGenerationMonitor()
        first = datetime(2026, 8, 1, tzinfo=timezone.utc)
        second = first + timedelta(minutes=1)
        response_cache.get_or_create(("generation-probe",), lambda: "old")

        with patch(
            "backend.app.db.session.scalar", side_effect=(None, second)
        ):
            monitor.refresh_if_needed()
            monitor._next_check = 0
            monitor.refresh_if_needed()

        self.assertNotIn(("generation-probe",), response_cache._values)

    def test_anime_detail_returns_detailed_tags(self):
        response = self.client.get("/api/v1/anime/52991")

        body = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["item"]["mal_id"], 52991)
        self.assertIn("season", body["item"])
        self.assertIn("synopsis", body["item"])
        self.assertIn("genres_detailed", body["item"])

    def test_detail_freshness_uses_verified_refresh_state(self):
        verified = datetime(2026, 9, 19, 9, 30, tzinfo=timezone.utc)
        payload = {"last_jikan_sync": "2026-09-01T00:00:00+00:00"}

        with patch("backend.app.db.session.scalar", return_value=verified):
            _attach_verified_refresh(payload, "anime", 52991)

        self.assertEqual(payload["last_verified_refresh"], verified.isoformat())

    def test_anime_list_accepts_a_season_filter(self):
        response = self.client.get("/api/v1/anime?season=winter&per_page=2")

        body = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(all(item["season"] == "winter" for item in body["items"]))

    def test_anime_random_endpoint_honors_genre_exclusions(self):
        response = self.client.get(
            "/api/v1/anime/random?exclude_genre=Action&limit=6"
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            all(
                "Action" not in item["genres"]
                for item in response.get_json()["items"]
            )
        )

    def test_catalogue_supports_database_ordered_sort_options(self):
        expectations = {
            "top_rated": ("score", True),
            "newest": ("year", True),
            "oldest": ("year", False),
            "most_episodes": ("episodes", True),
        }
        for sort, (field, reverse) in expectations.items():
            with self.subTest(sort=sort):
                response = self.client.get(
                    "/api/v1/catalogue",
                    query_string={
                        "content_type": "ANIME",
                        "sort": sort,
                        "per_page": 8,
                    },
                )
                self.assertEqual(response.status_code, 200)
                values = [
                    item[field]
                    for item in response.get_json()["items"]
                    if item[field] is not None
                ]
                self.assertEqual(values, sorted(values, reverse=reverse))

        title_response = self.client.get(
            "/api/v1/catalogue",
            query_string={
                "content_type": "MANGA",
                "sort": "title",
                "per_page": 8,
            },
        )
        repeated_title_response = self.client.get(
            "/api/v1/catalogue",
            query_string={
                "content_type": "MANGA",
                "sort": "title",
                "per_page": 8,
            },
        )
        self.assertEqual(title_response.status_code, 200)
        self.assertEqual(
            title_response.get_json()["items"],
            repeated_title_response.get_json()["items"],
        )
        with app.test_request_context(
            "/api/v1/catalogue?content_type=MANGA&sort=title"
        ):
            catalogue_rows = _catalogue_rows_subquery({"MANGA"})
            title_sql = str(
                _ordered_catalogue_rows(catalogue_rows, "title").compile(
                    dialect=postgresql.dialect()
                )
            ).lower()
        self.assertIn("order by lower(catalogue_rows.title)", title_sql)

    def test_popularity_and_member_sorts_are_available_for_every_catalogue_scope(self):
        for content_type in ("ANIME", "MANGA", "MANHWA", "ALL"):
            for sort in ("most_popular", "most_members"):
                with self.subTest(content_type=content_type, sort=sort):
                    response = self.client.get(
                        "/api/v1/catalogue",
                        query_string={
                            "content_type": content_type,
                            "sort": sort,
                            "per_page": 1,
                        },
                    )
                    self.assertEqual(response.status_code, 200)
                    for item in response.get_json()["items"]:
                        self.assertIn("popularity", item)
                        self.assertIn("members", item)

        with app.test_request_context(
            "/api/v1/catalogue?content_type=ALL&sort=most_popular"
        ):
            rows = _catalogue_rows_subquery({"ANIME", "MANGA", "MANHWA"})
            popularity_sql = str(
                _ordered_catalogue_rows(rows, "most_popular").compile(
                    dialect=postgresql.dialect()
                )
            ).lower()
            member_sql = str(
                _ordered_catalogue_rows(rows, "most_members").compile(
                    dialect=postgresql.dialect()
                )
            ).lower()
        self.assertIn("catalogue_rows.popularity asc nulls last", popularity_sql)
        self.assertIn("catalogue_rows.members desc nulls last", member_sql)

        with app.test_request_context("/api/v1/anime?sort=most_popular"):
            anime_sql = str(
                _ordered_anime_statement(
                    _filtered_anime_statement(), "most_popular"
                ).compile(dialect=postgresql.dialect())
            ).lower()
        self.assertIn("anime.popularity asc nulls last", anime_sql)

    def test_sort_options_are_validated_for_the_selected_content(self):
        anime_response = self.client.get(
            "/api/v1/catalogue",
            query_string={
                "content_type": "ANIME",
                "sort": "most_chapters",
            },
        )
        mixed_response = self.client.get(
            "/api/v1/catalogue",
            query_string={
                "content_type": "ALL",
                "sort": "most_episodes",
            },
        )

        self.assertEqual(anime_response.status_code, 400)
        self.assertEqual(mixed_response.status_code, 400)

    def test_catalogue_exposes_freshness_only_from_available_sync_data(self):
        response = self.client.get(
            "/api/v1/catalogue",
            query_string={"content_type": "ANIME", "per_page": 1},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("updated_at", response.get_json())

    def test_anime_maximum_episode_filter_supports_short_series(self):
        response = self.client.get(
            "/api/v1/catalogue",
            query_string={
                "content_type": "ANIME",
                "max_episodes": 13,
                "per_page": 8,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            all(
                item["episodes"] is not None and item["episodes"] <= 13
                for item in response.get_json()["items"]
            )
        )

    def test_type_filter_values_are_normalized(self):
        self.assertEqual(_normalized_type("Movie"), "MOVIE")
        self.assertEqual(_normalized_type("tv_special"), "SPECIAL")

    def test_readable_catalogue_filter_values_are_normalized(self):
        self.assertEqual(_normalized_content_type(" manhwa "), "MANHWA")
        self.assertEqual(_normalized_status("NOT_YET_PUBLISHED"), "not yet published")
        self.assertEqual(
            _normalized_anime_status("Currently Airing"),
            "CURRENTLY_AIRING",
        )

    def test_anime_serializer_exposes_normalized_airing_status(self):
        anime = Anime(
            animeID=12,
            mal_id=24,
            title="Example Anime",
            alternative_title=None,
            synopsis=None,
            type="TV",
            season="summer",
            status="CURRENTLY_AIRING",
            year=2026,
            score=8.0,
            popularity=125,
            members=500000,
            episodes=12,
            mal_url="https://myanimelist.net/anime/24",
            sequel=False,
            image_url="",
            legacy_genres=[],
            genres_detailed=[],
        )

        item = _serialize_anime(anime)

        self.assertEqual(item["status"], "CURRENTLY_AIRING")
        self.assertEqual(item["popularity"], 125)
        self.assertEqual(item["members"], 500000)

    def test_anime_serializer_exposes_sorted_studios_and_streaming_links(self):
        anime = Anime(
            animeID=13,
            mal_id=25,
            title="Relationship Example",
            alternative_title=None,
            synopsis=None,
            type="TV",
            season="summer",
            status="CURRENTLY_AIRING",
            year=2026,
            score=8.0,
            episodes=12,
            mal_url="https://myanimelist.net/anime/25",
            sequel=False,
            image_url="",
            legacy_genres=[],
            genres_detailed=[],
        )
        anime.studio_entries.extend(
            [
                Studio(mal_id=2, name="Zeta Studio", normalized_name="zeta studio"),
                Studio(mal_id=1, name="Alpha Studio", normalized_name="alpha studio"),
            ]
        )
        crunchyroll = StreamingService(
            name="Crunchyroll",
            normalized_name="crunchyroll",
        )
        netflix = StreamingService(name="Netflix", normalized_name="netflix")
        anime.streaming_links.extend(
            [
                AnimeStreamingService(
                    streaming_service=netflix,
                    url="https://example.test/netflix",
                ),
                AnimeStreamingService(
                    streaming_service=crunchyroll,
                    url="https://example.test/crunchyroll",
                ),
            ]
        )

        item = _serialize_anime(anime)

        self.assertEqual(
            item["studios"],
            [
                {"mal_id": 1, "name": "Alpha Studio"},
                {"mal_id": 2, "name": "Zeta Studio"},
            ],
        )
        self.assertEqual(
            item["streaming_services"],
            [
                {
                    "name": "Crunchyroll",
                    "url": "https://example.test/crunchyroll",
                },
                {
                    "name": "Netflix",
                    "url": "https://example.test/netflix",
                },
            ],
        )
        preview = _serialize_anime(anime, preview=True)
        self.assertLess(
            len(json.dumps(preview)),
            len(json.dumps(item)),
        )

    def test_anime_status_filter_uses_canonical_indexed_predicate(self):
        with app.test_request_context(
            "/api/v1/catalogue?content_type=ANIME"
            "&status=Currently%20Airing"
        ):
            statement = _filtered_anime_statement()
        sql = str(
            statement.compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        ).lower()

        self.assertIn(
            "anime.status in ('currently_airing')",
            sql,
        )

    def test_anime_studio_and_streaming_filters_use_any_match_exists_predicates(self):
        with app.test_request_context(
            "/api/v1/catalogue?content_type=ANIME"
            "&studio=Studio%20Pierrot&studio=MAPPA"
            "&streaming_service=Crunchyroll"
            "&streaming_service=Netflix"
        ):
            statement = _filtered_anime_statement()
        sql = str(
            statement.compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        ).lower()

        self.assertIn("exists", sql)
        self.assertIn("studio.normalized_name in ('studio pierrot', 'mappa')", sql)
        self.assertIn(
            "streaming_service.normalized_name in ('crunchyroll', 'netflix')",
            sql,
        )
        # One IN predicate gives match-any semantics. Repeating one EXISTS per
        # selected value would accidentally require an anime to match all.
        self.assertEqual(sql.count("studio.normalized_name in"), 1)
        self.assertEqual(sql.count("streaming_service.normalized_name in"), 1)

    def test_anime_only_relationship_filters_narrow_mixed_catalogue_results(self):
        with app.test_request_context(
            "/api/v1/catalogue?content_type=ALL&studio=MAPPA"
        ):
            catalogue_rows = _catalogue_rows_subquery(
                {"ANIME", "MANGA", "MANHWA"}
            )
        sql = str(
            select(catalogue_rows).compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        ).lower()

        self.assertIn("'anime' as content_type", sql)
        self.assertNotIn("from manga", sql)

    def test_streaming_filter_narrows_mixed_catalogue_results_to_anime(self):
        with app.test_request_context(
            "/api/v1/catalogue?content_type=ALL"
            "&streaming_service=Crunchyroll"
        ):
            catalogue_rows = _catalogue_rows_subquery(
                {"ANIME", "MANGA", "MANHWA"}
            )
        sql = str(
            select(catalogue_rows).compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        ).lower()

        self.assertIn("'anime' as content_type", sql)
        self.assertIn("streaming_service.normalized_name", sql)
        self.assertNotIn("from manga", sql)

    def test_print_length_filters_narrow_mixed_catalogue_results_to_print(self):
        with app.test_request_context(
            "/api/v1/catalogue?content_type=ALL"
            "&min_chapters=10&max_chapters=100"
            "&min_volumes=2&max_volumes=12"
        ):
            catalogue_rows = _catalogue_rows_subquery(
                {"ANIME", "MANGA", "MANHWA"}
            )
        sql = str(
            select(catalogue_rows).compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        ).lower()

        self.assertNotIn("from anime", sql)
        self.assertIn("from manga", sql)
        self.assertIn("manga.chapters >= 10", sql)
        self.assertIn("manga.chapters <= 100", sql)
        self.assertIn("manga.volumes >= 2", sql)
        self.assertIn("manga.volumes <= 12", sql)

    def test_anime_and_print_status_semantics_are_kept_separate(self):
        invalid_anime = self.client.get(
            "/api/v1/catalogue",
            query_string={
                "content_type": "ANIME",
                "status": "PUBLISHING",
            },
        )
        mixed_status = self.client.get(
            "/api/v1/catalogue",
            query_string={
                "content_type": "ALL",
                "status": "CURRENTLY_AIRING",
            },
        )
        print_status = self.client.get(
            "/api/v1/catalogue",
            query_string={
                "content_type": "MANGA",
                "status": "FINISHED",
                "per_page": 1,
            },
        )

        self.assertEqual(invalid_anime.status_code, 400)
        self.assertEqual(mixed_status.status_code, 400)
        self.assertEqual(print_status.status_code, 200)

    def test_public_api_excludes_hentai_records_and_filter_options(self):
        anime_response = self.client.get(
            "/api/v1/anime", query_string={"genre": "Hentai"}
        )
        genre_response = self.client.get("/api/v1/genres")
        tag_response = self.client.get(
            "/api/v1/tags", query_string={"q": "hentai"}
        )

        self.assertEqual(anime_response.status_code, 200)
        self.assertEqual(anime_response.get_json()["items"], [])
        self.assertFalse(
            any(
                name.strip().casefold() == "hentai"
                for name in genre_response.get_json()["items"]
            )
        )
        self.assertFalse(
            any(
                name.strip().casefold() == "hentai"
                for name in tag_response.get_json()["items"]
            )
        )

    def test_public_anime_query_uses_the_indexed_adult_flag(self):
        sql = str(_anime_statement()).lower()

        self.assertIn("anime.is_adult is false", sql)
        self.assertNotIn("unnest(", sql)
        self.assertNotIn("genre.name", sql)

    def test_manga_public_query_uses_the_indexed_adult_flag(self):
        sql = str(
            _manga_statement({"MANGA", "MANHWA"}).compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        ).lower()

        self.assertIn("manga.is_adult is false", sql)
        self.assertNotIn("unnest(", sql)
        self.assertNotIn("genre.name", sql)

    def test_catalogue_filters_include_unrated_public_titles(self):
        with app.test_request_context("/api/v1/catalogue?content_type=ANIME"):
            anime_sql = str(_filtered_anime_statement()).lower()
        with app.test_request_context("/api/v1/catalogue?content_type=MANGA"):
            manga_sql = str(_filtered_manga_statement({"MANGA"})).lower()

        self.assertNotIn("anime.score is not null", anime_sql)
        self.assertNotIn("manga.score is not null", manga_sql)

    def test_range_predicates_only_exclude_unknown_values_when_active(self):
        with app.test_request_context("/api/v1/catalogue?content_type=ANIME"):
            unrestricted_anime_sql = str(
                _filtered_anime_statement().compile(
                    dialect=postgresql.dialect(),
                    compile_kwargs={"literal_binds": True},
                )
            ).lower()
        with app.test_request_context(
            "/api/v1/catalogue?content_type=ANIME"
            "&min_year=2000&max_year=2026"
            "&min_episodes=6&max_episodes=24&min_score=7"
        ):
            restricted_anime_sql = str(
                _filtered_anime_statement().compile(
                    dialect=postgresql.dialect(),
                    compile_kwargs={"literal_binds": True},
                )
            ).lower()
        with app.test_request_context("/api/v1/catalogue?content_type=MANGA"):
            unrestricted_print_sql = str(
                _filtered_manga_statement({"MANGA"}).compile(
                    dialect=postgresql.dialect(),
                    compile_kwargs={"literal_binds": True},
                )
            ).lower()
        with app.test_request_context(
            "/api/v1/catalogue?content_type=MANGA"
            "&min_chapters=10&max_chapters=100"
            "&min_volumes=2&max_volumes=12"
        ):
            restricted_print_sql = str(
                _filtered_manga_statement({"MANGA"}).compile(
                    dialect=postgresql.dialect(),
                    compile_kwargs={"literal_binds": True},
                )
            ).lower()

        # With no range predicate, NULL/unknown metadata remains eligible.
        self.assertNotIn("anime.year >=", unrestricted_anime_sql)
        self.assertNotIn("anime.year <=", unrestricted_anime_sql)
        self.assertNotIn("anime.episodes >=", unrestricted_anime_sql)
        self.assertNotIn("anime.episodes <=", unrestricted_anime_sql)
        self.assertNotIn("anime.score >=", unrestricted_anime_sql)
        self.assertNotIn("manga.chapters >=", unrestricted_print_sql)
        self.assertNotIn("manga.chapters <=", unrestricted_print_sql)
        self.assertNotIn("manga.volumes >=", unrestricted_print_sql)
        self.assertNotIn("manga.volumes <=", unrestricted_print_sql)

        # SQL comparisons evaluate to UNKNOWN for NULL, so an active range
        # excludes records whose value cannot be confirmed to match.
        self.assertIn("anime.year >= 2000", restricted_anime_sql)
        self.assertIn("anime.year <= 2026", restricted_anime_sql)
        self.assertIn("anime.episodes >= 6", restricted_anime_sql)
        self.assertIn("anime.episodes <= 24", restricted_anime_sql)
        self.assertIn("anime.score >= 7", restricted_anime_sql)
        self.assertIn("manga.chapters >= 10", restricted_print_sql)
        self.assertIn("manga.chapters <= 100", restricted_print_sql)
        self.assertIn("manga.volumes >= 2", restricted_print_sql)
        self.assertIn("manga.volumes <= 12", restricted_print_sql)

    def test_pagination_pages_share_one_total_count_cache_key(self):
        with app.test_request_context(
            "/api/v1/catalogue?content_type=ANIME&page=1&per_page=24"
            "&genre=Action"
        ):
            first_page = _request_filter_signature(
                exclude={"content_type", "page", "per_page"}
            )
        with app.test_request_context(
            "/api/v1/catalogue?content_type=ANIME&page=7&per_page=48"
            "&genre=Action"
        ):
            later_page = _request_filter_signature(
                exclude={"content_type", "page", "per_page"}
            )

        self.assertEqual(first_page, later_page)

    def test_count_cache_canonicalizes_reordered_multi_value_filters(self):
        with app.test_request_context(
            "/api/v1/catalogue?genre=Action,Comedy&exclude_genre=Isekai,Harem"
            "&exclude_tag=harem&studio=MAPPA&studio=Bones"
        ):
            first = _request_filter_signature(exclude=set())
        with app.test_request_context(
            "/api/v1/catalogue?studio=bones,mappa&genre=Comedy&genre=Action"
            "&exclude_genre=Harem&exclude_genre=Isekai&exclude_tag=harem"
        ):
            second = _request_filter_signature(exclude=set())

        self.assertEqual(first, second)

    def test_genre_and_tag_exclusions_use_database_side_predicates(self):
        with app.test_request_context(
            "/api/v1/catalogue?content_type=ALL&genre=Action&tag=school"
            "&exclude_genre=Isekai&exclude_tag=harem"
        ):
            anime_sql = str(
                _filtered_anime_statement().compile(
                    dialect=postgresql.dialect(),
                    compile_kwargs={"literal_binds": True},
                )
            ).lower()
            manga_sql = str(
                _filtered_manga_statement({"MANGA", "MANHWA"}).compile(
                    dialect=postgresql.dialect(),
                    compile_kwargs={"literal_binds": True},
                )
            ).lower()
            mixed_sql = str(
                _catalogue_rows_subquery(
                    {"ANIME", "MANGA", "MANHWA"}
                ).select().compile(
                    dialect=postgresql.dialect(),
                    compile_kwargs={"literal_binds": True},
                )
            ).lower()

        for sql, model_name in ((anime_sql, "anime"), (manga_sql, "manga")):
            with self.subTest(model=model_name):
                self.assertIn("genre.name = 'action'", sql)
                self.assertIn("genre.name = 'isekai'", sql)
                self.assertIn("not (exists", sql)
                self.assertIn(f"{model_name}.genres_detailed @> array['school']", sql)
                self.assertIn(
                    f"not ({model_name}.genres_detailed @> array['harem'])",
                    sql,
                )
                self.assertIn(f"{model_name}.is_adult is false", sql)
        # The mixed catalogue keeps independent Anime, Manga, and Manhwa
        # branches so its database predicates remain type-correct.
        self.assertEqual(mixed_sql.count("genre.name = 'isekai'"), 3)
        self.assertEqual(mixed_sql.count("not (exists"), 3)

    def test_conflicting_included_and_excluded_genre_returns_no_matches(self):
        response = self.client.get(
            "/api/v1/catalogue?content_type=ALL&genre=Action"
            "&exclude_genre=Action&per_page=2"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["pagination"]["total"], 0)

    def test_mixed_page_limits_each_media_branch_before_global_sort(self):
        with app.test_request_context(
            "/api/v1/catalogue?content_type=ALL&sort=top_rated"
        ):
            rows = _catalogue_rows_subquery(
                {"ANIME", "MANGA", "MANHWA"},
                branch_limit=24,
                sort="top_rated",
            )
            sql = str(
                _ordered_catalogue_rows(rows, "top_rated").compile(
                    dialect=postgresql.dialect(),
                    compile_kwargs={"literal_binds": True},
                )
            ).lower()

        self.assertEqual(sql.count("limit 24"), 3)
        self.assertEqual(sql.count("union all"), 2)
        self.assertNotIn("synopsis", sql)
        self.assertNotIn("genres_detailed", sql)
        self.assertNotIn("alternative_title", sql)
        self.assertNotIn("last_jikan_sync", sql)

    def test_unfiltered_random_statement_samples_before_random_sorting(self):
        statement = _sampled_random_statement(
            {"ANIME", "MANGA", "MANHWA"}, 6
        )
        sql = str(
            statement.compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        ).lower()

        self.assertEqual(sql.count("tablesample system(2)"), 2)
        self.assertIn("order by random()", sql)
        self.assertIn("limit 6", sql)

    def test_filtered_random_statement_seeks_from_an_id_pivot(self):
        with app.test_request_context(
            "/api/v1/catalogue?content_type=ALL&genre=Action"
        ):
            rows = _catalogue_rows_subquery(
                {"ANIME", "MANGA", "MANHWA"}
            )
            sql = str(
                _random_window_statement(
                    rows,
                    pivot=500,
                    limit=6,
                ).compile(
                    dialect=postgresql.dialect(),
                    compile_kwargs={"literal_binds": True},
                )
            ).lower()

        self.assertIn("catalogue_rows.record_id >= 500", sql)
        self.assertIn("limit 6", sql)
        self.assertNotIn("order by random()", sql)
        self.assertNotIn(" offset ", sql)

    def test_filtered_random_wraps_without_using_physical_sampling(self):
        first_result = Mock()
        first_result.all.return_value = ["tail-row"]
        wrapped_result = Mock()
        wrapped_result.all.return_value = ["head-row"]

        with (
            app.test_request_context(
                "/api/v1/catalogue/random?genre=Action&limit=2"
            ),
            patch(
                "backend.app._random_id_bounds", return_value=(1, 10)
            ),
            patch("backend.app.randrange", return_value=9),
            patch(
                "backend.app._sampled_random_rows"
            ) as sampled_random,
            patch(
                "backend.app.db.session.execute",
                side_effect=(first_result, wrapped_result),
            ) as execute,
            patch(
                "backend.app._serialize_catalogue_rows",
                return_value=[{"id": 9}, {"id": 1}],
            ),
        ):
            response = _random_catalogue(
                {"ANIME", "MANGA", "MANHWA"}
            )

        self.assertEqual(response.get_json()["items"], [{"id": 9}, {"id": 1}])
        sampled_random.assert_not_called()
        self.assertEqual(execute.call_count, 2)
        first_sql = str(execute.call_args_list[0].args[0]).lower()
        wrapped_sql = str(execute.call_args_list[1].args[0]).lower()
        self.assertIn("catalogue_rows.record_id >=", first_sql)
        self.assertIn("catalogue_rows.record_id <", wrapped_sql)

    def test_tag_searches_reuse_one_precomputed_scope(self):
        with patch(
            "backend.app._load_detailed_tag_names",
            return_value=("action", "school", "space"),
        ) as load_tags:
            action = self.client.get(
                "/api/v1/tags?q=act&content_type=ANIME"
            )
            school = self.client.get(
                "/api/v1/tags?q=school&content_type=ANIME"
            )

        self.assertEqual(action.get_json()["items"], ["action"])
        self.assertEqual(school.get_json()["items"], ["school"])
        load_tags.assert_called_once()

    def test_unlimited_tag_and_studio_dropdowns_return_every_cached_option(self):
        with (
            patch(
                "backend.app._load_detailed_tag_names",
                return_value=("action", "school", "space"),
            ),
            patch(
                "backend.app._load_facet_names",
                return_value=("Bones", "MAPPA", "Studio Pierrot"),
            ),
            patch(
                "backend.app._load_facet_page",
                return_value=(("action",), True),
            ),
        ):
            tags = self.client.get("/api/v1/tags")
            studios = self.client.get("/api/v1/studios")
            limited_tags = self.client.get("/api/v1/tags?limit=1")

        self.assertEqual(tags.get_json()["items"], ["action", "school", "space"])
        self.assertEqual(
            studios.get_json()["items"],
            ["Bones", "MAPPA", "Studio Pierrot"],
        )
        self.assertEqual(limited_tags.get_json()["items"], ["action"])

    def test_facets_support_incremental_pages_without_limiting_search(self):
        def facet_page(_types, _facet, _query, *, offset, limit):
            self.assertEqual(limit, 2)
            return (
                (("Author A", "Author B"), True)
                if offset == 0
                else (("Author C",), False)
            )

        with patch(
            "backend.app._load_facet_page", side_effect=facet_page
        ) as load_page:
            first = self.client.get("/api/v1/authors?limit=2&offset=0")
            second = self.client.get("/api/v1/authors?limit=2&offset=2")
            repeated_first = self.client.get(
                "/api/v1/authors?limit=2&offset=0"
            )

        self.assertEqual(first.get_json()["items"], ["Author A", "Author B"])
        self.assertTrue(first.get_json()["pagination"]["has_more"])
        self.assertEqual(second.get_json()["items"], ["Author C"])
        self.assertFalse(second.get_json()["pagination"]["has_more"])
        self.assertEqual(repeated_first.get_json(), first.get_json())
        self.assertEqual(load_page.call_count, 2)

    def test_studio_and_streaming_searches_reuse_precomputed_facets(self):
        def load_facets(_content_types, facet_type):
            if facet_type == "studio":
                return ("Bones", "MAPPA", "Studio Pierrot")
            return ("Crunchyroll", "Netflix")

        with patch(
            "backend.app._load_facet_names",
            side_effect=load_facets,
        ) as load_options:
            mappa = self.client.get("/api/v1/studios?q=map")
            pierrot = self.client.get("/api/v1/studios?q=pier")
            netflix = self.client.get("/api/v1/streaming-services?q=net")

        self.assertEqual(mappa.get_json()["items"], ["MAPPA"])
        self.assertEqual(pierrot.get_json()["items"], ["Studio Pierrot"])
        self.assertEqual(netflix.get_json()["items"], ["Netflix"])
        self.assertEqual(load_options.call_count, 2)

    def test_filter_range_endpoint_returns_content_appropriate_bounds(self):
        anime = self.client.get(
            "/api/v1/filter-ranges",
            query_string={"content_type": "ANIME"},
        )
        manga = self.client.get(
            "/api/v1/filter-ranges",
            query_string={"content_type": "MANGA"},
        )
        mixed = self.client.get(
            "/api/v1/filter-ranges",
            query_string={"content_type": "ALL"},
        )

        self.assertEqual(anime.status_code, 200)
        self.assertIsNotNone(anime.get_json()["ranges"]["episodes"])
        self.assertIsNone(anime.get_json()["ranges"]["chapters"])
        self.assertEqual(manga.status_code, 200)
        self.assertIsNotNone(manga.get_json()["ranges"]["chapters"])
        self.assertIsNotNone(manga.get_json()["ranges"]["volumes"])
        self.assertIsNone(manga.get_json()["ranges"]["episodes"])
        self.assertEqual(mixed.status_code, 200)
        self.assertIsNotNone(mixed.get_json()["ranges"]["year"])
        self.assertIsNotNone(mixed.get_json()["ranges"]["score"])

    def test_filter_range_endpoint_reuses_one_cached_scope(self):
        ranges = {
            "year": {"min": 2000, "max": 2026},
            "score": {"min": 1.0, "max": 10.0},
            "episodes": {"min": 1, "max": 1000},
            "chapters": None,
            "volumes": None,
        }
        with patch(
            "backend.app._load_filter_ranges",
            return_value=ranges,
        ) as load_ranges:
            first = self.client.get(
                "/api/v1/filter-ranges",
                query_string={"content_type": "ANIME"},
            )
            second = self.client.get(
                "/api/v1/filter-ranges",
                query_string={"content_type": "ANIME"},
            )

        self.assertEqual(first.get_json()["ranges"], ranges)
        self.assertEqual(second.get_json()["ranges"], ranges)
        load_ranges.assert_called_once()

    def test_manga_serializer_exposes_print_metadata(self):
        manga = Manga(
            mangaID=9,
            mal_id=42,
            content_type="MANHWA",
            title="Example Manhwa",
            alternative_title="Alternate",
            synopsis="A synopsis.",
            manga_type="Manhwa",
            publication_year=2024,
            status="Publishing",
            score=8.1,
            popularity=45,
            members=250000,
            chapters=50,
            volumes=5,
            mal_url="https://myanimelist.net/manga/42",
            image_url="https://example.test/cover.jpg",
            legacy_genres=[],
            genres_detailed=["school"],
        )
        manga.author_links.append(
            MangaAuthor(
                author=Author(
                    mal_id=123,
                    name="Example Author",
                    normalized_name="example author",
                ),
                role="Story & Art",
            )
        )

        item = _serialize_manga(manga, detailed=True)

        self.assertEqual(item["content_type"], "MANHWA")
        self.assertEqual(item["publication_year"], 2024)
        self.assertEqual(item["chapters"], 50)
        self.assertEqual(item["volumes"], 5)
        self.assertEqual(item["popularity"], 45)
        self.assertEqual(item["members"], 250000)
        self.assertEqual(item["synopsis"], "A synopsis.")
        self.assertEqual(item["genres_detailed"], ["school"])
        self.assertEqual(
            item["authors"],
            [
                {
                    "mal_id": 123,
                    "name": "Example Author",
                    "role": "Story & Art",
                }
            ],
        )

    def test_canonical_catalogue_supports_all_content(self):
        response = self.client.get(
            "/api/v1/catalogue",
            query_string={"content_type": "ALL", "per_page": 2},
        )

        body = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertIn("items", body)
        self.assertEqual(body["pagination"]["per_page"], 2)
        self.assertTrue(
            all(
                item["content_type"] in {"ANIME", "MANGA", "MANHWA"}
                for item in body["items"]
            )
        )
        self.assertTrue(
            all(
                "authors" in item
                for item in body["items"]
                if item["content_type"] in {"MANGA", "MANHWA"}
            )
        )

    def test_manga_and_manhwa_filters_and_aliases_are_available(self):
        for content_type, alias in (("MANGA", "manga"), ("MANHWA", "manhwa")):
            with self.subTest(content_type=content_type):
                response = self.client.get(
                    "/api/v1/catalogue",
                    query_string={
                        "content_type": content_type,
                        "status": "PUBLISHING",
                        "min_chapters": 1,
                        "min_volumes": 1,
                        "min_score": 1,
                        "min_year": 1900,
                        "genre": "Action",
                        "tag": "school",
                        "per_page": 2,
                    },
                )
                alias_response = self.client.get(
                    f"/api/v1/{alias}", query_string={"per_page": 1}
                )
                random_response = self.client.get(
                    f"/api/v1/{alias}/random", query_string={"limit": 1}
                )

                self.assertEqual(response.status_code, 200)
                self.assertEqual(alias_response.status_code, 200)
                self.assertEqual(random_response.status_code, 200)
                self.assertTrue(
                    all(
                        item["content_type"] == content_type
                        for item in response.get_json()["items"]
                    )
                )
                self.assertTrue(
                    all(
                        "authors" in item
                        for item in alias_response.get_json()["items"]
                    )
                )
                self.assertTrue(
                    all(
                        "authors" in item
                        for item in random_response.get_json()["items"]
                    )
                )
                alias_items = alias_response.get_json()["items"]
                if alias_items:
                    detail_response = self.client.get(
                        f"/api/v1/{alias}/{alias_items[0]['mal_id']}"
                    )
                    self.assertEqual(detail_response.status_code, 200)
                    self.assertIn("authors", detail_response.get_json()["item"])

    def test_manhwa_filter_query_contains_every_requested_predicate(self):
        with app.test_request_context(
            "/api/v1/catalogue?content_type=MANHWA&status=PUBLISHING"
            "&min_chapters=25&max_chapters=250"
            "&min_volumes=3&max_volumes=30&min_score=7&min_year=2000"
            "&max_year=2030&genre=Action&tag=school"
            "&author=SIU&author=Lee%20Jong-hui"
        ):
            statement = _filtered_manga_statement({"MANHWA"})
        sql = str(
            statement.compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        ).lower()

        self.assertIn("manga.content_type in ('manhwa')", sql)
        self.assertIn("lower(trim(manga.status)) in ('publishing')", sql)
        self.assertIn("manga.chapters >= 25", sql)
        self.assertIn("manga.chapters <= 250", sql)
        self.assertIn("manga.volumes >= 3", sql)
        self.assertIn("manga.volumes <= 30", sql)
        self.assertIn("manga.score >= 7", sql)
        self.assertIn("manga.publication_year >= 2000", sql)
        self.assertIn("manga.publication_year <= 2030", sql)
        self.assertIn("genre.name = 'action'", sql)
        self.assertIn("manga.genres_detailed @> array['school']", sql)
        self.assertIn("author.normalized_name in ('siu', 'lee jong-hui')", sql)

    def test_author_filter_on_all_content_excludes_anime(self):
        with app.test_request_context(
            "/api/v1/catalogue?content_type=ALL&author=SIU"
        ):
            catalogue_rows = _catalogue_rows_subquery(
                {"ANIME", "MANGA", "MANHWA"}
            )
        sql = str(
            catalogue_rows.select().compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        ).lower()

        self.assertIn("manga_author", sql)
        self.assertNotIn("from anime", sql)

    def test_author_facets_are_searchable_and_content_scoped(self):
        with patch(
            "backend.app._load_facet_names",
            return_value=("Hiromu Arakawa", "SIU"),
        ) as load_options:
            response = self.client.get(
                "/api/v1/authors",
                query_string={"content_type": "MANHWA", "q": "siu"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["items"], ["SIU"])
        load_options.assert_called_once_with(frozenset({"MANHWA"}), "author")

    def test_manga_range_filters_reject_reversed_bounds(self):
        chapters = self.client.get(
            "/api/v1/catalogue",
            query_string={
                "content_type": "MANGA",
                "min_chapters": 100,
                "max_chapters": 10,
            },
        )
        volumes = self.client.get(
            "/api/v1/catalogue",
            query_string={
                "content_type": "MANHWA",
                "min_volumes": 20,
                "max_volumes": 2,
            },
        )

        self.assertEqual(chapters.status_code, 400)
        self.assertIn(
            "min_chapters cannot be greater than max_chapters",
            chapters.get_json()["error"]["message"],
        )
        self.assertEqual(volumes.status_code, 400)
        self.assertIn(
            "min_volumes cannot be greater than max_volumes",
            volumes.get_json()["error"]["message"],
        )

    def test_content_scoped_facets_and_random_endpoint_are_available(self):
        genre_response = self.client.get(
            "/api/v1/genres", query_string={"content_type": "MANHWA"}
        )
        tag_response = self.client.get(
            "/api/v1/tags",
            query_string={"content_type": "MANGA", "q": "school"},
        )
        random_response = self.client.get(
            "/api/v1/catalogue/random",
            query_string={"content_type": "ALL", "limit": 2},
        )

        self.assertEqual(genre_response.status_code, 200)
        self.assertEqual(tag_response.status_code, 200)
        self.assertEqual(random_response.status_code, 200)
        self.assertLessEqual(len(random_response.get_json()["items"]), 2)

    def test_anime_random_endpoint_applies_relationship_filters(self):
        response = self.client.get(
            "/api/v1/anime/random",
            query_string={
                "studio": "Studio That Cannot Exist 4d27dc9f",
                "limit": 2,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["items"], [])

    def test_anime_routes_expose_studio_and_streaming_fields(self):
        routes = (
            "/api/v1/anime?per_page=1",
            "/api/v1/anime/random?limit=1",
            "/api/v1/catalogue?content_type=ALL&max_episodes=10000&per_page=1",
            "/api/v1/catalogue/random?content_type=ALL"
            "&max_episodes=10000&limit=1",
            "/api/v1/anime/52991",
        )
        for route in routes:
            with self.subTest(route=route):
                response = self.client.get(route)
                body = response.get_json()
                self.assertEqual(response.status_code, 200)
                entries = body.get("items") or [body.get("item")]
                self.assertTrue(entries)
                self.assertIsNotNone(entries[0])
                self.assertEqual(entries[0]["content_type"], "ANIME")
                self.assertIn("studios", entries[0])
                self.assertIn("streaming_services", entries[0])

        with patch(
            "backend.app._current_season_identity",
            return_value=(2026, "summer"),
        ):
            seasonal = self.client.get("/api/v1/anime/seasonal?limit=1")
        seasonal_body = seasonal.get_json()
        self.assertEqual(seasonal.status_code, 200)
        self.assertTrue(seasonal_body["items"])
        self.assertTrue(
            all(
                "popularity" in item and "members" in item
                for item in seasonal_body["items"]
            )
        )
        self.assertIn("studios", seasonal_body["items"][0])
        self.assertIn("streaming_services", seasonal_body["items"][0])

    def test_invalid_catalogue_content_type_returns_json_error(self):
        response = self.client.get(
            "/api/v1/catalogue", query_string={"content_type": "NOVEL"}
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("content_type must be", response.get_json()["error"]["message"])

    def test_unknown_manga_and_manhwa_details_return_json_404(self):
        for content_type in ("MANGA", "MANHWA"):
            with self.subTest(content_type=content_type):
                response = self.client.get(
                    f"/api/v1/catalogue/{content_type}/999999999"
                )
                self.assertEqual(response.status_code, 404)

    def test_detailed_tag_catalogue_supports_search(self):
        response = self.client.get("/api/v1/genres")

        body = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertIn("items", body)
        tag_response = self.client.get("/api/v1/tags?limit=1")
        self.assertEqual(tag_response.status_code, 200)
        tags = tag_response.get_json()["items"]
        self.assertIsInstance(tags, list)
        if tags:
            search_response = self.client.get(
                "/api/v1/tags", query_string={"q": tags[0][:3], "limit": 50}
            )
            self.assertEqual(search_response.status_code, 200)
            self.assertIn(tags[0], search_response.get_json()["items"])

    def test_anime_list_accepts_a_detailed_tag_filter(self):
        catalogue = self.client.get("/api/v1/tags?limit=1").get_json()
        if not catalogue["items"]:
            self.skipTest("The test catalogue does not contain detailed tags")
        tag = catalogue["items"][0]

        response = self.client.get("/api/v1/anime", query_string={"tag": tag})
        body = response.get_json()

        self.assertEqual(response.status_code, 200)
        mal_ids = [item["mal_id"] for item in body["items"]]
        with app.app_context():
            matching_anime = db.session.scalars(
                select(Anime).where(Anime.mal_id.in_(mal_ids))
            ).all()
        self.assertTrue(all(tag in anime.genres_detailed for anime in matching_anime))

    def test_popular_current_season_returns_a_limited_seasonal_list(self):
        with patch("backend.app._current_season_identity", return_value=(2026, "summer")):
            response = self.client.get("/api/v1/anime/seasonal?limit=6")

        body = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["sort"], "most_popular")
        self.assertEqual(body["season"], "summer")
        self.assertEqual(body["year"], 2026)
        self.assertLessEqual(len(body["items"]), 6)
        self.assertEqual(body["pagination"]["page"], 1)
        self.assertEqual(body["pagination"]["per_page"], 6)
        self.assertGreaterEqual(body["pagination"]["total"], len(body["items"]))
        self.assertTrue(
            all(item["season"] == "summer" and item["year"] == 2026 for item in body["items"])
        )

    def test_upcoming_season_uses_the_next_window_and_popularity_order(self):
        with patch("backend.app._next_season_identity", return_value=(2026, "summer")):
            response = self.client.get(
                "/api/v1/anime/seasonal",
                query_string={"period": "next", "sort": "most_popular", "limit": 6},
            )

        body = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["period"], "next")
        self.assertEqual(body["sort"], "most_popular")
        self.assertEqual((body["year"], body["season"]), (2026, "summer"))
        self.assertTrue(
            all(item["season"] == "summer" and item["year"] == 2026 for item in body["items"])
        )

    def test_next_season_rolls_over_after_fall(self):
        with patch("backend.app._current_season_identity", return_value=(2026, "fall")):
            self.assertEqual(_next_season_identity(), (2027, "winter"))

    def test_popular_current_season_supports_pagination(self):
        with patch("backend.app._current_season_identity", return_value=(2026, "summer")):
            response = self.client.get("/api/v1/anime/seasonal?limit=6&page=2")

        body = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["pagination"]["page"], 2)
        self.assertEqual(body["pagination"]["per_page"], 6)

    def test_invalid_filter_returns_json_error(self):
        response = self.client.get("/api/v1/anime?min_score=20")

        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.get_json())

    def test_database_failure_returns_safe_retryable_json(self):
        with patch.object(
            db.session,
            "scalar",
            side_effect=SQLAlchemyError("provider storage quota exceeded"),
        ):
            response = self.client.get("/api/v1/catalogue")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.get_json()["error"]["code"], "database_unavailable"
        )
        self.assertNotIn("quota", response.get_json()["error"]["message"])
        self.assertEqual(response.headers["Retry-After"], "300")

    def test_schema_failure_allows_frontend_only_degraded_startup(self):
        with (
            app.app_context(),
            patch(
                "backend.app.ensure_anime_schema",
                side_effect=SQLAlchemyError("database unavailable"),
            ),
            patch.object(db.session, "rollback") as rollback,
        ):
            initialized = initialize_database_schema()

        self.assertFalse(initialized)
        rollback.assert_called_once_with()

    def test_invalid_season_returns_json_error(self):
        response = self.client.get("/api/v1/anime?season=monsoon")

        self.assertEqual(response.status_code, 400)
        self.assertIn("season must be", response.get_json()["error"]["message"])

    def test_unknown_anime_returns_json_404(self):
        response = self.client.get("/api/v1/anime/999999999")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()["error"]["message"], "Anime not found")


class PrivacyAnalyticsTests(unittest.TestCase):
    def setUp(self):
        analytics_response_cache.clear()
        self.client = app.test_client()

    def test_first_and_repeat_html_visits_use_the_same_anonymous_cookie(self):
        with (
            patch.dict(os.environ, {"ANALYTICS_COOKIE_SECURE": "false"}),
            patch("backend.app._record_site_visit") as record_visit,
        ):
            first = self.client.get("/", headers={"User-Agent": "Mozilla/5.0"})
            second = self.client.get("/", headers={"User-Agent": "Mozilla/5.0"})

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(record_visit.call_count, 2)
        self.assertEqual(
            record_visit.call_args_list[0].args[0],
            record_visit.call_args_list[1].args[0],
        )
        self.assertIn("HttpOnly", first.headers["Set-Cookie"])
        self.assertIn("SameSite=Lax", first.headers["Set-Cookie"])
        first.close()
        second.close()

    def test_api_bot_probe_and_health_requests_are_not_tracked(self):
        with patch("backend.app._record_site_visit") as record_visit:
            bot_response = self.client.get(
                "/", headers={"User-Agent": "Googlebot/2.1"}
            )
            render_probe_response = self.client.get(
                "/", headers={"User-Agent": "Go-http-client/2.0"}
            )
            non_browser_response = self.client.get("/")
            health_response = self.client.get(
                "/health", headers={"User-Agent": "Mozilla/5.0"}
            )
            api_response = self.client.get("/api/v1/not-a-route")

        record_visit.assert_not_called()
        bot_response.close()
        render_probe_response.close()
        non_browser_response.close()
        health_response.close()
        api_response.close()

    def test_daily_aggregate_uses_an_atomic_unique_keyed_upsert(self):
        with (
            patch.object(db.session, "execute") as execute,
            patch.object(db.session, "commit") as commit,
        ):
            _record_site_visit("x" * 43)

        statement = execute.call_args.args[0]
        rendered_sql = str(statement.compile(dialect=postgresql.dialect())).lower()
        self.assertIn(
            "on conflict on constraint uq_site_visit_visitor_day_route",
            rendered_sql,
        )
        self.assertIn("visit_count = (site_visit.visit_count +", rendered_sql)
        commit.assert_called_once_with()

    def test_analytics_failure_never_breaks_a_frontend_page_load(self):
        with (
            patch.dict(os.environ, {"ANALYTICS_COOKIE_SECURE": "false"}),
            patch(
                "backend.app._record_site_visit",
                side_effect=SQLAlchemyError("database unavailable"),
            ),
            patch.object(db.session, "rollback") as rollback,
        ):
            response = self.client.get("/", headers={"User-Agent": "Mozilla/5.0"})

        self.assertEqual(response.status_code, 200)
        rollback.assert_called_once_with()
        response.close()

    def test_admin_analytics_requires_a_configured_bearer_token(self):
        with patch.dict(os.environ, {}, clear=True):
            missing_configuration = self.client.get("/api/v1/admin/analytics/visits")
        self.assertEqual(missing_configuration.status_code, 403)

        with patch.dict(os.environ, {"ADMIN_ANALYTICS_TOKEN": "expected-token"}):
            missing_token = self.client.get("/api/v1/admin/analytics/visits")
            invalid_token = self.client.get(
                "/api/v1/admin/analytics/visits",
                headers={"Authorization": "Bearer wrong-token"},
            )
        self.assertEqual(missing_token.status_code, 401)
        self.assertEqual(invalid_token.status_code, 403)

    def test_authorized_admin_analytics_returns_aggregate_data_only(self):
        report = {
            "total": {"visits": 12, "unique_visitors": 4},
            "daily": [],
            "weekly": [],
            "monthly": [],
            "categories": [
                {"category": "frontend", "visits": 12, "unique_visitors": 4}
            ],
        }
        with (
            patch.dict(os.environ, {"ADMIN_ANALYTICS_TOKEN": "expected-token"}),
            patch("backend.app._analytics_report", return_value=report),
        ):
            response = self.client.get(
                "/api/v1/admin/analytics/visits",
                headers={"Authorization": "Bearer expected-token"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["total"]["unique_visitors"], 4)
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_analytics_report_contains_only_aggregate_periods(self):
        total_result = Mock()
        total_result.one.return_value = (12, 4)
        category_result = Mock()
        category_result.all.return_value = [("frontend", 12, 4)]
        with (
            patch.object(
                db.session,
                "execute",
                side_effect=(total_result, category_result),
            ),
            patch("backend.app._analytics_period_rows", return_value=[]),
        ):
            report = _analytics_report()

        self.assertEqual(report["total"], {"visits": 12, "unique_visitors": 4})
        self.assertEqual(report["categories"][0]["category"], "frontend")
        self.assertNotIn("ip", json.dumps(report).casefold())
        self.assertNotIn("user_agent", json.dumps(report).casefold())


if __name__ == "__main__":
    unittest.main()
