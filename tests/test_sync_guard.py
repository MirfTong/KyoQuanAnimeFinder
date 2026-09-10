import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from backend.jobs.sync_guard import (
    _jobs_for_run,
    _successful_etl_completion,
    _successful_scheduled_runs,
    _successful_scheduled_etl_runs,
    main,
    should_run_scheduled_sync,
)


class SyncGuardTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 8, 14, 12, tzinfo=timezone.utc)

    def test_first_scheduled_run_is_allowed(self):
        should_run, _reason = should_run_scheduled_sync(
            [], now=self.now, minimum_hours=72
        )

        self.assertTrue(should_run)

    def test_no_previous_real_etl_is_allowed(self):
        guard_only_run = {
            "id": 100,
            "run_started_at": (self.now - timedelta(hours=24)).isoformat(),
        }
        skipped_job = {
            "steps": [
                {
                    "name": "Sync Anime, Manga, and Manhwa",
                    "conclusion": "skipped",
                    "completed_at": None,
                }
            ]
        }

        with (
            patch(
                "backend.jobs.sync_guard._successful_scheduled_runs",
                return_value=([guard_only_run], 1),
            ),
            patch("backend.jobs.sync_guard._jobs_for_run", return_value=[skipped_job]),
        ):
            verified_runs = _successful_scheduled_etl_runs(
                repository="owner/repo", workflow="jikan-sync.yml", token="token"
            )

        should_run, _reason = should_run_scheduled_sync(
            verified_runs, now=self.now, minimum_hours=72
        )
        self.assertTrue(should_run)

    def test_recent_success_is_skipped(self):
        should_run, reason = should_run_scheduled_sync(
            [{"run_started_at": (self.now - timedelta(hours=71)).isoformat()}],
            now=self.now,
            minimum_hours=72,
        )

        self.assertFalse(should_run)
        self.assertIn("next run is allowed", reason)

    def test_success_at_exact_interval_is_allowed(self):
        should_run, _reason = should_run_scheduled_sync(
            [{"run_started_at": (self.now - timedelta(hours=72)).isoformat()}],
            now=self.now,
            minimum_hours=72,
        )

        self.assertTrue(should_run)

    def test_manual_dispatch_bypasses_github_api_lookup(self):
        with (
            patch.dict(
                "os.environ",
                {"GITHUB_EVENT_NAME": "workflow_dispatch"},
                clear=True,
            ),
            patch("sys.argv", ["sync_guard", "--workflow", "jikan-sync.yml"]),
            patch("backend.jobs.sync_guard._successful_scheduled_runs") as lookup,
            patch("backend.jobs.sync_guard._write_output") as output,
            patch("backend.jobs.sync_guard._write_summary"),
        ):
            main()

        lookup.assert_not_called()
        output.assert_called_once_with("should_run", "true")

    def test_successful_guard_only_runs_do_not_delay_the_next_etl(self):
        runs = [
            {
                "id": run_id,
                "run_started_at": (self.now - timedelta(hours=hours_ago)).isoformat(),
            }
            for run_id, hours_ago in ((300, 24), (200, 48), (100, 73))
        ]
        skipped_step = {
            "steps": [
                {
                    "name": "Sync Anime, Manga, and Manhwa",
                    "conclusion": "skipped",
                    "completed_at": None,
                }
            ]
        }
        successful_step = {
            "steps": [
                {
                    "name": "Sync Anime, Manga, and Manhwa",
                    "conclusion": "success",
                    "completed_at": (self.now - timedelta(hours=73)).isoformat(),
                }
            ]
        }

        with (
            patch(
                "backend.jobs.sync_guard._successful_scheduled_runs",
                return_value=(runs, len(runs)),
            ),
            patch(
                "backend.jobs.sync_guard._jobs_for_run",
                side_effect=[[skipped_step], [skipped_step], [successful_step]],
            ),
        ):
            verified_runs = _successful_scheduled_etl_runs(
                repository="owner/repo", workflow="jikan-sync.yml", token="token"
            )

        should_run, _reason = should_run_scheduled_sync(
            verified_runs, now=self.now, minimum_hours=72
        )
        self.assertTrue(should_run)
        self.assertEqual([run["id"] for run in verified_runs], [100])

    def test_api_failure_fails_closed_without_enabling_database_write(self):
        with (
            patch.dict(
                "os.environ",
                {
                    "GITHUB_EVENT_NAME": "schedule",
                    "GITHUB_REPOSITORY": "owner/repo",
                    "GITHUB_TOKEN": "token",
                },
                clear=True,
            ),
            patch("sys.argv", ["sync_guard", "--workflow", "jikan-sync.yml"]),
            patch(
                "backend.jobs.sync_guard._successful_scheduled_etl_runs",
                side_effect=OSError("API unavailable"),
            ),
            patch("backend.jobs.sync_guard._write_output") as output,
            patch("backend.jobs.sync_guard._write_summary") as summary,
        ):
            with self.assertRaises(SystemExit):
                main()

        output.assert_called_once_with("should_run", "false")
        summary.assert_called_once()
        self.assertNotIn(
            ("should_run", "true"),
            [call.args for call in output.call_args_list],
        )

    def test_missing_etl_step_cannot_be_verified_as_guard_only(self):
        for jobs in ([], [{"steps": []}], [{"steps": [{"name": "Checkout"}]}]):
            with self.subTest(jobs=jobs), self.assertRaises(RuntimeError):
                _successful_etl_completion(jobs)

    def test_unknown_etl_step_conclusion_fails_closed(self):
        for conclusion in (None, "unknown"):
            with self.subTest(conclusion=conclusion), self.assertRaises(RuntimeError):
                _successful_etl_completion(
                    [
                        {
                            "steps": [
                                {
                                    "name": "Sync Anime, Manga, and Manhwa",
                                    "conclusion": conclusion,
                                }
                            ]
                        }
                    ]
                )

    def test_failed_earlier_attempt_does_not_hide_successful_rerun_step(self):
        completion = (self.now - timedelta(hours=24)).isoformat()
        jobs = [
            {
                "steps": [
                    {
                        "name": "Sync Anime, Manga, and Manhwa",
                        "conclusion": conclusion,
                        "completed_at": completion,
                    }
                ]
            }
            for conclusion in ("failure", "success")
        ]
        self.assertEqual(
            _successful_etl_completion(jobs), self.now - timedelta(hours=24)
        )

    def test_real_etl_is_found_after_a_page_of_guard_only_runs(self):
        skipped = [
            {
                "steps": [
                    {"name": "Sync Anime, Manga, and Manhwa", "conclusion": "skipped"}
                ]
            }
        ]
        successful = [
            {
                "steps": [
                    {
                        "name": "Sync Anime, Manga, and Manhwa",
                        "conclusion": "success",
                        "completed_at": self.now.isoformat(),
                    }
                ]
            }
        ]
        with (
            patch(
                "backend.jobs.sync_guard._successful_scheduled_runs",
                side_effect=[
                    ([{"id": 2}], 2),
                    ([{"id": 1, "run_started_at": self.now.isoformat()}], 2),
                ],
            ) as runs_lookup,
            patch(
                "backend.jobs.sync_guard._jobs_for_run",
                side_effect=[skipped, successful],
            ),
        ):
            runs = _successful_scheduled_etl_runs(
                repository="owner/repo", workflow="jikan-sync.yml", token="token"
            )
        self.assertEqual([run["id"] for run in runs], [1])
        self.assertEqual(
            [call.kwargs["page"] for call in runs_lookup.call_args_list], [1, 2]
        )

    def test_jobs_are_paginated_before_checking_etl_step(self):
        payloads = [
            {"total_count": 2, "jobs": [{"steps": [{"name": "Checkout"}]}]},
            {
                "total_count": 2,
                "jobs": [
                    {
                        "steps": [
                            {
                                "name": "Sync Anime, Manga, and Manhwa",
                                "conclusion": "skipped",
                            }
                        ]
                    }
                ],
            },
        ]
        with patch(
            "backend.jobs.sync_guard._github_payload", side_effect=payloads
        ) as lookup:
            jobs = _jobs_for_run(repository="owner/repo", run_id=1, token="token")
        self.assertIsNone(_successful_etl_completion(jobs))
        self.assertIn("page=2", lookup.call_args_list[-1].args[0])

    def test_exhausted_lookback_is_not_treated_as_no_prior_etl(self):
        with (
            patch("backend.jobs.sync_guard.MAX_WORKFLOW_RUN_PAGES", 1),
            patch(
                "backend.jobs.sync_guard._successful_scheduled_runs",
                return_value=([{"id": 1}], 2),
            ),
            patch(
                "backend.jobs.sync_guard._jobs_for_run",
                return_value=[
                    {
                        "steps": [
                            {
                                "name": "Sync Anime, Manga, and Manhwa",
                                "conclusion": "skipped",
                            }
                        ]
                    }
                ],
            ),
            self.assertRaises(RuntimeError),
        ):
            _successful_scheduled_etl_runs(
                repository="owner/repo", workflow="jikan-sync.yml", token="token"
            )

    def test_etl_completion_without_timezone_fails_closed(self):
        with self.assertRaises(RuntimeError):
            _successful_etl_completion(
                [
                    {
                        "steps": [
                            {
                                "name": "Sync Anime, Manga, and Manhwa",
                                "conclusion": "success",
                                "completed_at": "2026-08-10T12:00:00",
                            }
                        ]
                    }
                ]
            )

    def test_incomplete_workflow_runs_response_fails_closed(self):
        with (
            patch(
                "backend.jobs.sync_guard._github_payload",
                return_value={"total_count": 0},
            ),
            self.assertRaises(RuntimeError),
        ):
            _successful_scheduled_runs(
                repository="owner/repo",
                workflow="jikan-sync.yml",
                token="token",
                page=1,
            )

    def test_incomplete_jobs_response_fails_closed(self):
        with (
            patch(
                "backend.jobs.sync_guard._github_payload",
                return_value={"total_count": 0},
            ),
            self.assertRaises(RuntimeError),
        ):
            _jobs_for_run(repository="owner/repo", run_id=1, token="token")

    def test_prior_etl_without_aware_timestamp_fails_closed(self):
        for run in ({}, {"run_started_at": "2026-08-10T12:00:00"}):
            with self.subTest(run=run), self.assertRaises(ValueError):
                should_run_scheduled_sync([run], now=self.now, minimum_hours=72)

    def test_invalid_verified_timestamp_produces_explicit_safe_failure(self):
        with (
            patch.dict(
                "os.environ",
                {
                    "GITHUB_EVENT_NAME": "schedule",
                    "GITHUB_REPOSITORY": "owner/repo",
                    "GITHUB_TOKEN": "token",
                },
                clear=True,
            ),
            patch("sys.argv", ["sync_guard", "--workflow", "jikan-sync.yml"]),
            patch(
                "backend.jobs.sync_guard._successful_scheduled_etl_runs",
                return_value=[{}],
            ),
            patch("backend.jobs.sync_guard._write_output") as output,
            patch("backend.jobs.sync_guard._write_summary"),
            self.assertRaises(SystemExit),
        ):
            main()
        output.assert_called_once_with("should_run", "false")


if __name__ == "__main__":
    unittest.main()
