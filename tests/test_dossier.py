"""内置案卷端到端测试：从存根重放到选牌，覆盖题面要求的全部情形。

- 案卷 A：题面三幕（换座前选左牌、换座后选右牌、移动眼点选右牌）
- 案卷 B：多版座图、售出未入场、作废/离场、单人换座、双人互换与失败三例、
  勘误重放、重叠阴影、多观察点选择、牌号破同
"""

from __future__ import annotations

import os
import tempfile
import unittest

from seat_archive.cli import _build_study_report
from seat_archive.dossier import dossier_a, dossier_b
from seat_archive.replay import replay
from seat_archive.store import DossierStore


class DossierATests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = dossier_a(os.path.join(self.tmp.name, "A"))

    def tearDown(self):
        self.tmp.cleanup()

    def _report(self, study_id):
        return _build_study_report(self.store, study_id)

    def test_before_swap_selects_left_board_with_lengths_2_and_1(self):
        report = self._report("A1_BEFORE_SWAP")
        view = report["observers"]["EYE0"]["boards"]
        self.assertEqual(view["B_LEFT"]["visible_length"], "2")
        self.assertEqual(view["B_RIGHT"]["visible_length"], "1")
        self.assertEqual(report["selected_board"], "B_LEFT")

    def test_after_swap_selects_right_board(self):
        report = self._report("A2_AFTER_SWAP")
        view = report["observers"]["EYE0"]["boards"]
        self.assertEqual(view["B_LEFT"]["visible_length"], "3/2")
        self.assertEqual(view["B_RIGHT"]["visible_length"], "3")
        self.assertEqual(report["selected_board"], "B_RIGHT")

    def test_eye_moved_with_fixed_house_selects_right_board(self):
        report = self._report("A3_EYE_MOVED")
        view = report["observers"]["EYE3"]["boards"]
        self.assertEqual(view["B_LEFT"]["visible_length"], "1")
        self.assertEqual(view["B_RIGHT"]["visible_length"], "3")
        self.assertEqual(report["selected_board"], "B_RIGHT")

    def test_sold_unadmitted_and_voided_tickets_leave_no_occupancy(self):
        snap = replay(self.store.dossier_id, self.store.events(),
                      self.store.maps(), frozenset(self.store.seats()), 6,
                      "original", self.store.errata())
        self.assertEqual(list(snap.occupancy), ["S1"])
        reasons = {t["seq"]: (t["status"], t["reason"]) for t in snap.trace}
        self.assertEqual(reasons[4], ("rejected", "ticket_not_present"))
        self.assertEqual(reasons[6], ("rejected", "ticket_voided"))

    def test_erratum_r2_restores_pre_swap_occupancy(self):
        snap = replay(self.store.dossier_id, self.store.events(),
                      self.store.maps(), frozenset(self.store.seats()), 7,
                      "r2", self.store.errata())
        self.assertEqual(list(snap.occupancy), ["S1"])
        self.assertEqual(snap.occupancy["S1"].ticket_id, "T1")
        self.assertEqual(snap.errata_applied, ["ER-A1"])


class DossierBTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = dossier_b(os.path.join(self.tmp.name, "B"))

    def tearDown(self):
        self.tmp.cleanup()

    def _snapshot(self, revision="original"):
        return replay(self.store.dossier_id, self.store.events(),
                      self.store.maps(), frozenset(self.store.seats()), 24,
                      revision, self.store.errata())

    def test_sold_but_never_admitted_t8_never_occupies(self):
        snap = self._snapshot()
        self.assertNotIn("S8", snap.occupancy)

    def test_all_rejected_actions_keep_state_and_record_reasons(self):
        snap = self._snapshot()
        reasons = {t["seq"]: (t["status"], t["reason"]) for t in snap.trace}
        self.assertEqual(reasons[16], ("rejected", "unknown_ticket"))
        self.assertEqual(reasons[17], ("rejected", "ticket_not_present"))
        self.assertEqual(reasons[19], ("rejected", "target_not_empty"))
        self.assertEqual(reasons[20], ("rejected", "ticket_voided"))
        self.assertEqual(reasons[21], ("rejected", "party_missing"))
        self.assertEqual(reasons[22], ("rejected", "target_not_empty"))
        # 被拒目标座位的原观众从未被挤走：e22 时 S3 仍属 T3；
        # e23 的成功互换后 T1/T2 交换座位，各凭据仍可追溯
        self.assertEqual(snap.occupancy["S1"].ticket_id, "T2")
        self.assertEqual(snap.occupancy["S2"].ticket_id, "T1")
        self.assertEqual(snap.occupancy["S3"].ticket_id, "T3")

    def test_successful_double_exchange_swaps_both_at_once(self):
        snap = self._snapshot()
        self.assertEqual(snap.occupancy["S1"].ticket_id, "T2")
        self.assertEqual(snap.occupancy["S2"].ticket_id, "T1")

    def test_leave_creates_empty_seat_without_shadow(self):
        snap = self._snapshot()
        self.assertNotIn("S7", snap.occupancy)

    def test_erratum_corrected_revision_differs_but_original_stays_readable(self):
        original = self._snapshot("original")
        corrected = self._snapshot("r2")
        # 旧版切面：T1/T2 已互换，T5/T6 未动
        self.assertEqual(original.occupancy["S1"].ticket_id, "T2")
        self.assertEqual(original.occupancy["S5"].ticket_id, "T5")
        # 勘误版：T1/T2 未动，T5/T6 按更正动作互换
        self.assertEqual(corrected.occupancy["S1"].ticket_id, "T1")
        self.assertEqual(corrected.occupancy["S5"].ticket_id, "T6")
        self.assertEqual(corrected.occupancy["S6"].ticket_id, "T5")
        self.assertEqual(corrected.errata_applied, ["ER-B1"])

    def test_multi_observer_selection_and_overlapping_shadows(self):
        report = _build_study_report(self.store, "B-CORRECTED")
        # EYE0 在 B2 上有三段独立阴影，证明多阴影重叠合并后只扣一次
        eye0_b2 = report["observers"]["EYE0"]["boards"]["B2"]
        blocked = eye0_b2["blocked_length"]
        visible = eye0_b2["visible_length"]
        self.assertEqual(blocked, "9/4")
        self.assertEqual(visible, "3/4")  # 牌宽 3
        # 最差观察者驱动：B3 的最差可见长度 3/2 最大
        self.assertEqual(report["selected_board"], "B3")
        self.assertEqual(report["selected_worst_visible_length"], "3/2")

    def test_tie_broken_by_given_board_id(self):
        report = _build_study_report(self.store, "B-TIE")
        worst = {row["board_id"]: row["worst_visible_length"] for row in report["ranking"]}
        self.assertEqual(worst, {"B4": "3", "B5": "3"})
        self.assertEqual(report["selected_board"], "B4")

    def test_occupancy_carries_original_vouchers(self):
        snap = self._snapshot("r2")
        occ = snap.occupancy["S5"]
        self.assertEqual((occ.ticket_id, occ.issued_by, occ.admitted_by, occ.last_move_by),
                         ("T6", "e6", "e14", "e23"))


if __name__ == "__main__":
    unittest.main()
