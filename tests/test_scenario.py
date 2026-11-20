"""End-to-end and scale tests: the documented 751 flow and 200/10/500/100."""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from archive_handoff.scenario import (generate_scale, run_operations, run_scale,
                                      run_scenario)


class DocumentedScenario(unittest.TestCase):
    def test_751_walkthrough(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        result = run_scenario(os.path.join(tmp.name, "demo"))
        self.assertTrue(result["ok"], json.dumps(result.get("expected"), indent=2))
        # Every documented expectation is encoded as a positive boolean.
        for label, holds in result["expected"].items():
            self.assertTrue(holds, f"documented expectation failed: {label}")
        self.assertEqual(set(result["expected"]), {
            "P1_D1_releasable", "P1_D4_missing_trigger",
            "P2_D1_releasable", "P2_D2_releasable", "P2_D3_releasable",
            "P2_D4_still_missing", "boundary_34_frozen", "boundary_35_free",
            "D1_computed_not_paused", "P1_frozen_H1_end_50", "C1_after_whole1",
        })

        steps = {s["step"]: s for s in result["steps"]}
        self.assertEqual(steps["PARTIAL"]["code"], "not_whole_case")
        self.assertEqual(steps["custody_after_PARTIAL"]["C1"], "LOCAL")
        self.assertTrue(steps["WHOLE1_replay"]["replayed"])
        self.assertEqual(steps["WHOLE_new_id_from_LOCAL"]["code"], "custody_mismatch")
        self.assertEqual(steps["confirm_plan_P1"]["code"], "stale_plan")

    def test_artifacts_are_written_and_reopen(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        data = os.path.join(tmp.name, "demo")
        run_scenario(data)
        for name in ("P1.json", "P2.json", "scenario.json"):
            self.assertTrue(os.path.exists(os.path.join(data, "artifacts", name)), name)
        # Reopening the databases keeps the frozen P1 hold window at 50.
        from archive_handoff import RuleEventStore, HandoffRepo
        rules = RuleEventStore(os.path.join(data, "rules_events.sqlite"))
        handoff = HandoffRepo(os.path.join(data, "handoff.sqlite"))
        self.addCleanup(rules.close)
        self.addCleanup(handoff.close)
        p1 = rules.load_product("P1")
        self.assertEqual(p1.documents["D1"].holds_active[0].end, 50)
        self.assertEqual(handoff.current_custodian("C1"), "VAULT1")
        self.assertTrue(handoff.verify()["ok"])


class OperationsFlow(unittest.TestCase):
    def test_materials_operations(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        result = run_operations(os.path.join(tmp.name, "ops"))
        self.assertTrue(result["ok"], json.dumps(result, indent=2))
        # Pre-correction plan: C1 blocked by H1, C2 eligible.
        first = result["steps"][0]
        self.assertEqual(first["eligible"], ["C2"])
        self.assertIn("C1", first["blocked"])
        # Confirmation of the pre-correction plan is refused as stale.
        confirm = result["steps"][2]
        self.assertEqual(confirm["outcome"], "rejected")
        self.assertEqual(confirm["code"], "stale_plan")
        # Post-correction plan is usable and the historical P1 is unchanged.
        second = result["steps"][3]
        self.assertIn("C1", second["eligible"])
        historical = result["steps"][4]["plan"]
        self.assertEqual(sorted(historical["eligible"]), ["C2"])
        self.assertEqual(sorted(historical["blocked"]), ["C1", "C3"])
        # No custody moved (confirm was refused; plans do not transfer).
        self.assertEqual(result["custody"], {"C1": "LOCAL", "C2": "LOCAL",
                                             "C3": "LOCAL"})


class ScaleDataset(unittest.TestCase):
    def test_scale_targets(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        materials = os.path.join(tmp.name, "materials")
        stats = generate_scale(materials)
        self.assertEqual(stats["documents"], 200)
        self.assertEqual(stats["rules"], 10)
        self.assertEqual(stats["events_plus_holds"], 500)
        self.assertEqual(stats["requests"], 100)

        result = run_scale(os.path.join(tmp.name, "data"), materials)
        self.assertTrue(result["ok"], result.get("failures"))
        self.assertEqual(result["handoff_count"], 15)
        outcomes = result["outcomes"]
        self.assertEqual(outcomes.get("accept"), 15)
        self.assertEqual(outcomes.get("reject_not_whole_case"), 58)
        self.assertEqual(outcomes.get("reject_custody_mismatch"), 15)
        self.assertEqual(outcomes.get("reject_ineligible_members"), 12)


if __name__ == "__main__":
    unittest.main()
