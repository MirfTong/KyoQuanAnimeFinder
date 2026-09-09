import unittest
from contextlib import ExitStack
from datetime import datetime, timezone
from email.message import Message
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError

from backend.jobs.jikan_etl import (
    AnimeAssociationCaches,
    AnimeAssociationStats,
    BULK_SEASON_STATE_KEY,
    BulkSeasonSyncResult,
    CatalogueRefreshResult,
    CurrentSeasonSyncResult,
    SUPPLEMENTAL_PROVIDER_TYPES,
    SUPPLEMENTAL_STATE_KEYS,
    SeasonPageApplyResult,
    SeasonBackfillResult,
    _apply_season_page,
    _anime_status,
    _anime_type,
    _detailed_genres,
    _current_season_identity,
    _fetch_anime_data,
    _is_hentai,
    _names,
    _new_anime,
    _normalized_entity_name,
    _prepared_season_entry,
    _refresh_and_report_catalogue_facets,
    _report_anime_associations,
    _safe_streaming_url,
    _season,
    _season_from_air_date,
    _studio_for_value,
    _update_anime,
    _valid_catalogue_metric,
    _valid_score,
    backfill_missing_seasons,
    backfill_streaming_services,
    main,
    refresh_catalogue,
    remove_hentai_anime,
    run_scheduled_sync,
    sync_bulk_anime_seasons,
    sync_current_season,
    sync_upcoming_season,
    sync_season,
    sync_supplemental_anime_types,
)
from backend.models import Anime
from backend.jobs.manga_etl import (
    MangaCatalogueSyncResult,
    MangaRefreshResult,
)
from backend.services.jikan_client import (
    JikanAnimePage,
    JikanSeasonPage,
    JikanTemporaryError,
)
from backend.models import (
    AnimeStreamingService,
    AnimeStudio,
    StreamingService,
    Studio,
)


def anime_record(
    *,
    anime_type="TV",
    season="summer",
    status="FINISHED_AIRING",
):
    return Anime(
        animeID=1,
        mal_id=1,
        title="Example",
        alternative_title=None,
        synopsis=None,
        type=anime_type,
        season=season,
        status=status,
        year=2020,
        score=8.0,
        episodes=12,
        mal_url="https://example.com",
        sequel=False,
        image_url="",
        legacy_genres=[],
        genres_detailed=[],
    )


class JikanEtlTests(unittest.TestCase):
    def test_extracts_unique_genre_names(self):
        self.assertEqual(
            _names([{"name": "Action"}, {"name": "Action"}, {"name": " Drama "}]),
            ["Action", "Drama"],
        )

    def test_ignores_malformed_genre_entries(self):
        self.assertEqual(
            _names([None, "Action", {"name": None}, {"name": " Drama "}]),
            ["Drama"],
        )

    def test_normalizes_relationship_names_and_rejects_unsafe_streaming_urls(self):
        self.assertEqual(
            _normalized_entity_name("  Studio\u3000PIERROT  "),
            "studio pierrot",
        )
        self.assertIsNone(_normalized_entity_name("x" * 151))
        self.assertEqual(
            _safe_streaming_url(" https://example.test/watch/1 "),
            "https://example.test/watch/1",
        )
        self.assertIsNone(_safe_streaming_url("javascript:alert(1)"))
        self.assertIsNone(_safe_streaming_url("https:///missing-host"))

    def test_studio_identity_collision_merges_links_without_unique_conflicts(self):
        provider_studio = Studio(
            mal_id=20,
            name="Provider Studio",
            normalized_name="provider studio",
        )
        name_studio = Studio(
            mal_id=10,
            name="Renamed Studio",
            normalized_name="renamed studio",
        )
        first_anime = anime_record()
        second_anime = anime_record()
        second_anime.animeID = 2
        second_anime.mal_id = 2
        AnimeStudio(anime=first_anime, studio=provider_studio)
        AnimeStudio(anime=second_anime, studio=name_studio)
        caches = AnimeAssociationCaches(
            studios_by_name={
                provider_studio.normalized_name: provider_studio,
                name_studio.normalized_name: name_studio,
            },
            studios_by_mal_id={
                provider_studio.mal_id: provider_studio,
                name_studio.mal_id: name_studio,
            },
            streaming_services_by_name={},
        )

        with patch("backend.jobs.jikan_etl.db"):
            studio, changed = _studio_for_value(
                20,
                "Renamed Studio",
                "renamed studio",
                caches,
            )

        self.assertIs(studio, provider_studio)
        self.assertTrue(changed)
        self.assertEqual(studio.name, "Renamed Studio")
        self.assertEqual(studio.normalized_name, "renamed studio")
        self.assertEqual(
            {link.anime for link in studio.anime_links},
            {first_anime, second_anime},
        )
        self.assertNotIn(name_studio, caches.studios_by_name.values())
        self.assertIs(caches.studios_by_mal_id[10], provider_studio)
        self.assertIs(caches.studios_by_mal_id[20], provider_studio)

    def test_sparse_or_malformed_relationship_fields_preserve_existing_links(self):
        anime = anime_record()
        studio = Studio(
            mal_id=1,
            name="Existing Studio",
            normalized_name="existing studio",
        )
        service = StreamingService(
            name="Existing Service",
            normalized_name="existing service",
        )
        studio_link = AnimeStudio(studio=studio)
        streaming_link = AnimeStreamingService(
            streaming_service=service,
            url="https://example.test/old",
        )
        anime.studio_links.append(studio_link)
        anime.streaming_links.append(streaming_link)
        caches = AnimeAssociationCaches(
            studios_by_name={studio.normalized_name: studio},
            studios_by_mal_id={studio.mal_id: studio},
            streaming_services_by_name={service.normalized_name: service},
        )
        stats = AnimeAssociationStats()

        with patch("backend.jobs.jikan_etl.db") as mock_db:
            _update_anime(
                anime,
                {
                    "genres": [],
                    "studios": {"unexpected": "object"},
                    "streaming": "unexpected string",
                },
                {},
                caches,
                stats,
            )
            _update_anime(anime, {"genres": []}, {}, caches, stats)

        self.assertEqual(anime.studio_links, [studio_link])
        self.assertEqual(anime.streaming_links, [streaming_link])
        self.assertEqual(stats.malformed_studio_entries, 1)
        self.assertEqual(stats.malformed_streaming_entries, 1)
        mock_db.session.delete.assert_not_called()

    def test_explicit_empty_relationship_arrays_remove_stale_links(self):
        anime = anime_record()
        studio = Studio(
            mal_id=1,
            name="Existing Studio",
            normalized_name="existing studio",
        )
        service = StreamingService(
            name="Existing Service",
            normalized_name="existing service",
        )
        studio_link = AnimeStudio(studio=studio)
        streaming_link = AnimeStreamingService(
            streaming_service=service,
            url="https://example.test/old",
        )
        anime.studio_links.append(studio_link)
        anime.streaming_links.append(streaming_link)
        caches = AnimeAssociationCaches(
            studios_by_name={studio.normalized_name: studio},
            studios_by_mal_id={studio.mal_id: studio},
            streaming_services_by_name={service.normalized_name: service},
        )
        stats = AnimeAssociationStats()

        with patch("backend.jobs.jikan_etl.db") as mock_db:
            _update_anime(
                anime,
                {"genres": [], "studios": [], "streaming": []},
                {},
                caches,
                stats,
            )

        mock_db.session.delete.assert_called_once_with(studio_link)
        self.assertEqual(anime.streaming_links, [])
        self.assertEqual(stats.studio_links_removed, 1)
        self.assertEqual(stats.streaming_links_removed, 1)
        self.assertEqual(stats.anime_with_studios_updated, 1)
        self.assertEqual(stats.anime_with_streaming_updated, 1)

    def test_partially_malformed_relationship_arrays_are_additive_only(self):
        anime = anime_record()
        old_studio = Studio(
            mal_id=1,
            name="Old Studio",
            normalized_name="old studio",
        )
        old_service = StreamingService(
            name="Old Service",
            normalized_name="old service",
        )
        anime.studio_links.append(AnimeStudio(studio=old_studio))
        anime.streaming_links.append(
            AnimeStreamingService(
                streaming_service=old_service,
                url="https://example.test/old",
            )
        )
        caches = AnimeAssociationCaches(
            studios_by_name={old_studio.normalized_name: old_studio},
            studios_by_mal_id={old_studio.mal_id: old_studio},
            streaming_services_by_name={old_service.normalized_name: old_service},
        )
        stats = AnimeAssociationStats()

        with patch("backend.jobs.jikan_etl.db") as mock_db:
            _update_anime(
                anime,
                {
                    "genres": [],
                    "studios": [
                        {"mal_id": 2, "name": " New   Studio "},
                        None,
                    ],
                    "streaming": [
                        {
                            "name": "New Service",
                            "url": "https://example.test/new",
                        },
                        {
                            "name": "Unsafe Service",
                            "url": "javascript:alert(1)",
                        },
                    ],
                },
                {},
                caches,
                stats,
            )

        self.assertEqual(stats.studio_links_created, 1)
        self.assertEqual(stats.streaming_links_created, 1)
        self.assertEqual(stats.studio_links_removed, 0)
        self.assertEqual(stats.streaming_links_removed, 0)
        self.assertEqual(stats.malformed_studio_entries, 1)
        self.assertEqual(stats.malformed_streaming_entries, 1)
        self.assertEqual(len(anime.studio_links), 2)
        self.assertEqual(len(anime.streaming_links), 2)
        mock_db.session.delete.assert_not_called()

    def test_streaming_url_changes_are_reconciled_and_counted(self):
        anime = anime_record()
        service = StreamingService(
            name="Crunchyroll",
            normalized_name="crunchyroll",
        )
        link = AnimeStreamingService(
            streaming_service=service,
            url="https://example.test/old",
        )
        anime.streaming_links.append(link)
        caches = AnimeAssociationCaches(
            studios_by_name={},
            studios_by_mal_id={},
            streaming_services_by_name={service.normalized_name: service},
        )
        stats = AnimeAssociationStats()

        _update_anime(
            anime,
            {
                "genres": [],
                "streaming": [
                    {
                        "name": " Crunchyroll ",
                        "url": "https://example.test/new",
                    }
                ],
            },
            {},
            caches,
            stats,
        )

        self.assertEqual(link.url, "https://example.test/new")
        self.assertEqual(stats.streaming_urls_updated, 1)
        self.assertEqual(stats.anime_with_streaming_updated, 1)

    def test_named_season_listing_reconciles_studio_relationships(self):
        anime = anime_record(season=None)
        seasonal_data = {
            "mal_id": 1,
            "title": "Studio listing example",
            "type": "TV",
            "genres": [],
            "studios": [
                {
                    "mal_id": 11,
                    "name": "  Example   Studio  ",
                }
            ],
        }
        with (
            patch("backend.jobs.jikan_etl._ensure_schema"),
            patch("backend.jobs.jikan_etl.db") as mock_db,
        ):
            # Existing anime, genres, studios, and streaming services.
            mock_db.session.scalars.side_effect = [[anime], [], [], []]
            saved, skipped = sync_season(
                2026,
                "summer",
                fetch_season=lambda _year, _season: [seasonal_data],
            )

        self.assertEqual((saved, skipped), (1, 0))
        self.assertEqual(len(anime.studio_links), 1)
        self.assertEqual(anime.studio_links[0].studio.mal_id, 11)
        self.assertEqual(anime.studio_links[0].studio.name, "Example Studio")
        self.assertEqual(
            anime.studio_links[0].studio.normalized_name,
            "example studio",
        )
        mock_db.session.commit.assert_called()

    def test_full_detail_refresh_reconciles_streaming_relationships(self):
        anime = anime_record()
        payload = {
            "data": {
                "mal_id": anime.mal_id,
                "title": anime.title,
                "genres": [],
                "streaming": [
                    {
                        "name": "Crunchyroll",
                        "url": "https://www.crunchyroll.com/watch/example",
                    }
                ],
            }
        }
        with (
            patch("backend.jobs.jikan_etl._ensure_schema"),
            patch("backend.jobs.jikan_etl.db") as mock_db,
        ):
            # Selected anime, genres, studios, and streaming services.
            mock_db.session.scalars.side_effect = [[anime], [], [], []]
            result = refresh_catalogue(
                anime_ids=[anime.mal_id],
                fetch_anime=lambda _mal_id: payload,
            )

        self.assertEqual(result.updated, 1)
        self.assertEqual(result.associations.streaming_payloads_processed, 1)
        self.assertEqual(result.associations.streaming_links_created, 1)
        self.assertEqual(
            result.associations.anime_with_streaming_updated,
            1,
        )
        self.assertEqual(len(anime.streaming_links), 1)
        self.assertEqual(
            anime.streaming_links[0].streaming_service.name,
            "Crunchyroll",
        )
        self.assertEqual(
            anime.streaming_links[0].url,
            "https://www.crunchyroll.com/watch/example",
        )

    def test_streaming_backfill_retries_unlinked_anime_and_records_attempts(self):
        anime = anime_record()
        payload = {
            "data": {
                "mal_id": anime.mal_id,
                "title": anime.title,
                "genres": [],
                "streaming": [
                    {
                        "name": "Crunchyroll",
                        "url": "https://www.crunchyroll.com/watch/example",
                    }
                ],
            }
        }
        with (
            patch("backend.jobs.jikan_etl._ensure_schema"),
            patch("backend.jobs.jikan_etl.db") as mock_db,
        ):
            # Selected Anime and normalized streaming services.
            mock_db.session.scalars.side_effect = [[anime], []]
            result = backfill_streaming_services(
                limit=1,
                fetch_anime=lambda _mal_id: payload,
            )

        self.assertEqual(result.selected, 1)
        self.assertEqual(result.updated, 1)
        self.assertEqual(result.associations.streaming_links_created, 1)
        self.assertIsNotNone(anime.last_streaming_attempt)
        self.assertEqual(
            anime.streaming_links[0].streaming_service.name,
            "Crunchyroll",
        )

    def test_streaming_backfill_does_not_rewrite_unrelated_metadata(self):
        anime = anime_record()
        payload = {
            "data": {
                "mal_id": anime.mal_id,
                "title": "Provider changed title",
                "score": 9.9,
                "genres": [{"name": "Drama"}],
                "studios": [{"mal_id": 1, "name": "Provider Studio"}],
                "streaming": [
                    {
                        "name": "Crunchyroll",
                        "url": "https://www.crunchyroll.com/watch/example",
                    }
                ],
            }
        }
        with (
            patch("backend.jobs.jikan_etl._ensure_schema"),
            patch("backend.jobs.jikan_etl.db") as mock_db,
        ):
            mock_db.session.scalars.side_effect = [[anime], []]
            result = backfill_streaming_services(
                limit=1,
                fetch_anime=lambda _mal_id: payload,
            )

        self.assertEqual(result.updated, 1)
        self.assertEqual(anime.title, "Example")
        self.assertEqual(anime.score, 8.0)
        self.assertIsNone(anime.last_jikan_sync)
        self.assertEqual(result.associations.studio_payloads_processed, 0)
        self.assertEqual(result.associations.streaming_links_created, 1)

    def test_streaming_backfill_counts_empty_or_sparse_provider_responses(self):
        empty_anime = anime_record()
        sparse_anime = anime_record()
        sparse_anime.animeID = 2
        sparse_anime.mal_id = 2
        responses = iter(
            [
                {"data": {"mal_id": 1, "genres": [], "streaming": []}},
                {"data": {"mal_id": 2, "genres": []}},
            ]
        )
        with (
            patch("backend.jobs.jikan_etl._ensure_schema"),
            patch("backend.jobs.jikan_etl.db") as mock_db,
        ):
            mock_db.session.scalars.side_effect = [
                [empty_anime, sparse_anime],
                [],
            ]
            result = backfill_streaming_services(
                limit=2,
                fetch_anime=lambda _mal_id: next(responses),
            )

        self.assertEqual(result.updated, 0)
        self.assertEqual(result.success_rate, 0.0)
        self.assertEqual(result.empty_streaming_payloads, 1)
        self.assertEqual(result.missing_streaming_payloads, 1)
        self.assertIsNotNone(empty_anime.last_streaming_attempt)
        self.assertIsNotNone(sparse_anime.last_streaming_attempt)

        selection = mock_db.session.scalars.call_args_list[0].args[0]
        selection_sql = str(selection)
        self.assertIn("anime.mal_id IS NOT NULL", selection_sql)
        self.assertIn("anime.mal_id >", selection_sql)

    def test_streaming_backfill_does_not_count_malformed_payload_as_updated(self):
        anime = anime_record()
        with (
            patch("backend.jobs.jikan_etl._ensure_schema"),
            patch("backend.jobs.jikan_etl.db") as mock_db,
        ):
            mock_db.session.scalars.side_effect = [[anime], []]
            result = backfill_streaming_services(
                limit=1,
                fetch_anime=lambda _mal_id: {
                    "data": {"mal_id": 1, "streaming": {"bad": "shape"}}
                },
            )

        self.assertEqual(result.updated, 0)
        self.assertEqual(result.success_rate, 0.0)
        self.assertEqual(result.associations.streaming_payloads_processed, 1)
        self.assertEqual(result.associations.reconciliation_failures, 1)

    def test_preserves_existing_detailed_tags_and_adds_jikan_categories(self):
        data = {
            "genres": [{"name": "Action"}],
            "themes": [{"name": "School"}],
            "demographics": [{"name": "Shounen"}],
        }
        self.assertEqual(
            _detailed_genres(data, ["existing tag", "action"]),
            ["existing tag", "action", "school", "shounen"],
        )

    def test_treats_jikan_zero_as_an_unknown_score(self):
        self.assertEqual(_valid_score(8.25), 8.25)
        self.assertIsNone(_valid_score(0))
        self.assertIsNone(_valid_score(None))

    def test_normalizes_jikan_seasons(self):
        self.assertEqual(_season(" Winter "), "winter")
        self.assertEqual(_season("summer"), "summer")
        self.assertIsNone(_season("monsoon"))
        self.assertIsNone(_season(None))

    def test_normalizes_provider_and_legacy_anime_types(self):
        self.assertEqual(_anime_type("Movie"), "MOVIE")
        self.assertEqual(_anime_type("tv_special"), "SPECIAL")
        self.assertEqual(_anime_type(" TV Special "), "SPECIAL")

    def test_normalizes_supported_jikan_airing_statuses(self):
        self.assertEqual(_anime_status("Currently Airing"), "CURRENTLY_AIRING")
        self.assertEqual(_anime_status("Finished Airing"), "FINISHED_AIRING")
        self.assertEqual(_anime_status("Not yet aired"), "NOT_YET_AIRED")
        self.assertIsNone(_anime_status("Cancelled"))
        self.assertIsNone(_anime_status(None))

    def test_detects_hentai_from_categories_or_rating(self):
        self.assertTrue(
            _is_hentai({"explicit_genres": [{"name": " HENTAI "}]})
        )
        self.assertTrue(_is_hentai({"rating": "Rx - Hentai"}))
        self.assertTrue(_is_hentai({"genres": [{"name": " Erotica "}]}))
        self.assertFalse(
            _is_hentai(
                {
                    "genres": [{"name": "Ecchi"}],
                    "rating": "R+ - Mild Nudity",
                }
            )
        )

    def test_anime_mapping_maintains_the_indexed_adult_flag(self):
        anime = anime_record()

        _update_anime(anime, {"rating": "Rx - Hentai", "genres": []}, {})
        self.assertTrue(anime.is_adult)

        _update_anime(anime, {"rating": "PG-13", "genres": []}, {})
        self.assertFalse(anime.is_adult)

    def test_updates_anime_season_from_jikan(self):
        anime = anime_record()
        _update_anime(anime, {"season": "Fall", "genres": []}, {})
        self.assertEqual(anime.season, "fall")

    def test_updates_anime_status_without_erasing_valid_sparse_data(self):
        anime = anime_record(status="FINISHED_AIRING")

        _update_anime(
            anime,
            {"status": "Currently Airing", "genres": []},
            {},
        )
        self.assertEqual(anime.status, "CURRENTLY_AIRING")

        _update_anime(anime, {"genres": []}, {})
        self.assertEqual(anime.status, "CURRENTLY_AIRING")

        _update_anime(anime, {"status": {"bad": "value"}, "genres": []}, {})
        self.assertEqual(anime.status, "CURRENTLY_AIRING")

    def test_new_anime_stores_normalized_status_or_null(self):
        airing = _new_anime(
            {
                "mal_id": 10,
                "title": "Airing",
                "type": "TV",
                "status": "Currently Airing",
            }
        )
        unknown = _new_anime(
            {
                "mal_id": 11,
                "title": "Unknown",
                "type": "TV",
                "status": "Cancelled",
            }
        )

        self.assertEqual(airing.status, "CURRENTLY_AIRING")
        self.assertIsNone(unknown.status)

    def test_updates_anime_synopsis_from_jikan_detail_payload(self):
        anime = anime_record()
        _update_anime(anime, {"synopsis": "  A new synopsis.  ", "genres": []}, {})

        self.assertEqual(anime.synopsis, "A new synopsis.")

    def test_infers_missing_tv_season_from_premiere_date(self):
        anime = anime_record(season=None)
        _update_anime(
            anime,
            {
                "type": "TV",
                "season": None,
                "aired": {"from": "2020-10-03T00:00:00+00:00"},
                "genres": [],
            },
            {},
        )
        self.assertEqual(anime.season, "fall")
        self.assertEqual(
            _season_from_air_date({"aired": {"prop": {"from": {"month": 4}}}}),
            "spring",
        )

    def test_explicit_unrated_payload_clears_stale_score_and_episode_count(self):
        anime = anime_record()
        _update_anime(anime, {"score": 0, "episodes": None, "genres": []}, {})
        self.assertIsNone(anime.score)
        self.assertIsNone(anime.episodes)

    def test_popularity_and_member_updates_preserve_existing_values_on_bad_data(self):
        anime = anime_record()
        anime.popularity = 900
        anime.members = 1200

        _update_anime(
            anime,
            {"popularity": 42, "members": 250000, "genres": []},
            {},
        )
        self.assertEqual(anime.popularity, 42)
        self.assertEqual(anime.members, 250000)

        _update_anime(
            anime,
            {"popularity": "bad", "members": None, "genres": []},
            {},
        )
        self.assertEqual(anime.popularity, 42)
        self.assertEqual(anime.members, 250000)
        self.assertEqual(_valid_catalogue_metric(0), 0)
        self.assertIsNone(_valid_catalogue_metric(-1))
        self.assertIsNone(_valid_catalogue_metric(True))

    def test_update_tolerates_malformed_nested_provider_fields(self):
        anime = anime_record()
        _update_anime(
            anime,
            {
                "images": [],
                "relations": [None, "invalid", {"relation": "Sequel"}],
                "genres": [None, {"name": None}],
                "themes": "invalid",
            },
            {},
        )
        self.assertTrue(anime.sequel)
        self.assertEqual(anime.image_url, "")

    def test_sparse_tv_payload_does_not_erase_existing_season(self):
        anime = anime_record(season="spring")
        _update_anime(anime, {"type": "TV", "season": None, "genres": []}, {})
        self.assertEqual(anime.season, "spring")

    def test_non_tv_payload_clears_stale_season(self):
        anime = anime_record(anime_type="Movie", season="spring")
        _update_anime(anime, {"type": "Movie", "season": None, "genres": []}, {})
        self.assertIsNone(anime.season)

    def test_named_listing_forces_season_only_for_tv(self):
        tv = _prepared_season_entry({"mal_id": 1, "type": "TV"}, 2025, "winter")
        movie = _prepared_season_entry({"mal_id": 2, "type": "Movie"}, 2025, "winter")
        self.assertEqual(tv["season"], "winter")
        self.assertNotIn("season", movie)

    def test_current_season_uses_japan_calendar_at_year_boundary(self):
        self.assertEqual(
            _current_season_identity(datetime(2026, 12, 31, 18, tzinfo=timezone.utc)),
            (2027, "winter"),
        )

    def test_classifies_jikan_fetch_failures(self):
        data = {"mal_id": 1, "title": "Cowboy Bebop"}
        self.assertEqual(
            _fetch_anime_data(1, lambda _mal_id: {"data": data}).data,
            data,
        )

        def temporary_failure(_mal_id: int):
            raise JikanTemporaryError("Jikan is unavailable")

        self.assertEqual(_fetch_anime_data(1, temporary_failure).failure, "temporary")
        self.assertEqual(
            _fetch_anime_data(1, lambda _mal_id: {"data": []}).failure,
            "invalid_payload",
        )

    def test_skipped_records_are_marked_attempted_to_advance_queue(self):
        records = [
            SimpleNamespace(animeID=1, mal_id=1),
            SimpleNamespace(animeID=2, mal_id=2),
        ]
        with (
            patch("backend.jobs.jikan_etl._ensure_schema"),
            patch("backend.jobs.jikan_etl.db") as mock_db,
        ):
            mock_db.session.scalars.side_effect = [records, []]
            result = refresh_catalogue(
                limit=2,
                batch_size=1,
                fetch_anime=lambda _mal_id: {"data": []},
            )

        self.assertEqual(result.selected, 2)
        self.assertEqual(result.invalid_payloads, 2)
        self.assertEqual(mock_db.session.execute.call_count, 2)
        self.assertGreaterEqual(mock_db.session.commit.call_count, 3)

    def test_catalogue_refresh_updates_airing_status(self):
        anime = anime_record(status=None)
        with (
            patch("backend.jobs.jikan_etl._ensure_schema"),
            patch("backend.jobs.jikan_etl.db") as mock_db,
        ):
            mock_db.session.scalars.side_effect = [[anime], []]
            result = refresh_catalogue(
                anime_ids=[1],
                fetch_anime=lambda _mal_id: {
                    "data": {
                        "mal_id": 1,
                        "status": "Finished Airing",
                        "genres": [],
                    }
                },
            )

        self.assertEqual(result.updated, 1)
        self.assertEqual(anime.status, "FINISHED_AIRING")

    def test_health_metrics_flag_a_low_success_run(self):
        result = CatalogueRefreshResult(selected=1000, updated=43, temporary_errors=957)
        self.assertEqual(result.skipped, 957)
        self.assertAlmostEqual(result.success_rate, 0.043)

    def test_relationship_stats_accumulate_and_render_in_the_action_summary(self):
        combined = AnimeAssociationStats(
            studio_payloads_processed=6,
            anime_with_studios_updated=2,
            studio_links_created=3,
            malformed_studio_entries=1,
        )

        combined.add(
            AnimeAssociationStats(
                streaming_payloads_processed=7,
                anime_with_streaming_updated=4,
                streaming_links_created=5,
                streaming_links_removed=2,
                streaming_urls_updated=1,
                malformed_streaming_entries=2,
                reconciliation_failures=1,
            )
        )

        with TemporaryDirectory() as temporary_directory:
            summary_path = Path(temporary_directory) / "summary.md"
            with patch.dict(
                "os.environ",
                {"GITHUB_STEP_SUMMARY": str(summary_path)},
                clear=False,
            ):
                _report_anime_associations(combined)
            summary = summary_path.read_text(encoding="utf-8")

        self.assertIn("Anime studios and streaming services", summary)
        self.assertIn("**Studio payloads processed:** 6", summary)
        self.assertIn("**Anime with studios updated:** 2", summary)
        self.assertIn("**Studio relationships created:** 3", summary)
        self.assertIn("**Streaming payloads processed:** 7", summary)
        self.assertIn("**Streaming relationships created:** 5", summary)
        self.assertIn("**Streaming relationships removed:** 2", summary)
        self.assertIn("**Provider/reconciliation failures:** 1", summary)
        self.assertIn("**Relationship metadata failures:** 3", summary)

    def test_upcoming_season_uses_an_independent_next_season_cursor(self):
        fetched_requests = []
        season_page = JikanSeasonPage(entries=[], page=1, has_next_page=False)
        with patch("backend.jobs.jikan_etl._ensure_schema"):
            with patch("backend.jobs.jikan_etl._next_page", return_value=1):
                with patch("backend.jobs.jikan_etl._apply_season_page") as apply_page:
                    apply_page.return_value = SeasonPageApplyResult()
                    result = sync_upcoming_season(
                        now=datetime(2026, 12, 31, 14, tzinfo=timezone.utc),
                        fetch_page=lambda year, season, *, page: (
                            fetched_requests.append((year, season, page)) or season_page
                        ),
                    )

        self.assertEqual(fetched_requests, [(2027, "winter", 1)])
        self.assertTrue(result.complete)

    def test_scheduled_sync_uses_bounded_offline_worker(self):
        with patch("backend.jobs.efficient_sync.run") as run:
            result = run_scheduled_sync(
                limit=7, streaming_limit=11, batch_size=2, page_limit=3,
                request_budget=120,
            )
        run.assert_called_once_with(
            limit=7, streaming_limit=11, batch_size=2, page_limit=3,
            request_budget=120,
        )
        self.assertIs(result, run.return_value)

    def test_scheduled_sync_rejects_invalid_limits_before_running(self):
        with (
            patch("backend.jobs.jikan_etl.sync_current_season") as current_sync,
            self.assertRaises(ValueError),
        ):
            run_scheduled_sync(limit=0)
        current_sync.assert_not_called()

        with self.assertRaises(ValueError):
            run_scheduled_sync(streaming_limit=0)

    def test_facet_publication_rebuilds_and_reports_once(self):
        with (
            patch(
                "backend.jobs.jikan_etl.refresh_catalogue_facets",
                return_value=321,
            ) as refresh,
            patch(
                "backend.jobs.jikan_etl._report_catalogue_facets"
            ) as report,
        ):
            total = _refresh_and_report_catalogue_facets()

        self.assertEqual(total, 321)
        refresh.assert_called_once_with()
        report.assert_called_once_with(321)

    def test_every_standalone_cli_path_publishes_facets_once(self):
        command_lines = (
            ["jikan_etl"],
            ["jikan_etl", "--manga-catalogue"],
            ["jikan_etl", "--refresh-manga"],
            ["jikan_etl", "--bulk-seasons"],
            ["jikan_etl", "--backfill-seasons"],
            ["jikan_etl", "--season", "current"],
            ["jikan_etl", "--season", "winter", "--year", "2020"],
        )

        for command_line in command_lines:
            with self.subTest(command_line=command_line), ExitStack() as stack:
                stack.enter_context(patch("sys.argv", command_line))
                stack.enter_context(
                    patch(
                        "backend.jobs.jikan_etl.refresh_catalogue",
                        return_value=CatalogueRefreshResult(),
                    )
                )
                stack.enter_context(
                    patch(
                        "backend.jobs.jikan_etl.sync_manga_catalogue",
                        return_value=MangaCatalogueSyncResult(scans={}),
                    )
                )
                stack.enter_context(
                    patch(
                        "backend.jobs.jikan_etl.refresh_manga_catalogue",
                        return_value=MangaRefreshResult(),
                    )
                )
                stack.enter_context(
                    patch(
                        "backend.jobs.jikan_etl.sync_bulk_anime_seasons",
                        return_value=BulkSeasonSyncResult(),
                    )
                )
                stack.enter_context(
                    patch(
                        "backend.jobs.jikan_etl.backfill_missing_seasons",
                        return_value=SeasonBackfillResult(),
                    )
                )
                stack.enter_context(
                    patch(
                        "backend.jobs.jikan_etl.sync_current_season",
                        return_value=CurrentSeasonSyncResult(),
                    )
                )
                stack.enter_context(
                    patch(
                        "backend.jobs.jikan_etl.sync_season",
                        return_value=(1, 0),
                    )
                )
                for report_name in (
                    "_report_bulk_seasons",
                    "_report_catalogue",
                    "_report_current_season",
                    "_report_season_backfill",
                    "report_manga_catalogue",
                    "report_manga_refresh",
                ):
                    stack.enter_context(
                        patch(f"backend.jobs.jikan_etl.{report_name}")
                    )
                stack.enter_context(patch("builtins.print"))
                publish = stack.enter_context(
                    patch(
                        "backend.jobs.jikan_etl."
                        "_refresh_and_report_catalogue_facets"
                    )
                )

                main()

                publish.assert_called_once_with()

    def test_scheduled_cli_does_not_publish_facets_twice(self):
        with (
            patch("sys.argv", ["jikan_etl", "--scheduled-sync"]),
            patch("backend.jobs.jikan_etl.run_scheduled_sync") as scheduled,
            patch(
                "backend.jobs.jikan_etl._refresh_and_report_catalogue_facets"
            ) as standalone_publish,
        ):
            main()

        scheduled.assert_called_once()
        standalone_publish.assert_not_called()

    def test_season_sync_reuses_and_forces_listing_data(self):
        anime = SimpleNamespace(mal_id=1)
        seasonal_data = {
            "mal_id": 1,
            "title": "Example",
            "type": "TV",
            "status": "Currently Airing",
        }
        with (
            patch("backend.jobs.jikan_etl._ensure_schema"),
            patch("backend.jobs.jikan_etl._update_anime") as update_anime,
            patch("backend.jobs.jikan_etl.db") as mock_db,
        ):
            mock_db.session.scalars.side_effect = [[anime], []]
            saved, skipped = sync_season(
                2026,
                "summer",
                fetch_season=lambda _year, _season: [seasonal_data],
            )

        self.assertEqual((saved, skipped), (1, 0))
        mapped_data = update_anime.call_args.args[1]
        self.assertEqual(mapped_data["season"], "summer")
        self.assertEqual(mapped_data["year"], 2026)
        self.assertEqual(mapped_data["status"], "Currently Airing")

    def test_season_sync_deletes_existing_hentai_records(self):
        anime = SimpleNamespace(mal_id=1)
        seasonal_data = {
            "mal_id": 1,
            "title": "Adult example",
            "type": "OVA",
            "rating": "Rx - Hentai",
        }
        with (
            patch("backend.jobs.jikan_etl._ensure_schema"),
            patch("backend.jobs.jikan_etl.db") as mock_db,
        ):
            mock_db.session.scalars.side_effect = [[anime], []]
            saved, skipped = sync_season(
                fetch_season=lambda _year, _season: [seasonal_data],
            )

        self.assertEqual((saved, skipped), (0, 1))
        mock_db.session.delete.assert_called_once_with(anime)

    def test_season_backfill_selects_only_pending_tv_rows(self):
        anime = SimpleNamespace(
            animeID=1,
            mal_id=101,
            season=None,
            last_season_attempt=None,
        )
        with (
            patch("backend.jobs.jikan_etl._ensure_schema"),
            patch("backend.jobs.jikan_etl.db") as mock_db,
        ):
            mock_db.session.scalars.side_effect = [[anime], []]
            result = backfill_missing_seasons(
                limit=1,
                fetch_anime=lambda _mal_id: {"data": []},
            )
            statement = mock_db.session.scalars.call_args_list[0].args[0]

        sql = str(statement).lower()
        self.assertIn("anime.season is null", sql)
        self.assertIn("upper(anime.type)", sql)
        self.assertIn("anime.last_season_attempt", sql)
        self.assertEqual(result.selected, 1)
        self.assertEqual(result.invalid_payloads, 1)
        self.assertIsNotNone(anime.last_season_attempt)

    def test_season_backfill_assigns_season_from_anime_detail(self):
        anime = SimpleNamespace(
            animeID=1,
            mal_id=101,
            season=None,
            last_season_attempt=None,
        )

        def update_anime(record, data, _genres):
            record.season = data["season"]

        with (
            patch("backend.jobs.jikan_etl._ensure_schema"),
            patch("backend.jobs.jikan_etl._update_anime", side_effect=update_anime),
            patch("backend.jobs.jikan_etl.db") as mock_db,
        ):
            mock_db.session.scalars.side_effect = [[anime], []]
            result = backfill_missing_seasons(
                limit=1,
                fetch_anime=lambda _mal_id: {"data": {"season": "fall"}},
            )

        self.assertEqual(result.updated, 1)
        self.assertEqual(result.seasons_assigned, 1)
        self.assertEqual(result.still_missing, 0)
        self.assertEqual(anime.season, "fall")

    def test_season_backfill_marks_temporary_failures_and_advances_queue(self):
        records = [
            SimpleNamespace(
                animeID=index,
                mal_id=index,
                season=None,
                last_season_attempt=None,
            )
            for index in range(1, 3)
        ]

        def temporary_failure(_mal_id):
            raise JikanTemporaryError("Jikan is unavailable")

        with (
            patch("backend.jobs.jikan_etl._ensure_schema"),
            patch("backend.jobs.jikan_etl.db") as mock_db,
        ):
            mock_db.session.scalars.side_effect = [records, []]
            result = backfill_missing_seasons(
                limit=2,
                batch_size=1,
                fetch_anime=temporary_failure,
            )

        self.assertEqual(result.temporary_errors, 2)
        self.assertTrue(all(record.last_season_attempt for record in records))
        self.assertGreaterEqual(mock_db.session.commit.call_count, 3)

    def test_season_backfill_deletes_anime_reclassified_as_hentai(self):
        anime = SimpleNamespace(
            animeID=1,
            mal_id=101,
            season=None,
            last_season_attempt=None,
        )
        with (
            patch("backend.jobs.jikan_etl._ensure_schema"),
            patch("backend.jobs.jikan_etl.db") as mock_db,
        ):
            mock_db.session.scalars.side_effect = [[anime], []]
            result = backfill_missing_seasons(
                limit=1,
                fetch_anime=lambda _mal_id: {
                    "data": {"rating": "Rx - Hentai"}
                },
            )

        self.assertEqual(result.removed_hentai, 1)
        self.assertEqual(result.updated, 0)
        mock_db.session.delete.assert_called_once_with(anime)

    def test_current_season_resumes_failed_page(self):
        page_one = JikanSeasonPage(
            entries=[{"mal_id": 1, "type": "TV"}],
            page=1,
            has_next_page=True,
        )

        def first_run_fetch(_year, _season, *, page):
            if page == 1:
                return page_one
            raise JikanTemporaryError("page two failed")

        with (
            patch("backend.jobs.jikan_etl._ensure_schema"),
            patch("backend.jobs.jikan_etl._next_page", return_value=1),
            patch("backend.jobs.jikan_etl._record_page_error") as record_error,
            patch(
                "backend.jobs.jikan_etl._apply_season_page",
                return_value=SeasonPageApplyResult(saved=1, seasons_assigned=1),
            ) as apply_page,
        ):
            first = sync_current_season(
                fetch_page=first_run_fetch,
                now=datetime(2026, 7, 22, tzinfo=timezone.utc),
            )

        self.assertEqual(first.pages_completed, 1)
        self.assertEqual(first.pages_failed, 1)
        self.assertEqual(first.next_page, 2)
        self.assertFalse(first.complete)
        apply_page.assert_called_once()
        self.assertEqual(record_error.call_args.args[1], 2)

        final_page = JikanSeasonPage(entries=[], page=2, has_next_page=False)
        with (
            patch("backend.jobs.jikan_etl._ensure_schema"),
            patch("backend.jobs.jikan_etl._next_page", return_value=2),
            patch(
                "backend.jobs.jikan_etl._apply_season_page",
                return_value=SeasonPageApplyResult(),
            ) as resumed_apply,
        ):
            resumed = sync_current_season(
                fetch_page=lambda _year, _season, *, page: final_page,
                now=datetime(2026, 7, 22, tzinfo=timezone.utc),
            )

        self.assertTrue(resumed.complete)
        self.assertEqual(resumed.next_page, 1)
        self.assertEqual(resumed_apply.call_args.args[0].page, 2)

    def test_current_season_can_finish_six_pages_in_one_run(self):
        def fetch_page(_year, _season, *, page):
            return JikanSeasonPage(
                entries=[],
                page=page,
                has_next_page=page < 6,
            )

        with (
            patch("backend.jobs.jikan_etl._ensure_schema"),
            patch("backend.jobs.jikan_etl._next_page", return_value=1),
            patch(
                "backend.jobs.jikan_etl._apply_season_page",
                return_value=SeasonPageApplyResult(),
            ) as apply_page,
        ):
            result = sync_current_season(
                fetch_page=fetch_page,
                now=datetime(2026, 7, 22, tzinfo=timezone.utc),
            )

        self.assertTrue(result.complete)
        self.assertEqual(result.pages_completed, 6)
        self.assertEqual(result.next_page, 1)
        self.assertEqual(apply_page.call_count, 6)

    def test_current_season_accumulates_studio_relationship_metrics(self):
        season_page = JikanSeasonPage(
            entries=[
                {
                    "mal_id": 1,
                    "type": "TV",
                    "studios": [{"mal_id": 7, "name": "Bones"}],
                }
            ],
            page=1,
            has_next_page=False,
        )
        associations = AnimeAssociationStats(
            anime_with_studios_updated=1,
            studio_links_created=1,
        )
        with (
            patch("backend.jobs.jikan_etl._ensure_schema"),
            patch("backend.jobs.jikan_etl._next_page", return_value=1),
            patch(
                "backend.jobs.jikan_etl._apply_season_page",
                return_value=SeasonPageApplyResult(
                    saved=1,
                    associations=associations,
                ),
            ) as apply_page,
        ):
            result = sync_current_season(
                fetch_page=lambda _year, _season, *, page: season_page,
                now=datetime(2026, 7, 22, tzinfo=timezone.utc),
            )

        self.assertEqual(result.associations.anime_with_studios_updated, 1)
        self.assertEqual(result.associations.studio_links_created, 1)
        self.assertEqual(
            apply_page.call_args.args[0].entries[0]["studios"][0]["name"],
            "Bones",
        )

    def test_bulk_season_sync_commits_multiple_catalogue_pages(self):
        requested_pages = []

        def fetch_page(*, anime_type, page):
            requested_pages.append((anime_type, page))
            return JikanAnimePage(
                entries=[{"mal_id": page, "type": "TV", "season": "summer"}],
                page=page,
                has_next_page=page < 2,
                last_visible_page=2,
            )

        with (
            patch("backend.jobs.jikan_etl._ensure_schema"),
            patch("backend.jobs.jikan_etl._next_page", return_value=1),
            patch(
                "backend.jobs.jikan_etl._apply_season_page",
                return_value=SeasonPageApplyResult(saved=1, seasons_assigned=1),
            ) as apply_page,
        ):
            result = sync_bulk_anime_seasons(fetch_page=fetch_page)

        self.assertEqual(requested_pages, [("tv", 1), ("tv", 2)])
        self.assertEqual(result.pages_attempted, 2)
        self.assertEqual(result.pages_completed, 2)
        self.assertEqual(result.updated, 2)
        self.assertEqual(result.seasons_assigned, 2)
        self.assertTrue(result.complete)
        self.assertEqual(result.next_page, 1)
        self.assertEqual(apply_page.call_count, 2)

    def test_bulk_tv_sync_accumulates_studio_relationship_metrics(self):
        catalogue_page = JikanAnimePage(
            entries=[
                {
                    "mal_id": 1,
                    "type": "TV",
                    "studios": [{"mal_id": 7, "name": "Bones"}],
                }
            ],
            page=1,
            has_next_page=False,
            last_visible_page=1,
        )
        associations = AnimeAssociationStats(
            anime_with_studios_updated=1,
            studio_links_created=1,
        )
        with (
            patch("backend.jobs.jikan_etl._ensure_schema"),
            patch("backend.jobs.jikan_etl._next_page", return_value=1),
            patch(
                "backend.jobs.jikan_etl._apply_season_page",
                return_value=SeasonPageApplyResult(
                    saved=1,
                    associations=associations,
                ),
            ) as apply_page,
        ):
            result = sync_bulk_anime_seasons(
                fetch_page=lambda *, anime_type, page: catalogue_page,
            )

        self.assertEqual(result.associations.anime_with_studios_updated, 1)
        self.assertEqual(result.associations.studio_links_created, 1)
        self.assertEqual(
            apply_page.call_args.args[0].entries[0]["studios"][0]["name"],
            "Bones",
        )

    def test_bulk_tv_sync_uses_new_discovery_cursor(self):
        with patch(
            "backend.jobs.jikan_etl._sync_bulk_anime_type",
            return_value=BulkSeasonSyncResult(),
        ) as sync_type:
            sync_bulk_anime_seasons(max_pages=7)

        self.assertEqual(sync_type.call_args.kwargs["anime_type"], "tv")
        self.assertEqual(sync_type.call_args.kwargs["state_key"], BULK_SEASON_STATE_KEY)
        self.assertTrue(sync_type.call_args.kwargs["discover_missing"])
        self.assertEqual(sync_type.call_args.kwargs["max_pages"], 7)

    def test_bulk_season_sync_preserves_failed_page_and_stops_during_outage(self):
        def fetch_page(*, anime_type, page):
            raise JikanTemporaryError(f"page {page} unavailable")

        with (
            patch("backend.jobs.jikan_etl._ensure_schema"),
            patch("backend.jobs.jikan_etl._next_page", return_value=4),
            patch("backend.jobs.jikan_etl._record_page_error") as record_error,
        ):
            result = sync_bulk_anime_seasons(
                max_pages=10,
                max_consecutive_failures=2,
                fetch_page=fetch_page,
            )

        self.assertEqual(result.pages_attempted, 2)
        self.assertEqual(result.pages_failed, 2)
        self.assertEqual(result.next_page, 4)
        self.assertEqual(
            [call.args[1] for call in record_error.call_args_list],
            [4, 4],
        )

    def test_bulk_page_repairs_a_stale_local_type_from_provider_tv_data(self):
        anime = SimpleNamespace(
            mal_id=1,
            type="Unknown",
            season=None,
            status=None,
        )
        state = SimpleNamespace(
            next_page=1,
            last_attempt_at=None,
            last_error=None,
            last_completed_at=None,
        )

        def update_anime(record, data, _genres):
            record.type = data["type"]
            record.season = data["season"]
            record.status = _anime_status(data.get("status"))

        page = JikanAnimePage(
            entries=[
                {
                    "mal_id": 1,
                    "type": "TV",
                    "season": "winter",
                    "status": "Currently Airing",
                }
            ],
            page=1,
            has_next_page=True,
        )
        with (
            patch("backend.jobs.jikan_etl._update_anime", side_effect=update_anime),
            patch("backend.jobs.jikan_etl._sync_state", return_value=state),
            patch("backend.jobs.jikan_etl.db") as mock_db,
        ):
            mock_db.session.scalars.side_effect = [[anime], []]
            result = _apply_season_page(
                page,
                state_key="test",
                year=None,
                season=None,
                discover_missing=False,
                tv_only=True,
            )

        self.assertEqual(result.saved, 1)
        self.assertEqual(result.seasons_assigned, 1)
        self.assertEqual(anime.type, "TV")
        self.assertEqual(anime.season, "winter")
        self.assertEqual(anime.status, "CURRENTLY_AIRING")

    def test_type_filtered_page_uses_requested_type_when_payload_omits_it(self):
        anime = SimpleNamespace(mal_id=1, season=None)
        state = SimpleNamespace(
            next_page=1,
            last_attempt_at=None,
            last_error=None,
            last_completed_at=None,
        )

        def update_anime(record, data, _genres):
            record.type = data["type"]

        page = JikanAnimePage(
            entries=[{"mal_id": 1, "title": "OVA example"}],
            page=1,
            has_next_page=False,
        )
        with (
            patch("backend.jobs.jikan_etl._new_anime", return_value=anime),
            patch("backend.jobs.jikan_etl._update_anime", side_effect=update_anime),
            patch("backend.jobs.jikan_etl._sync_state", return_value=state),
            patch("backend.jobs.jikan_etl.db") as mock_db,
        ):
            mock_db.session.scalars.side_effect = [[], []]
            result = _apply_season_page(
                page,
                state_key="test:ova",
                year=None,
                season=None,
                discover_missing=True,
                tv_only=False,
                allowed_types=frozenset({"OVA"}),
                default_type="OVA",
            )

        self.assertEqual(result.inserted, 1)
        self.assertEqual(result.saved, 1)
        self.assertEqual(anime.type, "OVA")

    def test_tv_discovery_inserts_safe_titles_and_skips_hentai(self):
        anime = SimpleNamespace(mal_id=1, season=None)
        state = SimpleNamespace(
            next_page=1,
            last_attempt_at=None,
            last_error=None,
            last_completed_at=None,
        )
        page = JikanAnimePage(
            entries=[
                {"mal_id": 1, "title": "Safe TV title", "type": "TV"},
                {
                    "mal_id": 2,
                    "title": "Adult TV title",
                    "type": "TV",
                    "rating": "Rx - Hentai",
                },
            ],
            page=1,
            has_next_page=True,
        )
        with (
            patch("backend.jobs.jikan_etl._new_anime", return_value=anime),
            patch(
                "backend.jobs.jikan_etl._update_anime",
                side_effect=lambda record, data, _genres: setattr(
                    record, "type", data["type"]
                ),
            ),
            patch("backend.jobs.jikan_etl._sync_state", return_value=state),
            patch("backend.jobs.jikan_etl.db") as mock_db,
        ):
            mock_db.session.scalars.side_effect = [[], []]
            result = _apply_season_page(
                page,
                state_key=BULK_SEASON_STATE_KEY,
                year=None,
                season=None,
                discover_missing=True,
                tv_only=False,
                allowed_types=frozenset({"TV"}),
                default_type="TV",
            )

        self.assertEqual(result.inserted, 1)
        self.assertEqual(result.saved, 1)
        self.assertEqual(result.skipped, 1)
        self.assertEqual(anime.type, "TV")
        self.assertEqual(state.next_page, 2)
        mock_db.session.add.assert_called_once_with(anime)

    def test_type_filtered_page_removes_existing_hentai_record(self):
        anime = SimpleNamespace(mal_id=1)
        state = SimpleNamespace(
            next_page=1,
            last_attempt_at=None,
            last_error=None,
            last_completed_at=None,
        )
        page = JikanAnimePage(
            entries=[
                {
                    "mal_id": 1,
                    "type": "ONA",
                    "title": "Conflicting safe duplicate",
                },
                {
                    "mal_id": 1,
                    "type": "ONA",
                    "rating": "Rx - Hentai",
                }
            ],
            page=1,
            has_next_page=False,
        )
        with (
            patch("backend.jobs.jikan_etl._sync_state", return_value=state),
            patch("backend.jobs.jikan_etl.db") as mock_db,
        ):
            mock_db.session.scalars.side_effect = [[anime], []]
            result = _apply_season_page(
                page,
                state_key="test:ona",
                year=None,
                season=None,
                discover_missing=True,
                tv_only=False,
                allowed_types=frozenset({"ONA"}),
                default_type="ONA",
            )

        self.assertEqual(result.removed_hentai, 1)
        self.assertEqual(result.saved, 0)
        mock_db.session.delete.assert_called_once_with(anime)

    def test_supplemental_sync_uses_independent_type_cursors(self):
        scan_results = [
            BulkSeasonSyncResult(
                inserted=index,
                associations=AnimeAssociationStats(
                    anime_with_studios_updated=(
                        1 if anime_type == "movie" else 0
                    ),
                    studio_links_created=(
                        2 if anime_type == "movie" else 0
                    ),
                ),
            )
            for index, anime_type in enumerate(
                SUPPLEMENTAL_PROVIDER_TYPES,
                start=1,
            )
        ]
        with patch(
            "backend.jobs.jikan_etl._sync_bulk_anime_type",
            side_effect=scan_results,
        ) as sync_type:
            result = sync_supplemental_anime_types(max_pages=7)

        self.assertIn("movie", SUPPLEMENTAL_PROVIDER_TYPES)
        self.assertEqual(sync_type.call_count, len(SUPPLEMENTAL_PROVIDER_TYPES))
        for anime_type, sync_call in zip(
            SUPPLEMENTAL_PROVIDER_TYPES,
            sync_type.call_args_list,
            strict=True,
        ):
            self.assertEqual(sync_call.kwargs["anime_type"], anime_type)
            self.assertEqual(
                sync_call.kwargs["state_key"],
                SUPPLEMENTAL_STATE_KEYS[anime_type],
            )
            self.assertTrue(sync_call.kwargs["discover_missing"])
            self.assertEqual(sync_call.kwargs["max_pages"], 7)
            self.assertEqual(sync_call.kwargs["max_consecutive_failures"], 3)
        self.assertEqual(
            result.inserted,
            sum(range(1, len(SUPPLEMENTAL_PROVIDER_TYPES) + 1)),
        )
        self.assertEqual(result.associations.anime_with_studios_updated, 1)
        self.assertEqual(result.associations.studio_links_created, 2)
        movie_call = next(
            call
            for call in sync_type.call_args_list
            if call.kwargs["anime_type"] == "movie"
        )
        self.assertTrue(movie_call.kwargs["discover_missing"])
        self.assertEqual(
            movie_call.kwargs["state_key"],
            SUPPLEMENTAL_STATE_KEYS["movie"],
        )

    def test_cleanup_deletes_matching_adult_anime_but_keeps_shared_genres(self):
        with (
            patch("backend.jobs.jikan_etl._ensure_schema"),
            patch("backend.jobs.jikan_etl.db") as mock_db,
        ):
            mock_db.session.scalars.return_value = [10, 20]
            removed = remove_hentai_anime()

        self.assertEqual(removed, 2)
        # The shared Genre row is retained because Manga/Manhwa may still
        # reference the same normalized value; only links and anime are deleted.
        self.assertEqual(mock_db.session.execute.call_count, 2)
        cleanup_sql = str(mock_db.session.scalars.call_args.args[0]).lower()
        self.assertIn("anime_genre", cleanup_sql)
        self.assertIn("unnest(anime.genres)", cleanup_sql)
        self.assertIn("unnest(anime.genres_detailed)", cleanup_sql)
        mock_db.session.commit.assert_called_once()

    def test_bulk_rate_limit_stops_without_advancing_cursor(self):
        def rate_limited(*, anime_type, page):
            raise HTTPError(
                f"https://example.test/anime?page={page}",
                429,
                "rate limited",
                Message(),
                None,
            )

        with (
            patch("backend.jobs.jikan_etl._ensure_schema"),
            patch("backend.jobs.jikan_etl._next_page", return_value=12),
            patch("backend.jobs.jikan_etl._record_page_error") as record_error,
        ):
            result = sync_bulk_anime_seasons(fetch_page=rate_limited)

        self.assertEqual(result.pages_attempted, 1)
        self.assertEqual(result.pages_failed, 1)
        self.assertEqual(result.next_page, 12)
        self.assertEqual(record_error.call_args.args[1], 12)

    def test_bulk_season_sync_rejects_invalid_limits(self):
        with self.assertRaises(ValueError):
            sync_bulk_anime_seasons(max_pages=0)
        with self.assertRaises(ValueError):
            sync_bulk_anime_seasons(max_consecutive_failures=0)

    def test_backfill_rejects_invalid_limits(self):
        with self.assertRaises(ValueError):
            backfill_missing_seasons(limit=0)
        with self.assertRaises(ValueError):
            backfill_missing_seasons(batch_size=0)


if __name__ == "__main__":
    unittest.main()
