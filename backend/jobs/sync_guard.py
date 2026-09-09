"""Gate scheduled GitHub Actions runs behind a successful ETL interval."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


GITHUB_API_URL = "https://api.github.com"
SYNC_STEP_NAME = "Sync Anime, Manga, and Manhwa"
RESULTS_PER_PAGE = 100
MAX_WORKFLOW_RUN_PAGES = 10
MAX_JOB_PAGES = 10


def _github_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("GitHub timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def should_run_scheduled_sync(
    successful_etl_runs: list[dict[str, object]],
    *,
    now: datetime,
    minimum_hours: int,
) -> tuple[bool, str]:
    """Return whether the last verified successful scheduled ETL is old enough."""
    timestamps = []
    for run in successful_etl_runs:
        timestamp = (
            run.get("run_started_at")
            or run.get("created_at")
            or run.get("etl_completed_at")
        )
        if not isinstance(timestamp, str) or not timestamp:
            raise ValueError("Verified ETL run had no timestamp")
        timestamps.append(_github_datetime(timestamp))
    if not timestamps:
        return True, "No previous successful scheduled ETL run was found."

    last_success = max(timestamps)
    next_allowed = last_success + timedelta(hours=minimum_hours)
    if now >= next_allowed:
        return (
            True,
            f"The last successful scheduled ETL was {last_success.isoformat()}.",
        )
    return (
        False,
        "The last successful scheduled ETL was "
        f"{last_success.isoformat()}; the next run is allowed after "
        f"{next_allowed.isoformat()}.",
    )


def _github_payload(url: str, *, token: str) -> dict[str, object]:
    request = Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "kyoquan-sync-guard",
        },
    )
    with urlopen(request, timeout=20) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise RuntimeError("GitHub returned an invalid API response")
    return payload


def _successful_scheduled_runs(
    *, repository: str, workflow: str, token: str, page: int
) -> tuple[list[dict[str, object]], int]:
    workflow_id = quote(workflow, safe="")
    query = urlencode(
        {
            "event": "schedule",
            "status": "success",
            "per_page": RESULTS_PER_PAGE,
            "page": page,
        }
    )
    payload = _github_payload(
        f"{GITHUB_API_URL}/repos/{repository}/actions/workflows/"
        f"{workflow_id}/runs?{query}",
        token=token,
    )
    runs = payload.get("workflow_runs")
    total_count = payload.get("total_count")
    if (
        not isinstance(runs, list)
        or not isinstance(total_count, int)
        or isinstance(total_count, bool)
        or total_count < 0
    ):
        raise RuntimeError("GitHub returned an invalid workflow-runs response")
    if any(not isinstance(run, dict) for run in runs):
        raise RuntimeError("GitHub returned an invalid workflow run")
    return runs, total_count


def _jobs_for_run(
    *, repository: str, run_id: int, token: str
) -> list[dict[str, object]]:
    jobs: list[dict[str, object]] = []
    total_count: int | None = None

    for page in range(1, MAX_JOB_PAGES + 1):
        query = urlencode({"filter": "all", "per_page": RESULTS_PER_PAGE, "page": page})
        payload = _github_payload(
            f"{GITHUB_API_URL}/repos/{repository}/actions/runs/{run_id}/jobs?{query}",
            token=token,
        )
        page_jobs = payload.get("jobs")
        page_total = payload.get("total_count")
        if (
            not isinstance(page_jobs, list)
            or any(not isinstance(job, dict) for job in page_jobs)
            or not isinstance(page_total, int)
            or isinstance(page_total, bool)
            or page_total < 0
        ):
            raise RuntimeError("GitHub returned an invalid jobs response")
        if total_count is None:
            total_count = page_total
        elif page_total != total_count:
            raise RuntimeError("GitHub jobs pagination changed during verification")

        jobs.extend(page_jobs)
        if len(jobs) >= total_count:
            return jobs
        if not page_jobs:
            raise RuntimeError("GitHub jobs pagination ended unexpectedly")

    raise RuntimeError("GitHub jobs exceeded the safe verification lookback")


def _successful_etl_completion(jobs: list[dict[str, object]]) -> datetime | None:
    completions: list[datetime] = []
    identified_step = False
    for job in jobs:
        steps = job.get("steps")
        if not isinstance(steps, list):
            raise RuntimeError("GitHub returned a job without a valid steps list")
        for step in steps:
            if not isinstance(step, dict):
                raise RuntimeError("GitHub returned an invalid workflow step")
            if step.get("name") != SYNC_STEP_NAME:
                continue
            identified_step = True
            conclusion = step.get("conclusion")
            if conclusion not in {
                "success",
                "skipped",
                "failure",
                "cancelled",
                "timed_out",
                "neutral",
            }:
                raise RuntimeError(
                    "GitHub returned an ETL step without a final conclusion"
                )
            if conclusion != "success":
                continue
            completed_at = step.get("completed_at")
            if not isinstance(completed_at, str) or not completed_at:
                raise RuntimeError("Successful ETL step had no completion timestamp")
            try:
                completions.append(_github_datetime(completed_at))
            except ValueError as error:
                raise RuntimeError(
                    "Successful ETL step had an invalid completion timestamp"
                ) from error
    if not identified_step:
        raise RuntimeError("GitHub jobs did not identify the expected ETL step")
    return max(completions) if completions else None


def _successful_scheduled_etl_runs(
    *, repository: str, workflow: str, token: str
) -> list[dict[str, object]]:
    """Return the newest verified real ETL runs, ignoring guard-only successes."""
    examined = 0
    expected_total: int | None = None

    for page in range(1, MAX_WORKFLOW_RUN_PAGES + 1):
        runs, total_count = _successful_scheduled_runs(
            repository=repository,
            workflow=workflow,
            token=token,
            page=page,
        )
        if expected_total is None:
            expected_total = total_count
        elif total_count != expected_total:
            raise RuntimeError(
                "GitHub workflow-run pagination changed during verification"
            )

        verified: list[dict[str, object]] = []
        for run in runs:
            run_id = run.get("id")
            if not isinstance(run_id, int) or isinstance(run_id, bool):
                raise RuntimeError("GitHub returned a workflow run without a valid id")
            completion = _successful_etl_completion(
                _jobs_for_run(repository=repository, run_id=run_id, token=token)
            )
            if completion is not None:
                timestamp = run.get("run_started_at") or run.get("created_at")
                if not isinstance(timestamp, str) or not timestamp:
                    raise RuntimeError(
                        "GitHub returned a successful ETL run without a start timestamp"
                    )
                try:
                    _github_datetime(timestamp)
                except ValueError as error:
                    raise RuntimeError(
                        "GitHub returned a successful ETL run with an invalid start timestamp"
                    ) from error
                verified.append(dict(run))

        # Workflow runs are returned newest first. Once a page contains a real ETL,
        # older pages cannot contain the most recent one.
        if verified:
            return verified

        examined += len(runs)
        if examined >= expected_total:
            return []
        if not runs:
            raise RuntimeError("GitHub workflow-run pagination ended unexpectedly")

    raise RuntimeError("GitHub workflow runs exceeded the safe verification lookback")


def _write_output(name: str, value: str) -> None:
    output_path = os.getenv("GITHUB_OUTPUT")
    if output_path:
        with Path(output_path).open("a", encoding="utf-8") as output:
            output.write(f"{name}={value}\n")
    else:
        print(f"{name}={value}")


def _write_summary(should_run: bool, reason: str) -> None:
    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    decision = "run" if should_run else "skip"
    with Path(summary_path).open("a", encoding="utf-8") as summary:
        summary.write("### Scheduled sync cadence\n\n")
        summary.write(f"Decision: **{decision}**\n\n{reason}\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workflow", required=True)
    parser.add_argument("--minimum-hours", type=int, default=72)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.minimum_hours <= 0:
        raise SystemExit("--minimum-hours must be positive")

    event_name = os.getenv("GITHUB_EVENT_NAME", "")
    if event_name == "workflow_dispatch":
        should_run = True
        reason = "Manual workflow dispatch bypasses the scheduled cadence guard."
    elif event_name == "schedule":
        repository = os.getenv("GITHUB_REPOSITORY", "")
        token = os.getenv("GITHUB_TOKEN", "")
        if not repository or not token:
            raise SystemExit(
                "GITHUB_REPOSITORY and GITHUB_TOKEN are required for scheduled runs"
            )
        try:
            runs = _successful_scheduled_etl_runs(
                repository=repository,
                workflow=args.workflow,
                token=token,
            )
            should_run, reason = should_run_scheduled_sync(
                runs,
                now=datetime.now(timezone.utc),
                minimum_hours=args.minimum_hours,
            )
        except Exception as error:
            reason = (
                "Unable to verify the previous successful workflow run; "
                "refusing to start a database-writing sync "
                f"({type(error).__name__})."
            )
            _write_output("should_run", "false")
            _write_summary(False, reason)
            raise SystemExit(reason) from None
    else:
        should_run = True
        reason = f"Event {event_name or 'unknown'} is not a scheduled run."

    _write_output("should_run", str(should_run).lower())
    _write_summary(should_run, reason)
    print(reason)


if __name__ == "__main__":
    main()
