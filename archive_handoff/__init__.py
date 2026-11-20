"""Archive adjacency: rules/event interpretation and whole-case handoff."""

from .models import (  # noqa: F401
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
from .rules_service import RuleEventStore  # noqa: F401
from .handoff_service import HandoffRepo  # noqa: F401

__all__ = [
    "RuleEventStore",
    "HandoffRepo",
    "QualificationProduct",
    "DocQualification",
    "RejectError",
    "ConflictError",
    "InvalidInterval",
    "UnknownReference",
]
