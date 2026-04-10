from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class ConflictPairs(ABC):
    ours_index: int
    theirs_index: int

    @property
    @abstractmethod
    def conflict_type(self) -> str:
        raise NotImplementedError

    def dedupe_key(self) -> tuple[object, ...]:
        return (self.conflict_type, self.ours_index, self.theirs_index)

    @abstractmethod
    def to_dict(self, base_to_ours: Sequence[str], base_to_theirs: Sequence[str]) -> dict[str, object]:
        raise NotImplementedError


@dataclass(frozen=True)
class PrimaryKeyConflict(ConflictPairs):
    @property
    def conflict_type(self) -> str:
        return "primary_key"

    def to_dict(self, base_to_ours: Sequence[str], base_to_theirs: Sequence[str]) -> dict[str, object]:
        return {
            "conflict_type": self.conflict_type,
            "conflict_pair": [base_to_ours[self.ours_index], base_to_theirs[self.theirs_index]],
        }


@dataclass(frozen=True)
class UniqueIndexesConflict(ConflictPairs):
    index_name: str
    index_columns: tuple[str, ...]

    @property
    def conflict_type(self) -> str:
        return "unique_index"

    def dedupe_key(self) -> tuple[object, ...]:
        return super().dedupe_key() + (self.index_name, self.index_columns)

    def to_dict(self, base_to_ours: Sequence[str], base_to_theirs: Sequence[str]) -> dict[str, object]:
        return {
            "conflict_type": self.conflict_type,
            "conflict_pair": [base_to_ours[self.ours_index], base_to_theirs[self.theirs_index]],
            "index_name": self.index_name,
            "index_columns": list(self.index_columns),
        }