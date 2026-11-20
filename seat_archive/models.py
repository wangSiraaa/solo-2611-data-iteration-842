"""不可变领域记录。

全部记录以普通 JSON 落盘，不依赖 ORM。座位的几何位置（横截面高度 y、
该高度上的不透明闭线段 [lx, rx]）以精确有理数字符串保存。

案卷身份与可复查性由 :mod:`seat_archive.store` 的追加式写入与内容摘要保证。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Optional

from .rationals import fmt_q, parse_q


# ---------------------------------------------------------------- 几何与座位


@dataclass(frozen=True)
class Seat:
    """稳定座位：跨座图版本不变的物理座位及其横截面线段。

    ``y`` 严格位于 0（眼点高度）与 10（字幕牌高度）之间。
    """

    seat_id: str
    y: Fraction
    lx: Fraction  # 该高度上不透明闭线段左端
    rx: Fraction  # 右端

    def __post_init__(self) -> None:
        if not (Fraction(0) < self.y < Fraction(10)):
            raise ValueError(f"座位 {self.seat_id} 的 y={self.y} 不在 0<y<10 内")
        if self.rx <= self.lx:
            raise ValueError(f"座位 {self.seat_id} 的线段退化或反向")

    def as_dict(self) -> dict:
        return {
            "seat_id": self.seat_id,
            "y": fmt_q(self.y),
            "lx": fmt_q(self.lx),
            "rx": fmt_q(self.rx),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Seat":
        return cls(d["seat_id"], parse_q(d["y"]), parse_q(d["lx"]), parse_q(d["rx"]))


@dataclass(frozen=True)
class SeatMapVersion:
    """同一案卷内保留的一版座图：把票面 (排号, 座号) 解释到稳定座位。"""

    map_version: str
    labels: dict[tuple[str, int], str]  # (row, number) -> seat_id

    def resolve(self, row: str, number: int) -> str:
        key = (row, number)
        if key not in self.labels:
            raise KeyError(f"座图 {self.map_version} 未记载票面 {row}{number}")
        return self.labels[key]

    def as_dict(self) -> dict:
        return {
            "map_version": self.map_version,
            "labels": [
                {"row": row, "number": number, "seat_id": seat_id}
                for (row, number), seat_id in sorted(self.labels.items())
            ],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SeatMapVersion":
        labels = {(item["row"], item["number"]): item["seat_id"] for item in d["labels"]}
        return cls(d["map_version"], labels)


@dataclass(frozen=True)
class TicketEvent:
    """一张存根在业务流水上的一次动作（含被拒动作，拒绝动作不改状态）。

    动作类型：
      issue   发票（票面记载 map_version/row/number，先解释为稳定 seat_id）
      admit   持票入场，占座
      leave   持票离场，释放
      void    作废存根（未入场时方可作废）
      swap    单人换到空座
      exchange 双人互换，按换前状态同时核对、同时生效

    被拒记录在档案里仍带 ``status="rejected"`` 与 ``reason``，可复查。
    """

    seq: int
    event_id: str
    action: str
    ticket_id: Optional[str] = None
    map_version: Optional[str] = None
    # 票面记载（按凭证自身版本解释）
    row: Optional[str] = None
    number: Optional[int] = None
    # 解释后的稳定座位（双人动作为另一成员座位）
    seat_id: Optional[str] = None
    ticket2_id: Optional[str] = None
    seat2_id: Optional[str] = None
    status: str = "accepted"  # accepted | rejected
    reason: Optional[str] = None

    _ACTIONS = frozenset({"issue", "admit", "leave", "void", "swap", "exchange"})

    def __post_init__(self) -> None:
        if self.action not in self._ACTIONS:
            raise ValueError(f"未知动作 {self.action}")
        if self.status not in {"accepted", "rejected"}:
            raise ValueError(f"未知事件状态 {self.status}")

    def as_dict(self) -> dict:
        d = {
            "seq": self.seq,
            "event_id": self.event_id,
            "action": self.action,
            "ticket_id": self.ticket_id,
            "map_version": self.map_version,
            "row": self.row,
            "number": self.number,
            "seat_id": self.seat_id,
            "ticket2_id": self.ticket2_id,
            "seat2_id": self.seat2_id,
            "status": self.status,
        }
        if self.reason is not None:
            d["reason"] = self.reason
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "TicketEvent":
        return cls(
            seq=d["seq"],
            event_id=d["event_id"],
            action=d["action"],
            ticket_id=d.get("ticket_id"),
            map_version=d.get("map_version"),
            row=d.get("row"),
            number=d.get("number"),
            seat_id=d.get("seat_id"),
            ticket2_id=d.get("ticket2_id"),
            seat2_id=d.get("seat2_id"),
            status=d.get("status", "accepted"),
            reason=d.get("reason"),
        )


@dataclass(frozen=True)
class Erratum:
    """后补勘误：把 ``target_event_id`` 替换为更正动作，另起修订重放。

    旧修订（原始流水）保持可读；勘误本身也是追加记录，只增不删。
    """

    erratum_id: str
    revision: int
    target_event_id: str
    replacement: dict
    note: str

    def as_dict(self) -> dict:
        return {
            "erratum_id": self.erratum_id,
            "revision": self.revision,
            "target_event_id": self.target_event_id,
            "replacement": self.replacement,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Erratum":
        return cls(
            erratum_id=d["erratum_id"],
            revision=d["revision"],
            target_event_id=d["target_event_id"],
            replacement=dict(d["replacement"]),
            note=d.get("note", ""),
        )


@dataclass(frozen=True)
class Observer:
    observer_id: str
    eye_x: Fraction

    def as_dict(self) -> dict:
        return {"observer_id": self.observer_id, "eye_x": fmt_q(self.eye_x)}

    @classmethod
    def from_dict(cls, d: dict) -> "Observer":
        return cls(d["observer_id"], parse_q(d["eye_x"]))


@dataclass(frozen=True)
class CandidateBoard:
    """等宽候选字幕牌区间（牌面在 y=10 上的闭区间）。"""

    board_id: str
    lo: Fraction
    hi: Fraction

    def as_dict(self) -> dict:
        return {"board_id": self.board_id, "lo": fmt_q(self.lo), "hi": fmt_q(self.hi)}

    @classmethod
    def from_dict(cls, d: dict) -> "CandidateBoard":
        return cls(d["board_id"], parse_q(d["lo"]), parse_q(d["hi"]))


@dataclass(frozen=True)
class Study:
    """一次选牌研究：固定切面、观察眼点与允许使用的候选牌。"""

    study_id: str
    cut_seq: int
    revision: str  # "original" 或某勘误修订标识
    observers: tuple[Observer, ...]
    boards: tuple[CandidateBoard, ...]

    def as_dict(self) -> dict:
        return {
            "study_id": self.study_id,
            "cut_seq": self.cut_seq,
            "revision": self.revision,
            "observers": [o.as_dict() for o in self.observers],
            "boards": [b.as_dict() for b in self.boards],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Study":
        return cls(
            study_id=d["study_id"],
            cut_seq=d["cut_seq"],
            revision=d["revision"],
            observers=tuple(Observer.from_dict(o) for o in d["observers"]),
            boards=tuple(CandidateBoard.from_dict(b) for b in d["boards"]),
        )


@dataclass(frozen=True)
class Occupancy:
    """某切面上一个占座及其原凭据（发票存根 + 入场动作）。"""

    seat_id: str
    ticket_id: str
    issued_by: str   # issue 事件 id
    admitted_by: str  # admit 事件 id
    last_move_by: Optional[str] = None  # 最近一次换座/互换事件 id

    def as_dict(self) -> dict:
        return {
            "seat_id": self.seat_id,
            "ticket_id": self.ticket_id,
            "issued_by": self.issued_by,
            "admitted_by": self.admitted_by,
            "last_move_by": self.last_move_by,
        }
