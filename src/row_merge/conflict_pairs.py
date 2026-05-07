from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class ConflictPairs(ABC):

    @property
    @abstractmethod
    def conflict_type(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def to_dict(self) -> dict[str, object]:
        raise NotImplementedError


@dataclass(frozen=True)
class PrimaryKeyConflict(ConflictPairs):
    @property
    def conflict_type(self) -> str:
        return "primary_key"

    def to_dict(self) -> dict[str, object]:
        return {
            "conflict_type": self.conflict_type,
        }


@dataclass(frozen=True)
class UniqueIndexesConflict(ConflictPairs):
    index_name: str
    index_columns: tuple[str, ...]

    @property
    def conflict_type(self) -> str:
        return "unique_index"

    def to_dict(self) -> dict[str, object]:
        return {
            "conflict_type": self.conflict_type,
            "index_name": self.index_name,
            "index_columns": list(self.index_columns),
        }

@dataclass(frozen=True)
class ForeignIndexesConflict(ConflictPairs):
    index_columns_parent: tuple[str, ...]
    index_columns_child: tuple[str, ...]

    @property
    def conflict_type(self) -> str:
        return "foreign_index"

    def to_dict(self) -> dict[str, object]:
        return {
            "conflict_type": self.conflict_type,
            "index_columns_parent": list(self.index_columns_parent),
            "index_columns_child": list(self.index_columns_child),
        }
    