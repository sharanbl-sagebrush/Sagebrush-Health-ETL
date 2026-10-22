"""The commit engine: persisting a load through the layers.

``engine`` decides and orders the writes; ``records`` is the row shapes and
the store interface; ``memory`` and ``sql`` are the two stores; ``discover``
finds a source's deliveries on disk and orders them by as-of. See
``engine`` for the outcomes and the order of checks.
"""

from sbh_data_bank.commit.discover import Found, discover, in_as_of_order
from sbh_data_bank.commit.engine import CommitEngine, CommitOutcome, CommitResult
from sbh_data_bank.commit.memory import MemoryStore
from sbh_data_bank.commit.sql import SqlStore

__all__ = [
    "CommitEngine",
    "CommitOutcome",
    "CommitResult",
    "Found",
    "MemoryStore",
    "SqlStore",
    "discover",
    "in_as_of_order",
]
