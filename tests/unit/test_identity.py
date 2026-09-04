"""Tests for stable identity.

These are the guarantees idempotency rests on, so they are asserted as
properties (two encodings must differ / must agree) rather than against
hardcoded digests, which would only prove the implementation matches itself.
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
    def test_field_boundaries_cannot_be_confused(self):
        """Without length prefixes these two collide, and one row silently
        supersedes a genuinely different one."""
        assert canonical_encode([("a", "bc")]) != canonical_encode([("ab", "c")])

    def test_null_is_distinct_from_empty_string(self):
        """A missing invoice number and a blank invoice number are different
        facts and must not share an identity."""
        assert canonical_encode([("n", None)]) != canonical_encode([("n", "")])

    def test_null_is_distinct_from_the_literal_string_none(self):
        assert canonical_encode([("n", None)]) != canonical_encode([("n", "None")])

    def test_bool_does_not_collide_with_int(self):
        """bool subclasses int, so True would encode as "1" without the guard."""
        assert canonical_encode([("x", True)]) != canonical_encode([("x", 1)])
        assert canonical_encode([("x", False)]) != canonical_encode([("x", 0)])

    def test_separator_in_the_data_cannot_forge_a_boundary(self):
        hostile = "1:a\x1f1:1"
        assert canonical_encode([("x", hostile)]) != canonical_encode([("a", 1)])

    def test_field_order_is_significant(self):
        assert stable_hash([("a", 1), ("b", 2)]) != stable_hash([("b", 2), ("a", 1)])


class TestNumericEncoding:
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
    def test_date_and_datetime_do_not_collide(self):
        d = date(2025, 7, 17)
        assert stable_hash([("d", d)]) != stable_hash([("d", datetime(2025, 7, 17))])

    def test_dates_encode_iso(self):
        assert b"2025-07-17" in canonical_encode([("d", date(2025, 7, 17))])


class TestRowHash:
    def test_absent_field_encodes_as_null(self):
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
        assert stable_row_hash({"a": 1}, ["a"]) != stable_row_hash({"a": 2}, ["a"])

    def test_field_list_order_is_part_of_the_contract(self):
        row = {"a": 1, "b": 2}
        assert stable_row_hash(row, ["a", "b"]) != stable_row_hash(row, ["b", "a"])


class TestCrossProcessStability:
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
        assert seen == {stable_row_hash({"a": 1, "b": None, "c": "x"}, ["a", "b", "c"])}


class TestFileHash:
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
        f = tmp_path / "big.csv"
        f.write_bytes(b"x" * (3 * 1024 * 1024 + 17))
        assert len(file_sha256(f)) == 64

    def test_empty_file_hashes_without_error(self, tmp_path):
        f = tmp_path / "empty.csv"
        f.write_bytes(b"")
        assert len(file_sha256(f)) == 64


class TestGeneratedIds:
    def test_ids_are_unique(self):
        assert len({new_run_id() for _ in range(500)}) == 500
        assert len({new_batch_id() for _ in range(500)}) == 500

    def test_ids_sort_chronologically_even_within_one_millisecond(self):
        """Generating 20 ids takes well under a millisecond, so this is exactly
        the case a bare timestamp plus random suffix gets wrong."""
        ids = [new_batch_id() for _ in range(500)]
        assert ids == sorted(ids)
        assert len(set(ids)) == len(ids)

    def test_ids_remain_ordered_across_a_millisecond_boundary(self):
        first = [new_batch_id() for _ in range(5)]
        time.sleep(0.005)
        second = [new_batch_id() for _ in range(5)]
        assert first + second == sorted(first + second)

    def test_prefixes_distinguish_runs_from_batches(self):
        assert new_run_id().startswith("run_")
        assert new_batch_id().startswith("bat_")

    def test_replay_batch_id_depends_only_on_content(self):
        """Replay must be reproducible, so its batch ids cannot come from a clock."""
        a = deterministic_batch_id("abc123", "wholesaler.cardinal.invoice")
        b = deterministic_batch_id("abc123", "wholesaler.cardinal.invoice")
        assert a == b

    def test_replay_batch_id_differs_by_file_and_by_source(self):
        assert deterministic_batch_id("abc", "src.a") != deterministic_batch_id("abc", "src.b")
        assert deterministic_batch_id("abc", "src.a") != deterministic_batch_id("xyz", "src.a")
