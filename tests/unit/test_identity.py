"""Tests for stable identity - ``sbh_data_bank.core.identity``.

Identity is what idempotency rests on. The row hash decides whether a
re-delivered row is the same record (skip), a changed record (supersede) or a
new one (insert). The file hash decides whether a file has already been
processed. If either digest drifted between runs, a replay would supersede the
whole history instead of changing nothing.

These are the guarantees idempotency rests on, so they are asserted as
properties (two encodings must differ / must agree) rather than against
hardcoded digests, which would only prove the implementation matches itself.

How to read this file:

- TestCanonicalEncoding     the byte encoding under every hash cannot be fooled
- TestNumericEncoding       numbers hash by value, not by how a reader spelt them
- TestTemporalEncoding      dates and datetimes stay distinct
- TestRowHash               the per-row digest and its field-list contract
- TestCrossProcessStability the digest does not depend on the interpreter
- TestFileHash              content-based file identity
- TestGeneratedIds          run and batch ids: unique, sortable, replayable
"""

from __future__ import annotations

import subprocess
import sys
import time
from datetime import date, datetime
from decimal import Decimal

import pytest

from sbh_data_bank.core.identity import (
    canonical_encode,
    deterministic_batch_id,
    file_sha256,
    new_batch_id,
    new_run_id,
    stable_hash,
    stable_row_hash,
)


class TestCanonicalEncoding:
    """The length-prefixed byte encoding every hash is built on.

    Each test is one way two different inputs could produce the same bytes,
    and shows that they do not.
    """

    def test_field_boundaries_cannot_be_confused(self):
        """Without length prefixes these two collide, and one row silently
        supersedes a genuinely different one."""
        assert canonical_encode([("a", "bc")]) != canonical_encode([("ab", "c")])

    def test_null_is_distinct_from_empty_string(self):
        """A missing invoice number and a blank invoice number are different
        facts and must not share an identity."""
        assert canonical_encode([("n", None)]) != canonical_encode([("n", "")])

    def test_null_is_distinct_from_the_literal_string_none(self):
        """A cell containing the text "None" is data; a missing cell is not."""
        assert canonical_encode([("n", None)]) != canonical_encode([("n", "None")])

    def test_bool_does_not_collide_with_int(self):
        """bool subclasses int, so True would encode as "1" without the guard."""
        assert canonical_encode([("x", True)]) != canonical_encode([("x", 1)])
        assert canonical_encode([("x", False)]) != canonical_encode([("x", 0)])

    def test_separator_in_the_data_cannot_forge_a_boundary(self):
        """A value that contains the internal separator and a fake length
        prefix must not be able to impersonate a different field list."""
        hostile = "1:a\x1f1:1"
        assert canonical_encode([("x", hostile)]) != canonical_encode([("a", 1)])

    def test_field_order_is_significant(self):
        """(a, b) and (b, a) are different rows; the field list is part of the
        contract, not a set."""
        assert stable_hash([("a", 1), ("b", 2)]) != stable_hash([("b", 2), ("a", 1)])


class TestNumericEncoding:
    """Numbers hash by value.

    A CSV reader yields strings and an Excel reader yields ints and floats.
    WeInfuse switched from CSV to XLSX mid-2026, so the same number must hash
    the same from both readers or that switch alone would supersede history.
    """

    @pytest.mark.parametrize(
        ("left", "right"),
        [
            (Decimal("1.1"), Decimal("1.10")),
            (Decimal("1.100"), Decimal("1.1")),
            (Decimal("0"), Decimal("0.00")),
            (Decimal("-0.0"), Decimal("0")),
        ],
    )
    def test_equal_decimals_encode_identically_regardless_of_scale(self, left, right):
        """The same money value read from CSV and from Excel must hash the same,
        or every row would appear changed on re-delivery."""
        assert stable_hash([("amt", left)]) == stable_hash([("amt", right)])

    def test_decimal_precision_is_not_lost(self):
        """Two amounts that differ in the last cent must not collide."""
        assert stable_hash([("amt", Decimal("56142.93"))]) != stable_hash(
            [("amt", Decimal("56142.94"))]
        )

    def test_large_decimal_keeps_full_precision(self):
        """No float conversion inside the encoder: a 22-digit amount still
        differs from itself plus one cent."""
        big = Decimal("12345678901234567890.12")
        assert stable_hash([("amt", big)]) != stable_hash([("amt", big + Decimal("0.01"))])

    def test_int_string_and_decimal_of_the_same_number_agree(self):
        """Deliberate. A CSV read yields "3" where an Excel read yields 3, and
        WeInfuse switched an export from CSV to XLSX mid-2026. If these hashed
        differently the format change alone would supersede all of history.

        Confusing a real "3" with 3 is prevented instead by hashing parsed,
        typed values - see the note in _encode_value.
        """
        assert stable_hash([("q", 3)]) == stable_hash([("q", "3")])
        assert stable_hash([("q", 3)]) == stable_hash([("q", Decimal("3"))])

    def test_leading_zeros_are_still_significant(self):
        """Invoice and account numbers carry meaningful leading zeros, so the
        forgiveness above must not extend to losing them."""
        assert stable_hash([("inv", "0007")]) != stable_hash([("inv", 7)])
        assert stable_hash([("acct", "0100583154")]) != stable_hash([("acct", 100583154)])


class TestTemporalEncoding:
    """Dates and datetimes encode as ISO text and never collide with each other."""

    def test_date_and_datetime_do_not_collide(self):
        """A date and a midnight datetime on the same day are different values."""
        d = date(2025, 7, 17)
        assert stable_hash([("d", d)]) != stable_hash([("d", datetime(2025, 7, 17))])

    def test_dates_encode_iso(self):
        """ISO text is locale-independent and readable if a digest is ever
        reconstructed by hand."""
        assert b"2025-07-17" in canonical_encode([("d", date(2025, 7, 17))])


class TestRowHash:
    """``stable_row_hash``: the digest stored on every landed row, computed
    over the field list the spec declares."""

    def test_absent_field_encodes_as_null(self):
        """A field missing from the row dict hashes the same as the field
        present with None, so a sparse reader and a dense reader agree."""
        assert stable_row_hash({"a": 1}, ["a", "b"]) == stable_row_hash(
            {"a": 1, "b": None}, ["a", "b"]
        )

    def test_extra_keys_not_in_the_field_list_are_ignored(self):
        """A vendor adding a column must not change the identity of every
        historical row - otherwise the next delivery supersedes the whole file."""
        base = stable_row_hash({"a": 1, "b": 2}, ["a", "b"])
        widened = stable_row_hash({"a": 1, "b": 2, "new_vendor_col": "x"}, ["a", "b"])
        assert base == widened

    def test_changing_a_hashed_value_changes_the_hash(self):
        """Sanity check: the digest really does depend on the values."""
        assert stable_row_hash({"a": 1}, ["a"]) != stable_row_hash({"a": 2}, ["a"])

    def test_field_list_order_is_part_of_the_contract(self):
        """Same row, different field order, different hash. A spec must not
        reorder its hashed fields casually, or every row looks changed."""
        row = {"a": 1, "b": 2}
        assert stable_row_hash(row, ["a", "b"]) != stable_row_hash(row, ["b", "a"])


class TestCrossProcessStability:
    """The digest must be identical in every Python process, or a replay on
    another machine could never reproduce history."""

    def test_hash_is_identical_in_a_fresh_interpreter_with_a_different_seed(self):
        """PYTHONHASHSEED randomisation must not reach these digests.

        Guards against anyone replacing the encoder with built-in hash() or a
        set/dict iteration order.
        """
        script = (
            "import sys; sys.path.insert(0, 'src');"
            "from sbh_data_bank.core.identity import stable_row_hash;"
            "print(stable_row_hash({'a': 1, 'b': None, 'c': 'x'}, ['a', 'b', 'c']))"
        )
        seen = set()
        # Three interpreters, three different hash seeds, an empty PATH so the
        # child can only run the interpreter it was handed. One digest expected.
        for seed in ("0", "1", "12345"):
            out = subprocess.run(
                [sys.executable, "-c", script],
                capture_output=True,
                text=True,
                env={"PYTHONHASHSEED": seed, "PATH": ""},
                check=True,
            )
            seen.add(out.stdout.strip())
        assert len(seen) == 1
        # And it is the same digest this process computes.
        assert seen == {stable_row_hash({"a": 1, "b": None, "c": "x"}, ["a", "b", "c"])}


class TestFileHash:
    """``file_sha256``: a file is identified by its bytes, not its name.

    This is what the ``uq_source_file`` constraint in the ledger relies on - a
    re-upload of the same file is a no-op, a corrected file is a new delivery.
    """

    def test_same_content_under_different_names_hashes_the_same(self, tmp_path):
        """File identity is content-based: a renamed re-delivery is a duplicate."""
        a, b = tmp_path / "drop1.csv", tmp_path / "drop2.csv"
        a.write_bytes(b"ndc,qty\n00310304000,1\n")
        b.write_bytes(b"ndc,qty\n00310304000,1\n")
        assert file_sha256(a) == file_sha256(b)

    def test_changed_content_under_the_same_name_hashes_differently(self, tmp_path):
        """A corrected re-delivery must not be skipped as already processed."""
        f = tmp_path / "drop.csv"
        f.write_bytes(b"ndc,qty\n00310304000,1\n")
        first = file_sha256(f)
        f.write_bytes(b"ndc,qty\n00310304000,2\n")
        assert file_sha256(f) != first

    def test_handles_a_file_larger_than_the_read_chunk(self, tmp_path):
        """Hashing streams the file in chunks. A file bigger than one chunk,
        with an odd-sized tail, still yields a 64-hex SHA-256."""
        f = tmp_path / "big.csv"
        f.write_bytes(b"x" * (3 * 1024 * 1024 + 17))
        assert len(file_sha256(f)) == 64

    def test_empty_file_hashes_without_error(self, tmp_path):
        """An empty delivery is still a delivery and gets an identity."""
        f = tmp_path / "empty.csv"
        f.write_bytes(b"")
        assert len(file_sha256(f)) == 64


class TestGeneratedIds:
    """Run and batch ids.

    Live runs get time-ordered random ids so a batch table sorts by creation.
    Replay gets deterministic ids derived from content, so re-running history
    produces the same batch ids and therefore the same ledger.
    """

    def test_ids_are_unique(self):
        """500 of each in a tight loop, no collisions."""
        assert len({new_run_id() for _ in range(500)}) == 500
        assert len({new_batch_id() for _ in range(500)}) == 500

    def test_ids_sort_chronologically_even_within_one_millisecond(self):
        """Generating 20 ids takes well under a millisecond, so this is exactly
        the case a bare timestamp plus random suffix gets wrong."""
        ids = [new_batch_id() for _ in range(500)]
        assert ids == sorted(ids)
        assert len(set(ids)) == len(ids)

    def test_ids_remain_ordered_across_a_millisecond_boundary(self):
        """Ordering also holds when the clock ticks between two bursts."""
        first = [new_batch_id() for _ in range(5)]
        time.sleep(0.005)
        second = [new_batch_id() for _ in range(5)]
        assert first + second == sorted(first + second)

    def test_prefixes_distinguish_runs_from_batches(self):
        """The prefix makes an id self-describing in a log line."""
        assert new_run_id().startswith("run_")
        assert new_batch_id().startswith("bat_")

    def test_replay_batch_id_depends_only_on_content(self):
        """Replay must be reproducible, so its batch ids cannot come from a clock."""
        a = deterministic_batch_id("abc123", "wholesaler.cardinal.invoice")
        b = deterministic_batch_id("abc123", "wholesaler.cardinal.invoice")
        assert a == b

    def test_replay_batch_id_differs_by_file_and_by_source(self):
        """Deterministic still has to mean unique per (file, source) pair."""
        assert deterministic_batch_id("abc", "src.a") != deterministic_batch_id("abc", "src.b")
        assert deterministic_batch_id("abc", "src.a") != deterministic_batch_id("xyz", "src.a")
