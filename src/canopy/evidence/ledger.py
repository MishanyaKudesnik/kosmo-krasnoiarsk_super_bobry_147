"""The Evidence Ledger: every number, and where it came from.

Each computed value becomes a node carrying the formula, the substituted
numbers, the nodes it consumed and -- for a value read from disk -- the file,
its sha256, the band and the window.  Following the edges from Q reaches the
bytes on disk, which is the whole product in one sentence.

The same records drive three things, so they cannot drift apart: the "why this
number" panel in the interface, the ledger section of the report, and
`canopy replay`, which recomputes an analysis and compares it field by field.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


@dataclass
class LedgerNode:
    node_id: str
    label: str
    kind: str                      # value | raster_read | table_read | evidence | status
    value: Optional[float] = None
    unit: str = ""
    formula: Optional[str] = None
    substituted: Optional[str] = None
    inputs: Dict[str, str] = field(default_factory=dict)
    params: List[Dict[str, Any]] = field(default_factory=list)
    file: Optional[str] = None
    sha256: Optional[str] = None
    band: Optional[str] = None
    window: Optional[Dict[str, int]] = None
    source_id: Optional[str] = None
    product_version: Optional[str] = None
    origin: Optional[str] = None
    pool: Optional[str] = None
    sign_convention: Optional[str] = None
    status: str = "OK"
    note: Optional[str] = None
    computed_at: str = ""

    def as_dict(self) -> Dict[str, Any]:
        out = {k: v for k, v in self.__dict__.items() if v not in (None, "", {}, [])}
        out["node_id"] = self.node_id
        out["kind"] = self.kind
        out["label"] = self.label
        return out


class Ledger:
    """An append-only provenance graph for one analysis."""

    def __init__(self, analysis_id: str, code_version: str = ""):
        self.analysis_id = analysis_id
        self.code_version = code_version
        self.nodes: Dict[str, LedgerNode] = {}
        self.order: List[str] = []

    # -- recording ---------------------------------------------------------
    def add(self, node: LedgerNode) -> LedgerNode:
        node.computed_at = node.computed_at or datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        )
        if node.node_id in self.nodes:
            suffix = 2
            base = node.node_id
            while f"{base}#{suffix}" in self.nodes:
                suffix += 1
            node.node_id = f"{base}#{suffix}"
        self.nodes[node.node_id] = node
        self.order.append(node.node_id)
        return node

    def value(
        self,
        node_id: str,
        label: str,
        value: Optional[float],
        unit: str,
        formula: Optional[str] = None,
        substituted: Optional[str] = None,
        inputs: Optional[Dict[str, str]] = None,
        params: Optional[List[Dict[str, Any]]] = None,
        pool: Optional[str] = None,
        sign_convention: Optional[str] = None,
        status: str = "OK",
        note: Optional[str] = None,
    ) -> LedgerNode:
        return self.add(
            LedgerNode(
                node_id=node_id,
                label=label,
                kind="value",
                value=value,
                unit=unit,
                formula=formula,
                substituted=substituted,
                inputs=inputs or {},
                params=params or [],
                pool=pool,
                sign_convention=sign_convention,
                status=status,
                note=note,
            )
        )

    def raster_read(
        self,
        node_id: str,
        label: str,
        asset,
        band: str,
        window: Optional[Dict[str, int]] = None,
        note: Optional[str] = None,
        with_hash: bool = True,
    ) -> LedgerNode:
        return self.add(
            LedgerNode(
                node_id=node_id,
                label=label,
                kind="raster_read",
                file=asset.relative_path,
                sha256=asset.sha256() if with_hash else None,
                band=band,
                window=window,
                source_id=asset.source_id,
                product_version=asset.version,
                origin=asset.origin,
                note=note,
            )
        )

    def table_read(
        self, node_id: str, label: str, file: str, note: Optional[str] = None,
        sha256: Optional[str] = None,
    ) -> LedgerNode:
        return self.add(
            LedgerNode(
                node_id=node_id, label=label, kind="table_read", file=file,
                sha256=sha256, note=note,
            )
        )

    # -- reading back ------------------------------------------------------
    def get(self, node_id: str) -> Optional[LedgerNode]:
        return self.nodes.get(node_id)

    def trace(self, node_id: str, depth: int = 12) -> Optional[Dict[str, Any]]:
        """The sub-tree behind one number, ready for the UI to render."""
        node = self.nodes.get(node_id)
        if node is None:
            return None
        out = node.as_dict()
        if depth > 0 and node.inputs:
            out["children"] = [
                self.trace(child, depth - 1) or {"node_id": child, "missing": True}
                for child in node.inputs.values()
            ]
        return out

    def as_list(self) -> List[Dict[str, Any]]:
        return [self.nodes[n].as_dict() for n in self.order]

    def write_jsonl(self, path: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            for node_id in self.order:
                fh.write(json.dumps(self.nodes[node_id].as_dict(), ensure_ascii=False) + "\n")

    # -- the human-facing case file ---------------------------------------
    def case_file(self, exhibits: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Numbered exhibits in plain language, each linked to its technical card."""
        out = []
        for i, ex in enumerate(exhibits, 1):
            out.append(
                {
                    "number": i,
                    "title": ex.get("title", ""),
                    "plain": ex.get("plain", ""),
                    "node_id": ex.get("node_id"),
                    "source_id": ex.get("source_id"),
                    "file": ex.get("file"),
                    "sha256": ex.get("sha256"),
                    "date": ex.get("date"),
                    "detail": ex.get("detail"),
                    "origin": ex.get("origin"),
                }
            )
        return out


def provenance_line(node: Optional[LedgerNode]) -> str:
    """The compact one-line trace printed under a headline number."""
    if node is None:
        return ""
    bits: List[str] = []
    if node.source_id:
        bits.append(node.source_id)
    if node.product_version:
        bits.append(f"v{node.product_version}")
    if node.file:
        bits.append(os.path.basename(node.file))
    if node.sha256:
        bits.append(f"sha256 {node.sha256[:8]}…")
    if node.formula:
        bits.append(node.formula)
    return " · ".join(bits)
