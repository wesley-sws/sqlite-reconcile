from __future__ import annotations

from collections.abc import Collection

from .models import ConflictKind


def kind_enabled(
    enabled_kinds: Collection[ConflictKind] | None,
    kind: ConflictKind,
) -> bool:
    """Return whether a conflict kind should be checked."""

    return enabled_kinds is None or kind in enabled_kinds
