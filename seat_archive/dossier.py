"""两套内置示例案卷的构造与落盘。

案卷 A：题面给定的数值例（[0,3]/[3,6] 等宽牌、y=5 单座、换座前后与移动眼点）。
案卷 B：全流程例，覆盖多版座图解释、售出未入场、作废/离场、单人换座、
        双人互换（成功与三类失败）、勘误重放、重叠阴影与多观察点选牌。

所有几何数均以精确有理数字符串记载。
"""

from __future__ import annotations

from .models import CandidateBoard, Erratum, Observer, Seat, SeatMapVersion, Study, TicketEvent
from .rationals import q
from .store import DossierStore


def _seat(seat_id: str, y: str, lx: str, rx: str) -> Seat:
    return Seat(seat_id, q(y), q(lx), q(rx))


def _ev(
    seq: int,
    event_id: str,
    action: str,
    *,
    ticket_id=None,
    map_version=None,
    row=None,
    number=None,
    seat_id=None,
    ticket2_id=None,
    seat2_id=None,
    status="accepted",
    reason=None,
) -> TicketEvent:
    return TicketEvent(
        seq=seq,
        event_id=event_id,
        action=action,
        ticket_id=ticket_id,
        map_version=map_version,
        row=row,
        number=number,
        seat_id=seat_id,
        ticket2_id=ticket2_id,
        seat2_id=seat2_id,
        status=status,
        reason=reason,
    )


# ===================================================================== 案卷 A


def dossier_a(root: str) -> DossierStore:
    """题面数值例：唯一占用座在 y=5，线段 [1/2, 5/4]。

    眼点 x=0 时 S1 在字幕线 y=10 的阴影为 [2, 5]（座位段 [1,5/2] 乘投影因子 2）。
    三个研究严格对应题面三幕：
      A1 换座前、眼点 0：阴影 [1,5/2]，左牌 [0,3] 可见 2、右牌 [3,6] 可见 1，选左牌；
      A2 换座后、眼点 0：T1 移入 S2，阴影移为 [0, 3/2]，选右牌；
      A3 固定原座图与占座（S1）、眼点移到 3：同样选右牌。
    """
    # y=5 时投影因子 10/(10-5)=2：
    # S1 段 [1, 5/2] -> 眼 0 阴影 [2, 5]；S2 段 [0, 3/4] -> 眼 0 阴影 [0, 3/2]
    seats = [_seat("S1", "5", "1", "5/2"), _seat("S2", "5", "0", "3/4")]
    maps = [SeatMapVersion("v1", {("A", 1): "S1", ("A", 2): "S2"})]
    boards = (
        CandidateBoard("B_LEFT", q(0), q(3)),
        CandidateBoard("B_RIGHT", q(3), q(6)),
    )
    studies = [
        Study("A1_BEFORE_SWAP", 6, "original", (Observer("EYE0", q(0)),), boards),
        Study("A2_AFTER_SWAP", 7, "original", (Observer("EYE0", q(0)),), boards),
        Study("A3_EYE_MOVED", 6, "original", (Observer("EYE3", q(3)),), boards),
    ]
    store = DossierStore.init(
        root,
        dossier_id="A",
        title="题面数值例：单座换座与眼点移动",
        seats=seats,
        maps=maps,
        studies=studies,
        note="字幕牌 y=10、眼点 y=0；S1 在 y=5 的线段为 [1,5/2]，投影因子 10/(10-y)=2。",
    )
    events = [
        _ev(1, "a-e1", "issue", ticket_id="T1", map_version="v1", row="A", number=1, seat_id="S1"),
        _ev(2, "a-e2", "issue", ticket_id="T2", map_version="v1", row="A", number=2, seat_id="S2"),
        # 售出但未入场：T2 始终不占座（e5 作废后 S2 成为可换入的空座）
        _ev(3, "a-e3", "admit", ticket_id="T1", seat_id="S1"),
        # 用未入场（sold）存根换座：拒绝，存根仅在 present 时可换
        _ev(4, "a-e4", "swap", ticket_id="T2", map_version="v1", row="A", number=1,
            seat_id="S1", status="rejected", reason="ticket_not_present"),
        # 未入场存根可作废，S2 腾空
        _ev(5, "a-e5", "void", ticket_id="T2"),
        # 已作废存根再换座：拒绝，不能借废票挤走 T1
        _ev(6, "a-e6", "swap", ticket_id="T2", map_version="v1", row="A", number=1,
            seat_id="S1", status="rejected", reason="ticket_voided"),
        # T1 单人换到空座 S2；对眼点 0 的遮挡由 [1,5/2] 移为 [0,3/2]
        _ev(7, "a-e7", "swap", ticket_id="T1", map_version="v1", row="A", number=2, seat_id="S2"),
    ]
    for event in events:
        store.append_event(event)
    # 勘误 r2（旧版切面 original 仍可读）：后补发现 e7 换座单系伪造，
    # 更正为“同座换座”拒绝动作；r2 重放至第 7 笔时 T1 仍在 S1。
    store.append_erratum(
        Erratum(
            erratum_id="ER-A1",
            revision=2,
            target_event_id="a-e7",
            replacement={
                "action": "swap",
                "ticket_id": "T1",
                "map_version": "v1",
                "row": "A",
                "number": 1,
                "seat_id": "S1",
                "status": "rejected",
                "reason": "same_seat",
            },
            note="勘误：第 7 笔换座未实际发生，T1 留在 S1，重放 r2 即恢复换座前占座。",
        )
    )
    return store


# ===================================================================== 案卷 B


def dossier_b(root: str) -> DossierStore:
    """全流程案卷：8 个稳定座位、两版座图、24 条流水与 1 条勘误。"""
    seats = [
        _seat("S1", "5", "1/2", "5/4"),    # 宽 3/4
        _seat("S2", "5", "2", "9/4"),      # 宽 1/4
        _seat("S3", "5", "5/2", "13/4"),   # 宽 2
        _seat("S4", "5", "9/2", "5"),      # 宽 1/2
        _seat("S5", "8", "1/4", "3/4"),    # 宽 1/2
        _seat("S6", "8", "2", "3"),        # 宽 1
        _seat("S7", "8", "9/2", "21/4"),   # 宽 3/4
        _seat("S8", "8", "7", "31/4"),     # 宽 3/4
    ]
    v1 = SeatMapVersion(
        "v1",
        {
            ("甲", 1): "S1", ("甲", 2): "S2", ("甲", 3): "S3", ("甲", 4): "S4",
            ("乙", 1): "S5", ("乙", 2): "S6", ("乙", 3): "S7", ("乙", 4): "S8",
        },
    )
    # 同物理座位在 v2 的票面号不同（同场保留的多版座图）
    v2 = SeatMapVersion(
        "v2",
        {
            ("A", 1): "S1", ("A", 2): "S2", ("A", 3): "S3", ("A", 4): "S4",
            ("B", 1): "S5", ("B", 2): "S6", ("B", 3): "S7", ("B", 4): "S8",
        },
    )
    boards = (
        CandidateBoard("B1", q(0), q(3)),
        CandidateBoard("B2", q(3), q(6)),
        CandidateBoard("B3", q(6), q(9)),
    )
    # 破同专用：两块牌都落在全部阴影之外，各观察者均可见全长 3，
    # 最差可见长度打平（均为 3），按给定牌号取较小的 B4。
    tie_boards = (
        CandidateBoard("B4", q(30), q(33)),
        CandidateBoard("B5", q(33), q(36)),
    )
    studies = [
        Study("B-ORIGINAL", 24, "original",
              (Observer("EYE0", q(0)), Observer("EYE3", q(3)), Observer("EYEM3", q(-3))),
              boards),
        Study("B-CORRECTED", 24, "r2",
              (Observer("EYE0", q(0)), Observer("EYE3", q(3)), Observer("EYEM3", q(-3))),
              boards),
        Study("B-TIE", 24, "r2",
              (Observer("EYE0", q(0)), Observer("EYE3", q(3))),
              tie_boards),
    ]
    store = DossierStore.init(
        root,
        dossier_id="B",
        title="全流程案卷：多版座图、换座互换、勘误与多观察点选牌",
        seats=seats,
        maps=[v1, v2],
        studies=studies,
        note="票面按凭证自身所载版解释到稳定座位；勘误 r2 更正 e23 的换座。",
    )
    events = [
        # ---- 发票（v1 票面）----
        _ev(1, "e1", "issue", ticket_id="T1", map_version="v1", row="甲", number=1, seat_id="S1"),
        _ev(2, "e2", "issue", ticket_id="T2", map_version="v1", row="甲", number=2, seat_id="S2"),
        _ev(3, "e3", "issue", ticket_id="T3", map_version="v1", row="甲", number=3, seat_id="S3"),
        _ev(4, "e4", "issue", ticket_id="T4", map_version="v1", row="甲", number=4, seat_id="S4"),
        _ev(5, "e5", "issue", ticket_id="T5", map_version="v1", row="乙", number=1, seat_id="S5"),
        _ev(6, "e6", "issue", ticket_id="T6", map_version="v1", row="乙", number=2, seat_id="S6"),
        _ev(7, "e7", "issue", ticket_id="T7", map_version="v1", row="乙", number=3, seat_id="S7"),
        _ev(8, "e8", "issue", ticket_id="T8", map_version="v1", row="乙", number=4, seat_id="S8"),
        # ---- 入场；T8 售出但永不入场（不占座）----
        _ev(9, "e9", "admit", ticket_id="T1", seat_id="S1"),
        _ev(10, "e10", "admit", ticket_id="T2", seat_id="S2"),
        _ev(11, "e11", "admit", ticket_id="T3", seat_id="S3"),
        _ev(12, "e12", "admit", ticket_id="T4", seat_id="S4"),
        _ev(13, "e13", "admit", ticket_id="T5", seat_id="S5"),
        _ev(14, "e14", "admit", ticket_id="T6", seat_id="S6"),
        _ev(15, "e15", "admit", ticket_id="T7", seat_id="S7"),
        # 未知存根入场：拒绝
        _ev(16, "e16", "admit", ticket_id="T99", seat_id="S8",
            status="rejected", reason="unknown_ticket"),
        # ---- 单人换座：T8 未入场不得换 ----
        _ev(17, "e17", "swap", ticket_id="T8", map_version="v2", row="B", number=4,
            seat_id="S8", status="rejected", reason="ticket_not_present"),
        # T8 作废（售出未入场可作废）
        _ev(18, "e18", "void", ticket_id="T8"),
        # T4 换到被占的 S2 且无移出：拒绝，T2 不被挤走
        _ev(19, "e19", "swap", ticket_id="T4", map_version="v2", row="A", number=2,
            seat_id="S2", status="rejected", reason="target_not_empty"),
        # 已作废存根换座：拒绝
        _ev(20, "e20", "swap", ticket_id="T8", map_version="v2", row="B", number=4,
            seat_id="S8", status="rejected", reason="ticket_voided"),
        # ---- 双人互换：失败三例 ----
        # 缺一方（T99 不存在）：拒绝，原状不动
        _ev(21, "e21", "exchange", ticket_id="T1", seat_id="S2",
            ticket2_id="T99", seat2_id="S1", status="rejected", reason="party_missing"),
        # 目标非空且无相应移出（T1 的目标 S3 由 T3 占用，T3 不参与）：拒绝
        _ev(22, "e22", "exchange", ticket_id="T1", seat_id="S3",
            ticket2_id="T2", seat2_id="S1", status="rejected", reason="target_not_empty"),
        # 成功互换：T1<->T2 同时核对、同时生效
        _ev(23, "e23", "exchange", ticket_id="T1", seat_id="S2",
            ticket2_id="T2", seat2_id="S1"),
        # T7 离场，S7 变空座（空座不产生遮挡）
        _ev(24, "e24", "leave", ticket_id="T7"),
    ]
    for event in events:
        store.append_event(event)
    store.append_erratum(
        Erratum(
            erratum_id="ER-B1",
            revision=2,
            target_event_id="e23",
            replacement={
                # 更正：实际是 T5 与 T6 互换（原档把存根号抄错）
                "action": "exchange",
                "ticket_id": "T5",
                "seat_id": "S6",
                "ticket2_id": "T6",
                "seat2_id": "S5",
            },
            note="勘误：e23 互换双方应为 T5/T6（乙排前两座），T1/T2 未动。",
        )
    )
    return store
