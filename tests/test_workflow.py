import unittest
from pathlib import Path


WORKFLOW = (
    Path(__file__).resolve().parents[1] / ".github" / "workflows" / "jikan-sync.yml"
)


class JikanWorkflowTests(unittest.TestCase):
    def test_sync_checks_daily_and_enforces_a_48_hour_cadence(self):
        workflow = WORKFLOW.read_text(encoding="utf-8")

        self.assertIn('cron: "17 5 * * *"', workflow)
        self.assertIn(
            "sync_guard --workflow jikan-sync.yml --minimum-hours 48",
            workflow,
        )
        self.assertIn("actions: read", workflow)
        self.assertNotIn("gh workflow run", workflow)
        self.assertIn("workflow_dispatch:", workflow)

    def test_sync_runs_all_phases_in_one_rate_limited_process(self):
        workflow = WORKFLOW.read_text(encoding="utf-8")

        command = (
            "python -m backend.jobs.jikan_etl "
            "--scheduled-sync --page-limit 10 --limit 200 "
            "--streaming-limit 100 --request-budget 800"
        )
        self.assertIn(command, workflow)
        self.assertEqual(workflow.count("python -m backend.jobs.jikan_etl"), 1)
        self.assertIn("Sync Anime, Manga, and Manhwa", workflow)
        self.assertIn("Scheduled catalogue metadata sync", workflow)

    def test_expensive_steps_only_run_when_the_guard_allows_them(self):
        workflow = WORKFLOW.read_text(encoding="utf-8")

        condition = "if: steps.cadence.outputs.should_run == 'true'"
        self.assertEqual(workflow.count(condition), 3)
        self.assertIn("concurrency:\n  group: jikan-sync", workflow)


if __name__ == "__main__":
    unittest.main()
