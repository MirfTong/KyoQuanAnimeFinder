"""Regression coverage for reconciling repeated payloads in one ETL batch."""

from datetime import timedelta
import unittest
import warnings

from sqlalchemy import func, select
from sqlalchemy.exc import SAWarning

from backend.models import AnimeStreamingService
from tests import test_efficient_sync as fixtures


class PersistenceFixture:
    # Share only the isolated database helpers, not the other test methods.
    setUp = fixtures.PersistenceTests.setUp
    anime = fixtures.PersistenceTests.anime


def streaming(name):
    return {"name": name, "url": f"https://{name.lower()}.example.test/watch"}


class RelationshipRecoveryTests(PersistenceFixture, unittest.TestCase):
    def apply(self, detail_streaming, dedicated_streaming, now=fixtures.NOW):
        with warnings.catch_warnings():
            # An expunged pending orphan must not remain on the service's
            # inverse collection and be considered for another cascade-add.
            warnings.simplefilter("error", SAWarning)
            fixtures.worker.apply_details(
                [
                    fixtures.item(data={"mal_id": 1, "streaming": detail_streaming}),
                    fixtures.item(
                        queue="streaming",
                        data={"mal_id": 1, "streaming": dedicated_streaming},
                    ),
                ],
                now,
                fixtures.RefreshPolicy(),
                self.metrics,
            )

    def test_complete_streaming_can_remove_a_pending_partial_detail_link(self):
        row = self.anime()
        # A malformed detail array is additive, so fetching the dedicated
        # streaming response is still necessary. Its valid empty array wins.
        self.apply([streaming("Partial"), None], [])

        self.session.expire_all()
        self.assertEqual(row.streaming_links, [])
        self.assertEqual(
            self.session.scalar(
                select(func.count()).select_from(AnimeStreamingService)
            ),
            0,
        )

    def test_complete_streaming_replaces_partial_links_and_rerun_is_duplicate_safe(
        self,
    ):
        row = self.anime()
        partial = [streaming("Partial"), None]
        complete = [streaming("Complete")]
        self.apply(partial, complete)
        self.apply(partial, complete, now=fixtures.NOW + timedelta(days=3))

        self.session.expire_all()
        self.assertEqual(
            [link.streaming_service.name for link in row.streaming_links],
            ["Complete"],
        )
        self.assertEqual(
            self.session.scalar(
                select(func.count()).select_from(AnimeStreamingService)
            ),
            1,
        )

    def test_complete_empty_streaming_removes_persistent_and_pending_links(self):
        row = self.anime()
        self.apply([streaming("Existing")], [streaming("Existing")])
        self.apply(
            [streaming("Partial"), None],
            [],
            now=fixtures.NOW + timedelta(days=3),
        )

        self.session.expire_all()
        self.assertEqual(row.streaming_links, [])


if __name__ == "__main__":
    unittest.main()
