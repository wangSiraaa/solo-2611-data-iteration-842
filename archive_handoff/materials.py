"""Load fictional archives from a ``materials/751``-style directory.

The directory is read-only input.  Nothing here deletes or mutates the source.
"""

from __future__ import annotations

import json
import os


def _read_json(path: str):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _read_jsonl(path: str):
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def load_materials(directory: str) -> dict:
    """Return raw rule/document/event/hold lists plus any operations script."""
    docs = _read_json(os.path.join(directory, "documents.json"))["documents"]
    rules = _read_json(os.path.join(directory, "rules.json"))["rules"]
    events = _read_jsonl(os.path.join(directory, "events.jsonl"))
    holds = _read_jsonl(os.path.join(directory, "holds.jsonl"))
    operations = []
    ops_path = os.path.join(directory, "operations.jsonl")
    if os.path.exists(ops_path):
        operations = _read_jsonl(ops_path)
    return {
        "documents": docs,
        "rules": rules,
        "events": events,
        "holds": holds,
        "operations": operations,
    }


def import_materials(rule_store, handoff_repo, directory: str) -> dict:
    """Import rules, documents, events and holds, then mirror membership."""
    materials = load_materials(directory)
    result = {
        "rules": rule_store.import_rules(materials["rules"]),
        "documents": rule_store.import_documents(materials["documents"]),
        "events": rule_store.import_events(materials["events"]),
        "holds": rule_store.import_holds(materials["holds"]),
    }
    handoff_repo.sync_catalog_membership(materials["documents"])
    result["mirrored"] = len(materials["documents"])
    return result
