"""重放状态机测试：业务顺序、占座凭据、换座/互换失败与勘误重放。"""

from __future__ import annotations

import unittest

from seat_archive.models import Erratum, SeatMapVersion, TicketEvent
from seat_archive import replay as R


def ev(seq, eid, action, **kw):
    kw.setdefault("status", "accepted")
    return TicketEvent(seq=seq, event_id=eid, action=action, **kw)


def issue(seq, eid, ticket, seat, row="A", number=None, version="v1"):
    # 稳定座位 S1/S2/S3 分别对应票面座号 1/2/3
    number = {"S1": 1, "S2": 2, "S3": 3}[seat] if number is None else number
    return ev(seq, eid, "issue", ticket_id=ticket, map_version=version,
              row=row, number=number, seat_id=seat)


class ReplayFixture(unittest.TestCase):
    def setUp(self):
        self.maps = {"v1": SeatMapVersion("v1", {
            ("A", 1): "S1", ("A", 2): "S2", ("A", 3): "S3",
        })}
        self.seats = frozenset({"S1", "S2", "S3"})

    def replay(self, events, cut=None, revision="original", errata=None):
        return R.replay("T", events, self.maps, self.seats, cut, revision, errata)

    def reason_at(self, snap, seq):
        entry = next(t for t in snap.trace if t["seq"] == seq)
        return entry["status"], entry["reason"]


class AdmissionAndSalesTests(ReplayFixture):
    def test_sold_but_not_admitted_does_not_occupy(self):
        events = [issue(1, "e1", "T1", "S1")]
        snap = self.replay(events)
        self.assertEqual(snap.occupancy, {})

    def test_admission_occupies_and_carries_provenance(self):
        events = [issue(1, "e1", "T1", "S1"),
                  ev(2, "e2", "admit", ticket_id="T1", seat_id="S1")]
        snap = self.replay(events)
        occ = snap.occupancy["S1"]
        self.assertEqual((occ.ticket_id, occ.issued_by, occ.admitted_by),
                         ("T1", "e1", "e2"))

    def test_double_issue_of_seat_rejected(self):
        # 两张不同存根指向同一稳定座位 S1（第二张票面号与第一张相同）
        events = [issue(1, "e1", "T1", "S1"),
                  issue(2, "e2", "T2", "S1", number=1)]
        snap = self.replay(events)
        self.assertEqual(self.reason_at(snap, 2), ("rejected", R.SEAT_ALREADY_SOLD))

    def test_unknown_ticket_admission_rejected(self):
        snap = self.replay([ev(1, "e1", "admit", ticket_id="NOPE", seat_id="S1")])
        self.assertEqual(self.reason_at(snap, 1), ("rejected", R.UNKNOWN_TICKET))

    def test_unknown_map_and_label_rejected(self):
        events = [ev(1, "e1", "issue", ticket_id="T1", map_version="v9",
                     row="A", number=1, seat_id="S1")]
        snap = self.replay(events)
        self.assertEqual(self.reason_at(snap, 1), ("rejected", R.UNKNOWN_MAP))
        events2 = [ev(1, "e1", "issue", ticket_id="T1", map_version="v1",
                      row="Z", number=9, seat_id="S1")]
        snap2 = self.replay(events2)
        self.assertEqual(self.reason_at(snap2, 1), ("rejected", R.UNRESOLVED_LABEL))

    def test_leave_releases_seat(self):
        events = [issue(1, "e1", "T1", "S1"),
                  ev(2, "e2", "admit", ticket_id="T1", seat_id="S1"),
                  ev(3, "e3", "leave", ticket_id="T1")]
        self.assertEqual(set(self.replay(events, cut=2).occupancy), {"S1"})
        self.assertEqual(self.replay(events).occupancy, {})

    def test_void_only_before_admission(self):
        events = [issue(1, "e1", "T1", "S1"),
                  ev(2, "e2", "admit", ticket_id="T1", seat_id="S1"),
                  ev(3, "e3", "void", ticket_id="T1")]
        snap = self.replay(events)
        # 已入场不能直接作废，座位仍占
        self.assertEqual(self.reason_at(snap, 3), ("rejected", R.NOT_SOLD))
        self.assertEqual(set(snap.occupancy), {"S1"})


class SingleSwapTests(ReplayFixture):
    def _three_seat_house(self):
        return [
            issue(1, "e1", "T1", "S1"),
            issue(2, "e2", "T2", "S2"),
            ev(3, "e3", "admit", ticket_id="T1", seat_id="S1"),
            ev(4, "e4", "admit", ticket_id="T2", seat_id="S2"),
        ]

    def test_swap_into_empty_seat_moves_occupancy(self):
        events = self._three_seat_house()
        events.append(ev(5, "e5", "swap", ticket_id="T1", map_version="v1",
                         row="A", number=3, seat_id="S3"))
        snap = self.replay(events)
        self.assertEqual(set(snap.occupancy), {"S2", "S3"})
        self.assertEqual(snap.occupancy["S3"].ticket_id, "T1")
        self.assertEqual(snap.occupancy["S3"].last_move_by, "e5")

    def test_swap_into_occupied_seat_does_not_displace(self):
        events = self._three_seat_house()
        events.append(ev(5, "e5", "swap", ticket_id="T1", map_version="v1",
                         row="A", number=2, seat_id="S2"))
        snap = self.replay(events)
        self.assertEqual(self.reason_at(snap, 5), ("rejected", R.TARGET_NOT_EMPTY))
        # 原观众 T2 未被挤走，T1 仍在 S1
        self.assertEqual(snap.occupancy["S1"].ticket_id, "T1")
        self.assertEqual(snap.occupancy["S2"].ticket_id, "T2")

    def test_swap_with_voided_ticket_rejected(self):
        events = [issue(1, "e1", "T1", "S1"),
                  ev(2, "e2", "void", ticket_id="T1"),
                  ev(3, "e3", "swap", ticket_id="T1", map_version="v1",
                     row="A", number=2, seat_id="S2")]
        snap = self.replay(events)
        self.assertEqual(self.reason_at(snap, 3), ("rejected", R.TICKET_VOIDED))
        self.assertEqual(snap.occupancy, {})

    def test_sold_not_present_cannot_swap(self):
        events = [issue(1, "e1", "T1", "S1"),
                  ev(2, "e2", "swap", ticket_id="T1", map_version="v1",
                     row="A", number=2, seat_id="S2")]
        snap = self.replay(events)
        self.assertEqual(self.reason_at(snap, 2), ("rejected", R.NOT_PRESENT))


class ExchangeTests(ReplayFixture):
    def _two_present(self):
        return [
            issue(1, "e1", "T1", "S1"),
            issue(2, "e2", "T2", "S2"),
            ev(3, "e3", "admit", ticket_id="T1", seat_id="S1"),
            ev(4, "e4", "admit", ticket_id="T2", seat_id="S2"),
        ]

    def test_successful_exchange_is_simultaneous(self):
        events = self._two_present()
        events.append(ev(5, "e5", "exchange", ticket_id="T1", seat_id="S2",
                         ticket2_id="T2", seat2_id="S1"))
        snap = self.replay(events)
        self.assertEqual(snap.occupancy["S1"].ticket_id, "T2")
        self.assertEqual(snap.occupancy["S2"].ticket_id, "T1")
        self.assertEqual(snap.occupancy["S1"].last_move_by, "e5")

    def test_missing_party_rejected_and_state_untouched(self):
        events = self._two_present()
        events.append(ev(5, "e5", "exchange", ticket_id="T1", seat_id="S2",
                         ticket2_id="GHOST", seat2_id="S1"))
        snap = self.replay(events)
        self.assertEqual(self.reason_at(snap, 5), ("rejected", R.PARTY_MISSING))
        self.assertEqual(snap.occupancy["S1"].ticket_id, "T1")
        self.assertEqual(snap.occupancy["S2"].ticket_id, "T2")

    def test_occupied_target_without_matching_mover_rejected(self):
        # T1 想换到 S3（T3 占用，T3 不参与），T2 目标 S1：整体拒绝
        events = self._two_present()
        events += [
            issue(5, "e5", "T3", "S3"),
            ev(6, "e6", "admit", ticket_id="T3", seat_id="S3"),
            # T1 目标 S3（由 T3 占用且 T3 不参与），整体拒绝
            ev(7, "e7", "exchange", ticket_id="T1", seat_id="S3",
               ticket2_id="T2", seat2_id="S1"),
        ]
        snap = self.replay(events)
        self.assertEqual(self.reason_at(snap, 7), ("rejected", R.TARGET_NOT_EMPTY))
        self.assertEqual(snap.occupancy["S1"].ticket_id, "T1")
        self.assertEqual(snap.occupancy["S3"].ticket_id, "T3")

    def test_exchange_with_non_present_or_voided_party_rejected(self):
        # T2 离场后作废：互换因一方既不在场也是废票而拒绝，T1 原地不动
        events = self._two_present()
        events += [
            ev(5, "e5", "leave", ticket_id="T2"),
            ev(6, "e6", "void", ticket_id="T2"),
            ev(7, "e7", "exchange", ticket_id="T1", seat_id="S2",
               ticket2_id="T2", seat2_id="S1"),
        ]
        snap = self.replay(events)
        status, reason = self.reason_at(snap, 7)
        self.assertEqual(status, "rejected")
        self.assertIn(reason, (R.PARTY_VOIDED, R.PARTY_NOT_PRESENT))
        self.assertEqual(snap.occupancy["S1"].ticket_id, "T1")
        self.assertNotIn("S2", snap.occupancy)

    def test_exchange_rejected_when_party_holds_voided_stub(self):
        # T2 售出后从未入场即作废：持废票一方参与互换必须被拒，不能挤走 T1
        events = [
            issue(1, "e1", "T1", "S1"),
            issue(2, "e2", "T2", "S2"),
            ev(3, "e3", "admit", ticket_id="T1", seat_id="S1"),
            ev(4, "e4", "void", ticket_id="T2"),
            ev(5, "e5", "exchange", ticket_id="T1", seat_id="S2",
               ticket2_id="T2", seat2_id="S1"),
        ]
        snap = self.replay(events)
        self.assertEqual(self.reason_at(snap, 5), ("rejected", R.PARTY_VOIDED))
        self.assertEqual(snap.occupancy["S1"].ticket_id, "T1")
        self.assertNotIn("S2", snap.occupancy)


class ErratumAndCutTests(ReplayFixture):
    def test_cut_stops_at_sequence(self):
        events = [
            issue(1, "e1", "T1", "S1"),
            ev(2, "e2", "admit", ticket_id="T1", seat_id="S1"),
            ev(3, "e3", "leave", ticket_id="T1"),
        ]
        self.assertEqual(set(self.replay(events, cut=2).occupancy), {"S1"})
        self.assertEqual(self.replay(events, cut=1).occupancy, {})

    def test_erratum_replays_corrected_action_while_original_stays_readable(self):
        events = [
            issue(1, "e1", "T1", "S1"),
            issue(2, "e2", "T2", "S2"),
            ev(3, "e3", "admit", ticket_id="T1", seat_id="S1"),
            ev(4, "e4", "admit", ticket_id="T2", seat_id="S2"),
            ev(5, "e5", "exchange", ticket_id="T1", seat_id="S2",
                ticket2_id="T2", seat2_id="S1"),
        ]
        erratum = Erratum(
            erratum_id="ER1", revision=2, target_event_id="e5",
            replacement={"action": "leave", "ticket_id": "T1"},
            note="更正：T1 当日离场，未发生互换",
        )
        original = self.replay(events)
        corrected = self.replay(events, revision="r2", errata=[erratum])
        # 旧修订仍读到互换结果
        self.assertEqual(original.occupancy["S1"].ticket_id, "T2")
        # 新修订按更正动作重放：T1 离场，T2 仍在 S2
        self.assertEqual(set(corrected.occupancy), {"S2"})
        self.assertEqual(corrected.occupancy["S2"].ticket_id, "T2")
        self.assertEqual(corrected.errata_applied, ["ER1"])
        basis = {t["event_id"]: t["replay_basis"] for t in corrected.trace}
        self.assertEqual(basis["e5"], "replacement")
        self.assertEqual(basis["e1"], "as_recorded")


if __name__ == "__main__":
    unittest.main()
