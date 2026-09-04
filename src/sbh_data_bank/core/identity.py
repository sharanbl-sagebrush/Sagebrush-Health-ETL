"""Stable identity for rows, files and runs.

Idempotency is the property the previous pipeline could not hold, and it rests
entirely on being able to ask "have I seen this exact thing before" and get the
same answer on every machine, every Python version, and every rerun.  So the
hashes here are computed from an explicit canonical encoding rather than from
anything Python happens to provide.

Specifically not used:

``hash()``
    Randomised per process by ``PYTHONHASHSEED``.  Two runs disagree.
``str(dict)`` or ``repr``
    Depends on insertion order and on each value's ``repr``, which differs
    between a ``float`` and a ``Decimal`` holding the same number.
``json.dumps``
    Turns ``Decimal`` into ``float``, losing precision on money, and its
    handling of non-ASCII depends on flags nobody remembers to set.

The encoding below is length-prefixed so that no combination of field values
can be confused with another.  Without length prefixes the fields
``("ab", "c")`` and ``("a", "bc")`` encode identically, which would let two
genuinely different rows collide and one silently disappear.
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
import uuid
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

__all__ = [
    "NULL_MARKER",
    "canonical_encode",
    "file_sha256",
    "new_batch_id",
    "new_run_id",
    "stable_hash",
    "stable_row_hash",
]

#: Distinguishes a null from the empty string.  Without it, a row with a
#: missing invoice number and a row with an empty-string invoice number hash
#: identically, and re-delivery of one supersedes the other.
NULL_MARKER: Final = "\x00<null>"

_FIELD_SEP: Final = "\x1f"  # ASCII unit separator - cannot occur in vendor text
_READ_CHUNK: Final = 1024 * 1024


def _encode_value(value: Any) -> str:
    """Render one value to a canonical, round-trip-stable string.

    Two values that mean the same number must encode identically even when
    they arrive as different Python types, because the same vendor column can
    be read as ``int`` from Excel and ``str`` from CSV.
    """
    if value is None:
        return NULL_MARKER
    if isinstance(value, bool):
        # Checked before int: bool is a subclass of int and would otherwise
        # encode as "1", colliding with a genuine 1.
        return "true" if value else "false"
    if isinstance(value, Decimal):
        # normalize() collapses 1.10, 1.1 and 1.100 to one representation.
        # The Decimal("0") special case exists because normalize() renders
        # zero as "0E-9" or similar depending on the incoming exponent.
        return "0" if value == 0 else format(value.normalize(), "f")
    if isinstance(value, float):
        # repr() is the shortest string that round-trips to the same float.
        # Money should never reach here - it should be Decimal - but a float
        # must still hash consistently if it does.
        return repr(value)
    if isinstance(value, int):
        # Deliberately encodes identically to the string "3" and to
        # Decimal("3").  The same vendor column yields str from a CSV read and
        # int from an Excel read, and WeInfuse switched one of its exports from
        # CSV to XLSX mid-2026.  If those hashed differently the format change
        # alone would supersede every historical row.  The real defence against
        # confusing "3" with 3 is that row hashes are computed over *parsed,
        # typed* values, after the transform layer has coerced the column to its
        # declared type - not over whatever the reader happened to produce.
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.hex()
    return str(value)


def canonical_encode(values: Iterable[tuple[str, Any]]) -> bytes:
    """Encode ``(name, value)`` pairs into bytes that hash deterministically.

    Each field contributes ``name`` and ``value``, both length-prefixed, so the
    boundary between fields is unambiguous regardless of their contents.

    >>> canonical_encode([("a", "bc")]) == canonical_encode([("ab", "c")])
    False
    >>> canonical_encode([("n", None)]) == canonical_encode([("n", "")])
    False
    """
    parts: list[str] = []
    for name, value in values:
        encoded = _encode_value(value)
        parts.append(f"{len(name)}:{name}{_FIELD_SEP}{len(encoded)}:{encoded}")
    return _FIELD_SEP.join(parts).encode("utf-8")


def stable_hash(values: Iterable[tuple[str, Any]]) -> str:
    """A 64-character SHA-256 hex digest over :func:`canonical_encode`."""
    return hashlib.sha256(canonical_encode(values)).hexdigest()


def stable_row_hash(row: Mapping[str, Any], fields: Iterable[str]) -> str:
    """Identity hash for one row over an explicit, ordered field list.

    ``fields`` is required rather than defaulting to the row's own keys.  A
    row's key set changes whenever a vendor adds a column, and if the hash
    followed it, every historical row would appear to change on the next
    delivery and the whole file would be re-superseded.  The field list belongs
    to the source spec and changes only deliberately.

    A field absent from ``row`` encodes as null rather than raising, so a
    vendor dropping an optional column does not crash the load - the row hash
    changes, which is the correct signal, and the drift is reported separately.

    >>> stable_row_hash({"a": 1, "b": None}, ["a", "b"])[:12]
    '941baa23896b'
    >>> stable_row_hash({"a": 1}, ["a", "b"])[:12]          # absent == null
    '941baa23896b'
    """
    return stable_hash((name, row.get(name)) for name in fields)


def file_sha256(path: str | Path) -> str:
    """Content hash of a file, streamed so a large extract does not load whole.

    File identity is content-based, not name-based: the same extract delivered
    under a different filename must be recognised as already processed, and a
    file re-delivered under the same name with changed content must not be.
    """
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(_READ_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


_seq_lock = threading.Lock()
_seq_state: dict[str, int] = {"millis": 0, "counter": 0}


def _time_ordered_id(prefix: str) -> str:
    """A sortable, collision-resistant identifier.

    Ids sort chronologically as plain strings, which is what makes a run's
    history readable in a database index and in log output without joining to
    a timestamp column.

    A millisecond timestamp alone is not enough: several batches are routinely
    created inside one millisecond, and a random suffix would order them
    arbitrarily.  A per-millisecond sequence counter makes ids from a single
    process strictly increasing.  The random tail keeps ids from colliding
    across processes; ids generated by *different* processes within the same
    millisecond have no defined relative order, which is acceptable because
    ordering is only ever relied on within a run.
    """
    millis = int(time.time() * 1000)
    with _seq_lock:
        if millis == _seq_state["millis"]:
            _seq_state["counter"] += 1
        else:
            _seq_state["millis"] = millis
            _seq_state["counter"] = 0
        counter = _seq_state["counter"]
    return f"{prefix}_{millis:013d}_{counter:06d}_{uuid.uuid4().hex[:8]}"


def new_run_id() -> str:
    """Identifier for one execution of the pipeline."""
    return _time_ordered_id("run")


def new_batch_id() -> str:
    """Identifier for one atomic commit of rows from one file.

    Every fact row carries this.  It is the unit of supersession: correcting a
    delivery means writing a new batch and marking the previous one's rows
    superseded, never updating them in place.
    """
    return _time_ordered_id("bat")


def deterministic_batch_id(file_hash: str, source_key: str) -> str:
    """A batch id derived from content rather than from the clock.

    Used by replay and by tests, where two runs over the same inputs must
    produce byte-identical output.  Not used in normal operation, where two
    genuine deliveries of identical content should still be distinguishable.
    """
    digest = hashlib.sha256(f"{source_key}{_FIELD_SEP}{file_hash}".encode()).hexdigest()
    return f"bat_replay_{digest[:24]}"


def is_ci() -> bool:
    """Whether the process is running in continuous integration."""
    return os.environ.get("CI", "").lower() in {"1", "true", "yes"}
