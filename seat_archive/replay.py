"""按业务顺序重放存根流水，复原任意切面的离线座位状态。

状态机（针对每张存根）::

    issue   -> sold（持有目标座位的售出权，未入场不占座）
    admit   -> present（占座；售出但未入场不算占座）
    leave   -> left（释放座位，存根失效）
    void    -> void（未入场时可作废，存根失效；已作废存根不得再换座/入场）
    swap    -> present 持票人单人换到空座
    exchange-> 两位 present 持票人按换前状态同时核对、同时生效

所有被拒动作保留在流水里（``status="rejected"`` 与原因码），但不改状态。
双人互换在任何写入之前完成全部核对，因此“缺一方/目标非空/已作废”等情况
都不会挤走原观众。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .models import Erratum, Occupancy, SeatMapVersion, TicketEvent


# 拒绝原因码（稳定标识，供测试与报告引用）
UNKNOWN_MAP = "unknown_map"
UNRESOLVED_LABEL = "unresolved_label"
UNKNOWN_SEAT = "unknown_seat"
TICKET_CONFLICT = "ticket_conflict"
UNKNOWN_TICKET = "unknown_ticket"
SEAT_ALREADY_SOLD = "seat_already_sold"
NOT_SOLD = "ticket_not_sold"
ALREADY_PRESENT = "already_present"
SEAT_OCCUPIED = "seat_occupied"
NOT_PRESENT = "ticket_not_present"
TICKET_VOIDED = "ticket_voided"
TARGET_NOT_EMPTY = "target_not_empty"
SAME_SEAT = "same_seat"
PARTY_MISSING = "party_missing"
PARTY_VOIDED = "party_voided"
PARTY_NOT_PRESENT = "party_not_present"


@dataclass
class _Ticket:
    ticket_id: str
    seat_id: str
    state: str  # sold | present | left | void
    issued_by: str
    admitted_by: Optional[str] = None
    last_move_by: Optional[str] = None


@dataclass
class Snapshot:
    """某修订、某切面（cut_seq，含该序号）的座位状态。"""

    dossier_id: str
    revision: str
    cut_seq: int
    occupancy: dict[str, Occupancy]
    trace: list[dict] = field(default_factory=list)
    errata_applied: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "dossier_id": self.dossier_id,
            "revision": self.revision,
            "cut_seq": self.cut_seq,
            "occupied_seats": [
                self.occupancy[k].as_dict() for k in sorted(self.occupancy)
            ],
            "errata_applied": list(self.errata_applied),
            "trace": list(self.trace),
        }


def _reject(event: TicketEvent, reason: str) -> TicketEvent:
    return TicketEvent(
        seq=event.seq,
        event_id=event.event_id,
        action=event.action,
        ticket_id=event.ticket_id,
        map_version=event.map_version,
        row=event.row,
        number=event.number,
        seat_id=event.seat_id,
        ticket2_id=event.ticket2_id,
        seat2_id=event.seat2_id,
        status="rejected",
        reason=reason,
    )


def build_corrected_stream(
    events: list[TicketEvent], errata: list[Erratum]
) -> tuple[list[TicketEvent], list[str]]:
    """按勘误把目标事件替换为更正动作，其余事件原样保留。

    勘误按修订号顺序链式应用；返回更正流水与已应用勘误 id。
    """
    ordered = sorted(errata, key=lambda e: e.revision)
    by_target: dict[str, Erratum] = {}
    for erratum in ordered:
        by_target[erratum.target_event_id] = erratum
    stream: list[TicketEvent] = []
    for event in events:
        erratum = by_target.get(event.event_id)
        if erratum is None:
            stream.append(event)
            continue
        repl = dict(erratum.replacement)
        repl.setdefault("seq", event.seq)
        repl.setdefault("event_id", event.event_id)
        corrected = TicketEvent.from_dict(repl)
        stream.append(corrected)
    return stream, [e.erratum_id for e in ordered]


def replay(
    dossier_id: str,
    events: list[TicketEvent],
    maps: dict[str, SeatMapVersion],
    known_seats: frozenset[str],
    cut_seq: Optional[int],
    revision: str = "original",
    errata: Optional[list[Erratum]] = None,
) -> Snapshot:
    """重放至 ``cut_seq``（含）；``cut_seq`` 为空表示重放全部。"""
    applied: list[str] = []
    corrected_ids: frozenset[str] = frozenset()
    if revision != "original":
        if errata is None:
            errata = []
        events, applied = build_corrected_stream(events, errata)
        corrected_ids = frozenset(e.target_event_id for e in errata)

    tickets: dict[str, _Ticket] = {}
    occupancy: dict[str, Occupancy] = {}
    trace: list[dict] = []

    def seat_of(row, number, map_version):
        """票面 (排号, 座号) 按凭证自身所载版解释为稳定座位。"""
        seat_map = maps.get(map_version)
        if seat_map is None:
            return None, UNKNOWN_MAP
        try:
            seat_id = seat_map.resolve(row, number)
        except KeyError:
            return None, UNRESOLVED_LABEL
        if seat_id not in known_seats:
            return None, UNKNOWN_SEAT
        return seat_id, None

    def remember(event: TicketEvent, effective: TicketEvent) -> None:
        trace.append(
            {
                "seq": effective.seq,
                "event_id": effective.event_id,
                "action": effective.action,
                "status": effective.status,
                "reason": effective.reason,
                "replay_basis": "replacement" if effective.event_id in corrected_ids else "as_recorded",
            }
        )

    for event in sorted(events, key=lambda e: e.seq):
        if cut_seq is not None and event.seq > cut_seq:
            break
        effective = event
        rejection: Optional[str] = None

        if event.action == "issue":
            seat_id, err = seat_of(event.row, event.number, event.map_version)
            if err:
                rejection = err
            elif event.ticket_id in tickets:
                rejection = TICKET_CONFLICT
            elif any(t.seat_id == seat_id and t.state in {"sold", "present"} for t in tickets.values()):
                rejection = SEAT_ALREADY_SOLD
            else:
                tickets[event.ticket_id] = _Ticket(
                    ticket_id=event.ticket_id,
                    seat_id=seat_id,
                    state="sold",
                    issued_by=event.event_id,
                )

        elif event.action == "admit":
            ticket = tickets.get(event.ticket_id)
            if ticket is None:
                rejection = UNKNOWN_TICKET
            elif ticket.state == "void":
                rejection = TICKET_VOIDED
            elif ticket.state == "left":
                rejection = NOT_SOLD
            elif ticket.state == "present":
                rejection = ALREADY_PRESENT
            elif ticket.seat_id in occupancy:
                rejection = SEAT_OCCUPIED
            else:
                ticket.state = "present"
                ticket.admitted_by = event.event_id
                occupancy[ticket.seat_id] = Occupancy(
                    seat_id=ticket.seat_id,
                    ticket_id=ticket.ticket_id,
                    issued_by=ticket.issued_by,
                    admitted_by=ticket.admitted_by,
                    last_move_by=ticket.last_move_by,
                )

        elif event.action == "leave":
            ticket = tickets.get(event.ticket_id)
            if ticket is None:
                rejection = UNKNOWN_TICKET
            elif ticket.state == "void":
                rejection = TICKET_VOIDED
            elif ticket.state != "present":
                rejection = NOT_PRESENT
            else:
                ticket.state = "left"
                occupancy.pop(ticket.seat_id, None)

        elif event.action == "void":
            ticket = tickets.get(event.ticket_id)
            if ticket is None:
                rejection = UNKNOWN_TICKET
            elif ticket.state == "void":
                rejection = TICKET_VOIDED
            elif ticket.state == "present":
                rejection = NOT_SOLD  # 已入场存根不能直接作废，须先离场
            elif ticket.state == "left":
                rejection = NOT_SOLD
            else:
                ticket.state = "void"

        elif event.action == "swap":
            ticket = tickets.get(event.ticket_id)
            target_id, err = seat_of(event.row, event.number, event.map_version)
            if err:
                rejection = err
            elif ticket is None:
                rejection = UNKNOWN_TICKET
            elif ticket.state == "void":
                rejection = TICKET_VOIDED
            elif ticket.state != "present":
                rejection = NOT_PRESENT
            elif target_id not in known_seats:
                rejection = UNKNOWN_SEAT
            elif target_id == ticket.seat_id:
                rejection = SAME_SEAT
            elif target_id in occupancy:
                # 目标非空且本动作没有任何相应移出：不得挤走原观众
                rejection = TARGET_NOT_EMPTY
            else:
                old_seat = ticket.seat_id
                occupancy.pop(old_seat, None)
                ticket.seat_id = target_id
                ticket.last_move_by = event.event_id
                occupancy[target_id] = Occupancy(
                    seat_id=target_id,
                    ticket_id=ticket.ticket_id,
                    issued_by=ticket.issued_by,
                    admitted_by=ticket.admitted_by,
                    last_move_by=event.event_id,
                )

        elif event.action == "exchange":
            t1 = tickets.get(event.ticket_id)
            t2 = tickets.get(event.ticket2_id)
            # 全部核对先于任何写入，保证双人同时生效、失败则原状不动
            if t1 is None or t2 is None:
                rejection = PARTY_MISSING
            elif event.ticket_id == event.ticket2_id:
                rejection = PARTY_MISSING
            elif t1.state == "void" or t2.state == "void":
                rejection = PARTY_VOIDED
            elif t1.state != "present" or t2.state != "present":
                rejection = PARTY_NOT_PRESENT
            elif event.seat_id not in known_seats or event.seat2_id not in known_seats:
                rejection = UNKNOWN_SEAT
            elif t1.seat_id == event.seat_id or t2.seat_id == event.seat2_id:
                rejection = SAME_SEAT
            else:
                blockers = []
                # 目标必须恰由换出方本人占用，否则即“目标非空且无相应移出”
                if occupancy.get(event.seat_id) is None or occupancy[event.seat_id].ticket_id != t2.ticket_id:
                    blockers.append(event.seat_id)
                if occupancy.get(event.seat2_id) is None or occupancy[event.seat2_id].ticket_id != t1.ticket_id:
                    blockers.append(event.seat2_id)
                if blockers:
                    rejection = TARGET_NOT_EMPTY

            if rejection is None:
                s1, s2 = t1.seat_id, t2.seat_id
                occupancy.pop(s1, None)
                occupancy.pop(s2, None)
                t1.seat_id, t2.seat_id = s2, s1
                for ticket in (t1, t2):
                    ticket.last_move_by = event.event_id
                    occupancy[ticket.seat_id] = Occupancy(
                        seat_id=ticket.seat_id,
                        ticket_id=ticket.ticket_id,
                        issued_by=ticket.issued_by,
                        admitted_by=ticket.admitted_by,
                        last_move_by=event.event_id,
                    )

        if rejection is not None:
            effective = _reject(event, rejection)
        remember(event, effective)

    return Snapshot(
        dossier_id=dossier_id,
        revision=revision,
        cut_seq=cut_seq if cut_seq is not None else max((e.seq for e in events), default=0),
        occupancy=occupancy,
        trace=trace,
        errata_applied=applied,
    )
