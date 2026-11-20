"""Independent whole-case handoff repository.

Responsibilities
----------------
* Own custody state (initially ``LOCAL`` for every case) via an append-only
  ledger plus a maintained current-state table that is reconciled on open.
* Enforce *whole-case* handoff: a request names a case, the complete document
  set, a from-party and a to-party; the set must equal the mirrored current
  membership, and every member must have a currently valid releasable
  qualification at the request's single ``as_of_day``.
* Consume qualification products produced by :mod:`rules_service`.  This layer
  never recomputes hold windows or save-day arithmetic.
* Optionally *prepare* a request and later confirm it; a prepare becomes stale
  and is refused if membership, custody or relevant qualification changed.
* Replay semantics: same ``handoff_id`` + same content reads back the fixed
  record; same id with different content is rejected.

The mirrored case membership is input from the catalog side and is only used to
validate completeness; it is never re-derived from historical handoffs.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from typing import Iterable, Optional

from .models import (
    ConflictError,
    QualificationProduct,
    RejectError,
    UnknownReference,
)
from .rules_service import canon

INITIAL_CUSTODIAN = "LOCAL"
SCHEMA_VERSION = 1


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


class HandoffRepo:
    def __init__(self, path: str, qualification_provider=None):
        """``qualification_provider`` is an optional callable ``day -> QualificationProduct``.

        When supplied (e.g. backed by the rule/event service), staleness checks
        compare against the *live* qualification for the day, so a hold/event
        revision invalidates an old plan/proposal even before a new product is
        explicitly ingested.  The handoff layer never computes dates itself; it
        only compares digests of products it is handed.
        """
        self.path = path
        self.qualification_provider = qualification_provider
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._depth = 0
        self._init_schema()
        self._reconcile_custody()

    # ------------------------------------------------------------------ setup
    @contextmanager
    def _tx(self):
        if self._depth == 0:
            self.conn.execute("BEGIN IMMEDIATE")
        self._depth += 1
        try:
            yield
        except Exception:
            self._depth -= 1
            if self._depth == 0:
                self.conn.rollback()
            raise
        else:
            self._depth -= 1
            if self._depth == 0:
                self.conn.commit()

    def _init_schema(self) -> None:
        with self._tx():
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                -- Mirror of catalog membership, supplied by the rules side.
                CREATE TABLE IF NOT EXISTS catalog_members (
                    doc_id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_catalog_case ON catalog_members(case_id);

                -- Append-only custody ledger.  Responsibility only transfers
                -- on a committed whole-case handoff.
                CREATE TABLE IF NOT EXISTS custody_events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    case_id TEXT NOT NULL,
                    handoff_id TEXT NOT NULL,
                    from_party TEXT NOT NULL,
                    to_party TEXT NOT NULL,
                    as_of_day INTEGER NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                );
                CREATE TABLE IF NOT EXISTS custody_current (
                    case_id TEXT PRIMARY KEY,
                    custodian TEXT NOT NULL
                );

                -- Immutable snapshots of qualification products received.
                CREATE TABLE IF NOT EXISTS qualification_products (
                    product_id TEXT PRIMARY KEY,
                    as_of_day INTEGER NOT NULL,
                    digest TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_qp_day
                    ON qualification_products(as_of_day);

                -- Prepared (not yet committed) whole-case requests.
                CREATE TABLE IF NOT EXISTS proposals (
                    proposal_id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL,
                    from_party TEXT NOT NULL,
                    to_party TEXT NOT NULL,
                    as_of_day INTEGER NOT NULL,
                    members_json TEXT NOT NULL,
                    product_id TEXT NOT NULL,
                    per_doc_json TEXT NOT NULL,
                    membership_digest TEXT NOT NULL,
                    custody_basis TEXT NOT NULL,
                    status TEXT NOT NULL,
                    handoff_id TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                );

                -- Committed handoffs: members and qualification source fixed.
                CREATE TABLE IF NOT EXISTS handoffs (
                    handoff_id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL,
                    from_party TEXT NOT NULL,
                    to_party TEXT NOT NULL,
                    as_of_day INTEGER NOT NULL,
                    members_json TEXT NOT NULL,
                    product_id TEXT NOT NULL,
                    per_doc_json TEXT NOT NULL,
                    request_canon TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                );

                -- Published, frozen handoff *lists* derived from a qualification
                -- product.  Purely a handoff-side artifact; never re-derived from
                -- the current catalog.
                CREATE TABLE IF NOT EXISTS plans (
                    plan_id TEXT PRIMARY KEY,
                    product_id TEXT NOT NULL,
                    as_of_day INTEGER NOT NULL,
                    basis_json TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                );
                """
            )

    def close(self) -> None:
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------- catalog mirror
    def sync_catalog_membership(self, documents: Iterable[dict]) -> dict:
        """Mirror catalog membership from the rules side (never owned here)."""
        incoming: dict[str, str] = {}
        for i, d in enumerate(documents):
            doc_id = str(d.get("doc_id", ""))
            case_id = str(d.get("case_id", ""))
            if not doc_id or not case_id:
                raise RejectError("invalid_document", "doc_id and case_id required", i)
            incoming[doc_id] = case_id
        with self._tx():
            for doc_id, case_id in incoming.items():
                self.conn.execute(
                    "INSERT INTO catalog_members(doc_id, case_id) VALUES (?,?) "
                    "ON CONFLICT(doc_id) DO UPDATE SET case_id=excluded.case_id",
                    (doc_id, case_id),
                )
        return {"mirrored": len(incoming)}

    def current_members(self, case_id: str) -> list[str]:
        return [
            r["doc_id"]
            for r in self.conn.execute(
                "SELECT doc_id FROM catalog_members WHERE case_id=? ORDER BY doc_id", (case_id,)
            )
        ]

    def known_cases(self) -> list[str]:
        return [
            r["case_id"]
            for r in self.conn.execute(
                "SELECT DISTINCT case_id FROM catalog_members ORDER BY case_id"
            )
        ]

    # ---------------------------------------------------- qualification input
    def ingest_product(self, product_id: str, product: QualificationProduct) -> dict:
        """Store an immutable qualification product snapshot from the rule side."""
        import hashlib

        payload = canon(product.as_dict())
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        row = self.conn.execute(
            "SELECT payload FROM qualification_products WHERE product_id=?", (product_id,)
        ).fetchone()
        if row is not None:
            if row["payload"] != payload:
                raise ConflictError(
                    "product_conflict",
                    f"qualification product {product_id} re-ingested with different content",
                )
            return {"product_id": product_id, "replayed": True, "digest": digest}
        with self._tx():
            self.conn.execute(
                "INSERT INTO qualification_products(product_id, as_of_day, digest, payload) "
                "VALUES (?,?,?,?)",
                (product_id, product.as_of_day, digest, payload),
            )
        return {"product_id": product_id, "replayed": False, "digest": digest}

    def _load_product(self, product_id: str) -> QualificationProduct:
        row = self.conn.execute(
            "SELECT payload FROM qualification_products WHERE product_id=?", (product_id,)
        ).fetchone()
        if row is None:
            raise UnknownReference(
                "unknown_product", f"qualification product {product_id} not ingested"
            )
        return QualificationProduct.from_dict(json.loads(row["payload"]))

    def _ingested_product_for_day(self, as_of_day: int) -> Optional[tuple[str, QualificationProduct]]:
        """Most recently ingested product snapshot for a day (handoff input only)."""
        row = self.conn.execute(
            "SELECT product_id, payload FROM qualification_products WHERE as_of_day=? "
            "ORDER BY rowid DESC LIMIT 1",
            (as_of_day,),
        ).fetchone()
        if row is None:
            return None
        return row["product_id"], QualificationProduct.from_dict(json.loads(row["payload"]))

    def _current_qualification_for_day(
        self, as_of_day: int
    ) -> Optional[tuple[str, QualificationProduct]]:
        """Live qualification when a provider is set, else the ingested snapshot.

        Only staleness checks use this; an actual transfer always resolves to an
        ingested, frozen product so the qualification source is persisted.
        """
        if self.qualification_provider is not None:
            product = self.qualification_provider(as_of_day)
            if product is not None:
                return f"live:{as_of_day}", product
        return self._ingested_product_for_day(as_of_day)

    # ---------------------------------------------------------------- custody
    def _ledger_custodian(self, case_id: str) -> str:
        row = self.conn.execute(
            "SELECT to_party FROM custody_events WHERE case_id=? ORDER BY seq DESC LIMIT 1",
            (case_id,),
        ).fetchone()
        return row["to_party"] if row else INITIAL_CUSTODIAN

    def current_custodian(self, case_id: str) -> str:
        row = self.conn.execute(
            "SELECT custodian FROM custody_current WHERE case_id=?", (case_id,)
        ).fetchone()
        if row is not None:
            return row["custodian"]
        return self._ledger_custodian(case_id)

    def custody_history(self, case_id: str) -> list[dict]:
        return [
            {
                "seq": r["seq"],
                "handoff_id": r["handoff_id"],
                "from": r["from_party"],
                "to": r["to_party"],
                "as_of_day": r["as_of_day"],
            }
            for r in self.conn.execute(
                "SELECT * FROM custody_events WHERE case_id=? ORDER BY seq", (case_id,)
            )
        ]

    def _ensure_known_case(self, case_id: str) -> list[str]:
        members = self.current_members(case_id)
        if not members:
            raise UnknownReference("unknown_case", f"case {case_id} is not mirrored")
        return members

    # ----------------------------------------------------------- validation
    @staticmethod
    def _normalise_request(request: dict) -> dict:
        if not isinstance(request, dict):
            raise RejectError("invalid_request", "request must be an object")
        handoff_id = request.get("handoff_id")
        case_id = request.get("case_id")
        doc_ids = request.get("doc_ids")
        from_party = request.get("from")
        to_party = request.get("to")
        as_of_day = request.get("as_of_day")
        product_id = request.get("qualification_product_id")

        if not isinstance(handoff_id, str) or not handoff_id:
            raise RejectError("invalid_request", "handoff_id required")
        if not isinstance(case_id, str) or not case_id:
            raise RejectError("invalid_request", "case_id required")
        if not isinstance(doc_ids, list) or not doc_ids:
            raise RejectError("invalid_request", "doc_ids must be a non-empty list")
        for i, d in enumerate(doc_ids):
            if not isinstance(d, str) or not d:
                raise RejectError("invalid_request", "doc_ids entries must be strings", i)
        if len(set(doc_ids)) != len(doc_ids):
            raise RejectError("invalid_request", "doc_ids contains duplicates")
        if not isinstance(from_party, str) or not from_party:
            raise RejectError("invalid_request", "from required")
        if not isinstance(to_party, str) or not to_party:
            raise RejectError("invalid_request", "to required")
        if from_party == to_party:
            raise RejectError("invalid_request", "from and to must differ")
        if not _is_int(as_of_day) or as_of_day < 0:
            raise RejectError("invalid_request", "as_of_day must be int >= 0")
        if product_id is not None and not isinstance(product_id, str):
            raise RejectError("invalid_request", "qualification_product_id must be a string")
        return {
            "handoff_id": handoff_id,
            "case_id": case_id,
            "doc_ids": sorted(doc_ids),
            "from": from_party,
            "to": to_party,
            "as_of_day": as_of_day,
            "qualification_product_id": product_id,
        }

    @staticmethod
    def _identity(req: dict) -> dict:
        """User-visible identity of a handoff request.

        The qualification product is provenance, not part of request identity,
        so the same request submitted directly or confirmed from a prepared
        proposal canonicalises identically.
        """
        return {
            "handoff_id": req["handoff_id"],
            "case_id": req["case_id"],
            "doc_ids": req["doc_ids"],
            "from": req["from"],
            "to": req["to"],
            "as_of_day": req["as_of_day"],
        }

    @staticmethod
    def _request_canon(req: dict) -> str:
        return canon(HandoffRepo._identity(req))

    def _resolve_product(self, req: dict) -> tuple[str, QualificationProduct]:
        if req["qualification_product_id"] is not None:
            pid = req["qualification_product_id"]
            return pid, self._load_product(pid)
        found = self._ingested_product_for_day(req["as_of_day"])
        if found is None:
            raise RejectError(
                "no_qualification",
                f"no qualification product ingested for as_of_day {req['as_of_day']}",
            )
        return found

    def _collect_eligibility_failures(
        self, req: dict, product_id: str, product: QualificationProduct, members: list[str]
    ) -> list[dict]:
        failures: list[dict] = []
        if product.as_of_day != req["as_of_day"]:
            failures.append(
                {
                    "kind": "wrong_as_of_day",
                    "detail": f"product {product_id} is for day {product.as_of_day}, "
                              f"request is for {req['as_of_day']}",
                }
            )
        for doc_id in members:
            q = product.documents.get(doc_id)
            if q is None:
                failures.append(
                    {"doc_id": doc_id, "kind": "absent_from_product",
                     "detail": "document missing from qualification product"}
                )
                continue
            if q.case_id != req["case_id"]:
                failures.append(
                    {"doc_id": doc_id, "kind": "case_mismatch",
                     "detail": f"qualification case {q.case_id} != {req['case_id']}"}
                )
            if not q.releasable:
                failures.append(
                    {
                        "doc_id": doc_id,
                        "kind": "not_releasable",
                        "detail": "document is not releasable on the requested day",
                        "block_reasons": [b.as_dict() for b in q.block_reasons],
                    }
                )
        return failures

    def _eligibility_map(
        self, product_id: str, product: QualificationProduct, members: list[str]
    ) -> dict:
        out = {}
        for doc_id in members:
            q = product.documents[doc_id]
            out[doc_id] = {
                "digest": q.content_digest(),
                "releasable": q.releasable,
                "product_id": product_id,
            }
        return out

    def _membership_digest(self, members: list[str]) -> str:
        import hashlib

        return hashlib.sha256(canon(sorted(members)).encode("utf-8")).hexdigest()

    def _validate_whole_case(
        self, req: dict, members: list[str]
    ) -> tuple[str, QualificationProduct]:
        requested = set(req["doc_ids"])
        actual = set(members)
        missing = sorted(actual - requested)
        extra = sorted(requested - actual)
        if missing or extra:
            raise RejectError(
                "not_whole_case",
                "requested document set does not equal current case membership"
                + (f"; missing={missing}" if missing else "")
                + (f"; extra={extra}" if extra else ""),
            )
        custodian = self.current_custodian(req["case_id"])
        if custodian != req["from"]:
            raise RejectError(
                "custody_mismatch",
                f"case {req['case_id']} is held by {custodian}, not {req['from']}",
            )
        product_id, product = self._resolve_product(req)
        failures = self._collect_eligibility_failures(
            req, product_id, product, sorted(actual)
        )
        if failures:
            raise RejectError(
                "ineligible_members",
                "one or more members are not releasable: "
                + json.dumps(failures, ensure_ascii=False, sort_keys=True),
            )
        return product_id, product

    # ------------------------------------------------------------ submission
    def _stored_handoff(self, handoff_id: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM handoffs WHERE handoff_id=?", (handoff_id,)
        ).fetchone()

    @staticmethod
    def _handoff_record(row: sqlite3.Row, replayed: bool) -> dict:
        return {
            "handoff_id": row["handoff_id"],
            "case_id": row["case_id"],
            "from": row["from_party"],
            "to": row["to_party"],
            "as_of_day": row["as_of_day"],
            "doc_ids": json.loads(row["members_json"]),
            "product_id": row["product_id"],
            "per_doc": json.loads(row["per_doc_json"]),
            "replayed": replayed,
            "replayed_readonly": replayed,
        }

    def submit(self, request: dict) -> dict:
        """Validate and, if whole-case + eligible + correct custodian, commit."""
        req = self._normalise_request(request)
        canon_req = self._request_canon(req)

        existing = self._stored_handoff(req["handoff_id"])
        if existing is not None:
            if existing["request_canon"] != canon_req:
                raise ConflictError(
                    "handoff_conflict",
                    f"handoff_id {req['handoff_id']} replayed with different content",
                )
            return self._handoff_record(existing, replayed=True)

        members = self._ensure_known_case(req["case_id"])
        product_id, product = self._validate_whole_case(req, members)
        elig = self._eligibility_map(product_id, product, req["doc_ids"])

        with self._tx():
            # Re-check under the write lock to avoid a lost update race.
            custodian = self._ledger_custodian(req["case_id"])
            if custodian != req["from"]:
                raise RejectError(
                    "custody_mismatch",
                    f"case {req['case_id']} is held by {custodian}, not {req['from']}",
                )
            self.conn.execute(
                "INSERT INTO handoffs(handoff_id, case_id, from_party, to_party, as_of_day, "
                "members_json, product_id, per_doc_json, request_canon) VALUES (?,?,?,?,?,?,?,?,?)",
                (req["handoff_id"], req["case_id"], req["from"], req["to"], req["as_of_day"],
                 json.dumps(req["doc_ids"]), product_id, canon(elig), canon_req),
            )
            self.conn.execute(
                "INSERT INTO custody_events(case_id, handoff_id, from_party, to_party, as_of_day) "
                "VALUES (?,?,?,?,?)",
                (req["case_id"], req["handoff_id"], req["from"], req["to"], req["as_of_day"]),
            )
            self.conn.execute(
                "INSERT INTO custody_current(case_id, custodian) VALUES (?,?) "
                "ON CONFLICT(case_id) DO UPDATE SET custodian=excluded.custodian",
                (req["case_id"], req["to"]),
            )
        row = self._stored_handoff(req["handoff_id"])
        return self._handoff_record(row, replayed=False)

    # -------------------------------------------------------------- prepare
    def prepare(self, request: dict) -> dict:
        """Validate and freeze a whole-case request without transferring custody."""
        req = self._normalise_request(request)
        proposal_id = request.get("proposal_id", req["handoff_id"])
        if not isinstance(proposal_id, str) or not proposal_id:
            raise RejectError("invalid_request", "proposal_id must be a non-empty string")

        row = self.conn.execute(
            "SELECT * FROM proposals WHERE proposal_id=?", (proposal_id,)
        ).fetchone()
        if row is not None:
            if row["status"] == "committed":
                raise ConflictError(
                    "proposal_conflict",
                    f"proposal {proposal_id} already committed",
                )
            stored_req = {
                "case_id": row["case_id"],
                "from": row["from_party"],
                "to": row["to_party"],
                "as_of_day": row["as_of_day"],
                "doc_ids": json.loads(row["members_json"]),
            }
            if (
                stored_req["case_id"] != req["case_id"]
                or stored_req["from"] != req["from"]
                or stored_req["to"] != req["to"]
                or stored_req["as_of_day"] != req["as_of_day"]
                or sorted(stored_req["doc_ids"]) != req["doc_ids"]
            ):
                raise ConflictError(
                    "proposal_conflict",
                    f"proposal {proposal_id} re-prepared with different content",
                )
            # Same request: if still valid, return the frozen snapshot; if the
            # world moved on, the (re)prepare also reveals staleness.
            if row["status"] == "prepared":
                stale = self._stale_reasons(row)
                if stale:
                    with self._tx():
                        self.conn.execute(
                            "UPDATE proposals SET status='stale' WHERE proposal_id=?",
                            (proposal_id,),
                        )
                    raise RejectError(
                        "stale_proposal",
                        f"proposal {proposal_id} no longer valid: " + "; ".join(stale),
                    )
            return self._proposal_record(row, replayed=True)

        members = self._ensure_known_case(req["case_id"])
        product_id, product = self._validate_whole_case(req, members)
        elig = self._eligibility_map(product_id, product, req["doc_ids"])
        mdigest = self._membership_digest(members)

        with self._tx():
            custodian = self._ledger_custodian(req["case_id"])
            if custodian != req["from"]:
                raise RejectError(
                    "custody_mismatch",
                    f"case {req['case_id']} is held by {custodian}, not {req['from']}",
                )
            self.conn.execute(
                "INSERT INTO proposals(proposal_id, case_id, from_party, to_party, as_of_day, "
                "members_json, product_id, per_doc_json, membership_digest, custody_basis, status) "
                "VALUES (?,?,?,?,?,?,?,?,?,?, 'prepared')",
                (proposal_id, req["case_id"], req["from"], req["to"], req["as_of_day"],
                 json.dumps(req["doc_ids"]), product_id, canon(elig), mdigest, custodian),
            )
        row = self.conn.execute(
            "SELECT * FROM proposals WHERE proposal_id=?", (proposal_id,)
        ).fetchone()
        return self._proposal_record(row, replayed=False)

    @staticmethod
    def _proposal_record(row: sqlite3.Row, replayed: bool) -> dict:
        return {
            "proposal_id": row["proposal_id"],
            "case_id": row["case_id"],
            "from": row["from_party"],
            "to": row["to_party"],
            "as_of_day": row["as_of_day"],
            "doc_ids": json.loads(row["members_json"]),
            "product_id": row["product_id"],
            "per_doc": json.loads(row["per_doc_json"]),
            "status": row["status"],
            "replayed": replayed,
        }

    def _stale_reasons(self, row: sqlite3.Row) -> list[str]:
        reasons: list[str] = []
        case_id = row["case_id"]
        members = self.current_members(case_id)
        proposed = json.loads(row["members_json"])
        if sorted(members) != sorted(proposed):
            reasons.append(
                f"membership changed: prepared {sorted(proposed)} vs current {sorted(members)}"
            )
        if self.current_custodian(case_id) != row["from_party"]:
            reasons.append(
                f"custody changed: prepared from {row['from_party']} vs current "
                f"{self.current_custodian(case_id)}"
            )
        found = self._current_qualification_for_day(row["as_of_day"])
        if found is None:
            reasons.append(
                f"no current qualification product for as_of_day {row['as_of_day']}"
            )
            return reasons
        pid, product = found
        stored = json.loads(row["per_doc_json"])
        for doc_id in sorted(proposed):
            q = product.documents.get(doc_id)
            if q is None:
                reasons.append(f"{doc_id}: absent from current qualification product ({pid})")
                continue
            if not q.releasable:
                reasons.append(f"{doc_id}: no longer releasable")
                continue
            if q.content_digest() != stored.get(doc_id, {}).get("digest"):
                reasons.append(f"{doc_id}: qualification changed since prepare")
        return reasons

    def confirm(self, proposal_id: str, handoff_id: Optional[str] = None) -> dict:
        row = self.conn.execute(
            "SELECT * FROM proposals WHERE proposal_id=?", (proposal_id,)
        ).fetchone()
        if row is None:
            raise UnknownReference("unknown_proposal", f"unknown proposal {proposal_id}")
        if row["status"] == "committed":
            stored = self._stored_handoff(row["handoff_id"])
            if stored is None:
                raise ConflictError(
                    "proposal_conflict",
                    f"proposal {proposal_id} marked committed but its handoff is missing",
                )
            return self._handoff_record(stored, replayed=True)
        if row["status"] == "stale":
            raise RejectError(
                "stale_proposal", f"proposal {proposal_id} was already invalidated"
            )

        stale = self._stale_reasons(row)
        if stale:
            with self._tx():
                self.conn.execute(
                    "UPDATE proposals SET status='stale' WHERE proposal_id=?", (proposal_id,)
                )
            raise RejectError(
                "stale_proposal",
                f"proposal {proposal_id} no longer valid: " + "; ".join(stale),
            )

        hid = handoff_id or row["proposal_id"]
        req_canon = canon(
            {
                "handoff_id": hid,
                "case_id": row["case_id"],
                "doc_ids": sorted(json.loads(row["members_json"])),
                "from": row["from_party"],
                "to": row["to_party"],
                "as_of_day": row["as_of_day"],
            }
        )
        existing = self._stored_handoff(hid)
        if existing is not None:
            if existing["request_canon"] != req_canon:
                raise ConflictError(
                    "handoff_conflict",
                    f"handoff_id {hid} already exists with different content",
                )
            return self._handoff_record(existing, replayed=True)

        with self._tx():
            custodian = self._ledger_custodian(row["case_id"])
            if custodian != row["from_party"]:
                self.conn.execute(
                    "UPDATE proposals SET status='stale' WHERE proposal_id=?", (proposal_id,)
                )
                raise RejectError(
                    "stale_proposal",
                    f"custody changed before commit: {custodian} != {row['from_party']}",
                )
            self.conn.execute(
                "INSERT INTO handoffs(handoff_id, case_id, from_party, to_party, as_of_day, "
                "members_json, product_id, per_doc_json, request_canon) VALUES (?,?,?,?,?,?,?,?,?)",
                (hid, row["case_id"], row["from_party"], row["to_party"], row["as_of_day"],
                 row["members_json"], row["product_id"], row["per_doc_json"], req_canon),
            )
            self.conn.execute(
                "INSERT INTO custody_events(case_id, handoff_id, from_party, to_party, as_of_day) "
                "VALUES (?,?,?,?,?)",
                (row["case_id"], hid, row["from_party"], row["to_party"], row["as_of_day"]),
            )
            self.conn.execute(
                "INSERT INTO custody_current(case_id, custodian) VALUES (?,?) "
                "ON CONFLICT(case_id) DO UPDATE SET custodian=excluded.custodian",
                (row["case_id"], row["to_party"]),
            )
            self.conn.execute(
                "UPDATE proposals SET status='committed', handoff_id=? WHERE proposal_id=?",
                (hid, proposal_id),
            )
        return self._handoff_record(self._stored_handoff(hid), replayed=False)

    def proposal_status(self, proposal_id: str) -> str:
        row = self.conn.execute(
            "SELECT status FROM proposals WHERE proposal_id=?", (proposal_id,)
        ).fetchone()
        if row is None:
            raise UnknownReference("unknown_proposal", f"unknown proposal {proposal_id}")
        return row["status"]

    # --------------------------------------------------------------- plans
    def prepare_and_publish(
        self,
        plan_id: str,
        product_id: str,
        as_of_day: int,
        *,
        from_party: str = INITIAL_CUSTODIAN,
        to_party: str = "VAULT1",
    ) -> dict:
        """Publish a frozen whole-case handoff list from a qualification product.

        This is the list-level "prepare": it decides, from the *received*
        product, which cases are wholly releasable to ``to_party`` and records
        the per-document justification.  It never transfers custody.  The list
        is immutable; a later hold/event change must not rewrite it.
        """
        product = self._load_product(product_id)
        if product.as_of_day != as_of_day:
            raise RejectError(
                "wrong_as_of_day",
                f"product {product_id} is for day {product.as_of_day}, requested {as_of_day}",
            )
        cases: dict[str, list[str]] = {}
        for doc_id in sorted(product.documents):
            q = product.documents[doc_id]
            cases.setdefault(q.case_id, []).append(doc_id)

        eligible: dict[str, dict] = {}
        blocked: dict[str, dict] = {}
        for case_id in sorted(cases):
            members = cases[case_id]
            if self.current_members(case_id) != sorted(members):
                blocked[case_id] = {"reason": "membership_differs_from_catalog",
                                    "members": sorted(members)}
                continue
            if self.current_custodian(case_id) != from_party:
                blocked[case_id] = {
                    "reason": "custody_mismatch",
                    "custodian": self.current_custodian(case_id),
                }
                continue
            bad = [d for d in members if not product.documents[d].releasable]
            if bad:
                blocked[case_id] = {
                    "reason": "ineligible_members",
                    "doc_ids": sorted(bad),
                    "detail": {
                        d: [b.as_dict() for b in product.documents[d].block_reasons]
                        for d in sorted(bad)
                    },
                }
                continue
            eligible[case_id] = {
                "case_id": case_id,
                "doc_ids": sorted(members),
                "from": self.current_custodian(case_id),
                "to": to_party,
                "as_of_day": as_of_day,
                "product_id": product_id,
                "per_doc": {
                    d: {
                        "computed_day": product.documents[d].computed_day,
                        "releasable": True,
                        "digest": product.documents[d].content_digest(),
                    }
                    for d in sorted(members)
                },
            }

        payload = {
            "plan_id": plan_id,
            "product_id": product_id,
            "as_of_day": as_of_day,
            "from": from_party,
            "to": to_party,
            # Full product digests: any qualification change (for any document
            # in the published product) invalidates the plan, even if it only
            # affects a case that was blocked at publish time.
            "product_basis": dict(product.catalog_basis),
            "doc_digests": {
                d: product.documents[d].content_digest() for d in sorted(product.documents)
            },
            "eligible": eligible,
            "blocked": blocked,
        }
        canon_payload = canon(payload)
        row = self.conn.execute(
            "SELECT payload FROM plans WHERE plan_id=?", (plan_id,)
        ).fetchone()
        if row is not None:
            if row["payload"] != canon_payload:
                raise ConflictError(
                    "plan_conflict", f"plan {plan_id} already published with different content"
                )
            return {"plan_id": plan_id, "replayed": True, "payload": payload}
        with self._tx():
            self.conn.execute(
                "INSERT INTO plans(plan_id, product_id, as_of_day, basis_json, payload) "
                "VALUES (?,?,?,?,?)",
                (plan_id, product_id, as_of_day,
                 canon(product.catalog_basis), canon_payload),
            )
        return {"plan_id": plan_id, "replayed": False, "payload": payload}

    def get_plan(self, plan_id: str) -> dict:
        row = self.conn.execute(
            "SELECT payload FROM plans WHERE plan_id=?", (plan_id,)
        ).fetchone()
        if row is None:
            raise UnknownReference("unknown_plan", f"unknown plan {plan_id}")
        return json.loads(row["payload"])

    def plan_staleness(self, plan_id: str) -> list[str]:
        """Reasons a published plan must not be confirmed now (empty == current)."""
        plan = self.get_plan(plan_id)
        reasons: list[str] = []

        # 1) The qualification of every document in the published product must
        #    still match.  Per-document digests are used rather than a global
        #    catalog counter, so an unrelated ingest does not stale the plan but
        #    any change that actually affects a published qualification does.
        found = self._current_qualification_for_day(plan["as_of_day"])
        if found is None:
            reasons.append(f"no current qualification product for day {plan['as_of_day']}")
            return reasons
        _, product = found
        for doc_id, digest in sorted(plan.get("doc_digests", {}).items()):
            q = product.documents.get(doc_id)
            if q is None:
                reasons.append(f"{doc_id}: absent from current qualification product")
            elif q.content_digest() != digest:
                reasons.append(f"{doc_id}: qualification changed since publish")

        # 2) Membership and custody must be unchanged for every eligible case.
        for case_id, entry in sorted(plan["eligible"].items()):
            members = self.current_members(case_id)
            if members != sorted(entry["doc_ids"]):
                reasons.append(
                    f"{case_id}: membership changed {entry['doc_ids']} -> {members}"
                )
            custodian = self.current_custodian(case_id)
            if custodian != entry["from"]:
                reasons.append(
                    f"{case_id}: custody changed {entry['from']} -> {custodian}"
                )
        return reasons

    def confirm_handoff_list(self, plan_id: str, handoff_id: Optional[str] = None) -> dict:
        """Confirm a published plan; refuse wholesale if anything moved since."""
        plan = self.get_plan(plan_id)
        stale = self.plan_staleness(plan_id)
        if stale:
            raise RejectError(
                "stale_plan", f"plan {plan_id} no longer valid: " + "; ".join(stale)
            )
        if not plan["eligible"]:
            raise RejectError("empty_plan", f"plan {plan_id} has no eligible cases")
        committed = []
        for case_id in sorted(plan["eligible"]):
            entry = plan["eligible"][case_id]
            hid = handoff_id or f"{plan_id}:{case_id}"
            req = {
                "handoff_id": hid,
                "case_id": case_id,
                "doc_ids": entry["doc_ids"],
                "from": entry["from"],
                "to": entry["to"],
                "as_of_day": entry["as_of_day"],
                "qualification_product_id": entry["product_id"],
            }
            committed.append(self.submit(req))
        return {"plan_id": plan_id, "handoffs": committed}

    # -------------------------------------------------------- historical read
    def get_handoff(self, handoff_id: str) -> dict:
        row = self._stored_handoff(handoff_id)
        if row is None:
            raise UnknownReference("unknown_handoff", f"unknown handoff {handoff_id}")
        return self._handoff_record(row, replayed=True)

    def list_handoffs(self) -> list[dict]:
        return [
            self._handoff_record(r, replayed=True)
            for r in self.conn.execute("SELECT * FROM handoffs ORDER BY created_at, handoff_id")
        ]

    # ----------------------------------------------------------- reconciling
    def _reconcile_custody(self) -> dict:
        """Ensure maintained custody equals the fold of the ledger; repair if not."""
        cases = {
            r["case_id"]
            for r in self.conn.execute("SELECT DISTINCT case_id FROM custody_events")
        } | {
            r["case_id"]
            for r in self.conn.execute("SELECT case_id FROM custody_current")
        }
        repaired = []
        with self._tx():
            for case_id in sorted(cases):
                expected = self._ledger_custodian(case_id)
                row = self.conn.execute(
                    "SELECT custodian FROM custody_current WHERE case_id=?", (case_id,)
                ).fetchone()
                if row is None:
                    self.conn.execute(
                        "INSERT INTO custody_current(case_id, custodian) VALUES (?,?)",
                        (case_id, expected),
                    )
                    repaired.append(case_id)
                elif row["custodian"] != expected:
                    self.conn.execute(
                        "UPDATE custody_current SET custodian=? WHERE case_id=?",
                        (expected, case_id),
                    )
                    repaired.append(case_id)
        return {"repaired": repaired}

    def verify(self) -> dict:
        """Reopen self-check: custody fold, handoff integrity, artifacts."""
        report: dict = {"ok": True, "custody": [], "handoffs": [], "errors": []}

        cases = {
            r["case_id"]
            for r in self.conn.execute("SELECT DISTINCT case_id FROM custody_events")
        } | {
            r["case_id"]
            for r in self.conn.execute("SELECT case_id FROM custody_current")
        } | set(self.known_cases())
        for case_id in sorted(cases):
            ledger = self._ledger_custodian(case_id)
            current = self.current_custodian(case_id)
            ok = ledger == current
            report["custody"].append(
                {"case_id": case_id, "ledger": ledger, "current": current, "ok": ok}
            )
            if not ok:
                report["ok"] = False
                report["errors"].append(
                    f"{case_id}: custody mismatch ledger={ledger} current={current}"
                )

        for row in self.conn.execute("SELECT * FROM handoffs ORDER BY handoff_id"):
            per_doc = json.loads(row["per_doc_json"])
            members = json.loads(row["members_json"])
            entry = {
                "handoff_id": row["handoff_id"],
                "case_id": row["case_id"],
                "members": members,
                "product_id": row["product_id"],
                "per_doc_ok": sorted(per_doc) == sorted(members),
                "from": row["from_party"],
                "to": row["to_party"],
            }
            if not entry["per_doc_ok"]:
                report["ok"] = False
                report["errors"].append(
                    f"{row['handoff_id']}: per-doc snapshot does not cover members"
                )
            # Verify the custody chain links: every non-initial transfer starts
            # at the previous custodian.
            report["handoffs"].append(entry)

        for case_id in sorted(set(self.known_cases())):
            history = self.custody_history(case_id)
            expected_from = INITIAL_CUSTODIAN
            for h in history:
                if h["from"] != expected_from:
                    report["ok"] = False
                    report["errors"].append(
                        f"{case_id}: custody chain break at handoff {h['handoff_id']} "
                        f"from={h['from']} expected={expected_from}"
                    )
                expected_from = h["to"]

        return report

    # -------------------------------------------------------------- artifacts
    def write_handoff_artifact(self, handoff_id: str, directory: str) -> str:
        os.makedirs(directory, exist_ok=True)
        rec = self.get_handoff(handoff_id)
        path = os.path.join(directory, f"handoff_{handoff_id}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(rec, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
        return path
