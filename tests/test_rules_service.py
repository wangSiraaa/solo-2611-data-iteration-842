"""Unittest suite for the rules/event side.

Run with::

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import tempfile
import unittest

from archive_handoff import ConflictError, RejectError, RuleEventStore


def seed(store: RuleEventStore) -> None:
    store.import_rules([
        {"rule_id": "RA", "revision": 1, "trigger_type": "CLOSED", "keep_days": 20},
        {"rule_id": "RB", "revision": 1, "trigger_type": "RECEIVED", "keep_days": 10},
    ])
    store.import_documents([
        {"doc_id": "D1", "case_id": "C1", "rule_ids": ["RA"]},
        {"doc_id": "D2", "case_id": "C1", "rule_ids": ["RB"]},
        {"doc_id": "D3", "case_id": "C2", "rule_ids": ["RA"]},
        {"doc_id": "D4", "case_id": "C3", "rule_ids": ["RA"]},
    ])
    store.import_events([
        {"event_id": "E1", "revision": 1, "doc_id": "D1", "type": "CLOSED", "day": 10},
        {"event_id": "E2", "revision": 1, "doc_id": "D2", "type": "RECEIVED", "day": 5},
        {"event_id": "E3", "revision": 1, "doc_id": "D3", "type": "CLOSED", "day": 18},
    ])
    store.import_holds([
        {"hold_id": "H1", "revision": 1, "case_id": "C1", "start": 25, "end": 50},
    ])


class RulesStoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RuleEventStore(os.path.join(self.tmp.name, "rules.db"))
        seed(self.store)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()


class DateArithmetic(RulesStoreCase):
    def test_thresholds(self):
        self.assertEqual(self.store.qualify_document("D1", 40).computed_day, 30)
        self.assertEqual(self.store.qualify_document("D2", 40).computed_day, 15)
        self.assertEqual(self.store.qualify_document("D3", 40).computed_day, 38)
        self.assertIsNone(self.store.qualify_document("D4", 40).computed_day)

    def test_integer_day_reaches_threshold(self):
        self.assertFalse(self.store.qualify_document("D1", 29).time_condition_met)
        self.assertTrue(self.store.qualify_document("D1", 30).time_condition_met)
        # D2 threshold 15 (boundary not off-by-one)
        self.assertFalse(self.store.qualify_document("D2", 14).time_condition_met)
        self.assertTrue(self.store.qualify_document("D2", 15).time_condition_met)

    def test_latest_event_wins(self):
        self.store.import_events([
            {"event_id": "E1b", "revision": 1, "doc_id": "D1", "type": "CLOSED", "day": 12},
        ])
        self.assertEqual(self.store.qualify_document("D1", 40).computed_day, 32)

    def test_event_revision_folds_to_latest(self):
        self.store.import_events([
            {"event_id": "E1", "revision": 2, "doc_id": "D1", "type": "CLOSED", "day": 4},
        ])
        self.assertEqual(self.store.qualify_document("D1", 40).computed_day, 24)


class MissingTrigger(RulesStoreCase):
    def test_missing_is_pending_not_day_zero(self):
        q = self.store.qualify_document("D4", 40)
        self.assertIsNone(q.computed_day)
        self.assertFalse(q.time_condition_met)
        self.assertEqual(q.missing_trigger_rules, ["RA"])
        self.assertEqual([b.kind for b in q.block_reasons], ["missing_trigger"])

    def test_multiple_rules_all_must_be_satisfied(self):
        self.store.import_documents([
            {"doc_id": "D5", "case_id": "C4", "rule_ids": ["RA", "RB"]},
        ])
        self.store.import_events([
            {"event_id": "E5a", "revision": 1, "doc_id": "D5", "type": "CLOSED", "day": 10},
        ])
        # RB (RECEIVED) missing -> pending even though RA computes 30.
        q = self.store.qualify_document("D5", 100)
        self.assertIsNone(q.computed_day)
        self.assertFalse(q.releasable)
        self.store.import_events([
            {"event_id": "E5b", "revision": 1, "doc_id": "D5", "type": "RECEIVED", "day": 5},
        ])
        q = self.store.qualify_document("D5", 100)
        self.assertEqual(q.computed_day, 30)  # max(10+20, 5+10)
        self.assertTrue(q.releasable)


class HoldsAndClock(RulesStoreCase):
    def test_hold_blocks_but_does_not_pause_clock(self):
        q = self.store.qualify_document("D1", 40)
        self.assertFalse(q.releasable)
        self.assertEqual(q.computed_day, 30)  # NOT 30 + 25
        self.assertEqual([b.kind for b in q.block_reasons], ["hold"])
        self.assertEqual(q.holds_active[0].hold_id, "H1")

    def test_interval_half_open(self):
        for day, active in [(24, False), (25, True), (49, True), (50, False)]:
            q = self.store.qualify_document("D1", day)
            self.assertEqual(bool(q.holds_active), active, f"day {day}")

    def test_open_ended_hold(self):
        self.store.import_holds([
            {"hold_id": "H2", "revision": 1, "case_id": "C2", "start": 5, "end": None},
        ])
        self.assertFalse(self.store.qualify_document("D3", 10_000).releasable)

    def test_document_scoped_hold(self):
        self.store.import_holds([
            {"hold_id": "H3", "revision": 1, "doc_id": "D3", "start": 1, "end": None},
        ])
        self.assertFalse(self.store.qualify_document("D3", 40).releasable)
        # The document-scoped hold on D3 does not leak onto D1.
        d1_holds = [h.hold_id for h in self.store.qualify_document("D1", 40).holds_active]
        self.assertEqual(d1_holds, ["H1"])

    def test_hold_revision_releases(self):
        self.store.import_holds([
            {"hold_id": "H1", "revision": 2, "case_id": "C1", "start": 25, "end": 35},
        ])
        self.assertTrue(self.store.qualify_document("D1", 34).holds_active)
        self.assertFalse(self.store.qualify_document("D1", 35).holds_active)
        self.assertTrue(self.store.qualify_document("D1", 35).releasable)


class MultipleBlockReasons(RulesStoreCase):
    def test_hold_and_not_due_deduped(self):
        # Day 26: hold [25,50) is active AND computed day 30 is not reached.
        q = self.store.qualify_document("D1", 26)
        kinds = sorted(b.kind for b in q.block_reasons)
        self.assertEqual(kinds, ["hold", "not_due"])
        # Each detail appears exactly once.
        keys = [b.key for b in q.block_reasons]
        self.assertEqual(len(keys), len(set(keys)))

    def test_repeated_block_does_not_duplicate(self):
        q = self.store.qualify_document("D1", 26)
        from archive_handoff.models import BlockReason
        q.add_block(BlockReason(kind="hold", key="hold:H1", detail="again", hold_id="H1"))
        self.assertEqual(sum(1 for b in q.block_reasons if b.key == "hold:H1"), 1)


class AtomicIngestion(RulesStoreCase):
    def test_exact_replay_is_noop(self):
        result = self.store.import_events([
            {"event_id": "E1", "revision": 1, "doc_id": "D1", "type": "CLOSED", "day": 10},
        ])
        self.assertFalse(result["changed"])

    def test_conflicting_replay_rejects_batch(self):
        before = self.store.qualify_document("D2", 40).computed_day
        with self.assertRaises(ConflictError):
            self.store.import_events([
                {"event_id": "E9", "revision": 1, "doc_id": "D2", "type": "RECEIVED", "day": 6},
                {"event_id": "E1", "revision": 1, "doc_id": "D1", "type": "CLOSED", "day": 11},
            ])
        # Whole batch rejected: E9 not applied.
        self.assertIsNone(self.store.conn.execute(
            "SELECT 1 FROM events WHERE event_id='E9'").fetchone())
        self.assertEqual(self.store.qualify_document("D2", 40).computed_day, before)

    def test_unknown_reference_rejects_batch(self):
        with self.assertRaises(RejectError):
            self.store.import_events([
                {"event_id": "E7", "revision": 1, "doc_id": "D1", "type": "CLOSED", "day": 1},
                {"event_id": "E8", "revision": 1, "doc_id": "GHOST", "type": "CLOSED", "day": 1},
            ])
        self.assertIsNone(self.store.conn.execute(
            "SELECT 1 FROM events WHERE event_id='E7'").fetchone())

    def test_illegal_hold_interval_rejects_batch(self):
        with self.assertRaises(RejectError):
            self.store.import_holds([
                {"hold_id": "H9", "revision": 1, "case_id": "C2", "start": 10, "end": 10},
            ])
        self.assertIsNone(self.store.conn.execute(
            "SELECT 1 FROM holds WHERE hold_id='H9'").fetchone())

    def test_stale_revision_rejected(self):
        with self.assertRaises(ConflictError):
            self.store.import_holds([
                {"hold_id": "H1", "revision": 1, "case_id": "C1", "start": 25, "end": 40},
            ])
        # A numerically older revision is rejected once a newer one exists.
        self.store.import_holds([
            {"hold_id": "H1", "revision": 2, "case_id": "C1", "start": 25, "end": 35},
        ])
        with self.assertRaises(ConflictError):
            self.store.import_holds([
                {"hold_id": "H1", "revision": 1, "case_id": "C1", "start": 25, "end": 50},
            ])

    def test_unknown_hold_case_rejected(self):
        with self.assertRaises(RejectError):
            self.store.import_holds([
                {"hold_id": "H9", "revision": 1, "case_id": "NOPE", "start": 1, "end": 5},
            ])


class ProductFreezing(RulesStoreCase):
    def test_historical_product_is_immutable(self):
        p1 = self.store.build_product(40)
        self.store.save_product("P1", p1)
        self.store.import_holds([
            {"hold_id": "H1", "revision": 2, "case_id": "C1", "start": 25, "end": 35},
        ])
        loaded = self.store.load_product("P1")
        self.assertFalse(loaded.documents["D1"].releasable)
        self.assertEqual(loaded.documents["D1"].holds_active[0].end, 50)
        # Current query differs.
        self.assertTrue(self.store.qualify_document("D1", 40).releasable)

    def test_product_records_source_revision(self):
        p = self.store.build_product(40)
        self.store.save_product("QX", p)
        record = self.store.product_record("QX")
        self.assertEqual(record["source_revision"], self.store.catalog_revision)
        self.assertEqual(p.as_of_day, 40)

    def test_product_replay_identical_ok_conflict_not(self):
        p = self.store.build_product(40)
        self.store.save_product("PZ", p)
        self.assertTrue(self.store.save_product("PZ", p)["replayed"])
        with self.assertRaises(ConflictError):
            self.store.save_product("PZ", self.store.build_product(41))


if __name__ == "__main__":
    unittest.main()
