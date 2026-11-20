"""追加式案卷仓测试：身份保存、幂等复读、异文冲突、摘要防篡改。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from seat_archive.models import Erratum, Seat, SeatMapVersion, Study, TicketEvent
from seat_archive.rationals import q
from seat_archive.store import ArchiveError, ConflictError, DossierStore, canonical_digest


def _make_store(root: str) -> DossierStore:
    seats = [Seat("S1", q(5), q(1), q(2))]
    maps = [SeatMapVersion("v1", {("A", 1): "S1"})]
    return DossierStore.init(root, "D1", "测试案卷", seats, maps, [])


def _event(seq=1, eid="e1", note="x"):
    return TicketEvent(seq=seq, event_id=eid, action="issue", ticket_id="T1",
                       map_version="v1", row="A", number=1, seat_id="S1",
                       status="accepted", reason=None)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "case")
        self.store = _make_store(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_init_is_idempotent_on_identical_content(self):
        again = _make_store(self.root)  # 同内容再次建立：不报错
        self.assertEqual(again.dossier_id, "D1")

    def test_init_conflicts_on_changed_identity(self):
        seats = [Seat("S1", q(5), q(1), q(9))]  # 几何被改
        maps = [SeatMapVersion("v1", {("A", 1): "S1"})]
        with self.assertRaises(ConflictError):
            DossierStore.init(self.root, "D1", "测试案卷", seats, maps, [])

    def test_event_append_is_idempotent_same_content(self):
        self.store.append_event(_event())
        self.store.append_event(_event())  # 同号同文复读
        self.assertEqual(len(self.store.events()), 1)

    def test_event_append_rejects_changed_content(self):
        self.store.append_event(_event())
        changed = TicketEvent(seq=1, event_id="e1", action="admit", ticket_id="T1",
                              map_version="v1", row="A", number=1, seat_id="S1")
        with self.assertRaises(ConflictError):
            self.store.append_event(changed)

    def test_seq_collision_rejected(self):
        self.store.append_event(_event(seq=1, eid="e1"))
        with self.assertRaises(ConflictError):
            self.store.append_event(_event(seq=1, eid="e2"))

    def test_erratum_must_target_known_event(self):
        with self.assertRaises(ArchiveError):
            self.store.append_erratum(Erratum(
                erratum_id="ER1", revision=2, target_event_id="ghost",
                replacement={"action": "leave", "ticket_id": "T1"}, note="x"))

    def test_reopen_rejects_tampered_dossier(self):
        DossierStore.open(self.root)  # 正常重开
        meta_path = os.path.join(self.root, "dossier.json")
        with open(meta_path, encoding="utf-8") as fh:
            meta = json.load(fh)
        meta["title"] = "被篡改的标题"
        with open(meta_path, "w", encoding="utf-8") as fh:
            json.dump(meta, fh, ensure_ascii=False)
        with self.assertRaises(ArchiveError):
            DossierStore.open(self.root)

    def test_derived_report_digest_roundtrip(self):
        payload = {"study_id": "X", "selected_board": "B1"}
        path = self.store.write_report("report_X", payload)
        on_disk = json.load(open(path, encoding="utf-8"))
        self.assertEqual(on_disk["digest"],
                         canonical_digest({k: v for k, v in on_disk.items() if k != "digest"}))

    def test_source_ledger_uses_append_lines(self):
        self.store.append_event(_event(seq=1, eid="e1"))
        self.store.append_event(_event(seq=2, eid="e2"))
        with open(self.store.events_path, encoding="utf-8") as fh:
            lines = [line for line in fh if line.strip()]
        self.assertEqual(len(lines), 2)


if __name__ == "__main__":
    unittest.main()
