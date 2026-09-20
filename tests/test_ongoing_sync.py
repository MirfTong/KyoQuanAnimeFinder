"""Offline provider contracts and safety for lightweight ongoing refreshes."""

import io
import json
import os
import unittest
from unittest.mock import Mock
from urllib.error import HTTPError, URLError

os.environ.setdefault("DATABASE_URL", "sqlite://")
from backend.jobs import efficient_sync as worker, ongoing_sync
from backend.services.jikan_budget import RequestBudget
from backend.services.jikan_client import JikanClient, JikanTemporaryError
from tests.test_jikan_client import FakeClock, Response


def entry(**changes):
    return (
        dict(
            mal_id=1,
            status="Currently Airing",
            score=8,
            popularity=1,
            members=10,
            episodes=None,
        )
        | changes
    )


class OngoingContractTests(unittest.TestCase):
    def test_sparse_invalid_or_wrong_media_pages_cannot_claim_success(self):
        bad = [
            entry(score=True),
            entry(score=float("nan")),
            entry(score=11),
            entry(mal_id=True),
            entry(episodes=-1),
            entry(status="Finished Airing"),
            entry(status="Publishing"),
            entry(members="10"),
            {key: value for key, value in entry().items() if key != "score"},
        ]
        for payload in bad:
            with (
                self.subTest(payload=payload),
                self.assertRaises(JikanTemporaryError),
            ):
                ongoing_sync.validate_listing("anime", [payload])
        ongoing_sync.validate_listing("anime", [entry(), entry()])
        for entries in (
            [entry(), entry(score=7)],
            [entry(members=10**1000)],
            [entry(mal_id=2147483648)],
            [entry()] * 26,
            {"data": []},
        ):
            with (
                self.subTest(entries=entries),
                self.assertRaises(JikanTemporaryError),
            ):
                ongoing_sync.validate_listing("anime", entries)
        with self.assertRaises(JikanTemporaryError):
            ongoing_sync.validate_listing("anime", [entry(mal_id=2), entry()])

    def test_page_failure_ends_only_its_cursor_and_budget_stop_is_deferred(
        self,
    ):
        plan = dict(
            kind="ongoing_page",
            media="anime",
            key="ongoing:anime:v1",
            page=4,
            cap=2,
        )
        for status in (404, 429, 503):
            with self.subTest(status=status):
                client = Mock()
                client.get_ongoing_page.side_effect = HTTPError(
                    "https://test", status, "failure", {}, None
                )
                spool = io.StringIO()
                worker.fetch_work(
                    client,
                    RequestBudget(40),
                    [plan],
                    dict(anime=[], manga=[], manhwa=[], streaming=[]),
                    spool,
                    worker.SyncMetrics(),
                )
                result = json.loads(spool.getvalue())
                self.assertEqual(result["page"], 4)
                self.assertIn("failure", result)
                self.assertEqual(client.get_ongoing_page.call_count, 1)

        clock = FakeClock()
        budget = RequestBudget(1)
        client = JikanClient(
            budget=budget,
            base_url="https://provider.test/v1",
            fallback_base_url="",
            clock=clock,
            sleeper=clock.sleep,
            opener=Mock(side_effect=URLError("timeout")),
        )
        metrics, spool = worker.SyncMetrics(), io.StringIO()
        worker.fetch_work(
            client,
            budget,
            [plan],
            dict(anime=[], manga=[], manhwa=[], streaming=[]),
            spool,
            metrics,
        )
        self.assertEqual(spool.getvalue(), "")
        self.assertEqual(metrics.listing_by_media["anime"]["pages_deferred"], 2)
        self.assertEqual(budget.attempted, 1)

    def test_ongoing_request_uses_fixed_provider_and_validates_pagination(self):
        clock = FakeClock()
        opener = Mock(
            return_value=Response(
                json.dumps(
                    {
                        "data": [entry()],
                        "pagination": {
                            "current_page": 2,
                            "has_next_page": False,
                        },
                    }
                ).encode()
            )
        )
        client = JikanClient(
            base_url="https://provider.test/v1",
            fallback_base_url="",
            clock=clock,
            sleeper=clock.sleep,
            opener=opener,
        )
        result = client.get_ongoing_page("anime", page=2)
        self.assertEqual(result.page, 2)
        url = opener.call_args.args[0].full_url
        for token in ("status=airing", "limit=25", "order_by=mal_id", "page=2"):
            self.assertIn(token, url)


if __name__ == "__main__":
    unittest.main()
