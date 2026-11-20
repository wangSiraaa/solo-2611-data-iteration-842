"""追加式案卷仓：保存案卷身份与不可改流水。

磁盘布局（全部为离线 JSON/JSONL，无外部服务）::

    <root>/dossier.json      案卷身份、稳定座位、各版座图、研究配置（一次建立）
    <root>/events.jsonl      票面动作流水，只追加
    <root>/errata.jsonl      后补勘误，只追加
    <root>/snapshots/*.json  重放得到的切面座位状态（派生物，可重建）
    <root>/reports/*.json    可见度与选牌报告（派生物，可重建）

流水文件以 ``"a"`` 方式打开并做身份/异文核对：同号同文复读是幂等 no-op，
同号异文整笔拒绝；源流水永不就地改写，勘误另起修订，旧切面始终可读。
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Optional

from .models import CandidateBoard, Erratum, Observer, Seat, SeatMapVersion, Study, TicketEvent


class ArchiveError(Exception):
    """案卷仓层面的拒绝（身份冲突、非法改写等）。"""


class ConflictError(ArchiveError):
    """同一标识以不同内容再次写入。"""


def canonical_digest(obj: dict) -> str:
    """对可 JSON 化对象做确定性 SHA-256 摘要（Fractions 已格式化为字符串）。"""
    raw = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _read_jsonl(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


class DossierStore:
    """一个案卷目录的读写句柄。"""

    def __init__(self, root: str):
        self.root = root
        self.events_path = os.path.join(root, "events.jsonl")
        self.errata_path = os.path.join(root, "errata.jsonl")
        self.snap_dir = os.path.join(root, "snapshots")
        self.report_dir = os.path.join(root, "reports")
        self._meta: Optional[dict] = None

    # ------------------------------------------------------------ 建立/打开

    @classmethod
    def init(
        cls,
        root: str,
        dossier_id: str,
        title: str,
        seats: list[Seat],
        maps: list[SeatMapVersion],
        studies: list[Study],
        note: str = "",
    ) -> "DossierStore":
        os.makedirs(root, exist_ok=True)
        store = cls(root)
        meta_path = os.path.join(root, "dossier.json")
        meta = {
            "dossier_id": dossier_id,
            "title": title,
            "note": note,
            "seats": [s.as_dict() for s in sorted(seats, key=lambda s: s.seat_id)],
            "maps": [m.as_dict() for m in sorted(maps, key=lambda m: m.map_version)],
            "studies": [s.as_dict() for s in sorted(studies, key=lambda s: s.study_id)],
        }
        meta["digest"] = canonical_digest({k: v for k, v in meta.items()})
        if os.path.exists(meta_path):
            with open(meta_path, encoding="utf-8") as fh:
                existing = json.load(fh)
            if existing != meta:
                raise ConflictError(f"案卷 {dossier_id} 已以不同内容建立，身份不可改写")
        else:
            with open(meta_path, "w", encoding="utf-8") as fh:
                json.dump(meta, fh, ensure_ascii=False, indent=2, sort_keys=True)
                fh.write("\n")
        for sub in ("snapshots", "reports"):
            os.makedirs(os.path.join(root, sub), exist_ok=True)
        store._meta = meta
        return store

    @classmethod
    def open(cls, root: str) -> "DossierStore":
        store = cls(root)
        meta_path = os.path.join(root, "dossier.json")
        if not os.path.exists(meta_path):
            raise ArchiveError(f"{root} 下没有 dossier.json")
        with open(meta_path, encoding="utf-8") as fh:
            meta = json.load(fh)
        stored_digest = meta.pop("digest", None)
        if stored_digest != canonical_digest(meta):
            raise ArchiveError("dossier.json 内容摘要不符，案卷身份可能被篡改")
        meta["digest"] = stored_digest
        store._meta = meta
        return store

    # ------------------------------------------------------------ 案卷内容

    @property
    def meta(self) -> dict:
        if self._meta is None:
            raise ArchiveError("案卷尚未打开")
        return self._meta

    @property
    def dossier_id(self) -> str:
        return self.meta["dossier_id"]

    def seats(self) -> dict[str, Seat]:
        return {s["seat_id"]: Seat.from_dict(s) for s in self.meta["seats"]}

    def maps(self) -> dict[str, SeatMapVersion]:
        return {m["map_version"]: SeatMapVersion.from_dict(m) for m in self.meta["maps"]}

    def studies(self) -> dict[str, Study]:
        return {s["study_id"]: Study.from_dict(s) for s in self.meta["studies"]}

    # ------------------------------------------------------------ 流水追加

    def append_event(self, event: TicketEvent) -> None:
        records = _read_jsonl(self.events_path)
        for existing in records:
            if existing["event_id"] == event.event_id:
                if existing == event.as_dict():
                    return  # 同号同文幂等复读
                raise ConflictError(f"事件 {event.event_id} 已以不同内容存在")
            if existing["seq"] == event.seq:
                raise ConflictError(f"序号 {event.seq} 已被事件 {existing['event_id']} 占用")
        with open(self.events_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event.as_dict(), ensure_ascii=False, sort_keys=True) + "\n")

    def append_erratum(self, erratum: Erratum) -> None:
        records = _read_jsonl(self.errata_path)
        for existing in records:
            if existing["erratum_id"] == erratum.erratum_id:
                if existing == erratum.as_dict():
                    return
                raise ConflictError(f"勘误 {erratum.erratum_id} 已以不同内容存在")
            if existing["revision"] == erratum.revision:
                raise ConflictError(f"修订号 {erratum.revision} 已被占用")
        target_ids = {e["event_id"] for e in _read_jsonl(self.events_path)}
        if erratum.target_event_id not in target_ids:
            raise ArchiveError(f"勘误 {erratum.erratum_id} 指向未知事件 {erratum.target_event_id}")
        with open(self.errata_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(erratum.as_dict(), ensure_ascii=False, sort_keys=True) + "\n")

    def events(self) -> list[TicketEvent]:
        return [TicketEvent.from_dict(d) for d in _read_jsonl(self.events_path)]

    def errata(self) -> list[Erratum]:
        return [Erratum.from_dict(d) for d in _read_jsonl(self.errata_path)]

    # ------------------------------------------------------------ 派生落盘

    def write_snapshot(self, name: str, payload: dict) -> str:
        return self._write_derived(self.snap_dir, name, payload)

    def write_report(self, name: str, payload: dict) -> str:
        return self._write_derived(self.report_dir, name, payload)

    def _write_derived(self, directory: str, name: str, payload: dict) -> str:
        os.makedirs(directory, exist_ok=True)
        if not name.endswith(".json"):
            name = name + ".json"
        path = os.path.join(directory, name)
        body = dict(payload)
        body["digest"] = canonical_digest({k: v for k, v in body.items()})
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(body, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
        return path

    def read_json(self, path: str) -> dict:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
