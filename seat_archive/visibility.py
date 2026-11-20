"""字幕牌可见度：横截面直线遮挡的精确有理数计算。

几何约定（题内固定）：

- 字幕牌在水平线 ``y=10``，阅览眼点在 ``y=0``，眼点横坐标为 ``eye_x``。
- 实际占用座位在高度 ``0<y<10`` 给出横向不透明闭线段 ``[lx, rx]``。
- 眼点到字幕位置 ``(x_s, 10)`` 的直线穿过任一遮挡线段即被挡住。
- 空座不产生遮挡；多个阴影重叠只扣一次；仅端点相碰对长度无影响
  （闭线段相接只在零测度点上，长度不变）。

座位线段到字幕线的投影（眼点 (eye_x,0) 与座位点 (x,y) 的直线延至 y=10）::

    x_s = (10*x - eye_x*y) / (10 - y)
        = eye_x + (x - eye_x) * 10 / (10 - y)

比例因子为 ``10/(10-y)``：阴影落在座位远离眼点的一侧。例如眼点 0、y=5
的座位段 [1, 5/2] 投影为字幕线上的 [2, 5]（题面给定结果）。
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Iterable

from .models import CandidateBoard, Observer, Seat
from .rationals import fmt_q
from .replay import Snapshot

Y_EYE = Fraction(0)
Y_BOARD = Fraction(10)


@dataclass(frozen=True)
class Segment:
    lo: Fraction
    hi: Fraction

    def length(self) -> Fraction:
        return self.hi - self.lo

    def as_dict(self) -> dict:
        return {"lo": fmt_q(self.lo), "hi": fmt_q(self.hi)}


def project_shadow(seat: Seat, eye_x: Fraction) -> Segment:
    """单个座位线段对某眼点在字幕线上的遮挡投影。"""
    factor = Y_BOARD / (Y_BOARD - seat.y)
    lo = eye_x + (seat.lx - eye_x) * factor
    hi = eye_x + (seat.rx - eye_x) * factor
    return Segment(lo, hi)


def merge_segments(segments: Iterable[Segment]) -> list[Segment]:
    """合并重叠/相接的闭线段；重叠阴影只保留一份。"""
    ordered = sorted(segments, key=lambda seg: (seg.lo, seg.hi))
    merged: list[Segment] = []
    for seg in ordered:
        if not merged or seg.lo > merged[-1].hi:
            merged.append(Segment(seg.lo, seg.hi))
        elif seg.hi > merged[-1].hi:
            merged.append(Segment(merged.pop().lo, seg.hi))
    return merged


def clip_segments(segments: list[Segment], lo: Fraction, hi: Fraction) -> list[Segment]:
    """把已合并的阴影线段裁剪到候选牌区间 [lo, hi] 内。"""
    clipped: list[Segment] = []
    for seg in segments:
        c_lo = max(seg.lo, lo)
        c_hi = min(seg.hi, hi)
        if c_lo < c_hi:  # 仅端点相碰（相等）不计长度
            clipped.append(Segment(c_lo, c_hi))
    return merge_segments(clipped)


def subtract_segments(board: CandidateBoard, blocked: list[Segment]) -> list[Segment]:
    """牌区间减去被遮挡部分，返回真实可见的闭区段列表。"""
    visible: list[Segment] = []
    cursor = board.lo
    for seg in blocked:
        if seg.lo > cursor:
            visible.append(Segment(cursor, seg.lo))
        if seg.hi > cursor:
            cursor = seg.hi
        if cursor >= board.hi:
            break
    if cursor < board.hi:
        visible.append(Segment(cursor, board.hi))
    return visible


def total_length(segments: list[Segment]) -> Fraction:
    return sum((seg.length() for seg in segments), Fraction(0))


def board_shadows(
    observer: Observer,
    occupied_seats: list[Seat],
    board: CandidateBoard,
) -> tuple[list[Segment], list[Segment]]:
    """返回 (牌内被遮挡并集, 牌内可见区段)。"""
    raw = [project_shadow(seat, observer.eye_x) for seat in occupied_seats]
    union = merge_segments(raw)
    blocked_in_board = clip_segments(union, board.lo, board.hi)
    visible = subtract_segments(board, blocked_in_board)
    return blocked_in_board, visible


def _equal_width(boards: list[CandidateBoard]) -> Fraction:
    widths = {b.hi - b.lo for b in boards}
    if len(widths) != 1:
        raise ValueError("候选字幕牌必须等宽")
    width = widths.pop()
    if width <= 0:
        raise ValueError("候选字幕牌长度必须为正")
    return width


def evaluate_study(
    snapshot: Snapshot,
    seats: dict[str, Seat],
    observers: list[Observer],
    boards: list[CandidateBoard],
) -> dict:
    """计算各观察者对各候选牌的真实可见区段，并选出最优牌。

    选择标准：让最差观察者的可见长度最大；平手按给定牌号（board_id）破同。
    """
    width = _equal_width(list(boards))
    occupied = [seats[seat_id] for seat_id in sorted(snapshot.occupancy)]

    per_observer: dict[str, dict] = {}
    # 每块牌的“最差观察者可见长度”
    worst: dict[str, Fraction] = {b.board_id: width for b in boards}

    for observer in observers:
        raw_shadows = merge_segments(
            [project_shadow(seat, observer.eye_x) for seat in occupied]
        )
        board_views: dict[str, dict] = {}
        for board in boards:
            blocked, visible = board_shadows(observer, occupied, board)
            visible_len = total_length(visible)
            blocked_len = total_length(blocked)
            board_views[board.board_id] = {
                "visible_segments": [seg.as_dict() for seg in visible],
                "visible_length": fmt_q(visible_len),
                "blocked_segments": [seg.as_dict() for seg in blocked],
                "blocked_length": fmt_q(blocked_len),
            }
            worst[board.board_id] = min(worst[board.board_id], visible_len)
        per_observer[observer.observer_id] = {
            "eye_x": fmt_q(observer.eye_x),
            "union_shadows": [seg.as_dict() for seg in raw_shadows],
            "boards": board_views,
        }

    def rank_key(board: CandidateBoard):
        # 最大最差可见长度；平手按牌号字典序（即“给定牌号”破同）
        return (-worst[board.board_id], board.board_id)

    chosen = sorted(boards, key=rank_key)[0]
    ranking = [
        {
            "board_id": b.board_id,
            "worst_visible_length": fmt_q(worst[b.board_id]),
        }
        for b in sorted(boards, key=rank_key)
    ]
    return {
        "dossier_id": snapshot.dossier_id,
        "revision": snapshot.revision,
        "cut_seq": snapshot.cut_seq,
        "board_width": fmt_q(width),
        "occupied_seats": [
            snapshot.occupancy[k].as_dict() for k in sorted(snapshot.occupancy)
        ],
        "observers": per_observer,
        "ranking": ranking,
        "selected_board": chosen.board_id,
        "selected_worst_visible_length": fmt_q(worst[chosen.board_id]),
    }
