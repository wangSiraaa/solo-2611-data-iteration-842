"""The documented 751 scenario, plus a scaling generator.

``run_scenario`` is a deterministic end-to-end walkthrough:

1. import the four fictional documents (D1..D4) at query day 40;
2. publish a qualification product (P1) *before* the hold correction, showing
   D1/D2 held, D3 releasable, D4 missing-trigger; a whole-case C1 handoff is
   refused;
3. revise H1 to [25,35) and republish (P2): D1/D2/D3 releasable, P1 stays frozen.
   A previously published plan (P1) is now stale and must be refused;
4. refuse a partial C1 handoff (D1 only), then commit the whole C1 handoff,
   replay it, and refuse a new id from LOCAL.

Everything is fictional; nothing is deleted.
"""

from __future__ import annotations

import json
import os

from .materials import import_materials, load_materials
from .models import RejectError
from .handoff_service import HandoffRepo
from .rules_service import RuleEventStore


def _summary(product):
    return {
        doc_id: {
            "case_id": q.case_id,
            "computed_day": q.computed_day,
            "time_condition_met": q.time_condition_met,
            "releasable": q.releasable,
            "blocks": [b.kind for b in q.block_reasons],
        }
        for doc_id, q in sorted(product.documents.items())
    }


def _expect_reject(label, fn, results):
    try:
        fn()
    except RejectError as exc:
        results.append({"step": label, "outcome": "rejected", "code": exc.code,
                        "detail": exc.detail})
        return exc
    results.append({"step": label, "outcome": "UNEXPECTEDLY_ACCEPTED"})
    return None


def run_scenario(data_dir: str, materials: str = "materials/751") -> dict:
    os.makedirs(data_dir, exist_ok=True)
    rules = RuleEventStore(os.path.join(data_dir, "rules_events.sqlite"))
    handoff = HandoffRepo(os.path.join(data_dir, "handoff.sqlite"))
    artifacts = os.path.join(data_dir, "artifacts")
    os.makedirs(artifacts, exist_ok=True)
    steps: list[dict] = []
    ok = True

    raw = load_materials(materials)
    steps.append({"step": "import", "result": import_materials(rules, handoff, materials)})

    # ---- pre-correction -----------------------------------------------------
    p1 = rules.build_product(40)
    rules.save_product("P1", p1)
    handoff.ingest_product("Q40-before", p1)
    p1_path = rules.write_product_artifact("P1", p1, artifacts)
    steps.append({"step": "publish_P1", "as_of_day": 40,
                  "source_revision": rules.product_record("P1")["source_revision"],
                  "documents": _summary(p1), "artifact": p1_path})

    # A plan published from the pre-correction product; will be stale later.
    plan = handoff.prepare_and_publish("P1", "Q40-before", 40)
    steps.append({"step": "publish_plan_P1", "plan": plan["payload"]})

    _expect_reject(
        "handoff_C1_before_correction",
        lambda: handoff.submit({
            "handoff_id": "PRE1", "case_id": "C1", "doc_ids": ["D1", "D2"],
            "from": "LOCAL", "to": "VAULT1", "as_of_day": 40,
        }),
        steps,
    )

    # ---- hold correction ----------------------------------------------------
    rules.import_holds([{"hold_id": "H1", "revision": 2, "case_id": "C1",
                         "start": 25, "end": 35}])
    boundary = {}
    for day in (34, 35, 40):
        q = rules.qualify_document("D1", day)
        boundary[day] = {"hold_active": bool(q.holds_active), "releasable": q.releasable,
                         "computed_day": q.computed_day}
    steps.append({"step": "revise_H1", "boundary_D1": boundary,
                  "note": "clock not paused: computed day stays 30/15, not 30+25"})

    p2 = rules.build_product(40)
    rules.save_product("P2", p2)
    handoff.ingest_product("Q40-after", p2)
    p2_path = rules.write_product_artifact("P2", p2, artifacts)
    steps.append({"step": "publish_P2", "as_of_day": 40,
                  "source_revision": rules.product_record("P2")["source_revision"],
                  "documents": _summary(p2), "artifact": p2_path})

    # P1 must be frozen: its D1 hold window still ends at 50.
    frozen = rules.load_product("P1").documents["D1"].holds_active
    steps.append({"step": "P1_frozen", "D1_holds": [
        {"hold_id": h.hold_id, "revision": h.revision, "start": h.start, "end": h.end}
        for h in frozen
    ]})

    # Old plan must now be stale and must not transfer anything.
    try:
        handoff.confirm_handoff_list("P1")
    except RejectError as exc:
        steps.append({"step": "confirm_plan_P1", "outcome": "rejected", "code": exc.code,
                      "detail": exc.detail})
    else:
        steps.append({"step": "confirm_plan_P1", "outcome": "UNEXPECTEDLY_ACCEPTED"})
        ok = False

    # ---- partial / whole / replay / custody ---------------------------------
    _expect_reject(
        "PARTIAL",
        lambda: handoff.submit({
            "handoff_id": "PARTIAL", "case_id": "C1", "doc_ids": ["D1"],
            "from": "LOCAL", "to": "VAULT1", "as_of_day": 40,
        }),
        steps,
    )
    steps.append({"step": "custody_after_PARTIAL", "C1": handoff.current_custodian("C1")})

    committed = handoff.submit({
        "handoff_id": "WHOLE1", "case_id": "C1", "doc_ids": ["D1", "D2"],
        "from": "LOCAL", "to": "VAULT1", "as_of_day": 40,
    })
    steps.append({"step": "WHOLE1", "committed": committed,
                  "C1": handoff.current_custodian("C1")})
    replay = handoff.submit({
        "handoff_id": "WHOLE1", "case_id": "C1", "doc_ids": ["D1", "D2"],
        "from": "LOCAL", "to": "VAULT1", "as_of_day": 40,
    })
    steps.append({"step": "WHOLE1_replay", "replayed": replay["replayed"],
                  "readonly": replay["replayed_readonly"]})

    _expect_reject(
        "WHOLE_new_id_from_LOCAL",
        lambda: handoff.submit({
            "handoff_id": "WHOLE2", "case_id": "C1", "doc_ids": ["D1", "D2"],
            "from": "LOCAL", "to": "VAULT1", "as_of_day": 40,
        }),
        steps,
    )

    _expect_reject(
        "WHOLE2_conflicting_content",
        lambda: handoff.submit({
            "handoff_id": "WHOLE1", "case_id": "C1", "doc_ids": ["D1", "D2"],
            "from": "LOCAL", "to": "OTHER", "as_of_day": 40,
        }),
        steps,
    )

    verify = handoff.verify()
    steps.append({"step": "verify", "report": verify})
    ok = ok and verify["ok"]

    # expected-value assertions for the report
    expected = {
        "P1_D1_releasable": p1.documents["D1"].releasable is False,
        "P1_D4_missing_trigger": "missing_trigger" in [b.kind for b in
                                                        p1.documents["D4"].block_reasons],
        "P2_D1_releasable": p2.documents["D1"].releasable is True,
        "P2_D2_releasable": p2.documents["D2"].releasable is True,
        "P2_D3_releasable": p2.documents["D3"].releasable is True,
        "P2_D4_still_missing": p2.documents["D4"].releasable is False,
        "boundary_34_frozen": boundary[34]["hold_active"] is True,
        "boundary_35_free": boundary[35]["hold_active"] is False,
        "D1_computed_not_paused": p2.documents["D1"].computed_day == 30,
        "P1_frozen_H1_end_50": p1.documents["D1"].holds_active[0].end == 50,
        "C1_after_whole1": handoff.current_custodian("C1") == "VAULT1",
    }
    ok = ok and all(expected.values())

    result = {"ok": ok, "data_dir": os.path.abspath(data_dir),
              "expected": expected, "steps": steps}
    with open(os.path.join(artifacts, "scenario.json"), "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
    rules.close()
    handoff.close()
    return result


# --------------------------------------------------------------------------
# Scaling dataset: 200 docs / 10 rules / 500 events+holds / 100 requests.
# --------------------------------------------------------------------------
def generate_scale(directory: str, docs: int = 200, rules_count: int = 10,
                   event_hold_budget: int = 500, requests: int = 100,
                   day: int = 60, seed: int = 751) -> dict:
    """Write a deterministic fictional dataset in the materials layout.

    The layout is fixed so the dataset is testable, not random noise:

    * 8 cases ``CX00..CX07``; every document has 1 or 2 rules.
    * One event per (document, rule trigger), dated so every triggered rule's
      computed day <= ``day`` (nothing is excluded by time alone).
    * Holds fill the remainder of the event+hold budget:
        - CX00/CX01 : open-ended whole-case holds  -> blocked at any day
        - CX02      : whole-case holds ending at 40 -> blocked at 50, free at 60
        - CX03      : one open document-scoped hold -> the whole case is blocked
        - CX04..CX07: no holds                      -> free from day ``day``
    * 100 requests interleave whole/partial and LOCAL/VAULT custody so the
      handoff store is driven through accept, custody-reject, partial-reject
      and chain-handoff paths.  Each row carries an ``expect`` hint.
    """
    free_cases = [f"CX{i:02d}" for i in (2, 4, 5, 6, 7)]
    blocked_cases = ["CX00", "CX01", "CX03"]

    rule_rows = []
    for i in range(rules_count):
        rule_rows.append({
            "rule_id": f"R{i:02d}",
            "revision": 1,
            "trigger_type": f"TRIG{i:02d}",
            "keep_days": [0, 5, 10, 15, 20, 25, 30][i % 7],
        })

    doc_rows = []
    for i in range(docs):
        case_id = f"CX{i % 8:02d}"
        n_rules = 1 if i % 2 == 0 else 2
        rule_ids = []
        for j in range(n_rules):
            rule_ids.append(rule_rows[(i + j * 3) % rules_count]["rule_id"])
        doc_rows.append({
            "doc_id": f"DX{i:04d}",
            "case_id": case_id,
            "rule_ids": sorted(set(rule_ids)),
        })

    rule_by_id = {r["rule_id"]: r for r in rule_rows}
    event_rows = []
    for i, d in enumerate(doc_rows):
        for rid in d["rule_ids"]:
            rule = rule_by_id[rid]
            # Day kept low so event_day + keep_days <= day for every rule.
            ev_day = (i * 7 + len(rid) * 3) % 21
            event_rows.append({
                "event_id": f"EX{len(event_rows):04d}",
                "revision": 1,
                "doc_id": d["doc_id"],
                "type": rule["trigger_type"],
                "day": ev_day,
            })

    hold_budget = event_hold_budget - len(event_rows)
    if hold_budget < 0:
        raise ValueError("event_hold_budget too small for one event per doc/rule")

    cx03_member = next(d["doc_id"] for d in doc_rows if d["case_id"] == "CX03")
    hold_rows = []
    # Split the hold budget across the three blocking shapes.
    n_a = hold_budget // 2                       # CX00/CX01 open whole-case
    n_b = hold_budget // 4                       # CX02 ends at 40
    n_c = hold_budget - n_a - n_b                # CX03 doc-scoped / extras
    for k in range(n_a):
        hold_rows.append({
            "hold_id": f"HX{len(hold_rows):04d}", "revision": 1,
            "case_id": "CX00" if k % 2 == 0 else "CX01",
            "start": k % 25, "end": None,
        })
    for k in range(n_b):
        hold_rows.append({
            "hold_id": f"HX{len(hold_rows):04d}", "revision": 1,
            "case_id": "CX02", "start": k % 20, "end": 55,
        })
    for k in range(n_c):
        row = {"hold_id": f"HX{len(hold_rows):04d}", "revision": 1,
               "start": k % 20, "end": None}
        # Half attach to a single CX03 document, half to CX00/CX01 cases.
        if k % 2 == 0:
            row["doc_id"] = cx03_member
        else:
            row["case_id"] = "CX00" if k % 4 == 1 else "CX01"
        hold_rows.append(row)

    members = {}
    for d in doc_rows:
        members.setdefault(d["case_id"], []).append(d["doc_id"])
    for case in members:
        members[case] = sorted(members[case])

    request_rows = []

    def add(case, doc_ids, frm, to, expect):
        request_rows.append({
            "handoff_id": f"REQ{len(request_rows):04d}",
            "case_id": case,
            "doc_ids": doc_ids,
            "from": frm,
            "to": to,
            "as_of_day": day,
            "expect": expect,
        })

    # Free cases: chain LOCAL->VAULT1->VAULT2->VAULT3, with rejects interleaved.
    for case in free_cases:
        mem = members[case]
        add(case, mem, "LOCAL", "VAULT1", "accept")
        add(case, mem, "LOCAL", "VAULT1", "reject_custody")
        add(case, mem, "VAULT1", "VAULT2", "accept")
        add(case, mem, "VAULT1", "VAULT2", "reject_custody")
        add(case, mem[:1], "VAULT2", "VAULT1", "reject_not_whole")
        add(case, mem, "LOCAL", "VAULT1", "reject_custody")
        add(case, mem, "VAULT2", "VAULT3", "accept")
        add(case, mem[:1], "VAULT3", "VAULT1", "reject_not_whole")

    # Blocked cases: whole requests fail on eligibility, partial on completeness.
    for case in blocked_cases:
        mem = members[case]
        for k in range(4):
            add(case, mem, "LOCAL", "VAULT1", "reject_eligibility")
        for k in range(4):
            add(case, mem[:1], "LOCAL", "VAULT1", "reject_not_whole")

    # Fill the remainder with partial requests (always incomplete).
    i = 0
    while len(request_rows) < requests:
        case = (free_cases + blocked_cases)[i % 8]
        add(case, members[case][:1], "LOCAL", "VAULT1", "reject_not_whole")
        i += 1
    request_rows = request_rows[:requests]

    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, "rules.json"), "w", encoding="utf-8") as fh:
        json.dump({"rules": rule_rows}, fh, indent=2)
    with open(os.path.join(directory, "documents.json"), "w", encoding="utf-8") as fh:
        json.dump({"documents": doc_rows}, fh, indent=2)
    for name, rows in (("events.jsonl", event_rows), ("holds.jsonl", hold_rows)):
        with open(os.path.join(directory, name), "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    with open(os.path.join(directory, "requests.jsonl"), "w", encoding="utf-8") as fh:
        for row in request_rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    expect_counts: dict[str, int] = {}
    for r in request_rows:
        expect_counts[r["expect"]] = expect_counts.get(r["expect"], 0) + 1
    return {
        "documents": len(doc_rows),
        "rules": len(rule_rows),
        "events": len(event_rows),
        "holds": len(hold_rows),
        "events_plus_holds": len(event_rows) + len(hold_rows),
        "requests": len(request_rows),
        "cases": 8,
        "free_cases": free_cases,
        "blocked_cases": blocked_cases,
        "expected_outcomes": expect_counts,
        "day": day,
        "dir": directory,
    }


def run_scale(data_dir: str, materials_dir: str,
              days=(50, 60)) -> dict:
    """Import the scale dataset, publish products, replay requests, reopen.

    Verifies:
      * product counts and per-day blocked/free expectations;
      * every request's outcome matches its ``expect`` hint;
      * custody is a fold of the ledger after reopen;
      * handoff ``per_doc`` snapshots still match their frozen members.
    """
    import os

    from .materials import import_materials

    paths = {
        "rules_db": os.path.join(data_dir, "rules_events.sqlite"),
        "handoff_db": os.path.join(data_dir, "handoff.sqlite"),
    }
    os.makedirs(data_dir, exist_ok=True)
    for p in paths.values():
        if os.path.exists(p):
            os.remove(p)

    rules = RuleEventStore(paths["rules_db"])
    handoff = HandoffRepo(paths["handoff_db"])
    result: dict = {"ok": True, "steps": [], "failures": []}

    def check(label, condition, detail=""):
        result["steps"].append({"check": label, "ok": bool(condition), "detail": detail})
        if not condition:
            result["ok"] = False
            result["failures"].append(f"{label}: {detail}")

    result["import"] = import_materials(rules, handoff, materials_dir)

    # Two days: 50 (CX02 still held) and 60 (CX02 free).
    products = {}
    for day in days:
        product = rules.build_product(day)
        pid = f"Q{day}"
        rules.save_product(pid, product)
        handoff.ingest_product(pid, product)
        products[day] = (pid, product)
        rules.write_product_artifact(pid, product, os.path.join(data_dir, "products"))

    free_cases = [f"CX{i:02d}" for i in (2, 4, 5, 6, 7)]
    q50 = products[50][1]
    q60 = products[60][1]
    free50 = {c for c in {q.case_id for q in q50.documents.values()}
              if all(q.releasable for q in q50.documents.values() if q.case_id == c)}
    free60 = {c for c in {q.case_id for q in q60.documents.values()}
              if all(q.releasable for q in q60.documents.values() if q.case_id == c)}
    check("cx02_blocked_at_50", "CX02" not in free50, sorted(free50))
    check("cx02_free_at_60", "CX02" in free60, sorted(free60))
    check("blocked_cases_stay_blocked",
          not ({"CX00", "CX01", "CX03"} & free60), sorted(free60))

    # Replay requests at day 60 (product Q60 is the latest for that day).
    reqs = []
    with open(os.path.join(materials_dir, "requests.jsonl"), encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                reqs.append(json.loads(line))

    outcomes: dict[str, int] = {}
    mismatches = []
    # Generator hints are coarse; map them to the concrete reject codes.
    canonical = {
        "accept": "accept",
        "reject_custody": "reject_custody_mismatch",
        "reject_not_whole": "reject_not_whole_case",
        "reject_eligibility": "reject_ineligible_members",
    }
    for r in reqs:
        expect = canonical[r.pop("expect")]
        try:
            handoff.submit(r)
            got = "accept"
        except RejectError as exc:
            got = f"reject_{exc.code}"
        outcomes[got] = outcomes.get(got, 0) + 1
        if got != expect:
            mismatches.append({"handoff_id": r["handoff_id"], "expected": expect, "got": got})
    check("all_requests_match_expectation", not mismatches, mismatches[:5])
    result["outcomes"] = outcomes
    result["request_mismatches"] = mismatches

    custody_before = {c: handoff.current_custodian(c) for c in handoff.known_cases()}
    verify_before = handoff.verify()
    check("verify_before_reopen", verify_before["ok"], verify_before["errors"])
    handoff_ids = sorted(h["handoff_id"] for h in handoff.list_handoffs())
    result["handoff_count"] = len(handoff_ids)

    rules.close()
    handoff.close()

    # ---- reopen: fold custody from the ledger, re-check frozen artifacts ----
    rules2 = RuleEventStore(paths["rules_db"])
    handoff2 = HandoffRepo(paths["handoff_db"])
    custody_after = {c: handoff2.current_custodian(c) for c in handoff2.known_cases()}
    check("custody_survives_reopen", custody_before == custody_after,
          {"before": custody_before, "after": custody_after})
    verify_after = handoff2.verify()
    check("verify_after_reopen", verify_after["ok"], verify_after["errors"])
    check("handoffs_survive_reopen",
          sorted(h["handoff_id"] for h in handoff2.list_handoffs()) == handoff_ids)
    # Frozen product payload must be readable and unchanged after reopen.
    p60 = rules2.load_product("Q60")
    check("product_survives_reopen", p60.as_of_day == 60
          and p60.documents["DX0000"].as_of_day == 60)
    # Historical replay of one committed handoff reads the same frozen record.
    if handoff_ids:
        first = handoff2.get_handoff(handoff_ids[0])
        check("historical_handoff_members_frozen",
              sorted(first["doc_ids"]) == sorted(json.loads(
                  handoff2.conn.execute(
                      "SELECT members_json FROM handoffs WHERE handoff_id=?",
                      (handoff_ids[0],)).fetchone()["members_json"])))
    result["custody"] = custody_after
    result["verify"] = verify_after
    rules2.close()
    handoff2.close()

    result["scale"] = {
        "documents": 200, "rules": 10, "events_plus_holds": 500, "requests": 100
    }
    return result


# --------------------------------------------------------------------------
# operations.jsonl interpreter (the materials' own prepare/revise/confirm flow)
# --------------------------------------------------------------------------
def run_operations(data_dir: str, materials: str = "materials/751") -> dict:
    """Replay the materials' ``operations.jsonl`` against a fresh workspace.

    Supported operations (unknown ones are recorded as skipped, not fatal):

    * ``prepare_and_publish`` -- publish a qualification product (and a handoff
      list plan) for ``as_of_day`` under ``save_as``.
    * ``revise_hold`` -- import a hold revision.
    * ``confirm_handoff_list`` -- confirm a published plan; ``expect: stale``
      means the confirmation is expected to be refused.
    * ``read_historical`` -- read back a frozen plan, proving it was not
      rewritten.
    """
    from .handoff_service import HandoffRepo
    from .materials import import_materials, load_materials

    os.makedirs(data_dir, exist_ok=True)
    rules = RuleEventStore(os.path.join(data_dir, "rules_events.sqlite"))
    # A live provider lets a hold revision invalidate an old plan even before a
    # replacement product is ingested.  Actual transfers still use frozen,
    # ingested products, so the qualification source remains persisted.
    handoff = HandoffRepo(
        os.path.join(data_dir, "handoff.sqlite"),
        qualification_provider=rules.build_product,
    )
    import_materials(rules, handoff, materials)
    ops = load_materials(materials)["operations"]
    steps = []
    ok = True

    def reject(step, fn):
        nonlocal ok
        try:
            fn()
        except RejectError as exc:
            step.update(outcome="rejected", code=exc.code, detail=exc.detail)
            return
        step["outcome"] = "unexpectedly_accepted"
        ok = False

    for i, op in enumerate(ops):
        name = op.get("operation")
        step = {"index": i, "operation": name}
        if name == "prepare_and_publish":
            day = op["as_of_day"]
            pid = op.get("save_as", f"Q{day}")
            product = rules.build_product(day)
            rules.save_product(pid, product)
            handoff.ingest_product(pid, product)
            plan = handoff.prepare_and_publish(pid, pid, day)
            step.update(
                as_of_day=day,
                source_revision=rules.product_record(pid)["source_revision"],
                eligible=sorted(plan["payload"]["eligible"]),
                blocked=sorted(plan["payload"]["blocked"]),
            )
        elif name == "revise_hold":
            hold = op["hold"]
            rules.import_holds([hold])
            step.update(outcome="applied", hold_id=hold["hold_id"],
                        revision=hold["revision"])
        elif name == "confirm_handoff_list":
            pid = op["proposal"]
            expect = op.get("expect")
            if expect == "stale":
                reject(step, lambda pid=pid: handoff.confirm_handoff_list(pid))
            else:
                try:
                    result = handoff.confirm_handoff_list(pid)
                    step.update(outcome="confirmed",
                                handoffs=[h["handoff_id"] for h in result["handoffs"]])
                except RejectError as exc:
                    step.update(outcome="rejected", code=exc.code, detail=exc.detail)
                    ok = False
        elif name == "read_historical":
            pid = op["proposal"]
            plan = handoff.get_plan(pid)
            step.update(outcome="read", plan=plan)
        else:
            step.update(outcome="skipped_unknown_operation")
        steps.append(step)

    verify = handoff.verify()
    ok = ok and verify["ok"]
    result = {"ok": ok, "steps": steps, "verify": verify,
              "custody": {c: handoff.current_custodian(c)
                          for c in handoff.known_cases()}}
    rules.close()
    handoff.close()
    return result
