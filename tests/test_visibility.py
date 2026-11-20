"""可见度几何与选牌测试：全部用精确 Fraction 断言。

覆盖：题面三幕数值例、重叠阴影只扣一次、端点相碰不扣长度、
多观察点最差长度最大化、等宽校验与牌号破同。
"""

from __future__ import annotations

import unittest
from fractions import Fraction

from seat_archive.models import CandidateBoard, Observer, Occupancy, Seat
from seat_archive.rationals import parse_q
from seat_archive.replay import Snapshot
from seat_archive.visibility import (
    Segment,
    board_shadows,
    clip_segments,
    evaluate_study,
    merge_segments,
    project_shadow,
    subtract_segments,
    total_length,
)


def _seat(seat_id, y, lx, rx):
    return Seat(seat_id, parse_q(y), parse_q(lx), parse_q(rx))


def _snapshot(occ):
    return Snapshot(
        dossier_id="T",
        revision="original",
        cut_seq=1,
        occupancy={
            seat_id: Occupancy(seat_id=seat_id, ticket_id=f"TK-{seat_id}",
                               issued_by="i", admitted_by="a")
            for seat_id in occ
        },
    )


class ProjectionTests(unittest.TestCase):
    def test_projection_factor_mid_height(self):
        # 眼点 0、y=5、座位段 [1, 5/2] -> 字幕线阴影 [2, 5]（题面给定）
        seg = project_shadow(_seat("S1", "5", "1", "5/2"), Fraction(0))
        self.assertEqual((seg.lo, seg.hi), (Fraction(2), Fraction(5)))

    def test_projection_with_shifted_eye(self):
        # 题面第三幕：同座位、眼点移到 3 -> 阴影 [-1, 2]（y=5 因子 2：2x-e）
        seg = project_shadow(_seat("S1", "5", "1", "5/2"), Fraction(3))
        self.assertEqual((seg.lo, seg.hi), (Fraction(-1), Fraction(2)))

    def test_projection_swapped_seat_gives_left_shadow(self):
        # 题面第二幕：换座后座位段 [0, 3/4]、眼点 0 -> 阴影 [0, 3/2]
        seg = project_shadow(_seat("S2", "5", "0", "3/4"), Fraction(0))
        self.assertEqual((seg.lo, seg.hi), (Fraction(0), Fraction(3, 2)))


class SegmentArithmeticTests(unittest.TestCase):
    def test_overlapping_shadows_counted_once(self):
        # 两个明显重叠的阴影，合并后长度只扣一次
        merged = merge_segments([Segment(Fraction(0), Fraction(2)),
                                 Segment(Fraction(1), Fraction(3))])
        self.assertEqual(merged, [Segment(Fraction(0), Fraction(3))])
        self.assertEqual(total_length(merged), Fraction(3))

    def test_endpoint_touch_has_zero_effect_on_length(self):
        # 仅端点相碰：[0,1] 与 [1,2] 合并成长度 2，缺口长度为 0
        merged = merge_segments([Segment(Fraction(0), Fraction(1)),
                                 Segment(Fraction(1), Fraction(2))])
        self.assertEqual(total_length(merged), Fraction(2))
        board = CandidateBoard("B", Fraction(0), Fraction(2))
        visible = subtract_segments(board, merged)
        self.assertEqual(total_length(visible), Fraction(0))

    def test_clip_drops_point_touch(self):
        # 阴影只在端点 x=3 碰到牌区间起点，不产生遮挡长度
        clipped = clip_segments([Segment(Fraction(2), Fraction(3))],
                                Fraction(3), Fraction(6))
        self.assertEqual(clipped, [])


class ProblemStatementBoardsTests(unittest.TestCase):
    """题面：左牌 [0,3]、右牌 [3,6]，等宽 3。"""

    def setUp(self):
        self.boards = [CandidateBoard("B_LEFT", Fraction(0), Fraction(3)),
                       CandidateBoard("B_RIGHT", Fraction(3), Fraction(6))]

    def test_before_swap_chooses_left_board(self):
        # 阴影 [2,5]：左牌可见 2（[0,2]），右牌可见 1（[5,6]）
        snap = _snapshot(["S1"])
        seats = {"S1": _seat("S1", "5", "1", "5/2")}
        report = evaluate_study(snap, seats, [Observer("E0", Fraction(0))], self.boards)
        views = report["observers"]["E0"]["boards"]
        self.assertEqual(views["B_LEFT"]["visible_length"], "2")
        self.assertEqual(views["B_RIGHT"]["visible_length"], "1")
        self.assertEqual(report["selected_board"], "B_LEFT")

    def test_after_swap_chooses_right_board(self):
        # 阴影 [0,3/2]：左牌可见 3/2，右牌完整可见 3
        snap = _snapshot(["S2"])
        seats = {"S2": _seat("S2", "5", "0", "3/4")}
        report = evaluate_study(snap, seats, [Observer("E0", Fraction(0))], self.boards)
        views = report["observers"]["E0"]["boards"]
        self.assertEqual(views["B_LEFT"]["visible_length"], "3/2")
        self.assertEqual(views["B_RIGHT"]["visible_length"], "3")
        self.assertEqual(report["selected_board"], "B_RIGHT")

    def test_eye_moved_chooses_right_board(self):
        # 固定占座 S1，眼点移到 3：阴影 [-1,2]，左牌仅 [2,3] 可见 1，右牌完整可见 3
        snap = _snapshot(["S1"])
        seats = {"S1": _seat("S1", "5", "1", "5/2")}
        report = evaluate_study(snap, seats, [Observer("E3", Fraction(3))], self.boards)
        views = report["observers"]["E3"]["boards"]
        self.assertEqual(views["B_LEFT"]["visible_length"], "1")
        self.assertEqual(views["B_RIGHT"]["visible_length"], "3")
        self.assertEqual(report["selected_board"], "B_RIGHT")


class MultiObserverAndTieTests(unittest.TestCase):
    def test_worst_observer_drives_choice(self):
        # 座位 S1@y=5 [1,5/2]。眼点 0 阴影 [2,5]；眼点 -6 阴影 [8,11]。
        # B1=[0,3]：眼0 可见 2、眼-6 完整可见 3 -> 最差 2
        # B2=[3,6]：眼0 可见 1、眼-6 完整可见 3 -> 最差 1
        # 因此最差长度 B1(2) > B2(1)，选 B1（避免被单个观察者的好成绩误导）
        seat = _seat("S1", "5", "1", "5/2")
        snap = _snapshot(["S1"])
        boards = [CandidateBoard("B1", Fraction(0), Fraction(3)),
                  CandidateBoard("B2", Fraction(3), Fraction(6))]
        report = evaluate_study(
            snap, {"S1": seat},
            [Observer("E0", Fraction(0)), Observer("EL", Fraction(-6))],
            boards,
        )
        self.assertEqual(report["ranking"][0]["board_id"], "B1")
        self.assertEqual(report["selected_worst_visible_length"], "2")
        worst = {row["board_id"]: row["worst_visible_length"] for row in report["ranking"]}
        self.assertEqual(worst, {"B1": "2", "B2": "1"})

    def test_tie_broken_by_board_id(self):
        seat = _seat("S1", "5", "1", "5/2")
        snap = _snapshot(["S1"])
        boards = [CandidateBoard("B_Z", Fraction(30), Fraction(33)),
                  CandidateBoard("B_A", Fraction(33), Fraction(36))]
        report = evaluate_study(snap, {"S1": seat},
                                [Observer("E0", Fraction(0))], boards)
        # 两块牌均无遮挡，最差长度都是 3，按牌号取 B_A
        self.assertEqual(report["selected_board"], "B_A")
        self.assertEqual(report["selected_worst_visible_length"], "3")

    def test_unequal_width_rejected(self):
        snap = _snapshot(["S1"])
        boards = [CandidateBoard("B1", Fraction(0), Fraction(3)),
                  CandidateBoard("B2", Fraction(0), Fraction(4))]
        with self.assertRaises(ValueError):
            evaluate_study(snap, {"S1": _seat("S1", "5", "1", "5/2")},
                           [Observer("E0", Fraction(0))], boards)

    def test_empty_house_full_visibility(self):
        snap = _snapshot([])
        boards = [CandidateBoard("B1", Fraction(0), Fraction(3))]
        report = evaluate_study(snap, {}, [Observer("E0", Fraction(0))], boards)
        self.assertEqual(report["observers"]["E0"]["union_shadows"], [])
        self.assertEqual(report["selected_worst_visible_length"], "3")


if __name__ == "__main__":
    unittest.main()
