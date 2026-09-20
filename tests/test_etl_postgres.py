"""Opt-in ETL integration tests against a disposable, loopback PostgreSQL DB.

Set ETL_TEST_DATABASE_URL to a PostgreSQL URL whose host is 127.0.0.1,
localhost, or ::1 and whose database ends in ``_test``. Every test owns a new
random schema and drops only that schema. DATABASE_URL is never used as the
integration target; neither a production Neon URL nor provider HTTP is used.
"""

from collections import Counter
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from importlib import import_module
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
    from backend.jobs import ongoing_sync
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
    from backend.services.jikan_client import JikanClient, JikanTemporaryError
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

    @staticmethod
    def listing(mal_id=1, **changes):
        return {
            "mal_id": mal_id,
            "status": "Currently Airing",
            "score": 9.1,
            "popularity": 10,
            "members": 1000,
            "episodes": None,
            **changes,
        }

    def test_listing_commit_preserves_details_and_replay_is_duplicate_safe(
        self,
    ):
        self.migrate()
        self.anime(
            synopsis="Keep this", last_jikan_sync=NOW - timedelta(days=10)
        )
        page = {
            "kind": "ongoing_page",
            "media": "anime",
            "key": "ongoing:anime:v1",
            "page": 1,
            "result": {
                "entries": [self.listing(), self.listing()],
                "page": 1,
                "has_next_page": True,
            },
        }
        metrics = worker.SyncMetrics()
        worker.apply_page(page, metrics)
        row = self.session.scalar(select(Anime))
        self.assertEqual(
            (row.score, row.episodes, row.synopsis, row.genres_detailed),
            (9.1, 12, "Keep this", ["Drama"]),
        )
        original_change = row.last_jikan_sync
        self.assertIsNone(
            self.session.get(JikanRefreshState, ("anime", 1, "detail"))
        )
        self.assertIsNotNone(
            self.session.get(
                JikanRefreshState, ("anime", 1, "listing")
            ).last_success_at
        )
        worker.apply_page(page, metrics)
        self.assertEqual(
            self.session.scalar(select(Anime.last_jikan_sync)), original_change
        )
        self.assertEqual(
            self.session.scalar(
                text("SELECT count(*) FROM jikan_refresh_state")
            ),
            1,
        )
        self.assertIsNone(
            self.session.get(JikanSyncState, "catalogue_facets_dirty")
        )
        self.assertIsNotNone(
            self.session.get(JikanSyncState, "catalogue_cache_generation")
        )
        self.assertEqual(
            self.session.get(JikanSyncState, page["key"]).next_page, 2
        )
        with patch.object(
            self.session, "commit", side_effect=RuntimeError("interrupt")
        ):
            with self.assertRaises(RuntimeError):
                worker.apply_page(
                    {
                        **page,
                        "page": 2,
                        "result": {
                            "entries": [self.listing(score=7)],
                            "page": 2,
                            "has_next_page": False,
                        },
                    },
                    metrics,
                )
        self.assertEqual(self.session.scalar(select(Anime.score)), 9.1)
        self.assertEqual(
            self.session.get(JikanSyncState, page["key"]).next_page, 2
        )
        self.assertEqual(metrics.listing_by_media["anime"]["changed"], 1)

    def test_scalar_detail_and_discovery_commit_without_dirtying_facets(self):
        self.migrate()
        self.anime()
        metrics = worker.SyncMetrics()
        worker.apply_details(
            [
                {
                    "kind": "anime",
                    "queue": "detail",
                    "mal_id": 1,
                    "data": {"mal_id": 1, "score": 9},
                    "failure": None,
                }
            ],
            NOW,
            RefreshPolicy(),
            metrics,
        )
        self.assertIsNone(
            self.session.get(JikanSyncState, "catalogue_facets_dirty")
        )
        worker.apply_page(
            {
                "kind": "anime_page",
                "provider_type": "tv",
                "key": "scalar-page",
                "page": 1,
                "result": {
                    "entries": [{"mal_id": 1, "type": "TV", "score": 9.2}],
                    "page": 1,
                    "has_next_page": False,
                },
            },
            metrics,
        )
        self.assertIsNone(
            self.session.get(JikanSyncState, "catalogue_facets_dirty")
        )

    def test_listing_commit_is_visible_through_api_and_cache_generation(self):
        self.migrate()
        self.anime()
        api = import_module("backend.app")
        monitor = api.CacheGenerationMonitor()
        with (
            patch.object(api, "db", self.proxy),
            patch.object(api, "cache_generation_monitor", monitor),
        ):
            api.response_cache.clear()
            self.addCleanup(api.response_cache.clear)
            client = api.app.test_client()
            before = client.get("/api/v1/catalogue/ANIME/1")
            self.assertEqual(before.status_code, 200)
            self.assertEqual(before.json["item"]["score"], 8)
            metrics = worker.SyncMetrics()
            worker.apply_page(
                {
                    "kind": "ongoing_page",
                    "media": "anime",
                    "key": "ongoing:anime:v1",
                    "page": 1,
                    "result": {
                        "entries": [self.listing()],
                        "page": 1,
                        "last_visible_page": 2,
                        "has_next_page": True,
                    },
                },
                metrics,
            )
            monitor._next_check = 0
            after = client.get("/api/v1/catalogue/ANIME/1")
            self.assertEqual(after.status_code, 200)
            self.assertEqual(after.json["item"]["score"], 9.1)
            self.assertIsNotNone(after.json["item"]["last_listing_refresh"])
            self.assertIsNone(after.json["item"]["last_verified_refresh"])
            self.assertEqual(
                metrics.listing_by_media["anime"]["estimated_remaining_runs"], 1
            )

    def test_facet_repair_marker_survives_failure_and_clears_after_publication(
        self,
    ):
        self.migrate()
        self.anime()
        worker.apply_details(
            [
                {
                    "kind": "anime",
                    "queue": "detail",
                    "mal_id": 1,
                    "data": {"mal_id": 1, "genres": [{"name": "Action"}]},
                    "failure": None,
                }
            ],
            NOW,
            RefreshPolicy(),
            worker.SyncMetrics(),
        )
        self.session.remove()
        self.assertIsNotNone(
            self.session.get(JikanSyncState, "catalogue_facets_dirty")
        )
        schema.refresh_catalogue_facets()
        self.session.expire_all()
        self.assertIsNone(
            self.session.get(JikanSyncState, "catalogue_facets_dirty")
        )

    def test_real_entry_point_uses_listing_budget_for_all_media(self):
        self.migrate()
        self.anime()
        for mal_id, kind in ((2, "MANGA"), (3, "MANHWA")):
            self.session.add(
                Manga(
                    mal_id=mal_id,
                    content_type=kind,
                    title=kind,
                    status="Publishing",
                    mal_url="https://example.test",
                    image_url="",
                    legacy_genres=[],
                    genres_detailed=[],
                )
            )
        self.session.commit()

        def respond(url):
            if "status=airing" in url:
                entries = [self.listing()]
            elif "status=publishing" in url:
                manhwa = "type=manhwa" in url
                entries = [
                    self.listing(
                        3 if manhwa else 2,
                        status="Publishing",
                        type="Manhwa" if manhwa else "Manga",
                        chapters=99,
                        volumes=10,
                    )
                ]
            elif "/full" in url:
                mal_id = int(urlsplit(url).path.split("/")[-2])
                if "/anime/" in url:
                    return {
                        "data": {
                            **self.listing(mal_id),
                            "title": "Existing",
                            "genres": [],
                            "studios": [],
                            "streaming": [],
                        }
                    }
                return {
                    "data": {
                        **self.listing(mal_id),
                        "title": "Print",
                        "status": "Publishing",
                        "type": "Manga" if mal_id == 2 else "Manhwa",
                        "authors": [],
                        "genres": [],
                        "chapters": 99,
                        "volumes": 10,
                    }
                }
            else:
                entries = []
            return {"data": entries, "pagination": {"has_next_page": False}}

        requests, (metrics, budget, outcome) = self.invoke_scheduled(
            respond, limit=20
        )
        self.assertEqual(
            set(metrics.listing_by_media), {"anime", "manga", "manhwa"}
        )
        for kind in metrics.listing_by_media:
            self.assertEqual(metrics.listing_by_media[kind]["committed"], 1)
        self.assertLessEqual(budget.attempted, 200)
        self.assertNotEqual(outcome, "failed")
        self.assertEqual(sum("status=" in url for url in requests), 3)

    def test_listing_resume_validation_and_complete_pass_wait(self):
        self.migrate()
        self.anime()
        key = "ongoing:anime:v1"
        self.session.add(JikanSyncState(key=key, next_page=8))
        self.session.commit()
        planned = ongoing_sync.plan(
            self.session, "anime", 200, 10, NOW, RefreshPolicy()
        )
        self.assertEqual(planned["page"], 7)
        metrics = worker.SyncMetrics()
        bad = {
            **planned,
            "result": {
                "entries": [self.listing(score="bad")],
                "page": 7,
                "has_next_page": True,
            },
        }
        with self.assertRaises(JikanTemporaryError):
            worker.apply_page(bad, metrics)
        self.assertEqual(self.session.get(JikanSyncState, key).next_page, 8)
        for failure in ("not_found", "temporary"):
            worker.apply_page(
                {**planned, "page": 8, "failure": failure}, metrics
            )
            self.assertEqual(self.session.get(JikanSyncState, key).next_page, 8)
        worker.apply_page(
            {
                **planned,
                "page": 8,
                "result": {"entries": [], "page": 8, "has_next_page": False},
            },
            metrics,
        )
        cursor = self.session.get(JikanSyncState, key)
        self.assertEqual(cursor.next_page, 1)
        self.assertIsNone(
            ongoing_sync.plan(
                self.session,
                "anime",
                200,
                10,
                cursor.last_completed_at,
                RefreshPolicy(),
            )
        )
        self.assertEqual(
            ongoing_sync.plan(
                self.session,
                "anime",
                200,
                10,
                cursor.last_completed_at + timedelta(days=3),
                RefreshPolicy(),
            )["page"],
            1,
        )

    def test_postgresql_queue_fairness_over_repeated_partial_runs(self):
        self.migrate()
        for mal_id, days in ((1, 4), (900, 20), (999, 10)):
            row = self.anime(mal_id)
            row.status = "CURRENTLY_AIRING"
            self.session.add(
                JikanRefreshState(
                    kind="anime",
                    mal_id=mal_id,
                    queue="detail",
                    last_attempt_at=NOW - timedelta(days=days),
                    last_success_at=NOW - timedelta(days=days),
                    next_attempt_at=NOW - timedelta(days=1),
                )
            )
        self.session.commit()
        selected = []
        for offset in range(3):
            now = NOW + timedelta(days=offset)
            work, _, _ = worker.plan_details(
                "anime", "detail", 3, now, RefreshPolicy()
            )
            first = work[0]
            selected.append(first["mal_id"])
            # Only the first request fitted in the remaining HTTP budget.
            # Its failure advances attempt ordering but never success.
            worker.apply_details(
                [{**first, "data": None, "failure": "temporary"}],
                now,
                RefreshPolicy(),
                worker.SyncMetrics(),
            )
        self.assertEqual(selected, [900, 999, 1])

    def test_large_backlogs_keep_media_and_active_allocations_bounded(self):
        self.migrate()
        self.session.execute(
            text("""
            INSERT INTO anime (mal_id, title, status, is_adult, year,
                               type, mal_url, sequel, image_url, genres, genres_detailed)
            SELECT id, 'Fixture', CASE WHEN id > 10000
                THEN 'CURRENTLY_AIRING' ELSE 'FINISHED_AIRING' END, false, 2000,
                'TV', '', false, '', '{}', '{}'
            FROM generate_series(1, 10274) AS id
        """)
        )
        self.session.execute(
            text("""
            INSERT INTO manga (mal_id, title, content_type, status, is_adult,
                               mal_url, image_url)
            SELECT id, 'Fixture', CASE WHEN id <= 11000 THEN 'MANGA' ELSE 'MANHWA' END,
                CASE WHEN id <= 8000 OR id BETWEEN 11001 AND 12572
                THEN 'Publishing' ELSE 'Finished' END, false, '', ''
            FROM generate_series(1, 15000) AS id
        """)
        )
        self.session.commit()
        for kind, cap, active in (
            ("anime", 190, 274),
            ("manga", 90, 8000),
            ("manhwa", 90, 1572),
        ):
            with self.subTest(kind=kind):
                work, candidates, deferred = worker.plan_details(
                    kind, "detail", cap, NOW, RefreshPolicy()
                )
                self.assertEqual(len(work), cap)
                self.assertTrue(all(item["kind"] == kind for item in work))
                self.assertEqual(
                    sum(item["tier"] == "active" for item in work), cap * 4 // 5
                )
                self.assertEqual(candidates["active"], active)
                self.assertGreater(sum(deferred.values()), 1000)
                self.assertEqual(
                    worker.active_refresh_health(kind, NOW, "listing"),
                    (active, active, None),
                )

    def test_version_six_upgrade_preserves_catalogue_relationships_and_cursor(
        self,
    ):
        # 5341b50 -> 066fa3b changed only the schema version and introduced
        # JikanRefreshState. Build the pre-refactor tables with the unchanged
        # additive migration, excluding precisely that new table.
        old_tables = [
            table
            for table in db.metadata.sorted_tables
            if table.name != "jikan_refresh_state"
        ]
        old_metadata = SimpleNamespace(
            create_all=lambda bind: db.metadata.create_all(
                bind, tables=old_tables
            )
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
