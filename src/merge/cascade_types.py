from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

CascadeEventKind = Literal["delete", "update"]
ForeignKeyAction = Literal[
    "NO ACTION",
    "RESTRICT",
    "CASCADE",
    "SET NULL",
    "SET DEFAULT",
]
ForeignKeyEdges = tuple["ForeignKeyEdge", ...]
ForeignKeyEdgeMap = dict[str, ForeignKeyEdges]


@dataclass(frozen=True)
class CascadeWriteEvent:
    """One hidden child-table write caused by a foreign-key action."""

    table: str
    columns: frozenset[str]
    kind: CascadeEventKind


@dataclass(frozen=True)
class ForeignKeyEdge:
    child_table: str
    child_columns: tuple[str, ...]
    parent_table: str
    parent_columns: tuple[str, ...]
    on_update: ForeignKeyAction
    on_delete: ForeignKeyAction
