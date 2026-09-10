"""Isolated persistence and scheduling tests; never require a live provider/DB."""

from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import io
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from sqlalchemy import JSON, create_engine, event, select
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import scoped_session, sessionmaker

os.environ.setdefault("DATABASE_URL", "sqlite://")
from backend.jobs import efficient_sync as worker, jikan_etl, manga_etl
from backend.jobs.refresh_policy import RefreshPolicy, next_streaming_check, utc
from backend.models import (
    Anime,
    AnimeStreamingService,
    Genre,
    JikanRefreshState,
    JikanSyncState,
    Manga,
    MangaGenre,
    StreamingService,
    db,
)
from backend.services.jikan_budget import (
    RequestBudget,
    RequestBudgetExhausted,
    ResponseCache,
)
from backend.services.jikan_client import JikanClient, JikanTemporaryError
from tests.test_jikan_client import FakeClock, Response


NOW = datetime(2026, 9, 6, 12, tzinfo=timezone.utc)


def queues(**kwargs):
    return dict(
        anime=kwargs.get("anime", []),
        manga=kwargs.get("manga", []),
        manhwa=kwargs.get("manhwa", []),
        streaming=kwargs.get("streaming", []),
    )


def item(mal_id=1, kind="anime", queue="detail", data=None, failure=None):
    return dict(kind=kind, queue=queue, mal_id=mal_id, data=data, failure=failure)


def full_detail(mal_id=1, **kwargs):
    return dict(
        mal_id=mal_id,
        title="Example",
        status="Finished Airing",
        genres=[],
        studios=[],
        **kwargs,
    )


class PolicyTests(unittest.TestCase):
    def test_status_and_actual_end_date_determine_freshness(self):
        policy = RefreshPolicy()
        for status in ("Currently Airing", "CURRENTLY_AIRING", "Publishing"):
            self.assertEqual(policy.detail_days({"status": status}, NOW), 3)
        for status in (None, "Not yet aired", "On Hiatus", "Finished"):
            self.assertEqual(policy.detail_days({"status": status}, NOW), 7)
        self.assertEqual(
            policy.detail_days(
                {
                    "status": "Finished Airing",
                    "aired": {"to": (NOW - timedelta(days=30)).isoformat()},
                },
                NOW,
            ),
            7,
        )
        self.assertEqual(
            policy.detail_days(
                {"status": "Finished", "published": {"to": "2010-01-01T00:00:00Z"}}, NOW
            ),
            60,
        )
        self.assertEqual(
            policy.detail_days(
                {"status": "Finished", "published": {"from": "1990-01-01"}}, NOW
            ),
            7,
        )

    def test_empty_backoff_and_transient_failures_are_distinct(self):
        streak = 0
        for days in (7, 30, 90, 90):
            streak, due = next_streaming_check(
                streak, empty=True, failed=False, now=NOW, policy=RefreshPolicy()
            )
            self.assertEqual(due, NOW + timedelta(days=days))
        self.assertEqual(
            next_streaming_check(
                streak, empty=False, failed=True, now=NOW, policy=RefreshPolicy()
            ),
            (3, NOW + timedelta(days=1)),
        )
        self.assertEqual(
            next_streaming_check(
                streak, empty=False, failed=False, now=NOW, policy=RefreshPolicy()
            )[0],
            0,
        )

    def test_invalid_intervals_rejected(self):
        with self.assertRaises(ValueError):
            RefreshPolicy(stable_days=0)


class FetchTests(unittest.TestCase):
    def test_exact_url_is_cached_but_different_provider_is_not(self):
        budget = RequestBudget(4)
        clock = FakeClock()
        opener = Mock(side_effect=lambda *a, **k: Response(b'{"data":{"mal_id":1}}'))
        client = JikanClient(
            opener=opener,
            clock=clock,
            sleeper=clock.sleep,
            base_url="https://example.test/v1",
            fallback_base_url="",
            budget=budget,
            response_cache=ResponseCache(io.StringIO()),
        )
        client.get_anime_full(1)
        client.get_anime_streaming(1)
        self.assertEqual(opener.call_count, 1)
        self.assertEqual(budget.avoided, 1)
        client._streaming_base_url = "https://second.test/v4"
        client.get_anime_streaming(1)
        self.assertEqual(opener.call_count, 2)

    def test_partial_streaming_from_detail_does_not_suppress_valid_request(self):
        client = Mock()
        client.get_anime_full.return_value = {
            "data": {"mal_id": 1, "streaming": [None]}
        }
        client.get_anime_streaming.return_value = {
            "data": {"mal_id": 1, "streaming": []}
        }
        worker.fetch_work(
            client,
            RequestBudget(10),
            [],
            queues(anime=[item()], streaming=[item(queue="streaming")]),
            io.StringIO(),
            worker.SyncMetrics(),
        )
        client.get_anime_streaming.assert_called_once_with(1)

    def test_budget_counts_retries_and_prevents_request_after_cap(self):
        budget = RequestBudget(2)
        clock = FakeClock()
        opener = Mock(
            side_effect=HTTPError("https://example.test", 503, "outage", {}, None)
        )
        client = JikanClient(
            opener=opener,
            clock=clock,
            sleeper=clock.sleep,
            fallback_base_url="",
            budget=budget,
        )
        with self.assertRaises(RequestBudgetExhausted):
            client.get_anime_full(1)
        self.assertEqual(opener.call_count, 2)
        self.assertEqual(
            (budget.attempted, budget.failed, budget.successful), (2, 2, 0)
        )

    def test_malformed_json_counts_as_http_failure(self):
        budget = RequestBudget(4)
        client = JikanClient(
            opener=lambda *a, **k: Response(b"no json"),
            budget=budget,
            fallback_base_url="",
        )
        with self.assertRaises(JikanTemporaryError):
            client.get_anime(1)
        self.assertEqual((budget.attempted, budget.failed), (1, 1))

    def test_detail_streaming_payload_is_reused(self):
        client = Mock()
        client.get_anime_full.return_value = {"data": {"mal_id": 1, "streaming": []}}
        budget, metrics, spool = RequestBudget(10), worker.SyncMetrics(), io.StringIO()
        worker.fetch_work(
            client,
            budget,
            [],
            queues(anime=[item()], streaming=[item(queue="streaming")]),
            spool,
            metrics,
        )
        client.get_anime_streaming.assert_not_called()
        self.assertEqual(budget.avoided, 1)
        self.assertEqual(len(spool.getvalue().splitlines()), 2)

    def test_sparse_detail_does_not_suppress_streaming_fetch(self):
        client = Mock()
        client.get_anime_full.return_value = {"data": {"mal_id": 1}}
        client.get_anime_streaming.return_value = {
            "data": {"mal_id": 1, "streaming": []}
        }
        worker.fetch_work(
            client,
            RequestBudget(10),
            [],
            queues(anime=[item()], streaming=[item(queue="streaming")]),
            io.StringIO(),
            worker.SyncMetrics(),
        )
        client.get_anime_streaming.assert_called_once_with(1)

    def test_one_exhausted_lane_does_not_block_other_lanes(self):
        client = Mock()
        client.get_anime_full.side_effect = RequestBudgetExhausted()
        client.get_manga_full.return_value = {"data": {"mal_id": 2}}
        metrics, spool = worker.SyncMetrics(), io.StringIO()
        worker.fetch_work(
            client,
            RequestBudget(10),
            [],
            queues(anime=[item(), item(3)], manga=[item(2, "manga")]),
            spool,
            metrics,
        )
        self.assertEqual(metrics.deferred, 2)
        self.assertEqual(json.loads(spool.getvalue())["mal_id"], 2)

    def test_wrong_title_id_is_rejected(self):
        client = Mock()
        client.get_anime_full.return_value = {"data": {"mal_id": 99}}
        self.assertEqual(
            worker._fetch_record(client, item()), (None, "invalid_payload")
        )


class PersistenceTests(unittest.TestCase):
    def test_offline_pipeline_through_real_client_and_database_batches(self):
        self.anime()
        clock = FakeClock()
        calls = []

        def opener(request, **kwargs):
            self.assertFalse(self.session.registry.has())
            calls.append(request.full_url)
            if "/anime/1/full" in request.full_url:
                payload = {"data": {"mal_id": 1, "title": "Updated", "streaming": []}}
            else:
                payload = {"data": [], "pagination": {"has_next_page": False}}
            return Response(json.dumps(payload).encode())

        def make_client(**kwargs):
            return JikanClient(
                opener=opener,
                clock=clock,
                sleeper=clock.sleep,
                base_url="https://provider.test/v1",
                fallback_base_url="",
                **kwargs,
            )

        with (
            patch.object(worker, "JikanClient", side_effect=make_client),
            patch.object(jikan_etl, "_ensure_schema"),
            patch.object(jikan_etl, "remove_hentai_anime", return_value=0),
            patch.object(manga_etl, "remove_adult_manga", return_value=0),
            patch.object(jikan_etl, "_refresh_and_report_catalogue_facets") as publish,
            patch.object(self.engine, "dispose"),
            patch.object(worker, "report") as report,
        ):
            result = worker.run(
                limit=2, streaming_limit=1, page_limit=1, request_budget=40
            )
        self.assertEqual(len(calls), 11)
        self.assertEqual(result.pages_applied, 10)
        self.assertEqual(self.session.scalar(select(Anime.title)), "Updated")
        self.assertEqual(report.call_args.args[1].avoided, 1)
        publish.assert_called_once()

    def test_mixed_malformed_adult_classification_is_still_excluded(self):
        self.anime()
        worker.apply_details(
            [item(data={"mal_id": 1, "genres": [None, {"name": "Hentai"}]})],
            NOW,
            RefreshPolicy(),
            self.metrics,
        )
        self.assertEqual(list(self.session.scalars(select(Anime))), [])
        self.assertEqual(self.metrics.removed, 1)

    def test_provider_capped_page_finishes_atomically_and_waits_before_restart(self):
        key = manga_etl.MANGA_STATE_KEYS["manga"]
        page = {
            "kind": "manga_page",
            "provider_type": "manga",
            "key": key,
            "page": 1000,
            "result": {"entries": [], "page": 1000, "has_next_page": True},
        }
        worker.apply_page(page, self.metrics)
        state = self.session.get(JikanSyncState, key)
        self.assertEqual(state.next_page, 1)
        self.assertIsNotNone(state.last_completed_at)
        self.assertEqual(self.metrics.provider_caps, [key])
        self.assertNotIn(
            key,
            [
                p["key"]
                for p in worker.plan_pages(
                    utc(state.last_completed_at), 10, RefreshPolicy()
                )
            ],
        )

    def setUp(self):
        self.stack = ExitStack()
        # SQLite verifies transactions and ORM queries. Only the test engine's
        # array storage differs; PostgreSQL DDL is covered by model/schema tests.
        columns = [
            (column, column.type)
            for table in db.metadata.tables.values()
            for column in table.columns
            if isinstance(column.type, ARRAY)
        ]
        for column, _ in columns:
            column.type = JSON()
        self.stack.callback(
            lambda: [setattr(column, "type", original) for column, original in columns]
        )
        self.engine = create_engine("sqlite://")
        db.metadata.create_all(self.engine)
        self.session = scoped_session(sessionmaker(bind=self.engine))
        self.stack.callback(self.engine.dispose)
        self.stack.callback(self.session.remove)
        proxy = SimpleNamespace(session=self.session, engine=self.engine)
        for module in (worker, jikan_etl, manga_etl):
            self.stack.enter_context(patch.object(module, "db", proxy))
        self.addCleanup(self.stack.close)
        self.metrics = worker.SyncMetrics()

    def anime(self, mal_id=1, status="FINISHED_AIRING", **kwargs):
        row = Anime(
            mal_id=mal_id,
            title="Example",
            type="TV",
            season="summer",
            status=status,
            year=2000,
            score=8,
            episodes=12,
            is_adult=False,
            mal_url="https://example.test",
            sequel=False,
            image_url="",
            legacy_genres=[],
            genres_detailed=[],
            **kwargs,
        )
        self.session.add(row)
        self.session.commit()
        return row

    def state(self, mal_id=1, queue="detail", **kwargs):
        row = JikanRefreshState(
            kind="anime", mal_id=mal_id, queue=queue, empty_streak=0, **kwargs
        )
        self.session.add(row)
        self.session.commit()
        return row

    def test_due_selection_preserves_never_overdue_active_and_archive_slots(self):
        for mal_id in range(1, 9):
            self.anime(mal_id, "CURRENTLY_AIRING" if mal_id <= 4 else "FINISHED_AIRING")
        for mal_id in (3, 4, 7, 8):
            self.state(
                mal_id,
                last_attempt_at=NOW - timedelta(days=100),
                next_attempt_at=NOW - timedelta(days=1),
            )
        work, deferred = worker.plan_details("anime", "detail", 4, NOW, RefreshPolicy())
        self.assertEqual([record["mal_id"] for record in work], [1, 5, 3, 7])
        self.assertEqual(deferred, 4)

    def test_listing_does_not_postpone_detail_and_newly_active_title_is_accelerated(
        self,
    ):
        self.anime(1, last_jikan_sync=NOW)
        self.anime(2, "CURRENTLY_AIRING")
        self.state(
            2,
            last_attempt_at=NOW - timedelta(days=4),
            next_attempt_at=NOW + timedelta(days=55),
        )
        self.anime(3)
        self.state(3, next_attempt_at=NOW + timedelta(days=10))
        work, _ = worker.plan_details("anime", "detail", 10, NOW, RefreshPolicy())
        self.assertEqual({record["mal_id"] for record in work}, {1, 2})

    def test_planner_selects_ids_without_synopsis_or_relationship_objects(self):
        statement, _ = worker._due_statement("anime", "detail", NOW, RefreshPolicy())
        projection = str(statement).split("FROM")[0]
        self.assertNotIn("synopsis", projection)
        self.assertNotIn("genres", projection)

    def test_manga_sparse_genres_preserved_and_detail_freshness_is_separate(self):
        row = Manga(
            mal_id=10,
            content_type="MANHWA",
            title="Example",
            is_adult=False,
            status="Publishing",
            mal_url="https://example.test",
            image_url="",
            legacy_genres=["Drama"],
            genres_detailed=["Drama"],
        )
        row.genre_links.append(MangaGenre(genre=Genre(name="Drama")))
        self.session.add(row)
        self.session.commit()
        worker.apply_details(
            [
                item(
                    10,
                    "manhwa",
                    data={"mal_id": 10, "genres": [None], "status": "Publishing"},
                )
            ],
            NOW,
            RefreshPolicy(),
            self.metrics,
        )
        self.assertEqual([link.genre.name for link in row.genre_links], ["Drama"])
        state = self.session.get(JikanRefreshState, ("manhwa", 10, "detail"))
        self.assertEqual(utc(state.next_attempt_at), NOW + timedelta(days=1))
        self.assertEqual(state.last_failure, "incomplete_detail")
        self.assertIsNone(state.last_success_at)

    def test_streaming_projection_omits_synopsis(self):
        self.anime()
        statements = []
        event.listen(
            self.engine,
            "before_cursor_execute",
            lambda con, cur, sql, *args: statements.append(sql),
        )
        worker.apply_details(
            [item(queue="streaming", data={"mal_id": 1, "streaming": []})],
            NOW,
            RefreshPolicy(),
            self.metrics,
        )
        reads = [
            sql
            for sql in statements
            if sql.startswith("SELECT") and "FROM anime " in sql
        ]
        self.assertTrue(reads)
        self.assertFalse(any("synopsis" in sql for sql in reads))

    def test_detail_and_streaming_same_run_count_empty_response_once(self):
        self.anime()
        data = {"mal_id": 1, "streaming": []}
        worker.apply_details(
            [item(data=data), item(queue="streaming", data=data)],
            NOW,
            RefreshPolicy(),
            self.metrics,
        )
        state = self.session.get(JikanRefreshState, ("anime", 1, "streaming"))
        self.assertEqual(state.empty_streak, 1)

    def test_provider_failure_makes_run_fail_after_saving_retry_state(self):
        self.anime()

        def fetch(client, budget, pages, work, spool, metrics):
            spool.write(json.dumps(item(failure="temporary")) + "\n")

        with (
            patch.object(worker, "fetch_work", side_effect=fetch),
            patch.object(jikan_etl, "_ensure_schema"),
            patch.object(jikan_etl, "remove_hentai_anime", return_value=0),
            patch.object(manga_etl, "remove_adult_manga", return_value=0),
            patch.object(self.engine, "dispose"),
            patch.object(worker, "report") as report,
        ):
            with self.assertRaisesRegex(RuntimeError, "requests failed"):
                worker.run()
        self.assertEqual(report.call_args.args[2], "failed")
        self.assertIsNotNone(
            self.session.get(JikanRefreshState, ("anime", 1, "detail"))
        )

    def test_committed_progress_with_recoverable_items_does_not_fail_cadence(self):
        """Sept 8/9 live runs committed pages, then failed on ordinary item errors."""
        self.anime()

        def fetch(client, budget, pages, work, spool, metrics):
            spool.write(
                json.dumps(
                    {
                        "kind": "anime_page",
                        "key": jikan_etl.BULK_SEASON_STATE_KEY,
                        "provider_type": "tv",
                        "page": 4,
                        "result": {
                            "entries": [
                                {"mal_id": 1, "title": "Updated", "type": "TV"}
                            ],
                            "page": 4,
                            "has_next_page": True,
                        },
                    }
                )
                + "\n"
            )
            for mal_id, failure in enumerate(
                ("not_found", "temporary", "invalid_payload"), 1
            ):
                spool.write(
                    json.dumps(item(mal_id, queue="streaming", failure=failure)) + "\n"
                )

        with (
            patch.object(worker, "fetch_work", side_effect=fetch),
            patch.object(jikan_etl, "_ensure_schema"),
            patch.object(jikan_etl, "remove_hentai_anime", return_value=0),
            patch.object(manga_etl, "remove_adult_manga", return_value=0),
            patch.object(jikan_etl, "_refresh_and_report_catalogue_facets"),
            patch.object(self.engine, "dispose"),
            patch.object(worker, "report") as report,
        ):
            result = worker.run()
        self.assertEqual(report.call_args.args[2], "success_with_warnings")
        self.assertEqual(result.pages_applied, 1)
        self.assertEqual(result.failed, 3)
        self.assertEqual(
            result.failure_reasons,
            {"not_found": 1, "temporary": 1, "invalid_payload": 1},
        )
        self.assertEqual(self.session.scalar(select(Anime.title)), "Updated")
        self.assertEqual(
            self.session.get(JikanSyncState, jikan_etl.BULK_SEASON_STATE_KEY).next_page,
            5,
        )

    def test_budget_stop_is_successful_without_marking_deferred_titles_attempted(self):
        self.anime()

        def fetch(client, budget, pages, work, spool, metrics):
            budget.exhausted.add("anime")
            metrics.deferred += 1

        with (
            patch.object(worker, "fetch_work", side_effect=fetch),
            patch.object(jikan_etl, "_ensure_schema"),
            patch.object(jikan_etl, "remove_hentai_anime", return_value=0),
            patch.object(manga_etl, "remove_adult_manga", return_value=0),
            patch.object(self.engine, "dispose"),
            patch.object(worker, "report") as report,
        ):
            worker.run()
        self.assertEqual(report.call_args.args[2], "bounded_success")
        self.assertIsNone(self.session.get(JikanRefreshState, ("anime", 1, "detail")))

    def test_total_outage_exhausting_budget_is_not_a_success(self):
        self.anime()

        def fetch(client, budget, pages, work, spool, metrics):
            budget.exhausted.add("anime")
            budget.attempted = budget.failed = 40

        with (
            patch.object(worker, "fetch_work", side_effect=fetch),
            patch.object(jikan_etl, "_ensure_schema"),
            patch.object(jikan_etl, "remove_hentai_anime", return_value=0),
            patch.object(manga_etl, "remove_adult_manga", return_value=0),
            patch.object(self.engine, "dispose"),
            patch.object(worker, "report") as report,
        ):
            with self.assertRaisesRegex(RuntimeError, "without verified progress"):
                worker.run()
        self.assertEqual(report.call_args.args[2], "failed")
        self.assertIsNone(self.session.get(JikanRefreshState, ("anime", 1, "detail")))

    def test_sparse_full_payload_does_not_claim_detail_success(self):
        row = self.anime()
        worker.apply_details(
            [item(data={"mal_id": 1})], NOW, RefreshPolicy(), self.metrics
        )
        state = self.session.get(JikanRefreshState, ("anime", 1, "detail"))
        self.assertIsNone(state.last_success_at)
        self.assertEqual(state.last_failure, "incomplete_detail")
        self.assertEqual(row.title, "Example")
        self.assertEqual(self.metrics.records_succeeded, 0)

    def test_complete_detail_without_streaming_completes_only_detail_queue(self):
        self.anime()
        worker.apply_details(
            [item(data=full_detail())], NOW, RefreshPolicy(), self.metrics
        )
        state = self.session.get(JikanRefreshState, ("anime", 1, "detail"))
        self.assertEqual(utc(state.last_success_at), NOW)
        self.assertIsNone(state.last_failure)
        self.assertEqual(self.metrics.records_succeeded, 1)
        self.assertIsNone(
            self.session.get(JikanRefreshState, ("anime", 1, "streaming"))
        )

    def test_empty_streaming_backoff_persists_and_filters_queue(self):
        self.anime()
        record = item(queue="streaming", data={"mal_id": 1, "streaming": []})
        checked_at = NOW
        for days in (7, 30, 90):
            worker.apply_details([record], checked_at, RefreshPolicy(), self.metrics)
            state = self.session.get(JikanRefreshState, ("anime", 1, "streaming"))
            self.assertEqual(
                utc(state.next_attempt_at), checked_at + timedelta(days=days)
            )
            checked_at += timedelta(days=days)
        work, _ = worker.plan_details("anime", "streaming", 100, NOW, RefreshPolicy())
        self.assertEqual(work, [])

    def test_malformed_streaming_preserves_links_and_retries_soon(self):
        row = self.anime()
        row.streaming_links.append(
            AnimeStreamingService(
                streaming_service=StreamingService(
                    name="Example", normalized_name="example"
                ),
                url="https://example.test/watch",
            )
        )
        self.session.commit()
        worker.apply_details(
            [item(queue="streaming", data={"mal_id": 1, "streaming": [None]})],
            NOW,
            RefreshPolicy(),
            self.metrics,
        )
        self.assertEqual(len(row.streaming_links), 1)
        state = self.session.get(JikanRefreshState, ("anime", 1, "streaming"))
        self.assertEqual(state.last_failure, "incomplete_streaming")
        self.assertIsNone(state.last_success_at)

    def test_sparse_fallback_keeps_links_and_is_not_a_full_detail_success(self):
        row = self.anime()
        row.streaming_links.append(
            AnimeStreamingService(
                streaming_service=StreamingService(
                    name="Example", normalized_name="example"
                ),
                url="https://example.test/watch",
            )
        )
        self.session.commit()
        worker.apply_details(
            [item(data={"mal_id": 1, "title": "New", "_etl_basic_fallback": True})],
            NOW,
            RefreshPolicy(),
            self.metrics,
        )
        self.assertEqual(row.title, "New")
        self.assertEqual(len(row.streaming_links), 1)
        state = self.session.get(JikanRefreshState, ("anime", 1, "detail"))
        self.assertIsNone(state.last_success_at)
        self.assertEqual(utc(state.next_attempt_at), NOW + timedelta(days=1))

    def test_unchanged_detail_does_not_rewrite_catalogue_timestamp(self):
        row = self.anime(last_jikan_sync=NOW - timedelta(days=10))
        original = row.last_jikan_sync
        statements = []
        event.listen(
            self.engine,
            "before_cursor_execute",
            lambda con, cur, sql, *args: statements.append(sql),
        )
        worker.apply_details(
            [item(data={"mal_id": 1, "title": "Example"})],
            NOW,
            RefreshPolicy(),
            self.metrics,
        )
        self.assertEqual((self.metrics.changed, self.metrics.unchanged), (0, 1))
        self.assertEqual(row.last_jikan_sync, original)
        self.assertFalse(any(sql.startswith("UPDATE anime ") for sql in statements))

    def test_apply_failure_rolls_back_metadata_and_due_state(self):
        row = self.anime()
        with patch.object(
            self.session, "commit", side_effect=RuntimeError("lost connection")
        ):
            with self.assertRaises(RuntimeError):
                worker.apply_details(
                    [item(data={"mal_id": 1, "title": "Must roll back"})],
                    NOW,
                    RefreshPolicy(),
                    self.metrics,
                )
        self.assertEqual(row.title, "Example")
        self.assertIsNone(self.session.get(JikanRefreshState, ("anime", 1, "detail")))
        self.assertEqual(self.metrics.changed, 0)

    def test_failed_attempt_advances_retry_queue_without_catalogue_reads(self):
        self.anime()
        with patch.object(jikan_etl, "_update_anime") as update:
            worker.apply_details(
                [item(failure="temporary")], NOW, RefreshPolicy(), self.metrics
            )
        update.assert_not_called()
        state = self.session.get(JikanRefreshState, ("anime", 1, "detail"))
        self.assertEqual(utc(state.next_attempt_at), NOW + timedelta(days=1))
        self.assertIsNone(state.last_success_at)

    def test_page_commit_and_cursor_are_atomic_and_replay_is_idempotent(self):
        page = {
            "kind": "anime_page",
            "key": jikan_etl.BULK_SEASON_STATE_KEY,
            "provider_type": "tv",
            "page": 4,
            "result": {
                "entries": [{"mal_id": 1, "title": "Example", "type": "TV"}],
                "page": 4,
                "has_next_page": True,
            },
        }
        with patch.object(
            self.session, "commit", side_effect=RuntimeError("disk failure")
        ):
            with self.assertRaises(RuntimeError):
                worker.apply_page(page, self.metrics)
        self.session.rollback()
        self.assertIsNone(self.session.get(JikanSyncState, page["key"]))
        self.assertEqual(list(self.session.scalars(select(Anime))), [])
        worker.apply_page(page, self.metrics)
        worker.apply_page(page, self.metrics)
        self.assertEqual(len(list(self.session.scalars(select(Anime)))), 1)
        self.assertEqual(self.session.get(JikanSyncState, page["key"]).next_page, 5)
        self.assertEqual((self.metrics.changed, self.metrics.unchanged), (1, 1))

    def test_completed_catalogue_waits_but_partial_cursor_resumes(self):
        self.session.add(
            JikanSyncState(
                key=jikan_etl.BULK_SEASON_STATE_KEY, next_page=1, last_completed_at=NOW
            )
        )
        self.session.add(
            JikanSyncState(
                key=jikan_etl.SUPPLEMENTAL_STATE_KEYS["movie"],
                next_page=4,
                last_completed_at=NOW,
            )
        )
        self.session.commit()
        plans = worker.plan_pages(NOW, 10, RefreshPolicy())
        self.assertNotIn(jikan_etl.BULK_SEASON_STATE_KEY, [p["key"] for p in plans])
        self.assertIn(
            4, [p["page"] for p in plans if p.get("provider_type") == "movie"]
        )

    def test_fetch_stage_has_no_database_session_or_queries(self):
        self.anime()

        def fetch(client, budget, pages, work, spool, metrics):
            self.assertFalse(self.session.registry.has())
            budget.attempted = budget.successful = 1
            spool.write(
                json.dumps(item(data={**full_detail(), "title": "Updated"})) + "\n"
            )

        with (
            patch.object(worker, "fetch_work", side_effect=fetch),
            patch.object(jikan_etl, "_ensure_schema"),
            patch.object(jikan_etl, "remove_hentai_anime", return_value=0),
            patch.object(manga_etl, "remove_adult_manga", return_value=0),
            patch.object(jikan_etl, "_refresh_and_report_catalogue_facets"),
            patch.object(self.engine, "dispose"),
            patch.object(worker, "report"),
        ):
            metrics = worker.run()
        self.assertEqual(metrics.changed, 1)


if __name__ == "__main__":
    unittest.main()
