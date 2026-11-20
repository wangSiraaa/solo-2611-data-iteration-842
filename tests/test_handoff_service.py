"""Unittest suite for the whole-case handoff repository."""

from __future__ import annotations

import os
import tempfile
import unittest

from archive_handoff import ConflictError, HandoffRepo, RejectError, RuleEventStore
from archive_handoff.materials import import_materials


def build(tmp, materials="materials/751"):
    rules = RuleEventStore(os.path.join(tmp, "rules.db"))
    handoff = HandoffRepo(os.path.join(tmp, "handoff.db"))
    import_materials(rules, handoff, materials)
    return rules, handoff


def publish(rules, handoff, day, product_id):
    product = rules.build_product(day)
    rules.save_product(product_id, product)
    handoff.ingest_product(product_id, product)
    return product


class HandoffCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.rules, self.handoff = build(self.tmpdir.name)
        # Correct the hold to [25, 35): D1/D2/D3 releasable at day 40.
        self.rules.import_holds([
            {"hold_id": "H1", "revision": 2, "case_id": "C1", "start": 25, "end": 35},
        ])

    def tearDown(self):
        self.rules.close()
        self.handoff.close()
        self.tmpdir.cleanup()

    def submit(self, **kw):
        base = {"handoff_id": "H", "case_id": "C1", "doc_ids": ["D1", "D2"],
                "from": "LOCAL", "to": "VAULT1", "as_of_day": 40}
        base.update(kw)
        return self.handoff.submit(base)


class WholeCase(HandoffCase):
    def test_partial_request_rejected(self):
        publish(self.rules, self.handoff, 40, "Q40")
        with self.assertRaises(RejectError) as ctx:
            self.submit(handoff_id="PARTIAL", doc_ids=["D1"])
        self.assertEqual(ctx.exception.code, "not_whole_case")
        self.assertEqual(self.handoff.current_custodian("C1"), "LOCAL")

    def test_missing_member_rejected_even_if_all_listed_are_ok(self):
        publish(self.rules, self.handoff, 40, "Q40")
        with self.assertRaises(RejectError):
            self.submit(doc_ids=["D2"])  # D2 alone is releasable, but incomplete
        self.assertEqual(self.handoff.current_custodian("C1"), "LOCAL")

    def test_whole_request_commits_and_replays(self):
        publish(self.rules, self.handoff, 40, "Q40")
        first = self.submit(handoff_id="WHOLE1")
        self.assertEqual(first["from"], "LOCAL")
        self.assertFalse(first["replayed"])
        self.assertEqual(self.handoff.current_custodian("C1"), "VAULT1")
        again = self.submit(handoff_id="WHOLE1")
        self.assertTrue(again["replayed"])
        self.assertEqual(again["doc_ids"], ["D1", "D2"])

    def test_new_id_from_old_custodian_rejected(self):
        publish(self.rules, self.handoff, 40, "Q40")
        self.submit(handoff_id="WHOLE1")
        with self.assertRaises(RejectError) as ctx:
            self.submit(handoff_id="WHOLE2")
        self.assertEqual(ctx.exception.code, "custody_mismatch")
        self.assertEqual(self.handoff.current_custodian("C1"), "VAULT1")

    def test_same_id_different_content_rejected(self):
        publish(self.rules, self.handoff, 40, "Q40")
        self.submit(handoff_id="WHOLE1")
        with self.assertRaises(ConflictError):
            self.submit(handoff_id="WHOLE1", to="OTHER")
        # The original record is untouched.
        self.assertEqual(self.handoff.get_handoff("WHOLE1")["to"], "VAULT1")

    def test_wrong_from_rejected(self):
        publish(self.rules, self.handoff, 40, "Q40")
        with self.assertRaises(RejectError) as ctx:
            self.handoff.submit({"handoff_id": "W", "case_id": "C1",
                                 "doc_ids": ["D1", "D2"], "from": "VAULT1",
                                 "to": "VAULT2", "as_of_day": 40})
        self.assertEqual(ctx.exception.code, "custody_mismatch")

    def test_extra_document_rejected(self):
        publish(self.rules, self.handoff, 40, "Q40")
        with self.assertRaises(RejectError) as ctx:
            self.submit(handoff_id="X", doc_ids=["D1", "D2", "D3"])
        self.assertEqual(ctx.exception.code, "not_whole_case")


class EligibilityGating(HandoffCase):
    def test_hold_blocks_whole_case(self):
        # Undo the setUp correction: put H1 back to [25,50), which holds D1/D2
        # at day 40, then republish so the product reflects it.
        self.rules.import_holds([
            {"hold_id": "H1", "revision": 3, "case_id": "C1", "start": 25, "end": 50},
        ])
        publish(self.rules, self.handoff, 40, "Q40held")
        with self.assertRaises(RejectError) as ctx:
            self.submit(handoff_id="W", as_of_day=40,
                        **{"qualification_product_id": "Q40held"})
        self.assertEqual(ctx.exception.code, "ineligible_members")
        self.assertEqual(self.handoff.current_custodian("C1"), "LOCAL")

    def test_missing_trigger_blocks(self):
        # C3/D4 has no CLOSED event: eligibility never holds.
        publish(self.rules, self.handoff, 40, "Q40")
        with self.assertRaises(RejectError) as ctx:
            self.submit(handoff_id="W3", case_id="C3", doc_ids=["D4"])
        self.assertEqual(ctx.exception.code, "ineligible_members")

    def test_day_34_blocked_day_35_free(self):
        publish(self.rules, self.handoff, 34, "Q34")
        publish(self.rules, self.handoff, 35, "Q35")
        with self.assertRaises(RejectError):
            self.submit(handoff_id="W34", as_of_day=34,
                        **{"qualification_product_id": "Q34"})
        result = self.submit(handoff_id="W35", as_of_day=35,
                             **{"qualification_product_id": "Q35"})
        self.assertEqual(result["handoff_id"], "W35")

    def test_product_required_even_if_rules_would_release(self):
        # The handoff side does not recompute dates; without a product it
        # cannot validate.
        with self.assertRaises(RejectError) as ctx:
            self.submit(handoff_id="NOPRODUCT")
        self.assertEqual(ctx.exception.code, "no_qualification")


class Preparation(HandoffCase):
    def test_stale_proposal_refused_after_hold_change(self):
        publish(self.rules, self.handoff, 40, "Q40")
        req = {"handoff_id": "P1", "proposal_id": "P1", "case_id": "C1",
               "doc_ids": ["D1", "D2"], "from": "LOCAL", "to": "VAULT1",
               "as_of_day": 40}
        self.handoff.prepare(req)
        # Introduce a new hold via a revision -> qualification changes.
        self.rules.import_holds([
            {"hold_id": "H1", "revision": 3, "case_id": "C1", "start": 25, "end": 60},
        ])
        publish(self.rules, self.handoff, 40, "Q40b")
        with self.assertRaises(RejectError) as ctx:
            self.handoff.confirm("P1")
        self.assertEqual(ctx.exception.code, "stale_proposal")
        self.assertEqual(self.handoff.current_custodian("C1"), "LOCAL")

    def test_stale_proposal_when_membership_changes(self):
        publish(self.rules, self.handoff, 40, "Q40")
        self.handoff.prepare({"handoff_id": "P1", "proposal_id": "P1",
                              "case_id": "C1", "doc_ids": ["D1", "D2"],
                              "from": "LOCAL", "to": "VAULT1", "as_of_day": 40})
        self.rules.import_documents([
            {"doc_id": "D5", "case_id": "C1", "rule_ids": ["RA"]},
        ])
        self.handoff.sync_catalog_membership([
            {"doc_id": "D5", "case_id": "C1"},
        ])
        with self.assertRaises(RejectError) as ctx:
            self.handoff.confirm("P1")
        self.assertEqual(ctx.exception.code, "stale_proposal")

    def test_confirm_success_then_replay(self):
        publish(self.rules, self.handoff, 40, "Q40")
        self.handoff.prepare({"handoff_id": "P1", "proposal_id": "P1",
                              "case_id": "C1", "doc_ids": ["D1", "D2"],
                              "from": "LOCAL", "to": "VAULT1", "as_of_day": 40})
        result = self.handoff.confirm("P1")
        self.assertEqual(result["case_id"], "C1")
        self.assertEqual(self.handoff.current_custodian("C1"), "VAULT1")
        again = self.handoff.confirm("P1")
        self.assertTrue(again["replayed"])
        self.assertEqual(self.handoff.proposal_status("P1"), "committed")


class PlanPublication(HandoffCase):
    def test_published_plan_stays_frozen(self):
        # Prepare under the setUp hold ([25,35)): C1 free, C3 blocked (D4).
        publish(self.rules, self.handoff, 40, "Qbefore")
        plan = self.handoff.prepare_and_publish("PL1", "Qbefore", 40)
        self.assertIn("C1", plan["payload"]["eligible"])
        self.assertIn("C3", plan["payload"]["blocked"])
        # Re-hold C1 and republish: the plan's C1 eligibility must not change.
        self.rules.import_holds([
            {"hold_id": "H1", "revision": 3, "case_id": "C1", "start": 25, "end": 60},
        ])
        publish(self.rules, self.handoff, 40, "Qafter")
        frozen = self.handoff.get_plan("PL1")
        self.assertIn("C1", frozen["eligible"])
        self.assertTrue(self.handoff.plan_staleness("PL1"))

    def test_stale_plan_refused(self):
        publish(self.rules, self.handoff, 40, "Qbefore")
        self.handoff.prepare_and_publish("PL1", "Qbefore", 40)
        # Re-hold C1 so the current qualification changes.
        self.rules.import_holds([
            {"hold_id": "H1", "revision": 3, "case_id": "C1", "start": 25, "end": 60},
        ])
        publish(self.rules, self.handoff, 40, "Qafter")
        with self.assertRaises(RejectError) as ctx:
            self.handoff.confirm_handoff_list("PL1")
        self.assertEqual(ctx.exception.code, "stale_plan")
        # Nothing moved.
        self.assertEqual(self.handoff.current_custodian("C2"), "LOCAL")


class ReopenState(unittest.TestCase):
    def test_custody_and_handoffs_survive_reopen(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        rules, handoff = build(tmp.name)
        rules.import_holds([
            {"hold_id": "H1", "revision": 2, "case_id": "C1", "start": 25, "end": 35},
        ])
        publish(rules, handoff, 40, "Q40")
        handoff.submit({"handoff_id": "W1", "case_id": "C1", "doc_ids": ["D1", "D2"],
                        "from": "LOCAL", "to": "VAULT1", "as_of_day": 40})
        frozen = handoff.get_handoff("W1")
        rules.close()
        handoff.close()

        rules2 = RuleEventStore(os.path.join(tmp.name, "rules.db"))
        handoff2 = HandoffRepo(os.path.join(tmp.name, "handoff.db"))
        self.addCleanup(rules2.close)
        self.addCleanup(handoff2.close)
        self.assertEqual(handoff2.current_custodian("C1"), "VAULT1")
        self.assertEqual(handoff2.current_custodian("C2"), "LOCAL")
        self.assertEqual(handoff2.get_handoff("W1")["doc_ids"], frozen["doc_ids"])
        self.assertTrue(handoff2.verify()["ok"])
        self.assertTrue(rules2.verify_products() if hasattr(rules2, "verify_products") else True)

    def test_custody_reconciled_from_ledger(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        rules, handoff = build(tmp.name)
        rules.import_holds([
            {"hold_id": "H1", "revision": 2, "case_id": "C1", "start": 25, "end": 35},
        ])
        publish(rules, handoff, 40, "Q40")
        handoff.submit({"handoff_id": "W1", "case_id": "C1", "doc_ids": ["D1", "D2"],
                        "from": "LOCAL", "to": "VAULT1", "as_of_day": 40})
        rules.close()
        handoff.close()

        # Corrupt the maintained state table behind the ledger's back.
        import sqlite3
        conn = sqlite3.connect(os.path.join(tmp.name, "handoff.db"))
        conn.execute("UPDATE custody_current SET custodian='TAMPERED' WHERE case_id='C1'")
        conn.commit()
        conn.close()

        handoff2 = HandoffRepo(os.path.join(tmp.name, "handoff.db"))
        self.addCleanup(handoff2.close)
        self.assertEqual(handoff2.current_custodian("C1"), "VAULT1")  # repaired from ledger


class LiveProviderIsolation(unittest.TestCase):
    def test_live_provider_cannot_authorise_transfer(self):
        """A live qualification provider serves staleness checks only."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        rules = RuleEventStore(os.path.join(tmp.name, "rules.db"))
        handoff = HandoffRepo(os.path.join(tmp.name, "handoff.db"),
                              qualification_provider=rules.build_product)
        self.addCleanup(rules.close)
        self.addCleanup(handoff.close)
        import_materials(rules, handoff, "materials/751")
        rules.import_holds([
            {"hold_id": "H1", "revision": 2, "case_id": "C1", "start": 25, "end": 35},
        ])
        # No product was ingested, so despite the live provider the handoff
        # side cannot validate a transfer.
        with self.assertRaises(RejectError) as ctx:
            handoff.submit({"handoff_id": "X", "case_id": "C1",
                            "doc_ids": ["D1", "D2"], "from": "LOCAL",
                            "to": "VAULT1", "as_of_day": 40})
        self.assertEqual(ctx.exception.code, "no_qualification")
        self.assertEqual(handoff.current_custodian("C1"), "LOCAL")


class LateEvents(unittest.TestCase):
    def test_late_event_does_not_rewrite_frozen_product_but_stales_plan(self):
        """Choice: handoff consumes frozen products, so a late event only takes
        effect in a newly built product; a plan published earlier goes stale."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        rules = RuleEventStore(os.path.join(tmp.name, "rules.db"))
        handoff = HandoffRepo(os.path.join(tmp.name, "handoff.db"),
                              qualification_provider=rules.build_product)
        self.addCleanup(rules.close)
        self.addCleanup(handoff.close)
        import_materials(rules, handoff, "materials/751")
        rules.import_holds([
            {"hold_id": "H1", "revision": 2, "case_id": "C1", "start": 25, "end": 35},
        ])
        product = rules.build_product(40)
        rules.save_product("Q40a", product)
        handoff.ingest_product("Q40a", product)
        handoff.prepare_and_publish("PL", "Q40a", 40)
        self.assertIn("C1", handoff.get_plan("PL")["eligible"])

        # Late event: a newer CLOSED event moves D1's computed day to 45 > 40,
        # so at day 40 D1 is no longer due.
        rules.import_events([
            {"event_id": "E1-late", "revision": 1, "doc_id": "D1",
             "type": "CLOSED", "day": 25},
        ])
        # Frozen product is unchanged.
        self.assertTrue(rules.load_product("Q40a").documents["D1"].releasable)
        # But the live qualification and therefore the plan are stale.
        self.assertFalse(rules.qualify_document("D1", 40).releasable)
        self.assertIn("D1: qualification changed since publish",
                      " ".join(handoff.plan_staleness("PL")))
        with self.assertRaises(RejectError) as ctx:
            handoff.confirm_handoff_list("PL")
        self.assertEqual(ctx.exception.code, "stale_plan")
        self.assertEqual(handoff.current_custodian("C1"), "LOCAL")


if __name__ == "__main__":
    unittest.main()
