import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from sqlalchemy import CheckConstraint

from backend import schema as catalogue_schema
from backend.models import (
    Anime,
    AnimeStreamingService,
    AnimeStudio,
    Author,
    CatalogueFacet,
    Genre,
    JikanRefreshState,
    Manga,
    MangaAuthor,
    MangaGenre,
    SiteVisit,
    StreamingService,
    Studio,
)


SCHEMA_SOURCE = (
    Path(__file__).resolve().parents[1] / "backend" / "schema.py"
)


class MangaSchemaTests(unittest.TestCase):
    def test_refresh_state_persists_adaptive_tier_and_failure_streak(self):
        columns = JikanRefreshState.__table__.c
        self.assertEqual(columns.refresh_tier.type.length, 12)
        self.assertFalse(columns.failure_streak.nullable)
        schema_source = SCHEMA_SOURCE.read_text(encoding="utf-8")
        self.assertIn('"refresh_tier VARCHAR(12)"', schema_source)
        self.assertIn('"failure_streak INTEGER NOT NULL DEFAULT 0"', schema_source)

    def test_anime_table_contains_nullable_indexed_airing_status(self):
        self.assertIn("status", Anime.__table__.columns)
        self.assertTrue(Anime.__table__.c.status.nullable)
        self.assertIn(
            "ix_anime_status_score",
            {index.name for index in Anime.__table__.indexes},
        )
        schema_source = SCHEMA_SOURCE.read_text(encoding="utf-8")
        self.assertIn('"status VARCHAR(30)"', schema_source)
        self.assertIn(
            "CREATE INDEX IF NOT EXISTS ix_anime_status_score",
            schema_source,
        )

    def test_anime_streaming_backfill_uses_a_separate_indexed_timestamp(self):
        self.assertIn("last_streaming_attempt", Anime.__table__.columns)
        self.assertIn(
            "ix_anime_last_streaming_attempt",
            {index.name for index in Anime.__table__.indexes},
        )
        schema_source = SCHEMA_SOURCE.read_text(encoding="utf-8")
        self.assertIn('"last_streaming_attempt TIMESTAMP WITH TIME ZONE"', schema_source)
        self.assertIn(
            "CREATE INDEX IF NOT EXISTS ix_anime_last_streaming_attempt",
            schema_source,
        )

    def test_popularity_and_member_fields_are_nullable_and_indexed(self):
        self.assertTrue(Anime.__table__.c.popularity.nullable)
        self.assertTrue(Anime.__table__.c.members.nullable)
        self.assertTrue(Manga.__table__.c.popularity.nullable)
        self.assertTrue(Manga.__table__.c.members.nullable)

        schema_source = SCHEMA_SOURCE.read_text(encoding="utf-8")
        self.assertIn("CATALOGUE_SCHEMA_VERSION = 8", schema_source)
        self.assertIn('"popularity INTEGER"', schema_source)
        self.assertIn('"members INTEGER"', schema_source)
        for index_name in (
            "ix_anime_public_popularity",
            "ix_anime_public_members",
            "ix_manga_public_popularity",
            "ix_manga_public_members",
        ):
            self.assertIn(index_name, schema_source)

    def test_manga_table_contains_the_complete_catalogue_shape(self):
        columns = set(Manga.__table__.columns.keys())

        self.assertTrue(
            {
                "manga_id",
                "mal_id",
                "content_type",
                "title",
                "alternative_title",
                "synopsis",
                "manga_type",
                "publication_year",
                "status",
                "score",
                "is_adult",
                "chapters",
                "volumes",
                "mal_url",
                "image_url",
                "genres",
                "genres_detailed",
                "last_jikan_sync",
                "last_jikan_attempt",
            }.issubset(columns)
        )
        self.assertTrue(Manga.__table__.c.mal_id.unique)

    def test_content_type_constraint_allows_only_manga_and_manhwa(self):
        checks = [
            str(constraint.sqltext)
            for constraint in Manga.__table__.constraints
            if isinstance(constraint, CheckConstraint)
        ]

        self.assertTrue(
            any(
                "MANGA" in expression and "MANHWA" in expression
                for expression in checks
            )
        )

    def test_manga_genres_reuse_the_shared_genre_table(self):
        foreign_key_targets = {
            foreign_key.target_fullname
            for foreign_key in MangaGenre.__table__.foreign_keys
        }

        self.assertEqual(
            foreign_key_targets,
            {"manga.manga_id", "genre.id"},
        )
        self.assertIs(
            MangaGenre.__mapper__.relationships["genre"].entity.class_,
            Genre,
        )

    def test_manga_authors_use_a_normalized_role_aware_link(self):
        self.assertTrue(Author.__table__.c.mal_id.unique)
        self.assertTrue(Author.__table__.c.normalized_name.unique)
        self.assertEqual(
            {
                column.name
                for column in MangaAuthor.__table__.primary_key.columns
            },
            {"manga_id", "author_id"},
        )
        self.assertEqual(
            {
                foreign_key.target_fullname
                for foreign_key in MangaAuthor.__table__.foreign_keys
            },
            {"manga.manga_id", "author.id"},
        )
        self.assertIn("role", MangaAuthor.__table__.columns)
        self.assertIn(
            "ix_manga_author_author_manga",
            {index.name for index in MangaAuthor.__table__.indexes},
        )

    def test_anime_studios_use_a_normalized_many_to_many_schema(self):
        self.assertTrue(Studio.__table__.c.mal_id.unique)
        self.assertTrue(Studio.__table__.c.normalized_name.unique)
        self.assertEqual(
            {
                column.name
                for column in AnimeStudio.__table__.primary_key.columns
            },
            {"anime_id", "studio_id"},
        )
        self.assertEqual(
            {
                foreign_key.target_fullname
                for foreign_key in AnimeStudio.__table__.foreign_keys
            },
            {"anime.anime_id", "studio.id"},
        )
        self.assertIn(
            "ix_anime_studio_studio_anime",
            {index.name for index in AnimeStudio.__table__.indexes},
        )
        self.assertIs(
            AnimeStudio.__mapper__.relationships["studio"].entity.class_,
            Studio,
        )

    def test_streaming_urls_live_on_the_normalized_anime_service_link(self):
        self.assertTrue(StreamingService.__table__.c.normalized_name.unique)
        self.assertIn("url", AnimeStreamingService.__table__.columns)
        self.assertEqual(
            {
                column.name
                for column in AnimeStreamingService.__table__.primary_key.columns
            },
            {"anime_id", "streaming_service_id"},
        )
        self.assertEqual(
            {
                foreign_key.target_fullname
                for foreign_key in AnimeStreamingService.__table__.foreign_keys
            },
            {"anime.anime_id", "streaming_service.id"},
        )
        self.assertIn(
            "ix_anime_streaming_service_service_anime",
            {
                index.name
                for index in AnimeStreamingService.__table__.indexes
            },
        )
        self.assertIs(
            AnimeStreamingService.__mapper__.relationships[
                "streaming_service"
            ].entity.class_,
            StreamingService,
        )

    def test_frequent_filter_and_search_indexes_are_declared(self):
        index_names = {index.name for index in Manga.__table__.indexes}

        self.assertTrue(
            {
                "ix_manga_content_score",
                "ix_manga_content_public_score",
                "ix_manga_content_year",
                "ix_manga_content_chapters",
                "ix_manga_content_volumes",
                "ix_manga_content_status_score",
                "ix_manga_genres_detailed_gin",
                "ix_manga_title_trgm",
                "ix_manga_alternative_title_trgm",
            }.issubset(index_names)
        )
        schema_source = SCHEMA_SOURCE.read_text(encoding="utf-8")
        self.assertIn("CREATE INDEX IF NOT EXISTS ix_anime_is_adult", schema_source)
        self.assertIn("CREATE INDEX IF NOT EXISTS ix_manga_is_adult", schema_source)
        self.assertIn("ix_manga_content_status_normalized_score", schema_source)
        self.assertIn("CREATE EXTENSION IF NOT EXISTS pg_trgm", schema_source)

    def test_public_catalogue_flags_are_indexed_for_both_media_tables(self):
        self.assertFalse(Anime.__table__.c.is_adult.nullable)
        self.assertFalse(Manga.__table__.c.is_adult.nullable)
        self.assertIn(
            "ix_anime_public_score",
            {index.name for index in Anime.__table__.indexes},
        )
        self.assertIn(
            "ix_manga_content_public_score",
            {index.name for index in Manga.__table__.indexes},
        )

    def test_top_rated_indexes_are_applied_by_versioned_migration(self):
        schema_source = SCHEMA_SOURCE.read_text(encoding="utf-8")

        self.assertIn("CATALOGUE_SCHEMA_VERSION = 8", schema_source)
        self.assertIn("ix_anime_public_top_rated", schema_source)
        self.assertIn("ix_manga_public_top_rated", schema_source)
        self.assertIn("score DESC NULLS LAST", schema_source)
        self.assertIn("WHERE is_adult = FALSE", schema_source)

    def test_catalogue_facets_have_a_composite_lookup_key(self):
        self.assertEqual(
            {
                column.name
                for column in CatalogueFacet.__table__.primary_key.columns
            },
            {"content_type", "facet_type", "value"},
        )
        schema_source = SCHEMA_SOURCE.read_text(encoding="utf-8")
        self.assertIn("refresh_catalogue_facets", schema_source)
        self.assertIn("INSERT INTO catalogue_facet", schema_source)
        self.assertIn(
            "DELETE FROM catalogue_facet AS existing WHERE NOT EXISTS",
            schema_source,
        )
        self.assertNotIn(
            'db.session.execute(text("DELETE FROM catalogue_facet"))',
            schema_source,
        )
        self.assertIn("catalogue_cache_generation", schema_source)

    def test_catalogue_facets_support_relationship_options(self):
        facet_checks = [
            str(constraint.sqltext)
            for constraint in CatalogueFacet.__table__.constraints
            if isinstance(constraint, CheckConstraint)
            and constraint.name == "ck_catalogue_facet_type"
        ]

        self.assertEqual(CatalogueFacet.__table__.c.facet_type.type.length, 30)
        self.assertTrue(
            any(
                "studio" in expression
                and "streaming_service" in expression
                and "author" in expression
                for expression in facet_checks
            )
        )
        schema_source = SCHEMA_SOURCE.read_text(encoding="utf-8")
        self.assertIn(
            "ALTER TABLE catalogue_facet ALTER COLUMN facet_type",
            schema_source,
        )
        self.assertIn(
            "'genre', 'tag', 'studio', 'streaming_service', 'author'",
            schema_source,
        )
        self.assertIn("JOIN anime_studio", schema_source)
        self.assertIn("JOIN anime_streaming_service", schema_source)
        self.assertIn("JOIN manga_author", schema_source)

    def test_schema_bootstrap_is_versioned_and_cross_process_safe(self):
        schema_source = SCHEMA_SOURCE.read_text(encoding="utf-8")

        self.assertIn("CATALOGUE_SCHEMA_VERSION", schema_source)
        self.assertIn("catalogue_schema_version", schema_source)
        self.assertIn("pg_advisory_xact_lock", schema_source)
        self.assertIn("_schema_version_is_current", schema_source)
        self.assertIn("db.metadata.create_all(bind=connection)", schema_source)
        self.assertNotIn("db.create_all()", schema_source)
        self.assertIn('"author",', schema_source)
        self.assertIn(
            "constraint_facet_types != required_facet_types",
            schema_source,
        )

    def test_site_visit_uses_anonymous_daily_aggregates_and_indexes(self):
        columns = set(SiteVisit.__table__.columns.keys())
        self.assertTrue(
            {
                "visitor_token_hash",
                "visit_date",
                "route",
                "visit_count",
                "first_visited_at",
                "last_visited_at",
            }.issubset(columns)
        )
        self.assertNotIn("ip_address", columns)
        self.assertNotIn("user_agent", columns)

        constraint_names = {
            constraint.name for constraint in SiteVisit.__table__.constraints
        }
        self.assertIn("uq_site_visit_visitor_day_route", constraint_names)
        self.assertIn("ck_site_visit_visit_count_positive", constraint_names)
        self.assertTrue(
            {
                "ix_site_visit_date",
                "ix_site_visit_date_route",
            }.issubset({index.name for index in SiteVisit.__table__.indexes})
        )

        schema_source = SCHEMA_SOURCE.read_text(encoding="utf-8")
        self.assertIn("CATALOGUE_SCHEMA_VERSION = 8", schema_source)
        self.assertIn("CREATE TABLE IF NOT EXISTS site_visit", schema_source)
        self.assertIn("uq_site_visit_visitor_day_route", schema_source)
        self.assertIn("ix_site_visit_date_route", schema_source)

    def test_schema_bootstrap_double_checks_under_lock_and_runs_once(self):
        mock_db = MagicMock()
        connection = object()
        mock_db.session.connection.return_value = connection

        with (
            patch.object(catalogue_schema, "db", mock_db),
            patch.object(
                catalogue_schema,
                "_catalogue_schema_ready",
                False,
            ),
            patch.object(
                catalogue_schema,
                "_schema_version_is_current",
                side_effect=(False, False),
            ) as version_check,
            patch.object(
                catalogue_schema,
                "_apply_catalogue_schema_migration",
            ) as apply_migration,
        ):
            catalogue_schema.ensure_catalogue_schema()
            catalogue_schema.ensure_catalogue_schema()

        self.assertEqual(version_check.call_count, 2)
        apply_migration.assert_called_once_with(connection)
        self.assertEqual(mock_db.session.commit.call_count, 1)
        statements = [
            str(call.args[0])
            for call in mock_db.session.execute.call_args_list
        ]
        self.assertTrue(
            any("pg_advisory_xact_lock" in statement for statement in statements)
        )

    def test_current_schema_version_skips_lock_and_ddl(self):
        mock_db = MagicMock()

        with (
            patch.object(catalogue_schema, "db", mock_db),
            patch.object(
                catalogue_schema,
                "_catalogue_schema_ready",
                False,
            ),
            patch.object(
                catalogue_schema,
                "_schema_version_is_current",
                return_value=True,
            ),
            patch.object(
                catalogue_schema,
                "_apply_catalogue_schema_migration",
            ) as apply_migration,
        ):
            catalogue_schema.ensure_catalogue_schema()

        apply_migration.assert_not_called()
        mock_db.session.execute.assert_not_called()
        mock_db.session.commit.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
