"""A dataclass has no memory layout: its [field:size] text is never checked."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Record:
    """Logical record, drawn like a strip on purpose: [kind:1B][size:4B]"""

    size: int
    kind: int
