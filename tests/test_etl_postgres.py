"""Opt-in ETL integration tests against a disposable, loopback PostgreSQL DB.

Set ETL_TEST_DATABASE_URL to a PostgreSQL URL whose host is 127.0.0.1,
localhost, or ::1 and whose database ends in ``_test``. Every test owns a new
random schema and drops only that schema. DATABASE_URL is never used as the
integration target; neither a production Neon URL nor provider HTTP is used.
"""

from collections import Counter
from contextlib import ExitStack
from datetime import datetime, timezone
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlsplit
from uuid import uuid4

from sqlalchemy import create_engine, event, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.schema import CreateSchema, DropSchema

# Import-time application initialization must never use credentials from .env.
with patch.dict(os.environ, {"DATABASE_URL": "sqlite://"}):
    from backend import schema
    from backend.jobs import efficient_sync as worker, jikan_etl, manga_etl
    from backend.jobs.refresh_policy import RefreshPolicy
    from backend.models import (
        Anime,
        AnimeGenre,
        AnimeStreamingService,
        Genre,
        JikanRefreshState,
        JikanSyncState,
        Manga,
        db,
    )
    from backend.services.jikan_client import JikanClient
    from tests.test_jikan_client import FakeClock, Response


TEST_URL = os.environ.get("ETL_TEST_DATABASE_URL")
NOW = datetime(2026, 9, 9, 12, tzinfo=timezone.utc)


def local_test_url(value):
    """Refuse remote/ambiguous URLs before establishing any connection."""
    url = make_url(value)
    if (
        url.get_backend_name() != "postgresql"
        or url.host not in {"127.0.0.1", "localhost", "::1"}
        or not (url.database or "").endswith("_test")
        or url.query
    ):
        raise ValueError("ETL tests require a loopback PostgreSQL *_test database")
    return url


class PostgreSQLSafetyTests(unittest.TestCase):
    def test_integration_target_cannot_be_production_or_override_host(self):
        for value in (
            "postgresql://example.neon.tech/catalogue_test",
            "postgresql://127.0.0.1/catalogue",
            "postgresql://127.0.0.1/catalogue_test?host=example.neon.tech",
            "sqlite://",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                local_test_url(value)


@unittest.skipUnless(
    TEST_URL, "set ETL_TEST_DATABASE_URL for isolated PostgreSQL tests"
)
class PostgreSQLETLTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.url = local_test_url(TEST_URL)
        cls.admin = create_engine(cls.url)
        with cls.admin.begin() as connection:
            connection.execute(
                text("CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public")
            )

    @classmethod
    def tearDownClass(cls):
        cls.admin.dispose()

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.schema_name = "etl_test_" + uuid4().hex
        self.application_name = self.schema_name
        with self.admin.begin() as connection:
            connection.execute(CreateSchema(self.schema_name))
        self.stack.callback(self.drop_test_schema)
        self.engine = create_engine(
            self.url,
            connect_args={
                "options": f"-csearch_path={self.schema_name},public",
                "application_name": self.application_name,
            },
        )
        self.session = scoped_session(sessionmaker(bind=self.engine))
        self.stack.callback(self.engine.dispose)
        self.stack.callback(self.session.remove)
        self.proxy = SimpleNamespace(
            session=self.session, engine=self.engine, metadata=db.metadata
        )
        for module in (schema, worker, jikan_etl, manga_etl):
            self.stack.enter_context(patch.object(module, "db", self.proxy))
        self.stack.enter_context(patch.object(schema, "_catalogue_schema_ready", False))

    def drop_test_schema(self):
        # The name is generated above, not obtained from a URL or user data.
        with self.admin.begin() as connection:
            connection.execute(DropSchema(self.schema_name, cascade=True))

    def migrate(self):
        schema._catalogue_schema_ready = False
        schema.ensure_catalogue_schema()

    def anime(self, mal_id=1, title="Existing", **kwargs):
        row = Anime(
            mal_id=mal_id,
            title=title,
            type="TV",
            season="summer",
            status="FINISHED_AIRING",
            year=2000,
            score=8,
            episodes=12,
            is_adult=False,
            mal_url=f"https://example.test/anime/{mal_id}",
            sequel=False,
            image_url="",
            legacy_genres=["Drama"],
            genres_detailed=["Drama"],
            **kwargs,
        )
        self.session.add(row)
        self.session.commit()
        return row

    def test_fresh_installation_creates_real_postgresql_arrays_and_refresh_queue(self):
        self.migrate()
        self.assertEqual(
            self.session.scalar(
                text("SELECT max(version) FROM catalogue_schema_version")
            ),
            8,
        )
        columns = {
            column["name"]: column
            for column in inspect(self.engine).get_columns("jikan_refresh_state")
        }
        self.assertTrue(columns["last_success_at"]["type"].timezone)
        self.assertIn("refresh_tier", columns)
        self.assertIn("failure_streak", columns)
        self.assertIn(
            "ix_jikan_refresh_due",
            {
                index["name"]
                for index in inspect(self.engine).get_indexes("jikan_refresh_state")
            },
        )
        self.anime()
        self.assertEqual(
            self.session.scalar(text("SELECT genres FROM anime")), ["Drama"]
        )
        self.migrate()
        self.assertEqual(self.session.scalar(text("SELECT count(*) FROM anime")), 1)

    def test_version_six_upgrade_preserves_catalogue_relationships_and_cursor(self):
        # 5341b50 -> 066fa3b changed only the schema version and introduced
        # JikanRefreshState. Build the pre-refactor tables with the unchanged
        # additive migration, excluding precisely that new table.
        old_tables = [
            table
            for table in db.metadata.sorted_tables
            if table.name != "jikan_refresh_state"
        ]
        old_metadata = SimpleNamespace(
            create_all=lambda bind: db.metadata.create_all(bind, tables=old_tables)
        )
        with (
            patch.object(schema, "CATALOGUE_SCHEMA_VERSION", 6),
            patch.object(self.proxy, "metadata", old_metadata),
        ):
            self.migrate()
        self.assertNotIn("jikan_refresh_state", inspect(self.engine).get_table_names())
        row = self.anime(last_jikan_sync=NOW)
        row.genre_links.append(AnimeGenre(genre=Genre(name="Drama")))
        self.session.add(
            JikanSyncState(
                key="bulk:catalogue:tv:v3",
                next_page=42,
                last_attempt_at=NOW,
                last_error="provider timeout",
            )
        )
        self.session.commit()
        before = self.session.execute(
            text("SELECT anime_id, mal_id, title, genres, last_jikan_sync FROM anime")
        ).one()
        self.migrate()
        self.session.expire_all()
        self.assertEqual(
            before,
            self.session.execute(
                text(
                    "SELECT anime_id, mal_id, title, genres, last_jikan_sync FROM anime"
                )
            ).one(),
        )
        self.assertEqual(
            self.session.scalar(text("SELECT count(*) FROM anime_genre")), 1
        )
        cursor = self.session.get(JikanSyncState, "bulk:catalogue:tv:v3")
        self.assertEqual(
            (cursor.next_page, cursor.last_attempt_at, cursor.last_error),
            (42, NOW, "provider timeout"),
        )
        self.assertEqual(
            list(
                self.session.scalars(
                    text(
                        "SELECT version FROM catalogue_schema_version ORDER BY version"
                    )
                )
            ),
            [6, 8],
        )
        self.assertEqual(
            self.session.scalar(text("SELECT count(*) FROM jikan_refresh_state")), 0
        )

    def test_version_seven_upgrade_preserves_refresh_state_catalogue_and_cursor(self):
        with patch.object(schema, "CATALOGUE_SCHEMA_VERSION", 7):
            self.migrate()
        self.session.execute(
            text("ALTER TABLE jikan_refresh_state DROP COLUMN refresh_tier")
        )
        self.session.execute(
            text("ALTER TABLE jikan_refresh_state DROP COLUMN failure_streak")
        )
        row = self.anime(last_jikan_sync=NOW)
        self.session.add(
            JikanSyncState(
                key="bulk:catalogue:tv:v3",
                next_page=17,
                last_attempt_at=NOW,
            )
        )
        self.session.flush()
        self.session.execute(
            text(
                "INSERT INTO jikan_refresh_state "
                "(kind, mal_id, queue, last_attempt_at, last_success_at, "
                "next_attempt_at, empty_streak, last_failure) VALUES "
                "('anime', :mal_id, 'detail', :now, :now, :now, 0, NULL)"
            ),
            {"mal_id": row.mal_id, "now": NOW},
        )
        self.session.commit()

        self.migrate()
        self.session.expire_all()
        state = self.session.get(JikanRefreshState, ("anime", row.mal_id, "detail"))
        self.assertEqual(state.last_success_at, NOW)
        self.assertIsNone(state.refresh_tier)
        self.assertEqual(state.failure_streak, 0)
        self.assertEqual(
            self.session.get(JikanSyncState, "bulk:catalogue:tv:v3").next_page,
            17,
        )
        self.assertEqual(self.session.get(Anime, row.animeID).title, "Existing")
        self.assertEqual(
            list(
                self.session.scalars(
                    text(
                        "SELECT version FROM catalogue_schema_version ORDER BY version"
                    )
                )
            ),
            [7, 8],
        )

    def client_factory(self, responder, requests):
        clock = FakeClock()

        def opener(request, **kwargs):
            # Unlike the SQLite test, disposal is real and checked on the server.
            self.assertFalse(self.session.registry.has())
            self.assertEqual(self.engine.pool.checkedout(), 0)
            with self.admin.connect() as connection:
                count = connection.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity WHERE application_name = :name"
                    ),
                    {"name": self.application_name},
                )
            self.assertEqual(
                count, 0, "ETL left a PostgreSQL connection open during HTTP"
            )
            requests.append(request.full_url)
            return Response(json.dumps(responder(request.full_url)).encode())

        def make_client(**kwargs):
            return JikanClient(
                opener=opener,
                clock=clock,
                sleeper=clock.sleep,
                base_url="https://provider.test/v1",
                fallback_base_url="",
                streaming_base_url="https://provider.test/v1",
                **kwargs,
            )

        return make_client

    def invoke_scheduled(
        self, responder, *, limit=6, budget=200, page_limit=1, batch_size=2
    ):
        requests = []
        argv = [
            "jikan_etl",
            "--scheduled-sync",
            "--limit",
            str(limit),
            "--streaming-limit",
            "1",
            "--page-limit",
            str(page_limit),
            "--batch-size",
            str(batch_size),
            "--request-budget",
            str(budget),
        ]
        with (
            patch.object(
                worker,
                "JikanClient",
                side_effect=self.client_factory(responder, requests),
            ),
            patch.object(worker, "report") as report,
            patch("sys.argv", argv),
        ):
            jikan_etl.main()
        return requests, report.call_args.args

    def test_actual_entry_point_commits_mixed_progress_without_daily_failure_loop(self):
        self.migrate()
        for mal_id in range(1, 7):
            self.anime(mal_id, synopsis="Keep known metadata")
        for mal_id, kind in ((100, "MANGA"), (101, "MANHWA")):
            self.session.add(
                Manga(
                    mal_id=mal_id,
                    content_type=kind,
                    title="Existing print",
                    status="Publishing",
                    is_adult=False,
                    mal_url="https://example.test",
                    image_url="",
                )
            )
        self.session.commit()
        calls = Counter()

        def responder(url):
            path = urlsplit(url).path
            calls[path] += 1
            if path in {"/v1/anime/3/full", "/v1/anime/5/full", "/v1/anime/5"}:
                raise HTTPError(url, 404 if "/3/" in path else 503, "fixture", {}, None)
            if path == "/v1/anime/4/full" and calls[path] == 1:
                raise HTTPError(url, 429, "fixture", {"Retry-After": "1"}, None)
            if path == "/v1/anime/6/full":
                raise HTTPError(url, 503, "fixture", {}, None)
            if path == "/v1/anime/2/full":
                return {"data": {"mal_id": 2, "genres": [None]}}
            if "/anime/" in path:
                mal_id = int(path.split("/")[3])
                return {
                    "data": {
                        "mal_id": mal_id,
                        "title": f"Updated {mal_id}",
                        "type": "TV",
                        "status": "Finished Airing",
                        "genres": [{"name": "Drama"}],
                        "studios": [],
                        "streaming": [],
                    }
                }
            if "/manga/" in path:
                mal_id = int(path.split("/")[3])
                return {
                    "data": {
                        "mal_id": mal_id,
                        "title": "Updated print",
                        "type": "Manga" if mal_id == 100 else "Manhwa",
                        "status": "Publishing",
                        "authors": [{"mal_id": 10, "name": "Same Author"}],
                        "genres": [{"name": "Drama"}],
                    }
                }
            return {"data": [], "pagination": {"has_next_page": False}}

        requests, (metrics, budget, outcome) = self.invoke_scheduled(responder)
        self.assertNotEqual(outcome, "failed")
        self.assertGreater(metrics.failed, 0)
        self.assertGreater(metrics.changed, 0)
        self.assertLessEqual(len(requests), 200)
        self.assertEqual(calls["/v1/anime/1/full"], 1)
        self.assertEqual(calls["/v1/anime/4/full"], 2)
        self.assertEqual(
            self.session.scalar(select(Anime.title).where(Anime.mal_id == 1)),
            "Updated 1",
        )
        self.assertEqual(
            self.session.scalar(select(Anime.synopsis).where(Anime.mal_id == 2)),
            "Keep known metadata",
        )
        for mal_id in (2, 3, 5, 6):
            state = self.session.get(JikanRefreshState, ("anime", mal_id, "detail"))
            self.assertIsNotNone(state.last_failure)
            self.assertIsNone(state.last_success_at)
            self.assertGreater(state.next_attempt_at, state.last_attempt_at)
        self.assertEqual(self.session.scalar(text("SELECT count(*) FROM author")), 1)
        self.assertEqual(
            self.session.scalar(text("SELECT count(*) FROM manga_author")), 2
        )
        self.assertGreater(
            self.session.scalar(text("SELECT count(*) FROM catalogue_facet")), 0
        )

    def test_budget_exhaustion_does_not_mark_unfetched_titles_successful(self):
        self.migrate()
        for mal_id in range(1, 26):
            self.anime(mal_id)

        def responder(url):
            path = urlsplit(url).path
            if "/anime/" in path:
                mal_id = int(path.split("/")[3])
                return {
                    "data": {
                        "mal_id": mal_id,
                        "title": "Fetched",
                        "status": "Finished Airing",
                        "genres": [],
                        "studios": [],
                        "streaming": [],
                    }
                }
            return {"data": [], "pagination": {"has_next_page": False}}

        requests, (metrics, budget, outcome) = self.invoke_scheduled(
            responder, limit=25, budget=40
        )
        self.assertNotEqual(outcome, "failed")
        self.assertGreater(metrics.deferred, 0)
        self.assertLessEqual(len(requests), 40)
        fetched = {
            int(urlsplit(url).path.split("/")[3])
            for url in requests
            if "/anime/" in url
        }
        recorded = set(
            self.session.scalars(
                select(JikanRefreshState.mal_id).where(
                    JikanRefreshState.queue == "detail"
                )
            )
        )
        self.assertEqual(recorded, fetched)
        self.assertLess(len(recorded), 25)

    def test_committed_page_survives_later_postgresql_error_and_retry_is_duplicate_safe(
        self,
    ):
        self.migrate()

        def page(mal_id, number):
            return {
                "kind": "anime_page",
                "provider_type": "tv",
                "key": "bulk:catalogue:tv:v3",
                "page": number,
                "result": {
                    "entries": [
                        {
                            "mal_id": mal_id,
                            "title": "Persisted",
                            "type": "TV",
                            "genres": [{"name": "Drama"}],
                            "studios": [{"mal_id": 1, "name": "Shared studio"}],
                            "streaming": [
                                {
                                    "name": "Crunchyroll",
                                    "url": "https://example.test/watch",
                                }
                            ],
                        }
                    ],
                    "page": number,
                    "has_next_page": True,
                },
            }

        first = page(991, 1)
        worker.apply_page(first, worker.SyncMetrics())

        def database_failure(
            connection, cursor, statement, parameters, context, executemany
        ):
            if statement.startswith("INSERT INTO anime "):
                cursor.execute("SELECT 1 / 0")

        event.listen(self.engine, "before_cursor_execute", database_failure)
        try:
            with self.assertRaises(Exception) as error:
                worker.apply_page(page(992, 2), worker.SyncMetrics())
            self.assertIn("division by zero", str(error.exception))
        finally:
            event.remove(self.engine, "before_cursor_execute", database_failure)
        self.session.remove()
        self.assertEqual(list(self.session.scalars(select(Anime.mal_id))), [991])
        self.assertEqual(self.session.get(JikanSyncState, first["key"]).next_page, 2)
        self.session.remove()
        worker.apply_page(page(991, 1), worker.SyncMetrics())
        worker.apply_page(page(992, 2), worker.SyncMetrics())
        self.assertEqual(self.session.scalar(text("SELECT count(*) FROM anime")), 2)
        self.assertEqual(self.session.scalar(text("SELECT count(*) FROM studio")), 1)
        self.assertEqual(
            self.session.scalar(text("SELECT count(*) FROM streaming_service")), 1
        )
        self.assertEqual(
            self.session.scalar(text("SELECT count(*) FROM anime_genre")), 2
        )
        self.assertEqual(
            self.session.scalar(
                select(AnimeStreamingService).join(Anime).where(Anime.mal_id == 991)
            ).url,
            "https://example.test/watch",
        )
        self.assertEqual(self.session.get(JikanSyncState, first["key"]).next_page, 3)

    def test_mixed_detail_and_streaming_batch_removes_pending_links_safely(self):
        self.migrate()
        self.anime(1)
        self.anime(2)
        for mal_id, replacement in (
            (1, []),
            (2, [{"name": "Replacement", "url": "https://example.test/replacement"}]),
        ):
            with self.subTest(mal_id=mal_id):
                records = [
                    {
                        "kind": "anime",
                        "queue": "detail",
                        "mal_id": mal_id,
                        "failure": None,
                        "data": {
                            "mal_id": mal_id,
                            "title": "Updated",
                            "status": "Finished Airing",
                            "genres": [],
                            "studios": [],
                            "streaming": [
                                {
                                    "name": "Incomplete",
                                    "url": "https://example.test/old",
                                },
                                None,
                            ],
                        },
                    },
                    {
                        "kind": "anime",
                        "queue": "streaming",
                        "mal_id": mal_id,
                        "failure": None,
                        "data": {"mal_id": mal_id, "streaming": replacement},
                    },
                ]
                worker.apply_details(
                    records, NOW, RefreshPolicy(), worker.SyncMetrics()
                )
                self.session.remove()
                links = list(
                    self.session.scalars(
                        select(AnimeStreamingService)
                        .join(Anime)
                        .where(Anime.mal_id == mal_id)
                    )
                )
                self.assertEqual(
                    [link.url for link in links],
                    [entry["url"] for entry in replacement],
                )
                # Replaying the exact batch exercises persisted relationships,
                # in addition to the first attempt's unflushed relationships.
                worker.apply_details(
                    records, NOW, RefreshPolicy(), worker.SyncMetrics()
                )
                self.session.remove()
                links = list(
                    self.session.scalars(
                        select(AnimeStreamingService)
                        .join(Anime)
                        .where(Anime.mal_id == mal_id)
                    )
                )
                self.assertEqual(
                    [link.url for link in links],
                    [entry["url"] for entry in replacement],
                )


if __name__ == "__main__":
    unittest.main()
