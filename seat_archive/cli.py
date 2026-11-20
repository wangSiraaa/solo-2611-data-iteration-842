"""命令行入口：建立案卷、重放切面、计算可见度与选牌。

典型流程::

    python3 -m seat_archive.cli build --all --data materials/theater
    python3 -m seat_archive.cli snapshot --data materials/theater/A --cut 24 --revision r2
    python3 -m seat_archive.cli visibility --data materials/theater/A --study B-CORRECTED
    python3 -m seat_archive.cli run-examples --data materials/theater
    python3 -m seat_archive.cli verify --data materials/theater

全部输出为离线 JSON，派生物（snapshots/reports）可随时重建；源流水只追加。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from .dossier import dossier_a, dossier_b
from .replay import replay
from .store import DossierStore, canonical_digest
from .visibility import evaluate_study


def _dump(obj) -> None:
    json.dump(obj, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def _snapshot_name(cut_seq: int, revision: str) -> str:
    return f"snapshot_cut{cut_seq}_{revision}.json"


def _report_name(study_id: str) -> str:
    return f"report_{study_id}.json"


def cmd_build(args) -> None:
    roots: list[str] = []
    if args.all or args.dossier == "A":
        roots.append(dossier_a(os.path.join(args.data, "A")).root)
    if args.all or args.dossier == "B":
        roots.append(dossier_b(os.path.join(args.data, "B")).root)
    _dump({"built": roots})


def _replay_study_cut(store: DossierStore, study):
    return replay(
        dossier_id=store.dossier_id,
        events=store.events(),
        maps=store.maps(),
        known_seats=frozenset(store.seats()),
        cut_seq=study.cut_seq,
        revision=study.revision,
        errata=store.errata(),
    )


def _build_study_report(store: DossierStore, study_id: str) -> dict:
    """纯计算：重放研究切面并生成报告（不落盘），供核对复用。"""
    study = store.studies()[study_id]
    snapshot = _replay_study_cut(store, study)
    report = evaluate_study(
        snapshot=snapshot,
        seats=store.seats(),
        observers=list(study.observers),
        boards=list(study.boards),
    )
    report["study_id"] = study.study_id
    report["snapshot_source"] = _snapshot_name(snapshot.cut_seq, snapshot.revision)
    return report


def _run_study(store: DossierStore, study_id: str) -> tuple[str, dict]:
    study = store.studies()[study_id]
    report = _build_study_report(store, study_id)
    report_path = store.write_report(_report_name(study_id), report)
    snapshot = _replay_study_cut(store, study)
    store.write_snapshot(
        _snapshot_name(snapshot.cut_seq, snapshot.revision), snapshot.as_dict()
    )
    return report_path, report


def cmd_snapshot(args) -> None:
    store = DossierStore.open(args.data)
    snapshot = replay(
        dossier_id=store.dossier_id,
        events=store.events(),
        maps=store.maps(),
        known_seats=frozenset(store.seats()),
        cut_seq=args.cut,
        revision=args.revision,
        errata=store.errata(),
    )
    path = store.write_snapshot(_snapshot_name(snapshot.cut_seq, snapshot.revision), snapshot.as_dict())
    _dump({"snapshot": os.path.abspath(path), **snapshot.as_dict()})


def cmd_visibility(args) -> None:
    store = DossierStore.open(args.data)
    if args.study not in store.studies():
        raise SystemExit(f"未知研究 {args.study}；可选：{sorted(store.studies())}")
    report_path, report = _run_study(store, args.study)
    _dump({"report": os.path.abspath(report_path), **report})


def cmd_run_examples(args) -> None:
    summary: dict[str, dict] = {}
    for dossier_id in ("A", "B"):
        root = os.path.join(args.data, dossier_id)
        store = DossierStore.open(root)
        for study_id in sorted(store.studies()):
            report_path, report = _run_study(store, study_id)
            summary[f"{dossier_id}/{study_id}"] = {
                "selected_board": report["selected_board"],
                "worst_visible_length": report["selected_worst_visible_length"],
                "report": os.path.relpath(report_path, root),
            }
    _dump({"data_root": os.path.abspath(args.data), "studies": summary})


def cmd_verify(args) -> None:
    findings: list[str] = []
    for dossier_id in sorted(os.listdir(args.data)):
        root = os.path.join(args.data, dossier_id)
        if not os.path.exists(os.path.join(root, "dossier.json")):
            continue
        store = DossierStore.open(root)  # 打开即核对 dossier.json 摘要
        for study_id, study in store.studies().items():
            report_path = os.path.join(store.report_dir, _report_name(study_id))
            if not os.path.exists(report_path):
                findings.append(f"{dossier_id}/{study_id}: 缺少报告，先运行 run-examples")
                continue
            on_disk = store.read_json(report_path)
            stored_digest = on_disk.get("digest")
            if stored_digest != canonical_digest({k: v for k, v in on_disk.items() if k != "digest"}):
                findings.append(f"{dossier_id}/{study_id}: 报告自身摘要不符（疑似被改写）")
                continue
            fresh = _build_study_report(store, study_id)
            fresh_digest = canonical_digest(fresh)
            if fresh_digest != stored_digest:
                findings.append(f"{dossier_id}/{study_id}: 落盘报告与重放结果不一致")
    _dump({"ok": not findings, "findings": findings})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="seat_archive", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_build = sub.add_parser("build", help="建立内置示例案卷（幂等）")
    p_build.add_argument("--data", required=True)
    p_build.add_argument("--dossier", choices=["A", "B"], default="A")
    p_build.add_argument("--all", action="store_true")
    p_build.set_defaults(func=cmd_build)

    p_snap = sub.add_parser("snapshot", help="重放至指定切面并落盘座位状态")
    p_snap.add_argument("--data", required=True)
    p_snap.add_argument("--cut", type=int, required=True)
    p_snap.add_argument("--revision", default="original")
    p_snap.set_defaults(func=cmd_snapshot)

    p_vis = sub.add_parser("visibility", help="按研究配置计算可见度并选牌")
    p_vis.add_argument("--data", required=True)
    p_vis.add_argument("--study", required=True)
    p_vis.set_defaults(func=cmd_visibility)

    p_run = sub.add_parser("run-examples", help="重放全部研究并落盘快照/报告")
    p_run.add_argument("--data", required=True)
    p_run.set_defaults(func=cmd_run_examples)

    p_ver = sub.add_parser("verify", help="重开案卷并核对落盘报告可重建且一致")
    p_ver.add_argument("--data", required=True)
    p_ver.set_defaults(func=cmd_verify)
    return parser


def main(argv=None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
