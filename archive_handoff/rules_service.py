"""Independent preservation-rule / event service.

Responsibilities
----------------
* Own the rule, document, event and hold catalog (SQLite, fictional data only).
* Validate ingestion batches atomically: stable IDs + revisions; identical
  re-plays are no-ops, differing content / illegal intervals / unknown
  references reject the *whole* batch.
* Answer temporal eligibility queries and publish immutable qualification
  products for a fixed ``as_of_day``.

This module never knows about handoff custody.  It answers only "may this
document be released at day X, and why (or why not)?".
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from typing import Iterable, Optional

from .models import (
    ActiveHold,
    BlockReason,
    ConflictError,
    DocQualification,
    Document,
    Event,
    Hold,
    InvalidInterval,
    QualificationProduct,
    RejectError,
    Rule,
    RuleReason,
    UnknownReference,
)

SCHEMA_VERSION = 1


def canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


class RuleEventStore:
    """SQLite-backed rule/event catalog and qualification engine."""

    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._depth = 0
        self._init_schema()

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
                CREATE TABLE IF NOT EXISTS rules (
                    rule_id TEXT PRIMARY KEY,
                    revision INTEGER NOT NULL,
                    trigger_type TEXT NOT NULL,
                    keep_days INTEGER NOT NULL,
                    canon TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS documents (
                    doc_id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL,
                    rules_json TEXT NOT NULL,
                    canon TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    doc_id TEXT NOT NULL,
                    type TEXT NOT NULL,
                    day INTEGER NOT NULL,
                    revision INTEGER NOT NULL,
                    canon TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS holds (
                    hold_id TEXT PRIMARY KEY,
                    case_id TEXT,
                    doc_id TEXT,
                    start INTEGER NOT NULL,
                    end INTEGER,
                    revision INTEGER NOT NULL,
                    canon TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS products (
                    product_id TEXT PRIMARY KEY,
                    as_of_day INTEGER NOT NULL,
                    source_revision INTEGER NOT NULL,
                    digest TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    canon TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_doc_type
                    ON events(doc_id, type);
                CREATE INDEX IF NOT EXISTS idx_holds_case ON holds(case_id);
                CREATE INDEX IF NOT EXISTS idx_holds_doc ON holds(doc_id);
                """
            )
            self.conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES ('catalog_revision', '0')"
            )
            self.conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    # ---------------------------------------------------------------- helpers
    def _meta_int(self, key: str) -> int:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return int(row["value"]) if row else 0

    def _set_meta_int(self, key: str, value: int) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )

    @property
    def catalog_revision(self) -> int:
        return self._meta_int("catalog_revision")

    def _bump_catalog(self) -> None:
        self._set_meta_int("catalog_revision", self.catalog_revision + 1)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -------------------------------------------------------------- ingest
    def import_rules(self, rules: Iterable[dict | Rule]) -> dict:
        parsed: list[Rule] = []
        for i, raw in enumerate(rules):
            r = raw if isinstance(raw, Rule) else Rule(
                rule_id=str(raw.get("rule_id", "")),
                revision=raw.get("revision"),
                trigger_type=str(raw.get("trigger_type", "")),
                keep_days=raw.get("keep_days"),
            )
            if not r.rule_id:
                raise RejectError("invalid_rule", "rule_id required", i)
            if not _is_int(r.revision) or r.revision < 1:
                raise RejectError("invalid_rule", "revision must be int >= 1", i)
            if not r.trigger_type:
                raise RejectError("invalid_rule", "trigger_type required", i)
            if not _is_int(r.keep_days) or r.keep_days < 0:
                raise RejectError("invalid_rule", "keep_days must be int >= 0", i)
            parsed.append(r)

        pending: dict[str, Rule] = {}
        changed = False
        for i, r in enumerate(parsed):
            existing = pending.get(r.rule_id)
            if existing is None:
                row = self.conn.execute(
                    "SELECT * FROM rules WHERE rule_id=?", (r.rule_id,)
                ).fetchone()
                if row is not None:
                    existing = Rule(
                        row["rule_id"], row["revision"], row["trigger_type"], row["keep_days"]
                    )
            if existing is not None:
                same_content = (
                    existing.trigger_type == r.trigger_type
                    and existing.keep_days == r.keep_days
                )
                if r.revision == existing.revision:
                    if not same_content:
                        raise ConflictError(
                            "rule_conflict",
                            f"rule {r.rule_id} rev {r.revision} replayed with different content",
                            i,
                        )
                    continue  # exact replay, no-op
                if r.revision < existing.revision:
                    raise ConflictError(
                        "stale_revision",
                        f"rule {r.rule_id} rev {r.revision} is older than stored rev {existing.revision}",
                        i,
                    )
                # higher revision supersedes only when it replaces the pending one
                pending[r.rule_id] = r
                changed = True
            else:
                pending[r.rule_id] = r
                changed = True

        if changed:
            with self._tx():
                for r in pending.values():
                    self.conn.execute(
                        "INSERT INTO rules(rule_id, revision, trigger_type, keep_days, canon) "
                        "VALUES (?,?,?,?,?) ON CONFLICT(rule_id) DO UPDATE SET "
                        "revision=excluded.revision, trigger_type=excluded.trigger_type, "
                        "keep_days=excluded.keep_days, canon=excluded.canon",
                        (r.rule_id, r.revision, r.trigger_type, r.keep_days,
                         canon([r.rule_id, r.revision, r.trigger_type, r.keep_days])),
                    )
                self._bump_catalog()
        return {"accepted": len(parsed), "changed": changed}

    def import_documents(self, documents: Iterable[dict | Document]) -> dict:
        parsed: list[Document] = []
        for i, raw in enumerate(documents):
            if isinstance(raw, Document):
                d = raw
            else:
                rule_ids = raw.get("rule_ids")
                if not isinstance(rule_ids, (list, tuple)) or not rule_ids:
                    raise RejectError(
                        "invalid_document", "rule_ids must be a non-empty list", i
                    )
                d = Document(
                    doc_id=str(raw.get("doc_id", "")),
                    case_id=str(raw.get("case_id", "")),
                    rule_ids=tuple(str(x) for x in rule_ids),
                )
            if not d.doc_id:
                raise RejectError("invalid_document", "doc_id required", i)
            if not d.case_id:
                raise RejectError("invalid_document", "case_id required", i)
            if len(set(d.rule_ids)) != len(d.rule_ids):
                raise RejectError("invalid_document", "duplicate rule_ids", i)
            for rid in d.rule_ids:
                row = self.conn.execute(
                    "SELECT 1 FROM rules WHERE rule_id=?", (rid,)
                ).fetchone()
                if row is None:
                    raise UnknownReference(
                        "unknown_rule", f"document {d.doc_id} references unknown rule {rid}", i
                    )
            parsed.append(d)

        pending: dict[str, Document] = {}
        changed = False
        for i, d in enumerate(parsed):
            existing = pending.get(d.doc_id)
            if existing is None:
                row = self.conn.execute(
                    "SELECT * FROM documents WHERE doc_id=?", (d.doc_id,)
                ).fetchone()
                if row is not None:
                    existing = Document(
                        row["doc_id"], row["case_id"], tuple(json.loads(row["rules_json"]))
                    )
            if existing is not None:
                if existing != d:
                    raise ConflictError(
                        "document_conflict",
                        f"document {d.doc_id} re-imported with different content",
                        i,
                    )
                continue
            pending[d.doc_id] = d
            changed = True

        if changed:
            with self._tx():
                for d in pending.values():
                    self.conn.execute(
                        "INSERT INTO documents(doc_id, case_id, rules_json, canon) "
                        "VALUES (?,?,?,?) ON CONFLICT(doc_id) DO UPDATE SET "
                        "case_id=excluded.case_id, rules_json=excluded.rules_json, "
                        "canon=excluded.canon",
                        (d.doc_id, d.case_id, json.dumps(list(d.rule_ids)),
                         canon([d.doc_id, d.case_id, list(d.rule_ids)])),
                    )
                self._bump_catalog()
        return {"accepted": len(parsed), "changed": changed}

    def _event_canon(self, e: Event) -> str:
        return canon([e.event_id, e.revision, e.doc_id, e.type, e.day])

    def import_events(self, events: Iterable[dict | Event]) -> dict:
        """Validate and apply an event batch atomically.

        Same id+revision+content -> replay no-op.  Same id+revision+different
        content -> conflict.  Older revision -> conflict.  Unknown document ->
        unknown reference.  Any failure rejects the entire batch.
        """
        parsed: list[Event] = []
        for i, raw in enumerate(events):
            if isinstance(raw, Event):
                e = raw
            else:
                e = Event(
                    event_id=str(raw.get("event_id", "")),
                    revision=raw.get("revision"),
                    doc_id=str(raw.get("doc_id", "")),
                    type=str(raw.get("type", "")),
                    day=raw.get("day"),
                )
            if not e.event_id:
                raise RejectError("invalid_event", "event_id required", i)
            if not _is_int(e.revision) or e.revision < 1:
                raise RejectError("invalid_event", "revision must be int >= 1", i)
            if not e.doc_id:
                raise RejectError("invalid_event", "doc_id required", i)
            if not e.type:
                raise RejectError("invalid_event", "type required", i)
            if not _is_int(e.day) or e.day < 0:
                raise RejectError("invalid_event", "day must be int >= 0", i)
            row = self.conn.execute(
                "SELECT 1 FROM documents WHERE doc_id=?", (e.doc_id,)
            ).fetchone()
            if row is None:
                raise UnknownReference(
                    "unknown_document", f"event {e.event_id} references unknown doc {e.doc_id}", i
                )
            parsed.append(e)

        pending: dict[str, Event] = {}
        changed = False
        for i, e in enumerate(parsed):
            current = pending.get(e.event_id)
            if current is None:
                row = self.conn.execute(
                    "SELECT * FROM events WHERE event_id=?", (e.event_id,)
                ).fetchone()
                if row is not None:
                    current = Event(
                        row["event_id"], row["revision"], row["doc_id"], row["type"], row["day"]
                    )
            if current is not None:
                if e.revision == current.revision:
                    if (e.doc_id, e.type, e.day) != (
                        current.doc_id, current.type, current.day
                    ):
                        raise ConflictError(
                            "event_conflict",
                            f"event {e.event_id} rev {e.revision} replayed with different content",
                            i,
                        )
                    continue
                if e.revision < current.revision:
                    raise ConflictError(
                        "stale_revision",
                        f"event {e.event_id} rev {e.revision} older than stored rev {current.revision}",
                        i,
                    )
                pending[e.event_id] = e
                changed = True
            else:
                pending[e.event_id] = e
                changed = True

        if changed:
            with self._tx():
                for e in pending.values():
                    self.conn.execute(
                        "INSERT INTO events(event_id, doc_id, type, day, revision, canon) "
                        "VALUES (?,?,?,?,?,?) ON CONFLICT(event_id) DO UPDATE SET "
                        "doc_id=excluded.doc_id, type=excluded.type, day=excluded.day, "
                        "revision=excluded.revision, canon=excluded.canon",
                        (e.event_id, e.doc_id, e.type, e.day, e.revision,
                         self._event_canon(e)),
                    )
                self._bump_catalog()
        return {"accepted": len(parsed), "changed": changed}

    def _hold_canon(self, h: Hold) -> str:
        return canon([h.hold_id, h.revision, h.case_id, h.start, h.end])

    def _known_cases(self) -> set[str]:
        return {r["case_id"] for r in self.conn.execute("SELECT DISTINCT case_id FROM documents")}

    def import_holds(self, holds: Iterable[dict | Hold]) -> dict:
        """Validate and apply a hold batch atomically.

        Holds may target a document (``doc_id``) or a whole case.  Illegal
        intervals (negative start, end <= start) and unknown case/document
        references reject the whole batch.
        """
        parsed: list[tuple[Hold, Optional[str]]] = []
        for i, raw in enumerate(holds):
            if isinstance(raw, Hold):
                h = raw
                doc_id = None
            else:
                doc_id = raw.get("doc_id")
                h = Hold(
                    hold_id=str(raw.get("hold_id", "")),
                    revision=raw.get("revision"),
                    case_id=raw.get("case_id"),
                    start=raw.get("start"),
                    end=raw.get("end"),
                )
                if doc_id is not None:
                    doc_id = str(doc_id)
            if not h.hold_id:
                raise RejectError("invalid_hold", "hold_id required", i)
            if not _is_int(h.revision) or h.revision < 1:
                raise RejectError("invalid_hold", "revision must be int >= 1", i)
            if not _is_int(h.start) or h.start < 0:
                raise RejectError("invalid_hold", "start must be int >= 0", i)
            if h.end is not None and (not _is_int(h.end) or h.end <= h.start):
                raise InvalidInterval(
                    "invalid_interval",
                    f"hold {h.hold_id} end ({h.end}) must be > start ({h.start})",
                    i,
                )
            if h.case_id is None and doc_id is None:
                raise RejectError(
                    "invalid_hold", "hold must target a case_id or a doc_id", i
                )
            if h.case_id is not None:
                h = Hold(h.hold_id, h.revision, str(h.case_id), h.start, h.end)
                if h.case_id not in self._known_cases():
                    raise UnknownReference(
                        "unknown_case", f"hold {h.hold_id} references unknown case {h.case_id}", i
                    )
            if doc_id is not None:
                row = self.conn.execute(
                    "SELECT case_id FROM documents WHERE doc_id=?", (doc_id,)
                ).fetchone()
                if row is None:
                    raise UnknownReference(
                        "unknown_document", f"hold {h.hold_id} references unknown doc {doc_id}", i
                    )
                if h.case_id is not None and row["case_id"] != h.case_id:
                    raise InvalidInterval(
                        "hold_scope_mismatch",
                        f"hold {h.hold_id} case {h.case_id} != doc's case {row['case_id']}",
                        i,
                    )
                if h.case_id is None:
                    h = Hold(h.hold_id, h.revision, row["case_id"], h.start, h.end)
            parsed.append((h, doc_id))

        pending: dict[str, tuple[Hold, Optional[str]]] = {}
        changed = False
        for i, (h, doc_id) in enumerate(parsed):
            current = pending.get(h.hold_id)
            if current is None:
                row = self.conn.execute(
                    "SELECT * FROM holds WHERE hold_id=?", (h.hold_id,)
                ).fetchone()
                if row is not None:
                    current = (
                        Hold(row["hold_id"], row["revision"], row["case_id"],
                             row["start"], row["end"]),
                        row["doc_id"],
                    )
            if current is not None:
                cur_h, cur_doc = current
                same_content = (
                    cur_h.case_id == h.case_id
                    and cur_doc == doc_id
                    and cur_h.start == h.start
                    and cur_h.end == h.end
                )
                if h.revision == cur_h.revision:
                    if not same_content:
                        raise ConflictError(
                            "hold_conflict",
                            f"hold {h.hold_id} rev {h.revision} replayed with different content",
                            i,
                        )
                    continue
                if h.revision < cur_h.revision:
                    raise ConflictError(
                        "stale_revision",
                        f"hold {h.hold_id} rev {h.revision} older than stored rev {cur_h.revision}",
                        i,
                    )
                pending[h.hold_id] = (h, doc_id)
                changed = True
            else:
                pending[h.hold_id] = (h, doc_id)
                changed = True

        if changed:
            with self._tx():
                for h, doc_id in pending.values():
                    self.conn.execute(
                        "INSERT INTO holds(hold_id, case_id, doc_id, start, end, revision, canon) "
                        "VALUES (?,?,?,?,?,?,?) ON CONFLICT(hold_id) DO UPDATE SET "
                        "case_id=excluded.case_id, doc_id=excluded.doc_id, start=excluded.start, "
                        "end=excluded.end, revision=excluded.revision, canon=excluded.canon",
                        (h.hold_id, h.case_id, doc_id, h.start, h.end, h.revision,
                         self._hold_canon(h)),
                    )
                self._bump_catalog()
        return {"accepted": len(parsed), "changed": changed}

    # ------------------------------------------------------------ catalog read
    def _document(self, doc_id: str) -> Document:
        row = self.conn.execute(
            "SELECT * FROM documents WHERE doc_id=?", (doc_id,)
        ).fetchone()
        return Document(row["doc_id"], row["case_id"], tuple(json.loads(row["rules_json"])))

    def _rule(self, rule_id: str) -> Rule:
        row = self.conn.execute(
            "SELECT * FROM rules WHERE rule_id=?", (rule_id,)
        ).fetchone()
        return Rule(row["rule_id"], row["revision"], row["trigger_type"], row["keep_days"])

    def _all_cases(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for row in self.conn.execute(
            "SELECT doc_id, case_id FROM documents ORDER BY case_id, doc_id"
        ):
            out.setdefault(row["case_id"], []).append(row["doc_id"])
        return out

    def source_revisions(self) -> dict:
        """All current input revisions, for provenance in a product."""
        return {
            "catalog_revision": self.catalog_revision,
            "rules": {
                r["rule_id"]: r["revision"]
                for r in self.conn.execute("SELECT rule_id, revision FROM rules ORDER BY rule_id")
            },
            "events": {
                r["event_id"]: r["revision"]
                for r in self.conn.execute("SELECT event_id, revision FROM events ORDER BY event_id")
            },
            "holds": {
                r["hold_id"]: r["revision"]
                for r in self.conn.execute("SELECT hold_id, revision FROM holds ORDER BY hold_id")
            },
        }

    # ---------------------------------------------------------- qualification
    def _latest_event(self, doc_id: str, trigger_type: str) -> Optional[sqlite3.Row]:
        # One row per event_id after revision folding; latest = max event day.
        return self.conn.execute(
            "SELECT event_id, revision, day FROM events "
            "WHERE doc_id=? AND type=? "
            "ORDER BY day DESC, revision DESC, event_id DESC LIMIT 1",
            (doc_id, trigger_type),
        ).fetchone()

    def _active_holds(self, doc: Document, as_of_day: int) -> list[tuple[sqlite3.Row, Hold]]:
        rows = self.conn.execute(
            "SELECT * FROM holds WHERE case_id=? AND (doc_id IS NULL OR doc_id=?) "
            "ORDER BY hold_id",
            (doc.case_id, doc.doc_id),
        ).fetchall()
        active = []
        for row in rows:
            h = Hold(row["hold_id"], row["revision"], row["case_id"], row["start"], row["end"])
            if h.is_effective(as_of_day):
                active.append((row, h))
        return active

    def qualify_document(self, doc_id: str, as_of_day: int) -> DocQualification:
        if not _is_int(as_of_day) or as_of_day < 0:
            raise RejectError("invalid_query", "as_of_day must be int >= 0")
        doc = self._document(doc_id)
        q = DocQualification(
            doc_id=doc.doc_id, case_id=doc.case_id, as_of_day=as_of_day
        )

        computed_days: list[int] = []
        any_missing = False
        for rule_id in doc.rule_ids:
            rule = self._rule(rule_id)
            q.rule_revisions[rule.rule_id] = rule.revision
            ev = self._latest_event(doc.doc_id, rule.trigger_type)
            missing = ev is None
            computed = None if missing else int(ev["day"]) + rule.keep_days
            if missing:
                any_missing = True
                q.missing_trigger_rules.append(rule.rule_id)
            else:
                computed_days.append(computed)
                q.event_revisions[ev["event_id"]] = int(ev["revision"])
            q.rules.append(
                RuleReason(
                    rule_id=rule.rule_id,
                    rule_revision=rule.revision,
                    trigger_type=rule.trigger_type,
                    keep_days=rule.keep_days,
                    event_day=None if missing else int(ev["day"]),
                    event_id=None if missing else ev["event_id"],
                    event_revision=None if missing else int(ev["revision"]),
                    computed_day=computed,
                    satisfied=(not missing and as_of_day >= computed),
                    missing_trigger=missing,
                )
            )

        # A missing necessary trigger keeps the whole document pending: we do
        # NOT treat the missing event as day 0.
        if any_missing or not computed_days:
            q.computed_day = None
            q.time_condition_met = False
        else:
            q.computed_day = max(computed_days)
            q.time_condition_met = as_of_day >= q.computed_day
        q.threshold_met = q.time_condition_met

        for rule_id in q.missing_trigger_rules:
            q.add_block(
                BlockReason(
                    kind="missing_trigger",
                    key=f"missing_trigger:{rule_id}",
                    detail=f"no {self._rule(rule_id).trigger_type} event for document",
                    rule_id=rule_id,
                )
            )

        if q.computed_day is not None and not q.time_condition_met:
            q.add_block(
                BlockReason(
                    kind="not_due",
                    key="not_due",
                    detail=f"as_of_day {as_of_day} < computed_day {q.computed_day}",
                )
            )

        for row, h in self._active_holds(doc, as_of_day):
            q.hold_revisions[h.hold_id] = int(row["revision"])
            q.holds_active.append(
                ActiveHold(h.hold_id, h.revision, h.case_id, h.start, h.end)
            )
            q.add_block(
                BlockReason(
                    kind="hold",
                    key=f"hold:{h.hold_id}",
                    detail=(
                        f"hold {h.hold_id} rev {h.revision} active on day {as_of_day} "
                        f"[{h.start},{'open' if h.end is None else h.end})"
                    ),
                    hold_id=h.hold_id,
                )
            )

        q.releasable = q.time_condition_met and not q.block_reasons
        return q

    def build_product(
        self, as_of_day: int, doc_ids: Optional[Iterable[str]] = None
    ) -> QualificationProduct:
        if not _is_int(as_of_day) or as_of_day < 0:
            raise RejectError("invalid_query", "as_of_day must be int >= 0")
        if doc_ids is None:
            ids = [r["doc_id"] for r in self.conn.execute(
                "SELECT doc_id FROM documents ORDER BY doc_id"
            )]
        else:
            ids = list(doc_ids)
            for i, d in enumerate(ids):
                if self.conn.execute(
                    "SELECT 1 FROM documents WHERE doc_id=?", (d,)
                ).fetchone() is None:
                    raise UnknownReference("unknown_document", f"unknown doc {d}", i)
        docs = {d: self.qualify_document(d, as_of_day) for d in ids}
        return QualificationProduct(
            as_of_day=as_of_day,
            documents=docs,
            catalog_basis=self.source_revisions(),
        )

    # ---------------------------------------------------------------- products
    def save_product(self, product_id: str, product: QualificationProduct) -> dict:
        """Persist an immutable qualification product for a fixed day."""
        if not product_id:
            raise RejectError("invalid_product", "product_id required")
        payload = canon(product.as_dict())
        digest = product_digest_from_product(product)
        row = self.conn.execute(
            "SELECT source_revision, digest, payload FROM products WHERE product_id=?",
            (product_id,),
        ).fetchone()
        if row is not None:
            if row["payload"] != payload:
                raise ConflictError(
                    "product_conflict",
                    f"product {product_id} already published with different content",
                )
            return {"product_id": product_id, "replayed": True, "digest": row["digest"]}
        with self._tx():
            self.conn.execute(
                "INSERT INTO products(product_id, as_of_day, source_revision, digest, payload, canon) "
                "VALUES (?,?,?,?,?,?)",
                (product_id, product.as_of_day, self.catalog_revision, digest, payload, payload),
            )
        return {"product_id": product_id, "replayed": False, "digest": digest}

    def load_product(self, product_id: str) -> QualificationProduct:
        row = self.conn.execute(
            "SELECT payload FROM products WHERE product_id=?", (product_id,)
        ).fetchone()
        if row is None:
            raise UnknownReference("unknown_product", f"unknown product {product_id}")
        return QualificationProduct.from_dict(json.loads(row["payload"]))

    def product_record(self, product_id: str) -> dict:
        row = self.conn.execute(
            "SELECT * FROM products WHERE product_id=?", (product_id,)
        ).fetchone()
        if row is None:
            raise UnknownReference("unknown_product", f"unknown product {product_id}")
        return {
            "product_id": row["product_id"],
            "as_of_day": row["as_of_day"],
            "source_revision": row["source_revision"],
            "digest": row["digest"],
        }

    def product_digest_report(self) -> list[dict]:
        """Check every stored product's payload still matches its digest."""
        out = []
        for row in self.conn.execute(
            "SELECT product_id, as_of_day, source_revision, digest, payload FROM products "
            "ORDER BY product_id"
        ):
            recomputed = hashlib.sha256(row["payload"].encode("utf-8")).hexdigest()
            out.append({
                "product_id": row["product_id"],
                "as_of_day": row["as_of_day"],
                "source_revision": row["source_revision"],
                "digest_ok": recomputed == row["digest"],
            })
        return out

    def verify(self) -> dict:
        """Reopen self-check for the rule/event side."""
        report = {"ok": True, "errors": [], "catalog_revision": self.catalog_revision,
                  "products": self.product_digest_report()}
        for p in report["products"]:
            if not p["digest_ok"]:
                report["ok"] = False
                report["errors"].append(f"product {p['product_id']} digest mismatch")
        # Event completeness: every rule-bearing document needs its triggers to
        # be *releasable*; we only report the current pending set, not an error.
        pending = []
        for row in self.conn.execute("SELECT doc_id FROM documents ORDER BY doc_id"):
            q = self.qualify_document(row["doc_id"], 0)
            # just collecting rule refs; missing triggers independent of day
            if q.missing_trigger_rules:
                pending.append({"doc_id": row["doc_id"],
                                "missing_rules": q.missing_trigger_rules})
        report["missing_triggers"] = pending
        return report

    def write_product_artifact(
        self, product_id: str, product: QualificationProduct, directory: str
    ) -> str:
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, f"{product_id}.json")
        doc = product.as_dict()
        doc["product_id"] = product_id
        doc["source_revision"] = self.catalog_revision
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
        return path


def product_digest_from_product(product: QualificationProduct) -> str:
    payload = canon(product.as_dict())
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
