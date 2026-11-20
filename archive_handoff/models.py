"""Shared immutable domain records for the preservation rules and handoff stores.

Both services exchange only these plain records.  There is no ORM and no
back-reference from a handoff row into the rule store: the handoff side receives
serialised *qualification products* and persists them unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


class RejectError(Exception):
    """Raised when an input batch/request must be rejected as a whole."""

    def __init__(self, code: str, detail: str, index: Optional[int] = None):
        self.code = code
        self.detail = detail
        self.index = index
        super().__init__(f"{code}: {detail}" + (f" (item {index})" if index is not None else ""))


class ConflictError(RejectError):
    """Same identifier replayed with different content."""


class UnknownReference(RejectError):
    """Reference to an unknown rule / document / case / hold."""


class InvalidInterval(RejectError):
    """Malformed or reversed effective interval."""


@dataclass(frozen=True)
class Rule:
    rule_id: str
    revision: int
    trigger_type: str
    keep_days: int


@dataclass(frozen=True)
class Document:
    doc_id: str
    case_id: str
    rule_ids: tuple[str, ...]


@dataclass(frozen=True)
class Event:
    event_id: str
    revision: int
    doc_id: str
    type: str
    day: int


@dataclass(frozen=True)
class Hold:
    hold_id: str
    revision: int
    case_id: str
    start: int
    end: Optional[int]  # None == open ended (not released)

    def is_effective(self, day: int) -> bool:
        """A hold is active on `day` when start <= day and (end is None or day < end)."""
        if day < self.start:
            return False
        return self.end is None or day < self.end


@dataclass
class RuleReason:
    """How one rule contributed to a document's eligibility."""

    rule_id: str
    rule_revision: int
    trigger_type: str
    keep_days: int
    # Latest applicable event day for the rule's trigger type (None when missing).
    event_day: Optional[int]
    event_id: Optional[str]
    event_revision: Optional[int]
    # computed = event_day + keep_days; None when the trigger is missing.
    computed_day: Optional[int]
    satisfied: bool
    missing_trigger: bool


@dataclass
class ActiveHold:
    hold_id: str
    revision: int
    case_id: str
    start: int
    end: Optional[int]


@dataclass
class BlockReason:
    """One reason a document is barred from handoff.  Deduped by key."""

    kind: str  # "hold" | "missing_trigger" | "not_due"
    key: str  # stable key used for de-duplication
    detail: str
    hold_id: Optional[str] = None
    rule_id: Optional[str] = None

    def as_dict(self) -> dict:
        d = {"kind": self.kind, "key": self.key, "detail": self.detail}
        if self.hold_id is not None:
            d["hold_id"] = self.hold_id
        if self.rule_id is not None:
            d["rule_id"] = self.rule_id
        return d


@dataclass
class DocQualification:
    """Per-document eligibility product produced by the rule/event side.

    ``releasable`` is the *time + hold* condition only (no custodian info).
    ``block_reasons`` may contain several reasons; each reason appears once.
    """

    doc_id: str
    case_id: str
    as_of_day: int
    rules: list[RuleReason] = field(default_factory=list)
    # overall computed day = max of all computed days, None if any trigger missing
    computed_day: Optional[int] = None
    time_condition_met: bool = False
    threshold_met: bool = False
    holds_active: list[ActiveHold] = field(default_factory=list)
    block_reasons: list[BlockReason] = field(default_factory=list)
    missing_trigger_rules: list[str] = field(default_factory=list)
    releasable: bool = False
    # provenance
    rule_revisions: dict[str, int] = field(default_factory=dict)
    event_revisions: dict[str, int] = field(default_factory=dict)
    hold_revisions: dict[str, int] = field(default_factory=dict)

    def add_block(self, reason: BlockReason) -> None:
        if all(existing.key != reason.key for existing in self.block_reasons):
            self.block_reasons.append(reason)

    def as_dict(self) -> dict:
        return {
            "doc_id": self.doc_id,
            "case_id": self.case_id,
            "as_of_day": self.as_of_day,
            "rules": [
                {
                    "rule_id": r.rule_id,
                    "rule_revision": r.rule_revision,
                    "trigger_type": r.trigger_type,
                    "keep_days": r.keep_days,
                    "event_day": r.event_day,
                    "event_id": r.event_id,
                    "event_revision": r.event_revision,
                    "computed_day": r.computed_day,
                    "satisfied": r.satisfied,
                    "missing_trigger": r.missing_trigger,
                }
                for r in self.rules
            ],
            "computed_day": self.computed_day,
            "time_condition_met": self.time_condition_met,
            "threshold_met": self.threshold_met,
            "holds_active": [
                {"hold_id": h.hold_id, "revision": h.revision, "case_id": h.case_id,
                 "start": h.start, "end": h.end}
                for h in self.holds_active
            ],
            "block_reasons": [b.as_dict() for b in self.block_reasons],
            "missing_trigger_rules": list(self.missing_trigger_rules),
            "releasable": self.releasable,
            "rule_revisions": dict(self.rule_revisions),
            "event_revisions": dict(self.event_revisions),
            "hold_revisions": dict(self.hold_revisions),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "DocQualification":
        q = cls(
            doc_id=d["doc_id"],
            case_id=d["case_id"],
            as_of_day=d["as_of_day"],
            computed_day=d.get("computed_day"),
            time_condition_met=d.get("time_condition_met", False),
            threshold_met=d.get("threshold_met", False),
            missing_trigger_rules=list(d.get("missing_trigger_rules", [])),
            releasable=d.get("releasable", False),
            rule_revisions=dict(d.get("rule_revisions", {})),
            event_revisions=dict(d.get("event_revisions", {})),
            hold_revisions=dict(d.get("hold_revisions", {})),
        )
        q.rules = [
            RuleReason(
                rule_id=r["rule_id"],
                rule_revision=r["rule_revision"],
                trigger_type=r["trigger_type"],
                keep_days=r["keep_days"],
                event_day=r.get("event_day"),
                event_id=r.get("event_id"),
                event_revision=r.get("event_revision"),
                computed_day=r.get("computed_day"),
                satisfied=r.get("satisfied", False),
                missing_trigger=r.get("missing_trigger", False),
            )
            for r in d.get("rules", [])
        ]
        q.holds_active = [
            ActiveHold(h["hold_id"], h["revision"], h["case_id"], h["start"], h.get("end"))
            for h in d.get("holds_active", [])
        ]
        q.block_reasons = [
            BlockReason(
                kind=b["kind"],
                key=b["key"],
                detail=b["detail"],
                hold_id=b.get("hold_id"),
                rule_id=b.get("rule_id"),
            )
            for b in d.get("block_reasons", [])
        ]
        return q

    def content_digest(self) -> str:
        """Digest of the *eligibility-relevant* content (ignores nothing that matters)."""
        return product_digest(self)


@dataclass
class QualificationProduct:
    """A batch of per-document qualifications at one fixed ``as_of_day``."""

    as_of_day: int
    documents: dict[str, DocQualification]
    catalog_basis: dict[str, object] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "as_of_day": self.as_of_day,
            "catalog_basis": dict(self.catalog_basis),
            "documents": {k: v.as_dict() for k, v in sorted(self.documents.items())},
        }

    @classmethod
    def from_dict(cls, d: dict) -> "QualificationProduct":
        return cls(
            as_of_day=d["as_of_day"],
            documents={
                k: DocQualification.from_dict(v)
                for k, v in d.get("documents", {}).items()
            },
            catalog_basis=dict(d.get("catalog_basis", {})),
        )


def _canonical(value):
    """Deterministic JSON-able normalisation for digesting."""
    if isinstance(value, dict):
        return {k: _canonical(value[k]) for k in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in value]
    return value


def product_digest(q: DocQualification) -> str:
    import hashlib, json

    payload = {
        "doc_id": q.doc_id,
        "case_id": q.case_id,
        "as_of_day": q.as_of_day,
        "computed_day": q.computed_day,
        "releasable": q.releasable,
        "rules": [
            (r.rule_id, r.rule_revision, r.event_day, r.event_id, r.event_revision,
             r.computed_day, r.missing_trigger, r.satisfied)
            for r in q.rules
        ],
        "holds": sorted(
            (h.hold_id, h.revision, h.case_id, h.start, h.end) for h in q.holds_active
        ),
        "blocks": sorted((b.kind, b.key, b.detail) for b in q.block_reasons),
    }
    raw = json.dumps(_canonical(payload), separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
